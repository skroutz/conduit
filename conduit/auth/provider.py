"""
MCP-spec OAuth2 support for the streamable HTTP transport.

Whereas :mod:`conduit.auth.oauth` makes *conduit* an OAuth **client** of
Phabricator (a local-loopback browser flow used in stdio mode), this module
makes *conduit itself* an OAuth Authorization Server to MCP clients, the way
public MCP servers (e.g. GitHub's) work: the MCP client points at the server
URL, the user is redirected in a browser to authenticate against Phabricator, a
token is issued, and that token authenticates every subsequent request.

Phabricator's OAuth server supports neither Dynamic Client Registration (DCR)
nor (per our deployment notes) PKCE, so we lean on FastMCP's ``OAuthProxy``,
which presents a DCR-compliant OAuth2.1 interface to MCP clients while proxying
the authorization-code flow to a fixed upstream app.  The upstream Phabricator
access token is stored server-side by the proxy and surfaced to tool handlers
through ``get_access_token().token`` after ``PhabricatorTokenVerifier`` validates
it via ``user.whoami``.

Endpoints (relative to the Phabricator base URL) mirror
``conduit.auth.oauth.OAuth2Client``:
  Authorization:  /oauthserver/auth/
  Token:          /oauthserver/token/
"""

from typing import List, Optional

import httpx

from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy

# Phabricator OAuth endpoint paths (kept in sync with conduit.auth.oauth).
AUTH_PATH = "/oauthserver/auth/"
TOKEN_PATH = "/oauthserver/token/"


class PhabricatorTokenVerifier(TokenVerifier):
    """
    Verify Phabricator OAuth access tokens by calling ``user.whoami``.

    Phabricator OAuth tokens are opaque (not JWTs), so the only way to validate
    one is to make an authenticated Conduit API call.  ``user.whoami`` is cheap
    and confirms both that the token is live and which user it belongs to.

    The verified token is returned unchanged in ``AccessToken.token`` so that
    tool handlers can recover it via ``get_access_token().token`` and use it as
    the ``access_token`` for downstream Conduit requests.
    """

    def __init__(
        self,
        *,
        api_url: str,
        required_scopes: Optional[List[str]] = None,
        timeout_seconds: int = 10,
        disable_cert_verify: bool = False,
        proxy: Optional[str] = None,
    ):
        super().__init__(required_scopes=required_scopes)
        # Normalise to a trailing slash so urljoin-style concatenation works.
        self.api_url = api_url.rstrip("/") + "/"
        self.timeout_seconds = timeout_seconds
        self._disable_cert_verify = disable_cert_verify
        self._proxy = proxy

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        """Validate a Phabricator OAuth token; return an AccessToken or None."""
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                verify=not self._disable_cert_verify,
                proxy=self._proxy,
                follow_redirects=True,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as client:
                response = await client.post(
                    self.api_url + "user.whoami",
                    data={"access_token": token},
                )
        except httpx.HTTPError:
            return None

        if response.status_code != 200:
            return None

        try:
            data = response.json()
        except ValueError:
            return None

        # Conduit signals auth/permission failures in the body, not the status.
        if data.get("error_code"):
            return None

        result = data.get("result")
        if not result:
            return None

        return AccessToken(
            token=token,
            client_id=str(result.get("phid", "unknown")),
            scopes=self.required_scopes or [],
            expires_at=None,
            claims={
                "sub": result.get("phid"),
                "username": result.get("userName"),
                "name": result.get("realName"),
                "whoami": result,
            },
        )


def build_phabricator_auth_provider(
    *,
    base_url: str,
    api_url: str,
    server_url: str,
    client_id: str,
    client_secret: Optional[str] = None,
    scope: str = "maniphest file",
    forward_pkce: bool = False,
    disable_cert_verify: bool = False,
    proxy: Optional[str] = None,
) -> OAuthProxy:
    """
    Build an :class:`OAuthProxy` that fronts Phabricator's OAuth server.

    Parameters
    ----------
    base_url:
        Phabricator instance base URL (no ``/api`` suffix), used for the
        ``/oauthserver/`` authorization and token endpoints.
    api_url:
        Phabricator Conduit API URL (typically ``base_url`` + ``/api/``), used
        by the token verifier to call ``user.whoami``.
    server_url:
        Public base URL of *this* MCP server.  The proxy advertises its issuer
        and redirect (``<server_url>/auth/callback``) relative to this.
    client_id / client_secret:
        Credentials of the OAuth application pre-registered in Phabricator.
    scope:
        Space-separated scopes to request and to treat as valid.
    forward_pkce:
        Whether to forward PKCE to the upstream.  Defaults to ``False`` because
        Phabricator's OAuth server is not known to support PKCE.
    """
    base = base_url.rstrip("/")
    scopes = scope.split()

    verifier = PhabricatorTokenVerifier(
        api_url=api_url,
        required_scopes=scopes,
        disable_cert_verify=disable_cert_verify,
        proxy=proxy,
    )

    return OAuthProxy(
        upstream_authorization_endpoint=base + AUTH_PATH,
        upstream_token_endpoint=base + TOKEN_PATH,
        upstream_client_id=client_id,
        upstream_client_secret=client_secret,
        token_verifier=verifier,
        base_url=server_url,
        valid_scopes=scopes,
        forward_pkce=forward_pkce,
    )
