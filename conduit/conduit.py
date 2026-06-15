import argparse
import os
import sys
from typing import Optional

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

from conduit.client import PhabricatorClient
from conduit.main_tools import register_tools


class PhabricatorConfig(object):
    def __init__(self, token=None, require_token=True, url=None, client_id=None):
        self.token = token or os.getenv("PHABRICATOR_TOKEN")
        self.url = url or os.getenv("PHABRICATOR_URL")
        self.proxy = os.getenv("PHABRICATOR_PROXY")
        self.client_id = client_id or os.getenv("PHABRICATOR_OAUTH_CLIENT_ID")
        self.disable_cert_verify = os.getenv(
            "PHABRICATOR_DISABLE_CERT_VERIFY", ""
        ).lower() in ("1", "true", "yes")

        if require_token and not self.token and not self.client_id:
            raise ValueError(
                "Either PHABRICATOR_TOKEN or --client-id / PHABRICATOR_OAUTH_CLIENT_ID "
                "is required"
            )

        if not self.url:
            raise ValueError("PHABRICATOR_URL environment variable is required")

        if self.token and not self.client_id and len(self.token) != 32:
            raise ValueError("PHABRICATOR_TOKEN must be exactly 32 characters long")

        if not self.url.startswith(("http://", "https://")):
            raise ValueError("PHABRICATOR_URL must start with http:// or https://")

        if self.url and not self.url.endswith("/"):
            self.url += "/"

    @property
    def api_headers(self):
        return {"Content-Type": "application/x-www-form-urlencoded"}

    @property
    def base_params(self):
        return {"api.token": self.token}


class ConduitApp:
    """Main application class for Conduit MCP Server."""

    def __init__(
        self,
        config: PhabricatorConfig,
        use_sse: bool = False,
        oauth_client=None,
        transport: Optional[str] = None,
        auth_provider=None,
    ):
        self.config = config
        # ``transport`` is the source of truth; ``use_sse`` is kept for
        # backward compatibility with existing callers/tests.
        if transport is None:
            transport = "sse" if use_sse else "stdio"
        self.transport = transport
        self.use_sse = transport == "sse"
        self._oauth_client = oauth_client
        self._auth_provider = auth_provider
        self.mcp = FastMCP("Conduit", auth=auth_provider)
        self._client = None

    def get_client(self):
        """Get or create a Phabricator client instance."""
        # Streamable HTTP with MCP-spec OAuth: the Phabricator access token is
        # carried in the authenticated request context by the OAuth proxy.
        if self.transport == "http" and self._auth_provider is not None:
            return self._client_from_oauth_context()

        # SSE, or HTTP without OAuth, are multi-user: always build a fresh
        # client from the per-request X-PHABRICATOR-TOKEN header so that user
        # identities never bleed across requests.
        if self.transport in ("sse", "http"):
            return self._client_from_header()

        # For stdio mode, use cached client (backward compatibility)
        if self._client is not None:
            return self._client

        token = self._resolve_token()

        self._client = PhabricatorClient(
            self.config.url,
            token,
            proxy=self.config.proxy,
            disable_cert_verify=self.config.disable_cert_verify,
            oauth_token=self._oauth_client is not None,
        )
        return self._client

    def _client_from_header(self):
        """Build a per-request client from the X-PHABRICATOR-TOKEN header."""
        headers = get_http_headers()
        http_token = headers.get("x-phabricator-token")

        if not http_token:
            raise ValueError("Must provide X-PHABRICATOR-TOKEN in HTTP/SSE mode.")

        if len(http_token) != 32:
            raise ValueError(
                "PHABRICATOR_TOKEN from HTTP header must be exactly 32 characters long"
            )

        return PhabricatorClient(
            self.config.url,
            http_token,
            proxy=self.config.proxy,
            disable_cert_verify=self.config.disable_cert_verify,
        )

    def _client_from_oauth_context(self):
        """Build a per-request client from the OAuth-authenticated token."""
        from fastmcp.server.dependencies import get_access_token

        access = get_access_token()
        if access is None or not access.token:
            raise ValueError(
                "No authenticated Phabricator token in request context. "
                "The MCP client must complete the OAuth flow first."
            )

        return PhabricatorClient(
            self.config.url,
            access.token,
            proxy=self.config.proxy,
            disable_cert_verify=self.config.disable_cert_verify,
            oauth_token=True,
        )

    def _resolve_token(self) -> str:
        """Return the API token to use, running the OAuth flow if needed."""
        if self._oauth_client is not None:
            return self._oauth_client.get_token()

        if not self.config.token:
            raise ValueError("PHABRICATOR_TOKEN is required for stdio mode")

        return self.config.token

    def register_tools(self):
        """Register all MCP tools."""
        register_tools(self.mcp, self.get_client)

    def run_sse_mode(self, host: str, port: int):
        """Run the application in SSE mode."""
        print(f"Starting in HTTP/SSE mode on {host}:{port}", file=sys.stderr)
        self.mcp.run(
            transport="sse",
            host=host,
            port=port,
            path="/sse",
        )

    def run_http_mode(self, host: str, port: int, stateless: bool = True):
        """Run the application in streamable HTTP mode."""
        auth_mode = "OAuth2" if self._auth_provider is not None else "header token"
        print(
            f"Starting in streamable HTTP mode on {host}:{port} "
            f"(stateless={stateless}, auth={auth_mode})",
            file=sys.stderr,
        )
        self.mcp.run(
            transport="http",
            host=host,
            port=port,
            path="/mcp",
            stateless_http=stateless,
        )

    def run_stdio_mode(self):
        """Run the application in stdio mode."""
        # Eagerly resolve the token (triggering the OAuth browser flow if needed)
        # before handing stdin/stdout to the MCP protocol.  Once mcp.run() starts,
        # stdout is the wire format and any interactive prompt would corrupt it.
        self.get_client()
        print("Starting in stdio mode", file=sys.stderr)
        self.mcp.run(transport="stdio")


