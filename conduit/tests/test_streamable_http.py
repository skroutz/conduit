"""Tests for streamable HTTP transport selection and per-request auth."""

import os
from unittest import TestCase
from unittest.mock import MagicMock, patch

from conduit.conduit import ConduitApp, PhabricatorConfig, resolve_transport


class TestTransportResolution(TestCase):
    def test_explicit_transport_wins(self):
        self.assertEqual(resolve_transport("http"), "http")
        self.assertEqual(resolve_transport("sse"), "sse")
        self.assertEqual(resolve_transport("stdio"), "stdio")

    def test_default_is_stdio(self):
        with patch("conduit.conduit.sys.argv", ["conduit-mcp"]):
            self.assertEqual(resolve_transport(None), "stdio")

    def test_legacy_host_port_implies_sse(self):
        with patch("conduit.conduit.sys.argv", ["conduit-mcp", "--port", "8000"]):
            self.assertEqual(resolve_transport(None), "sse")
        with patch("conduit.conduit.sys.argv", ["conduit-mcp", "--host", "0.0.0.0"]):
            self.assertEqual(resolve_transport(None), "sse")


class TestHttpHeaderAuth(TestCase):
    """HTTP mode without OAuth reuses the per-request X-PHABRICATOR-TOKEN path."""

    def setUp(self):
        super().setUp()
        os.environ["PHABRICATOR_URL"] = "https://test.example.com/api/"
        self.config = PhabricatorConfig(require_token=False)
        self.app = ConduitApp(self.config, transport="http")

    def tearDown(self):
        super().tearDown()
        os.environ.pop("PHABRICATOR_URL", None)

    def test_app_reports_http_transport(self):
        self.assertEqual(self.app.transport, "http")
        self.assertFalse(self.app.use_sse)

    @patch("conduit.conduit.get_http_headers")
    def test_fresh_client_per_request(self, mock_headers):
        token_a = "user_a_token_" + "x" * 19
        mock_headers.return_value = {"x-phabricator-token": token_a}
        client_a = self.app.get_client()

        token_b = "user_b_token_" + "y" * 19
        mock_headers.return_value = {"x-phabricator-token": token_b}
        client_b = self.app.get_client()

        self.assertIsNot(client_a, client_b)
        self.assertEqual(client_a.user.api_token, token_a)
        self.assertEqual(client_b.user.api_token, token_b)

    @patch("conduit.conduit.get_http_headers")
    def test_missing_token_raises(self, mock_headers):
        mock_headers.return_value = {}
        with self.assertRaises(ValueError) as cm:
            self.app.get_client()
        self.assertIn("X-PHABRICATOR-TOKEN", str(cm.exception))

    @patch("conduit.conduit.get_http_headers")
    def test_short_token_raises(self, mock_headers):
        mock_headers.return_value = {"x-phabricator-token": "short"}
        with self.assertRaises(ValueError) as cm:
            self.app.get_client()
        self.assertIn("32 characters", str(cm.exception))


class TestHttpOAuthAuth(TestCase):
    """HTTP mode with an OAuth provider reads the token from request context."""

    def setUp(self):
        super().setUp()
        os.environ["PHABRICATOR_URL"] = "https://test.example.com/api/"
        self.config = PhabricatorConfig(require_token=False)
        # A sentinel auth provider is enough; get_client only checks presence.
        self.app = ConduitApp(self.config, transport="http", auth_provider=MagicMock())

    def tearDown(self):
        super().tearDown()
        os.environ.pop("PHABRICATOR_URL", None)

    def test_token_from_context_builds_oauth_client(self):
        access = MagicMock()
        access.token = "oauth-access-token"
        with patch("fastmcp.server.dependencies.get_access_token", return_value=access):
            client = self.app.get_client()
        self.assertEqual(client.user.api_token, "oauth-access-token")
        # OAuth tokens are sent as access_token, not api.token.
        self.assertTrue(client.user.oauth_token)

    def test_missing_context_token_raises(self):
        with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
            with self.assertRaises(ValueError) as cm:
                self.app.get_client()
        self.assertIn("OAuth flow", str(cm.exception))


class TestHttpRunWiring(TestCase):
    """run_http_mode plumbs host/port/stateless into mcp.run."""

    def setUp(self):
        super().setUp()
        os.environ["PHABRICATOR_URL"] = "https://test.example.com/api/"

    def tearDown(self):
        super().tearDown()
        os.environ.pop("PHABRICATOR_URL", None)

    def test_run_http_mode_passes_stateless(self):
        app = ConduitApp(PhabricatorConfig(require_token=False), transport="http")
        app.mcp = MagicMock()
        app.run_http_mode("127.0.0.1", 9000, stateless=True)
        app.mcp.run.assert_called_once_with(
            transport="http",
            host="127.0.0.1",
            port=9000,
            path="/mcp",
            stateless_http=True,
        )
