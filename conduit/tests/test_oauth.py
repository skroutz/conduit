import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from conduit.auth.oauth import OAuth2Client, OAuthError
from conduit.auth.token_store import TokenStore


class TestTokenStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store_path = Path(self.tmp) / "tokens.json"
        self.store = TokenStore(path=self.store_path)

    # ------------------------------------------------------------------
    def test_load_missing_file_returns_none(self):
        self.assertIsNone(self.store.load("key"))

    def test_save_and_load_roundtrip(self):
        token = {"access_token": "abc", "expires_at": 9999999999}
        self.store.save("key", token)
        loaded = self.store.load("key")
        self.assertEqual(loaded["access_token"], "abc")

    def test_save_sets_restrictive_permissions(self):
        self.store.save("k", {"access_token": "x"})
        mode = stat.S_IMODE(self.store_path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_multiple_keys_do_not_overwrite_each_other(self):
        self.store.save("a", {"access_token": "token_a"})
        self.store.save("b", {"access_token": "token_b"})
        self.assertEqual(self.store.load("a")["access_token"], "token_a")
        self.assertEqual(self.store.load("b")["access_token"], "token_b")

    def test_delete_removes_key(self):
        self.store.save("k", {"access_token": "x"})
        result = self.store.delete("k")
        self.assertTrue(result)
        self.assertIsNone(self.store.load("k"))

    def test_delete_returns_false_for_missing_key(self):
        result = self.store.delete("nonexistent")
        self.assertFalse(result)

    def test_load_corrupt_json_returns_none(self):
        self.store_path.write_text("{broken json")
        self.assertIsNone(self.store.load("k"))

    def test_overwrite_updates_token(self):
        self.store.save("k", {"access_token": "old"})
        self.store.save("k", {"access_token": "new"})
        self.assertEqual(self.store.load("k")["access_token"], "new")


class TestOAuth2ClientCaching(unittest.TestCase):
    """Tests that do not require a browser or real HTTP server."""

    def _make_client(self, store, token=None, expires_at=None, refresh=None):
        client = OAuth2Client(
            base_url="https://phab.example.com",
            client_id="test_client_id",
            token_store=store,
        )
        if token:
            record = {"access_token": token}
            if expires_at is not None:
                record["expires_at"] = expires_at
            if refresh:
                record["refresh_token"] = refresh
            store.save(client._store_key, record)
        return client

    def _make_store(self):
        tmp = tempfile.mkdtemp()
        return TokenStore(path=Path(tmp) / "tokens.json")

    # ------------------------------------------------------------------
    def test_returns_cached_valid_token_without_browser(self):
        store = self._make_store()
        future = int(time.time()) + 3600
        client = self._make_client(store, token="valid_token", expires_at=future)

        with patch.object(client, "_authorize_interactive") as mock_auth:
            result = client.get_token()

        self.assertEqual(result, "valid_token")
        mock_auth.assert_not_called()

    def test_token_with_no_expiry_treated_as_valid(self):
        store = self._make_store()
        # expires_at=0 means "no expiry info" — should be trusted
        client = self._make_client(store, token="eternal_token", expires_at=0)

        with patch.object(client, "_authorize_interactive") as mock_auth:
            result = client.get_token()

        self.assertEqual(result, "eternal_token")
        mock_auth.assert_not_called()

    def test_expired_token_without_refresh_triggers_browser(self):
        store = self._make_store()
        past = int(time.time()) - 1
        client = self._make_client(store, token="old_token", expires_at=past)

        with patch.object(client, "_authorize_interactive", return_value="new_token"):
            result = client.get_token()

        self.assertEqual(result, "new_token")

    def test_expired_token_with_refresh_attempts_refresh_first(self):
        store = self._make_store()
        past = int(time.time()) - 1
        client = self._make_client(
            store, token="old_token", expires_at=past, refresh="refresh_tok"
        )

        with patch.object(
            client, "_refresh", return_value="refreshed_token"
        ) as mock_ref:
            with patch.object(client, "_authorize_interactive") as mock_auth:
                result = client.get_token()

        self.assertEqual(result, "refreshed_token")
        mock_ref.assert_called_once_with("refresh_tok")
        mock_auth.assert_not_called()

    def test_failed_refresh_falls_back_to_browser(self):
        store = self._make_store()
        past = int(time.time()) - 1
        client = self._make_client(
            store, token="old", expires_at=past, refresh="bad_refresh"
        )

        with patch.object(client, "_refresh", side_effect=OAuthError("expired")):
            with patch.object(
                client, "_authorize_interactive", return_value="fresh"
            ) as mock_auth:
                result = client.get_token()

        self.assertEqual(result, "fresh")
        mock_auth.assert_called_once()

    def test_no_cached_token_triggers_browser(self):
        store = self._make_store()
        client = self._make_client(store)  # no token saved

        with patch.object(client, "_authorize_interactive", return_value="brand_new"):
            result = client.get_token()

        self.assertEqual(result, "brand_new")

    def test_logout_removes_token(self):
        store = self._make_store()
        future = int(time.time()) + 3600
        client = self._make_client(store, token="tok", expires_at=future)
        self.assertTrue(client.logout())
        self.assertIsNone(store.load(client._store_key))

    def test_store_key_differs_across_instances(self):
        store = self._make_store()
        c1 = OAuth2Client("https://phab1.example.com", "id1", token_store=store)
        c2 = OAuth2Client("https://phab2.example.com", "id2", token_store=store)
        self.assertNotEqual(c1._store_key, c2._store_key)

    def test_fixed_redirect_port_stored(self):
        store = self._make_store()
        client = OAuth2Client(
            "https://phab.example.com",
            "cid",
            token_store=store,
            redirect_port=8888,
        )
        self.assertEqual(client._redirect_port, 8888)

    def test_no_redirect_port_defaults_to_none(self):
        store = self._make_store()
        client = OAuth2Client("https://phab.example.com", "cid", token_store=store)
        self.assertIsNone(client._redirect_port)


class TestOAuth2TokenExchange(unittest.TestCase):
    """Tests for the HTTP token-exchange logic with a mocked HTTP client."""

    def _client(self):
        tmp = tempfile.mkdtemp()
        store = TokenStore(path=Path(tmp) / "tokens.json")
        return OAuth2Client(
            base_url="https://phab.example.com",
            client_id="cid",
            token_store=store,
        )

    def _mock_http(self, json_body, status_code=200):
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = json_body
        response.raise_for_status.return_value = None
        client = MagicMock()
        client.post.return_value = response
        return client

    # ------------------------------------------------------------------
    def test_exchange_code_persists_and_returns_access_token(self):
        oa = self._client()
        oa._http_client = self._mock_http(
            {"access_token": "tok123", "token_type": "Bearer", "expires_in": 3600}
        )
        result = oa._exchange_code("auth_code_xyz", "http://localhost:12345")
        self.assertEqual(result["access_token"], "tok123")
        # Token should be persisted
        stored = oa._store.load(oa._store_key)
        self.assertEqual(stored["access_token"], "tok123")
        self.assertIn("expires_at", stored)

    def test_exchange_raises_on_error_response(self):
        oa = self._client()
        oa._http_client = self._mock_http(
            {"error": "invalid_client", "error_description": "Bad client ID"}
        )
        with self.assertRaises(OAuthError) as cm:
            oa._exchange_code("code", "http://localhost:1")
        self.assertIn("invalid_client", str(cm.exception))

    def test_exchange_raises_on_missing_access_token(self):
        oa = self._client()
        oa._http_client = self._mock_http({"token_type": "Bearer"})
        with self.assertRaises(OAuthError):
            oa._exchange_code("code", "http://localhost:1")

    def test_exchange_raises_on_http_error(self):
        import httpx

        oa = self._client()
        mock_client = MagicMock()
        mock_client.post.side_effect = httpx.NetworkError("connection refused")
        oa._http_client = mock_client
        with self.assertRaises(OAuthError) as cm:
            oa._exchange_code("code", "http://localhost:1")
        self.assertIn("Token endpoint request failed", str(cm.exception))

    def test_expires_at_computed_from_expires_in(self):
        oa = self._client()
        oa._http_client = self._mock_http({"access_token": "t", "expires_in": 1800})
        before = int(time.time())
        oa._exchange_code("c", "http://localhost:1")
        after = int(time.time())
        stored = oa._store.load(oa._store_key)
        self.assertGreaterEqual(stored["expires_at"], before + 1800)
        self.assertLessEqual(stored["expires_at"], after + 1800)

    def test_refresh_token_stored_when_provided(self):
        oa = self._client()
        oa._http_client = self._mock_http(
            {
                "access_token": "new_tok",
                "refresh_token": "ref_tok",
                "expires_in": 3600,
            }
        )
        oa._exchange_code("c", "http://localhost:1")
        stored = oa._store.load(oa._store_key)
        self.assertEqual(stored["refresh_token"], "ref_tok")


class TestOAuth2WithConduitApp(unittest.TestCase):
    """Verify ConduitApp uses the OAuth2Client correctly."""

    def setUp(self):
        os.environ["PHABRICATOR_URL"] = "https://phab.example.com/api/"

    def tearDown(self):
        os.environ.pop("PHABRICATOR_URL", None)

    def test_conduit_app_calls_oauth_get_token(self):
        from conduit.conduit import ConduitApp, PhabricatorConfig

        config = PhabricatorConfig(require_token=False)
        mock_oauth = MagicMock()
        mock_oauth.get_token.return_value = "oauth_token_" + "x" * 20

        app = ConduitApp(config, use_sse=False, oauth_client=mock_oauth)
        client = app.get_client()

        mock_oauth.get_token.assert_called_once()
        self.assertEqual(client.maniphest.api_token, "oauth_token_" + "x" * 20)

    def test_conduit_app_reuses_cached_client_with_oauth(self):
        from conduit.conduit import ConduitApp, PhabricatorConfig

        config = PhabricatorConfig(require_token=False)
        mock_oauth = MagicMock()
        mock_oauth.get_token.return_value = "oauth_token_" + "x" * 20

        app = ConduitApp(config, use_sse=False, oauth_client=mock_oauth)
        c1 = app.get_client()
        c2 = app.get_client()

        self.assertIs(c1, c2)
        mock_oauth.get_token.assert_called_once()

    def test_conduit_app_sets_oauth_token_flag_on_client(self):
        """Client built via OAuth must use access_token not api.token."""
        from conduit.conduit import ConduitApp, PhabricatorConfig

        config = PhabricatorConfig(require_token=False)
        mock_oauth = MagicMock()
        mock_oauth.get_token.return_value = "bearer_token_" + "x" * 20

        app = ConduitApp(config, use_sse=False, oauth_client=mock_oauth)
        client = app.get_client()

        self.assertTrue(client.maniphest.oauth_token)
        self.assertTrue(client.differential.oauth_token)
        self.assertTrue(client.user.oauth_token)

    def test_conduit_app_no_oauth_flag_for_token_mode(self):
        """Client built from a Conduit API token must NOT set oauth_token."""
        from conduit.conduit import ConduitApp, PhabricatorConfig

        config = PhabricatorConfig(token="a" * 32, require_token=False)
        app = ConduitApp(config, use_sse=False, oauth_client=None)
        client = app.get_client()

        self.assertFalse(client.maniphest.oauth_token)


class TestPhabricatorConfigOAuth(unittest.TestCase):
    """Verify PhabricatorConfig validation rules under OAuth mode."""

    def setUp(self):
        os.environ["PHABRICATOR_URL"] = "https://phab.example.com/api/"

    def tearDown(self):
        os.environ.pop("PHABRICATOR_URL", None)

    def test_oauth_token_of_any_length_accepted(self):
        """OAuth bearer tokens are not Conduit tokens — 32-char rule must not apply."""
        from conduit.conduit import PhabricatorConfig

        # Short bearer token — would fail the 32-char check without the client_id bypass.
        config = PhabricatorConfig(
            token="short_bearer",
            client_id="some_client",
            require_token=False,
        )
        self.assertEqual(config.token, "short_bearer")

    def test_conduit_token_still_validated_without_client_id(self):
        """Without client_id the 32-char rule must still be enforced."""
        from conduit.conduit import PhabricatorConfig

        with self.assertRaises(ValueError):
            PhabricatorConfig(token="too_short", require_token=False)

    def test_require_token_raises_without_token_or_client_id(self):
        from conduit.conduit import PhabricatorConfig

        with self.assertRaises(ValueError):
            PhabricatorConfig(require_token=True)


class TestBasePhabricatorClientOAuthFlag(unittest.TestCase):
    """Verify _make_request uses the correct auth parameter name."""

    def _make_base_client(self, oauth_token: bool):
        from conduit.client.base import BasePhabricatorClient

        class _Concrete(BasePhabricatorClient):
            pass

        mock_http = MagicMock()
        response = MagicMock()
        response.json.return_value = {"result": {"ok": True}, "error_code": None}
        response.raise_for_status.return_value = None
        mock_http.post.return_value = response

        client = _Concrete(
            "https://phab.example.com/api/",
            "mytoken",
            http_client=mock_http,
            oauth_token=oauth_token,
        )
        return client, mock_http

    def test_conduit_token_mode_sends_api_token_param(self):
        client, mock_http = self._make_base_client(oauth_token=False)
        client._make_request("conduit.ping")

        _, kwargs = mock_http.post.call_args
        sent_data = kwargs.get("data", {})
        self.assertIn("api.token", sent_data)
        self.assertNotIn("access_token", sent_data)
        self.assertEqual(sent_data["api.token"], "mytoken")

    def test_oauth_token_mode_sends_access_token_param(self):
        client, mock_http = self._make_base_client(oauth_token=True)
        client._make_request("conduit.ping")

        _, kwargs = mock_http.post.call_args
        sent_data = kwargs.get("data", {})
        self.assertIn("access_token", sent_data)
        self.assertNotIn("api.token", sent_data)
        self.assertEqual(sent_data["access_token"], "mytoken")


class TestOAuth2ClientSecret(unittest.TestCase):
    """Verify client_secret is included in token payloads when provided."""

    def _client_with_secret(self, secret=None):
        tmp = tempfile.mkdtemp()
        store = TokenStore(path=Path(tmp) / "tokens.json")
        return OAuth2Client(
            base_url="https://phab.example.com",
            client_id="cid",
            client_secret=secret,
            token_store=store,
        )

    def _mock_http(self, json_body):
        response = MagicMock()
        response.json.return_value = json_body
        response.raise_for_status.return_value = None
        http = MagicMock()
        http.post.return_value = response
        return http

    def test_client_secret_included_in_exchange_payload(self):
        oa = self._client_with_secret("s3cr3t")
        oa._http_client = self._mock_http({"access_token": "t", "expires_in": 3600})
        oa._exchange_code("code", "http://localhost:1")

        _, kwargs = oa._http_client.post.call_args
        self.assertEqual(kwargs.get("data", {}).get("client_secret"), "s3cr3t")

    def test_client_secret_omitted_when_not_provided(self):
        oa = self._client_with_secret(None)
        oa._http_client = self._mock_http({"access_token": "t", "expires_in": 3600})
        oa._exchange_code("code", "http://localhost:1")

        _, kwargs = oa._http_client.post.call_args
        self.assertNotIn("client_secret", kwargs.get("data", {}))

    def test_client_secret_included_in_refresh_payload(self):
        oa = self._client_with_secret("s3cr3t")
        oa._http_client = self._mock_http({"access_token": "new", "expires_in": 3600})
        oa._refresh("old_refresh")

        _, kwargs = oa._http_client.post.call_args
        self.assertEqual(kwargs.get("data", {}).get("client_secret"), "s3cr3t")


class TestCallbackHandler(unittest.TestCase):
    """Unit-test the HTTP callback handler in isolation."""

    def _make_server_and_handler(self, path, expected_state=None):
        """Simulate a GET request to *path* and return the server state."""
        import io

        server = MagicMock()
        server.auth_code = None
        server.auth_error = None
        server.expected_state = expected_state

        handler = object.__new__(
            __import__(
                "conduit.auth.oauth", fromlist=["_CallbackHandler"]
            )._CallbackHandler
        )
        handler.server = server
        handler.path = path
        handler.requestline = f"GET {path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.command = "GET"

        # Capture writes
        output = io.BytesIO()
        handler.wfile = output
        handler.rfile = io.BytesIO()

        responses = []
        handler.send_response = lambda code: responses.append(code)
        handler.send_header = lambda k, v: None
        handler.end_headers = lambda: None

        handler.do_GET()
        return server, responses, output.getvalue()

    def test_success_path_sets_auth_code(self):
        server, _, _ = self._make_server_and_handler(
            "/?code=abc123&state=xyz", expected_state="xyz"
        )
        self.assertEqual(server.auth_code, "abc123")
        self.assertIsNone(server.auth_error)

    def test_error_path_sets_auth_error(self):
        server, _, _ = self._make_server_and_handler(
            "/?error=access_denied&error_description=User+denied"
        )
        self.assertIsNone(server.auth_code)
        self.assertEqual(server.auth_error, "User denied")

    def test_no_params_sets_generic_error(self):
        server, _, _ = self._make_server_and_handler("/")
        self.assertIsNone(server.auth_code)
        self.assertIsNotNone(server.auth_error)

    def test_success_response_contains_success_message(self):
        _, _, body = self._make_server_and_handler(
            "/?code=tok&state=s", expected_state="s"
        )
        self.assertIn(b"Authentication successful", body)

    def test_error_response_contains_error_message(self):
        _, _, body = self._make_server_and_handler(
            "/?error=access_denied&error_description=Denied"
        )
        self.assertIn(b"Authentication failed", body)

    def test_state_mismatch_sets_error(self):
        server, _, _ = self._make_server_and_handler(
            "/?code=abc123&state=attacker_state", expected_state="legit_state"
        )
        self.assertIsNone(server.auth_code)
        self.assertIn("state mismatch", server.auth_error.lower())

    def test_missing_state_in_callback_sets_error(self):
        server, _, _ = self._make_server_and_handler(
            "/?code=abc123", expected_state="legit_state"
        )
        self.assertIsNone(server.auth_code)
        self.assertIn("state mismatch", server.auth_error.lower())

    def test_state_mismatch_response_contains_error_message(self):
        _, _, body = self._make_server_and_handler(
            "/?code=abc123&state=bad", expected_state="good"
        )
        self.assertIn(b"Authentication failed", body)


class TestBuildAuthUrl(unittest.TestCase):
    """Verify the authorization URL is constructed correctly."""

    def _client(self):
        tmp = tempfile.mkdtemp()
        return OAuth2Client(
            base_url="https://phab.example.com",
            client_id="my_client",
            scope="maniphest",
            token_store=TokenStore(path=Path(tmp) / "t.json"),
        )

    def test_auth_url_contains_required_params(self):
        import urllib.parse

        client = self._client()
        url = client._build_auth_url("http://localhost:8889", "state_val")
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)

        self.assertEqual(parsed.scheme, "https")
        self.assertIn("oauthserver/auth", parsed.path)
        self.assertEqual(params["client_id"], ["my_client"])
        self.assertEqual(params["redirect_uri"], ["http://localhost:8889"])
        self.assertEqual(params["response_type"], ["code"])
        self.assertEqual(params["scope"], ["maniphest"])
        self.assertEqual(params["state"], ["state_val"])