# Global app instance
_app = None


def print_server_info(config):
    """Print server configuration information."""
    print("Starting Conduit MCP Server...", file=sys.stderr)
    print(f"Phabricator URL: {config.url}", file=sys.stderr)
    if config.client_id:
        print(f"OAuth2 client ID: {config.client_id}", file=sys.stderr)
        print(
            f"Token configured: {'Yes (OAuth2)' if config.token else 'OAuth2 flow'}",
            file=sys.stderr,
        )
    else:
        print(f"Token configured: {'Yes' if config.token else 'No'}", file=sys.stderr)
    print(f"Proxy configured: {'Yes' if config.proxy else 'No'}", file=sys.stderr)
    if config.proxy:
        print(f"Proxy URL: {config.proxy}", file=sys.stderr)
    print(
        f"SSL certificate verification: {'Disabled' if config.disable_cert_verify else 'Enabled'}",
        file=sys.stderr,
    )


def should_use_sse_transport() -> bool:
    """Check if an HTTP-family transport was requested via host/port flags."""
    sse_args = ["--host", "-H", "--port", "-p"]
    return any(arg in sys.argv for arg in sse_args)


def resolve_transport(explicit: Optional[str]) -> str:
    """
    Resolve the transport to use.

    An explicit --transport always wins.  Otherwise, the presence of
    --host/--port implies the legacy SSE transport (backward compatible);
    absent both, default to stdio.
    """
    if explicit:
        return explicit
    if should_use_sse_transport():
        return "sse"
    return "stdio"


def _phabricator_base_url(raw_url: str) -> str:
    """Strip a trailing /api or /api/ so /oauthserver/ paths resolve."""
    base_url = raw_url.rstrip("/")
    if base_url.endswith("/api"):
        base_url = base_url[: -len("/api")]
    return base_url


