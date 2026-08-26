"""Tests for BasePhabricatorClient._make_request, in particular the
streamed, byte-ceilinged response handling."""

import gzip
import json

import httpx
import pytest

from conduit.client.base import BasePhabricatorClient
from conduit.utils import PhabricatorAPIError

_A_RESPONSE_LIMIT = 1_000_000


def _client_with(handler) -> BasePhabricatorClient:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return BasePhabricatorClient(
        api_url="http://test.example.com/api/",
        api_token="test_token",
        http_client=http_client,
    )


class TestMakeRequest:
    def test_returns_the_result_field_on_success(self):
        def handler(request):
            return httpx.Response(200, json={"result": {"ok": True}})

        client = _client_with(handler)

        assert client._make_request("conduit.ping") == {"ok": True}

    def test_sends_the_api_token(self):
        captured = {}

        def handler(request):
            captured["body"] = request.read().decode()
            return httpx.Response(200, json={"result": {}})

        client = _client_with(handler)
        client._make_request("conduit.ping", {"foo": "bar"})

        assert "api.token=test_token" in captured["body"]

    def test_sends_oauth_access_token_when_configured(self):
        captured = {}

        def handler(request):
            captured["body"] = request.read().decode()
            return httpx.Response(200, json={"result": {}})

        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        client = BasePhabricatorClient(
            api_url="http://test.example.com/api/",
            api_token="oauth_token",
            http_client=http_client,
            oauth_token=True,
        )
        client._make_request("conduit.ping")

        assert "access_token=oauth_token" in captured["body"]
        assert "api.token" not in captured["body"]

    def test_raises_on_an_application_error_code(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "error_code": "ERR-BAD-METHOD",
                    "error_info": "Unknown method",
                    "result": None,
                },
            )

        client = _client_with(handler)

        with pytest.raises(PhabricatorAPIError) as exc_info:
            client._make_request("bogus.method")

        assert exc_info.value.error_code == "ERR-BAD-METHOD"

    def test_raises_a_network_error_on_a_bad_status(self):
        def handler(request):
            return httpx.Response(500, text="Internal Server Error")

        client = _client_with(handler)

        with pytest.raises(PhabricatorAPIError, match="Network error"):
            client._make_request("conduit.ping")

    def test_raises_on_invalid_json(self):
        def handler(request):
            return httpx.Response(200, content=b"not json")

        client = _client_with(handler)

        with pytest.raises(PhabricatorAPIError, match="Invalid JSON response"):
            client._make_request("conduit.ping")

    def test_raises_before_fully_buffering_an_oversized_response(self):
        oversized = json.dumps(
            {"result": {"content": "x" * (_A_RESPONSE_LIMIT + 1)}}
        ).encode()

        def handler(request):
            return httpx.Response(
                200, content=oversized, headers={"content-type": "application/json"}
            )

        client = _client_with(handler)

        with pytest.raises(PhabricatorAPIError) as exc_info:
            client._make_request("paste.search", max_response_bytes=_A_RESPONSE_LIMIT)

        assert exc_info.value.error_code == "RESPONSE_TOO_LARGE"

    def test_allows_a_response_right_at_the_boundary(self):
        # A response just under the ceiling must still succeed -- this is
        # not an off-by-one trap for legitimate large-but-acceptable data.
        padding = "x" * (_A_RESPONSE_LIMIT - 100)
        payload = json.dumps({"result": {"content": padding}}).encode()
        assert len(payload) < _A_RESPONSE_LIMIT

        def handler(request):
            return httpx.Response(
                200, content=payload, headers={"content-type": "application/json"}
            )

        client = _client_with(handler)

        result = client._make_request(
            "paste.search", max_response_bytes=_A_RESPONSE_LIMIT
        )

        assert result["content"] == padding

    def test_no_size_ceiling_by_default(self):
        # max_response_bytes is opt-in per call -- established content APIs
        # (file.download, diffusion raw diffs, wiki content, ...) share
        # _make_request() and must not be capped unless they ask for it.
        payload = json.dumps(
            {"result": {"content": "x" * (_A_RESPONSE_LIMIT + 1)}}
        ).encode()

        def handler(request):
            return httpx.Response(
                200, content=payload, headers={"content-type": "application/json"}
            )

        client = _client_with(handler)

        result = client._make_request("file.download")

        assert len(result["content"]) == _A_RESPONSE_LIMIT + 1

    def test_requests_identity_encoding(self):
        captured = {}

        def handler(request):
            captured["headers"] = request.headers
            return httpx.Response(200, json={"result": {}})

        client = _client_with(handler)
        client._make_request("conduit.ping")

        assert captured["headers"]["accept-encoding"] == "identity"

    def test_rejects_a_gzip_encoded_response(self):
        # A compliant server never sends this back given the
        # Accept-Encoding: identity request above; this covers a
        # misconfigured proxy/gateway -- or a hostile one -- ignoring it and
        # sending a compressed body anyway. Real gzip content (highly
        # compressible, expands enormously) proves the rejection happens
        # off the Content-Encoding header alone, not by inspecting the body.
        huge_but_compressible = json.dumps(
            {"result": {"content": "0" * 40_000_000}}
        ).encode()
        gzipped = gzip.compress(huge_but_compressible)
        assert len(gzipped) < _A_RESPONSE_LIMIT

        def handler(request):
            return httpx.Response(
                200,
                content=gzipped,
                headers={
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                },
            )

        client = _client_with(handler)

        with pytest.raises(PhabricatorAPIError) as exc_info:
            client._make_request("paste.search", max_response_bytes=_A_RESPONSE_LIMIT)

        assert exc_info.value.error_code == "UNSAFE_RESPONSE_ENCODING"

    def test_never_reads_the_body_of_a_rejected_encoded_response(self):
        # Stronger than the gzip test above: the body stream raises if
        # iterated at all, so this proves the encoding check happens
        # strictly before any attempt to read/decode the response body --
        # not merely that the end result of decoding gets rejected.
        class _ExplodingStream(httpx.SyncByteStream):
            def __iter__(self):
                raise AssertionError(
                    "response body must not be read for a rejected encoding"
                )

            def close(self):
                pass

        def handler(request):
            return httpx.Response(
                200,
                headers={"content-encoding": "gzip"},
                stream=_ExplodingStream(),
            )

        client = _client_with(handler)

        with pytest.raises(PhabricatorAPIError) as exc_info:
            client._make_request("paste.search")

        assert exc_info.value.error_code == "UNSAFE_RESPONSE_ENCODING"
