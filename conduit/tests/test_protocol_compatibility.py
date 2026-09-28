"""Wire-level checks for the MCP protocol versions used by clients."""

import json
from unittest import IsolatedAsyncioTestCase

import httpx

from conduit.conduit import ConduitApp, PhabricatorConfig


class TestProtocolCompatibility(IsolatedAsyncioTestCase):
    def setUp(self):
        config = PhabricatorConfig(token="x" * 32, url="https://example.invalid/api/")
        conduit = ConduitApp(config, transport="http")
        conduit.register_tools()
        self.asgi_app = conduit.mcp.http_app(path="/mcp", stateless_http=True)

    async def _post(self, client, method, params, version):
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": version,
        }
        if version == "2026-07-28":
            headers["Mcp-Method"] = method

        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        if response.headers["content-type"].startswith("text/event-stream"):
            data = next(
                line.removeprefix("data: ")
                for line in response.text.splitlines()
                if line.startswith("data: ")
            )
            return json.loads(data)
        return response.json()

    async def test_modern_discovery_and_tool_listing(self):
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        async with self.asgi_app.lifespan(self.asgi_app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.asgi_app),
                base_url="http://localhost",
            ) as client:
                discovery = await self._post(
                    client, "server/discover", {"_meta": meta}, "2026-07-28"
                )
                tools = await self._post(
                    client, "tools/list", {"_meta": meta}, "2026-07-28"
                )

        self.assertIn("2026-07-28", discovery["result"]["supportedVersions"])
        self.assertIn("tools", discovery["result"]["capabilities"])
        self.assertIn(
            "pha_user_whoami",
            {tool["name"] for tool in tools["result"]["tools"]},
        )

    async def test_legacy_initialize_and_tool_listing(self):
        async with self.asgi_app.lifespan(self.asgi_app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.asgi_app),
                base_url="http://localhost",
            ) as client:
                initialization = await self._post(
                    client,
                    "initialize",
                    {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                    "2025-11-25",
                )
                tools = await self._post(client, "tools/list", {}, "2025-11-25")

        self.assertEqual(initialization["result"]["protocolVersion"], "2025-11-25")
        self.assertIn(
            "pha_user_whoami",
            {tool["name"] for tool in tools["result"]["tools"]},
        )