def main():
    """Main entry point for the Conduit MCP Server."""
    parser = argparse.ArgumentParser(
        description="Conduit MCP Server for Phabricator and Phorge"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "http"],
        default=None,
        help=(
            "Transport to use.  'stdio' (default) for local single-user; "
            "'http' for modern streamable HTTP (multi-user); 'sse' for the "
            "deprecated HTTP/SSE transport.  If omitted but --host/--port are "
            "given, defaults to 'sse' for backward compatibility."
        ),
    )
    parser.add_argument(
        "--host",
        "-H",
        default="127.0.0.1",
        help="Host to bind to for HTTP/SSE transport (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=8000,
        help="Port to bind to for HTTP/SSE transport (default: 8000)",
    )
    parser.add_argument(
        "--stateless",
        action="store_true",
        default=True,
        dest="stateless",
        help="Enable stateless HTTP mode (default: enabled). Only used with --transport http.",
    )
    parser.add_argument(
        "--no-stateless",
        action="store_false",
        dest="stateless",
        help="Disable stateless HTTP mode (keep per-session state).",
    )
    parser.add_argument(
        "--server-url",
        default=None,
        dest="server_url",
        help=(
            "Public base URL of THIS MCP server (e.g. http://localhost:8000).  "
            "Required for OAuth2 in streamable HTTP mode so the server can "
            "advertise its issuer and the redirect URI (<server-url>/auth/callback). "
            "Can also be set via PHABRICATOR_MCP_SERVER_URL."
        ),
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Phabricator instance URL (overrides PHABRICATOR_URL env var)",
    )
    parser.add_argument(
        "--client-id",
        default=None,
        dest="client_id",
        help=(
            "OAuth2 Application Client ID.  When provided, the server will "
            "perform an interactive browser-based OAuth2 flow instead of "
            "requiring a PHABRICATOR_TOKEN.  "
            "Can also be set via PHABRICATOR_OAUTH_CLIENT_ID."
        ),
    )
    parser.add_argument(
        "--client-secret",
        default=None,
        dest="client_secret",
        help=(
            "OAuth2 Application Client Secret.  Required by Phabricator's token "
            "endpoint.  Can also be set via PHABRICATOR_OAUTH_CLIENT_SECRET.  "
            "Only used when --client-id is provided."
        ),
    )
    parser.add_argument(
        "--scope",
        default=os.getenv("PHABRICATOR_OAUTH_SCOPE", "maniphest file"),
        help=(
            "Space-separated OAuth2 scopes to request (default: 'maniphest file').  "
            "Can also be set via PHABRICATOR_OAUTH_SCOPE.  "
            "Only used when --client-id is provided."
        ),
    )
    parser.add_argument(
        "--oauth-redirect-port",
        type=int,
        default=None,
        dest="oauth_redirect_port",
        help=(
            "Port for the local OAuth2 redirect listener (default: random).  "
            "Must match the port in the redirect URI registered in Phabricator "
            "(e.g. if registered as 'http://localhost:8888', pass 8888).  "
            "If the registered URI is bare 'http://localhost', pass 80 "
            "(requires root/admin privileges).  "
            "Only used when --client-id is provided."
        ),
    )
    parser.add_argument(
        "--logout",
        action="store_true",
        default=False,
        help=(
            "Remove any stored OAuth2 token for this Phabricator instance and "
            "exit.  Only used when --client-id is provided."
        ),
    )

    args = parser.parse_args()

    transport = resolve_transport(args.transport)

    oauth_requested = bool(args.client_id or os.getenv("PHABRICATOR_OAUTH_CLIENT_ID"))

    oauth_client = None  # stdio interactive OAuth client (conduit.auth.oauth)
    auth_provider = None  # http MCP-spec OAuth proxy (conduit.auth.provider)

    if oauth_requested:
        if transport == "sse":
            parser.error(
                "--client-id / OAuth2 mode is not supported with the deprecated "
                "SSE transport. Use PHABRICATOR_TOKEN via HTTP headers in SSE "
                "mode, or switch to --transport http."
            )

        raw_url = args.url or os.getenv("PHABRICATOR_URL", "")
        if not raw_url:
            parser.error("Phabricator URL is required.  Set --url or PHABRICATOR_URL.")
        base_url = _phabricator_base_url(raw_url)

        client_id = args.client_id or os.getenv("PHABRICATOR_OAUTH_CLIENT_ID")
        client_secret = args.client_secret or os.getenv(
            "PHABRICATOR_OAUTH_CLIENT_SECRET"
        )

        if transport == "http":
            # Streamable HTTP: conduit acts as the MCP-spec OAuth server,
            # proxying upstream to Phabricator.  No local browser flow here —
            # the MCP client drives the redirect.
            server_url = args.server_url or os.getenv("PHABRICATOR_MCP_SERVER_URL")
            if not server_url:
                parser.error(
                    "--server-url (or PHABRICATOR_MCP_SERVER_URL) is required for "
                    "OAuth2 in streamable HTTP mode."
                )

            config = PhabricatorConfig(
                require_token=False, url=args.url, client_id=client_id
            )
            from conduit.auth import build_phabricator_auth_provider

            auth_provider = build_phabricator_auth_provider(
                base_url=base_url,
                api_url=config.url,
                server_url=server_url,
                client_id=client_id,
                client_secret=client_secret,
                scope=args.scope,
                disable_cert_verify=config.disable_cert_verify,
                proxy=config.proxy,
            )
            print_server_info(config)
            print(
                f"OAuth2 redirect URI to register in Phabricator: "
                f"{server_url.rstrip('/')}/auth/callback",
                file=sys.stderr,
            )
        else:
            # stdio: interactive local-loopback browser flow (unchanged).
            from conduit.auth import OAuth2Client

            oauth_client = OAuth2Client(
                base_url=base_url,
                client_id=client_id,
                client_secret=client_secret,
                scope=args.scope,
                redirect_port=args.oauth_redirect_port,
            )

            if args.logout:
                removed = oauth_client.logout()
                if removed:
                    print("OAuth2 token removed successfully.", file=sys.stderr)
                else:
                    print("No stored OAuth2 token found.", file=sys.stderr)
                return

            config = PhabricatorConfig(
                require_token=False, url=args.url, client_id=args.client_id
            )
            print_server_info(config)

    elif transport in ("sse", "http"):
        config = PhabricatorConfig(require_token=False, url=args.url)
        print_server_info(config)
        print(
            "Note: In HTTP/SSE mode, PHABRICATOR_TOKEN should be provided via HTTP headers:",
            file=sys.stderr,
        )
        print("  - X-PHABRICATOR-TOKEN: <token>", file=sys.stderr)
    else:
        # stdio without OAuth: a token is required.
        config = PhabricatorConfig(
            require_token=True, url=args.url, client_id=args.client_id
        )
        print_server_info(config)

    # Create and run the application
    app = ConduitApp(
        config,
        oauth_client=oauth_client,
        transport=transport,
        auth_provider=auth_provider,
    )
    app.register_tools()

    if transport == "sse":
        app.run_sse_mode(args.host, args.port)
    elif transport == "http":
        app.run_http_mode(args.host, args.port, stateless=args.stateless)
    else:
        app.run_stdio_mode()


# Backward compatibility functions
def get_config():
    """Get configuration for backward compatibility."""
    return PhabricatorConfig(require_token=False)


def get_client():
    """Get client for backward compatibility."""
    config = get_config()
    return PhabricatorClient(
        config.url,
        config.token or "dummy_token",
        proxy=config.proxy,
        disable_cert_verify=config.disable_cert_verify,
    )


if __name__ == "__main__":
    main()
