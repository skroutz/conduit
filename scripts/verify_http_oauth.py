#!/usr/bin/env python
"""
End-to-end smoke test for Conduit's streamable HTTP + MCP-spec OAuth mode.

Acts as a real MCP client against a locally running Conduit server:
  1. Connects to http://localhost:8000/mcp
  2. Performs the full OAuth2 flow (DCR -> browser auth against Phabricator ->
     token issuance), opening a browser window for you to approve
  3. Lists the available tools
  4. Calls `pha_user_whoami` and prints the authenticated identity

Run the server first (in another terminal):

  export PHABRICATOR_OAUTH_CLIENT_SECRET=...
  .venv/bin/python run.py \
    --transport http --host 127.0.0.1 --port 8000 \
    --url <URL> \
    --server-url http://localhost:8000 \
    --client-id <client_id> \
    --scope "maniphest phriction"

Then run this script:

  .venv/bin/python scripts/verify_http_oauth.py

Note: unlike strict MCP clients (e.g. Cowork), this client does not require the
authorize URL to be HTTPS, so it works against a plain http://localhost server.
"""

import asyncio
import sys

from fastmcp import Client
from fastmcp.client.auth import OAuth

SERVER_URL = "http://localhost:8000/mcp"


async def main() -> int:
    # OAuth() drives DCR + the browser authorization-code flow and caches the
    # issued token. callback_port is the local port it listens on for the MCP
    # client redirect (distinct from the server's own /auth/callback).
    auth = OAuth(mcp_url=SERVER_URL, callback_port=8765)

    client = Client(SERVER_URL, auth=auth)

    print(f"Connecting to {SERVER_URL} ...", file=sys.stderr)
    print(
        "A browser window will open for Phabricator authorization.",
        file=sys.stderr,
    )

    async with client:
        print("\n=== Connected. Listing tools ===", file=sys.stderr)
        tools = await client.list_tools()
        print(f"{len(tools)} tools available. First few:")
        for tool in tools[:10]:
            print(f"  - {tool.name}")

        print("\n=== Calling pha_user_whoami ===")
        result = await client.call_tool("pha_user_whoami", {})
        print("Result:")
        print(result.data if hasattr(result, "data") else result)

    print("\n✅ End-to-end OAuth + tool call succeeded.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
