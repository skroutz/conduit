import argparse
import os
import sys

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
    ):
        self.config = config
        self.use_sse = use_sse
        self._oauth_client = oauth_client
        self.mcp = FastMCP("Conduit")
        self._client = None

    def get_client(self):
        """Get or create a Phabricator client instance."""
        # In SSE mode, always create a fresh client for each request
        # to prevent user identity confusion in multi-user environments
        if self.use_sse:
            headers = get_http_headers()
            http_token = headers.get("x-phabricator-token")

            if not http_token:
                raise ValueError("Must provide X-PHABRICATOR-TOKEN in SSE mode.")

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
        print(f"Token configured: {'Yes (OAuth2)' if config.token else 'OAuth2 flow'}", file=sys.stderr)
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
    """Check if SSE transport should be used based on command line arguments."""
    sse_args = ["--host", "-H", "--port", "-p"]
    return any(arg in sys.argv for arg in sse_args)


def main():
    """Main entry point for the Conduit MCP Server."""
    parser = argparse.ArgumentParser(
        description="Conduit MCP Server for Phabricator and Phorge"
    )
    parser.add_argument(
        "--host",
        "-H",
        default="127.0.0.1",
        help="Host to bind to for SSE transport (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=8000,
        help="Port to bind to for SSE transport (default: 8000)",
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
        default="whoami maniphest",
        help=(
            "Space-separated OAuth2 scopes to request (default: 'whoami maniphest').  "
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

    use_sse = should_use_sse_transport()

    oauth_client = None
    if args.client_id or os.getenv("PHABRICATOR_OAUTH_CLIENT_ID"):
        if use_sse:
            parser.error(
                "--client-id / OAuth2 mode is not supported with SSE transport. "
                "Use PHABRICATOR_TOKEN via HTTP headers in SSE mode."
            )

        from conduit.auth import OAuth2Client

        # Resolve the Phabricator base URL (strip the /api/ path suffix that
        # PHABRICATOR_URL typically carries so the OAuth endpoints work).
        raw_url = args.url or os.getenv("PHABRICATOR_URL", "")
        if not raw_url:
            parser.error(
                "Phabricator URL is required.  Set --url or PHABRICATOR_URL."
            )
        # Normalise: remove trailing /api/ or /api so OAuth paths resolve correctly.
        base_url = raw_url.rstrip("/")
        if base_url.endswith("/api"):
            base_url = base_url[: -len("/api")]

        client_id = args.client_id or os.getenv("PHABRICATOR_OAUTH_CLIENT_ID")
        client_secret = args.client_secret or os.getenv("PHABRICATOR_OAUTH_CLIENT_SECRET")
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

    if use_sse:
        config = PhabricatorConfig(require_token=False, url=args.url)
        print_server_info(config)
        print(
            "Note: In HTTP/SSE mode, PHABRICATOR_TOKEN should be provided via HTTP headers:",
            file=sys.stderr,
        )
        print("  - X-PHABRICATOR-TOKEN: <token>", file=sys.stderr)
    else:
        require_token = oauth_client is None
        config = PhabricatorConfig(
            require_token=require_token,
            url=args.url,
            client_id=args.client_id,
        )
        print_server_info(config)

    # Create and run the application
    app = ConduitApp(config, use_sse, oauth_client=oauth_client)
    app.register_tools()

    if use_sse:
        app.run_sse_mode(args.host, args.port)
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
