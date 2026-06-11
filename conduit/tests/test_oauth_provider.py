"""Tests for the MCP-spec OAuth provider used by streamable HTTP mode."""

import asyncio
from unittest import TestCase
from unittest.mock import MagicMock, patch

from conduit.auth.provider import (
    AUTH_PATH,
    TOKEN_PATH,
    PhabricatorTokenVerifier,
    build_phabricator_auth_provider,
)


class _FakeAsyncClient:
    """Async-context-manager stand-in for httpx.AsyncClient."""

    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise_exc = raise_exc
        self.posted = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None):
        self.posted.append((url, data))
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


def _make_response(status_code=200, json_body=None, raise_json=False):
    response = MagicMock()
    response.status_code = status_code
    if raise_json:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = json_body or {}
    return response


class TestPhabricatorTokenVerifier(TestCase):
    API_URL = "https://phab.example.com/api/"

    def _verify(self, fake_client, token="tok-123"):
        verifier = PhabricatorTokenVerifier(
            api_url=self.API_URL, required_scopes=["maniphest"]
        )
        with patch(
            "conduit.auth.provider.httpx.AsyncClient", return_value=fake_client
        ):
            return asyncio.run(verifier.verify_token(token))

    def test_valid_token_returns_access_token(self):
        body = {
            "result": {
                "phid": "PHID-USER-abc",
                "userName": "alice",
                "realName": "Alice Example",
            }
        }
        fake = _FakeAsyncClient(response=_make_response(json_body=body))
        result = self._verify(fake, token="good-token")

        self.assertIsNotNone(result)
        # The upstream token must be preserved so tools can reuse it.
        self.assertEqual(result.token, "good-token")
        self.assertEqual(result.client_id, "PHID-USER-abc")
        self.assertEqual(result.scopes, ["maniphest"])
        self.assertEqual(result.claims["username"], "alice")
        # whoami is called against the Conduit API with the token.
        url, data = fake.posted[0]
        self.assertEqual(url, self.API_URL + "user.whoami")
        self.assertEqual(data, {"access_token": "good-token"})

    def test_conduit_error_code_returns_none(self):
        body = {"error_code": "ERR-INVALID-AUTH", "error_info": "bad token"}
        fake = _FakeAsyncClient(response=_make_response(json_body=body))
        self.assertIsNone(self._verify(fake))

    def test_non_200_returns_none(self):
        fake = _FakeAsyncClient(response=_make_response(status_code=401, json_body={}))
        self.assertIsNone(self._verify(fake))

    def test_empty_result_returns_none(self):
        fake = _FakeAsyncClient(response=_make_response(json_body={"result": None}))
        self.assertIsNone(self._verify(fake))

    def test_http_error_returns_none(self):
        import httpx

        fake = _FakeAsyncClient(raise_exc=httpx.ConnectError("boom"))
        self.assertIsNone(self._verify(fake))

    def test_invalid_json_returns_none(self):
        fake = _FakeAsyncClient(response=_make_response(raise_json=True))
        self.assertIsNone(self._verify(fake))


class TestBuildPhabricatorAuthProvider(TestCase):
    def test_wires_upstream_endpoints_and_disables_pkce(self):
        provider = build_phabricator_auth_provider(
            base_url="https://phab.example.com",
            api_url="https://phab.example.com/api/",
            server_url="http://localhost:8000",
            client_id="CID",
            client_secret="SECRET",
            scope="maniphest file",
        )

        # Upstream endpoints point at Phabricator's OAuth server.
        self.assertEqual(
            str(provider._upstream_authorization_endpoint),
            "https://phab.example.com" + AUTH_PATH,
        )
        self.assertEqual(
            str(provider._upstream_token_endpoint),
            "https://phab.example.com" + TOKEN_PATH,
        )
        # Phabricator is not known to support PKCE.
        self.assertFalse(provider._forward_pkce)
