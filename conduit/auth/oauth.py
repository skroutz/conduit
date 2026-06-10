"""
OAuth2 Authorization Code flow for Phabricator.

Phabricator's OAuth2 server is described at:
  https://we.phorge.it/book/phabricator/article/using_oauth/

Endpoints (relative to the Phabricator base URL):
  Authorization:  /oauthserver/auth/
  Token:          /oauthserver/token/

The redirect URI registered in Phabricator is "http://localhost" and must
accept any ephemeral port, so we bind a loopback HTTP server on a random
available port and pass "http://localhost:<port>" as the redirect_uri.
"""

import hashlib
import http.server
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from typing import Optional

import httpx

from conduit.auth.token_store import TokenStore


class OAuthError(Exception):
    pass


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP handler that captures the authorization code from the redirect."""

    def do_GET(self):  # noqa: N802 — stdlib naming convention
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "error" in params:
            self.server.auth_code = None
            self.server.auth_error = params.get("error_description", params["error"])[0]
        elif "code" in params:
            returned_state = params.get("state", [None])[0]
            expected_state = getattr(self.server, "expected_state", None)
            if expected_state is None or returned_state != expected_state:
                self.server.auth_code = None
                self.server.auth_error = "OAuth2 state mismatch — possible CSRF attack"
            else:
                self.server.auth_code = params["code"][0]
                self.server.auth_error = None
        else:
            self.server.auth_code = None
            self.server.auth_error = "No authorization code in callback"

        body = self._build_response_html()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _build_response_html(self) -> bytes:
        if self.server.auth_code:
            message = (
                "<h2>Authentication successful!</h2>"
                "<p>You can close this tab and return to your terminal.</p>"
            )
        else:
            message = (
                f"<h2>Authentication failed</h2>"
                f"<p>{self.server.auth_error}</p>"
                "<p>You can close this tab and return to your terminal.</p>"
            )
        return (
            f"<!DOCTYPE html><html><body>{message}</body></html>"
        ).encode("utf-8")

    def log_message(self, format, *args):  # noqa: A002 — shadow stdlib intentional
        pass  # suppress default request logging to keep stdout clean


class _OAuthCallbackServer(http.server.HTTPServer):
    auth_code: Optional[str] = None
    auth_error: Optional[str] = None
    expected_state: Optional[str] = None

    def __init__(self, server_address, handler, af: int = socket.AF_INET):
        self.address_family = af
        super().__init__(server_address, handler)


def _resolve_localhost() -> tuple:
    """
    Return (address, af_family) matching how the OS resolves 'localhost'.
    On macOS, localhost typically resolves to ::1 (IPv6); on Linux it is
    usually 127.0.0.1 (IPv4).  We follow getaddrinfo so the listener binds
    to the same address the browser will connect to.
    """
    infos = socket.getaddrinfo(
        "localhost", None, socket.AF_UNSPEC, socket.SOCK_STREAM
    )
    if infos:
        af, _, _, _, addr = infos[0]
        return addr[0], af
    return "127.0.0.1", socket.AF_INET


def _find_free_port(address: str, af: int) -> int:
    """Bind to port 0 on *address* to let the OS pick a free port."""
    with socket.socket(af, socket.SOCK_STREAM) as s:
        s.bind((address, 0))
        return s.getsockname()[1]


class OAuth2Client:
    """
    Manages the OAuth2 Authorization Code flow against Phabricator.

    Parameters
    ----------
    base_url:
        The Phabricator instance URL, e.g. ``https://phab.example.com``.
        A trailing slash is normalized away.
    client_id:
        The OAuth Application client ID registered in Phabricator.
    scope:
        Space-separated OAuth scopes to request.  Defaults to ``whoami``.
    token_store:
        Where to persist tokens.  Defaults to ``~/.conduit/oauth_tokens.json``.
    http_client:
        An ``httpx.Client`` to use for token endpoint requests.  If omitted,
        a short-lived client is created for each exchange.
    timeout:
        Seconds to wait for the user to complete the browser auth flow.
    """

    AUTH_PATH = "/oauthserver/auth/"
    TOKEN_PATH = "/oauthserver/token/"

    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: Optional[str] = None,
        scope: str = "whoami maniphest",
        token_store: Optional[TokenStore] = None,
        http_client: Optional[httpx.Client] = None,
        timeout: int = 300,
        redirect_port: Optional[int] = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._store = token_store or TokenStore()
        self._http_client = http_client
        self._timeout = timeout
        self._redirect_port = redirect_port  # None means use a random port

        # Cache key is derived from (base_url, client_id) so that different
        # instances or scopes don't accidentally share tokens.
        self._store_key = hashlib.sha256(
            f"{self._base_url}\x00{self._client_id}".encode()
        ).hexdigest()[:16]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_token(self) -> str:
        """
        Return a valid Conduit API token, running the browser flow if needed.

        The token is retrieved in order of preference:
        1. Valid cached token (not expired / close to expiry).
        2. Refreshed via refresh_token (if available and ``offline_access`` was granted).
        3. Full interactive browser authorization.
        """
        cached = self._store.load(self._store_key)
        if cached:
            access_token = cached.get("access_token")
            expires_at = cached.get("expires_at", 0)
            refresh_token = cached.get("refresh_token")

            # Use cached token if it has more than 60 seconds of remaining life
            # (or if no expiry was provided, assume it's still valid).
            if access_token and (expires_at == 0 or time.time() < expires_at - 60):
                return access_token

            # Attempt a token refresh before falling back to full re-auth.
            if refresh_token:
                try:
                    return self._refresh(refresh_token)
                except OAuthError:
                    pass  # fall through to interactive flow

        return self._authorize_interactive()

    def logout(self) -> bool:
        """Remove the stored token.  Returns True if a token was removed."""
        return self._store.delete(self._store_key)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _authorize_interactive(self) -> str:
        """Run the browser-based authorization code flow."""
        # Resolve how 'localhost' maps on this machine so the listener and the
        # browser use the same address family (IPv4 vs IPv6).
        local_addr, af = _resolve_localhost()

        if self._redirect_port is not None:
            port = self._redirect_port
        else:
            # No explicit port — use a random one.  This only works if the
            # Phabricator OAuth app is registered with a redirect URI that
            # includes the same port (e.g. "http://localhost:8888").
            port = _find_free_port(local_addr, af)

        redirect_uri = f"http://localhost:{port}"

        state = secrets.token_urlsafe(32)

        auth_url = self._build_auth_url(redirect_uri, state)

        server = _OAuthCallbackServer((local_addr, port), _CallbackHandler, af=af)
        server.expected_state = state
        server.timeout = self._timeout

        print("\nOpening browser for Phabricator authentication...", file=sys.stderr)
        print(f"If the browser does not open automatically, visit:\n  {auth_url}\n", file=sys.stderr)

        webbrowser.open(auth_url)

        # Wait for the callback in the same thread (single request, then stop).
        deadline = time.time() + self._timeout
        while server.auth_code is None and server.auth_error is None:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise OAuthError(
                    f"Timed out waiting for OAuth2 callback after {self._timeout}s"
                )
            server.timeout = min(remaining, 5.0)
            server.handle_request()

        server.server_close()

        if server.auth_error:
            raise OAuthError(f"Authorization denied: {server.auth_error}")

        code = server.auth_code
        token_data = self._exchange_code(code, redirect_uri)
        self._persist(token_data)
        return token_data["access_token"]

    def _build_auth_url(self, redirect_uri: str, state: str) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": self._scope,
            "state": state,
        }
        return self._base_url + self.AUTH_PATH + "?" + urllib.parse.urlencode(params)

    def _exchange_code(self, code: str, redirect_uri: str) -> dict:
        """Exchange an authorization code for tokens."""
        payload = {
            "grant_type": "authorization_code",
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "code": code,
        }
        if self._client_secret:
            payload["client_secret"] = self._client_secret
        token_data = self._post_token(payload)
        self._persist(token_data)
        return token_data

    def _refresh(self, refresh_token: str) -> str:
        """Use a refresh token to obtain a new access token."""
        payload = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "refresh_token": refresh_token,
        }
        if self._client_secret:
            payload["client_secret"] = self._client_secret
        token_data = self._post_token(payload)
        self._persist(token_data)
        return token_data["access_token"]

    def _post_token(self, payload: dict) -> dict:
        url = self._base_url + self.TOKEN_PATH

        owns_client = self._http_client is None
        client = self._http_client or httpx.Client(
            timeout=30.0,
            follow_redirects=True,
        )
        try:
            response = client.post(url, data=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OAuthError(f"Token endpoint request failed: {exc}") from exc
        finally:
            if owns_client:
                client.close()

        try:
            data = response.json()
        except Exception as exc:
            raise OAuthError(f"Token endpoint returned invalid JSON: {exc}") from exc

        if "error" in data:
            raise OAuthError(
                f"Token endpoint error: {data['error']} — "
                f"{data.get('error_description', '')}"
            )

        if "access_token" not in data:
            raise OAuthError(
                "Token endpoint response missing 'access_token'"
            )

        return data

    def _persist(self, token_data: dict) -> None:
        """Store token data, computing expires_at from expires_in if present."""
        record = dict(token_data)
        expires_in = record.get("expires_in")
        if expires_in and "expires_at" not in record:
            record["expires_at"] = int(time.time()) + int(expires_in)
        self._store.save(self._store_key, record)