class TestBaseUrlNormalisation(unittest.TestCase):
    """Verify /api suffix is stripped before constructing OAuth URLs."""

    def setUp(self):
        os.environ["PHABRICATOR_URL"] = "https://phab.example.com/api/"

    def tearDown(self):
        os.environ.pop("PHABRICATOR_URL", None)
        os.environ.pop("PHABRICATOR_OAUTH_CLIENT_ID", None)

    def _run_main_with_args(self, argv):
        from unittest.mock import patch
        from conduit.auth.oauth import OAuth2Client

        captured = {}

        original_init = OAuth2Client.__init__

        def capturing_init(self, base_url, client_id, **kwargs):
            captured["base_url"] = base_url
            # Don't actually run the real __init__ side-effects we don't need
            original_init(self, base_url, client_id, **kwargs)

        with patch("sys.argv", argv):
            with patch.object(OAuth2Client, "__init__", capturing_init):
                with patch("conduit.conduit.ConduitApp.run_stdio_mode"):
                    try:
                        from conduit.conduit import main

                        main()
                    except SystemExit:
                        pass

        return captured

    def test_api_suffix_stripped_from_url(self):
        captured = self._run_main_with_args(
            [
                "conduit-mcp",
                "--url",
                "https://phab.example.com/api/",
                "--client-id",
                "cid",
                "--client-secret",
                "sec",
                "--oauth-redirect-port",
                "8889",
            ]
        )
        self.assertEqual(captured.get("base_url"), "https://phab.example.com")

    def test_url_without_api_suffix_unchanged(self):
        captured = self._run_main_with_args(
            [
                "conduit-mcp",
                "--url",
                "https://phab.example.com/",
                "--client-id",
                "cid",
                "--client-secret",
                "sec",
                "--oauth-redirect-port",
                "8889",
            ]
        )
        self.assertEqual(captured.get("base_url"), "https://phab.example.com")


if __name__ == "__main__":
    unittest.main()
