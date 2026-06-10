# Conduit - The MCP Server for Phabricator and Phorge
Conduit is a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/introduction) server that provides seamless integration with Phabricator and Phorge APIs, enabling advanced automation and interaction capabilities for developers and tools.

## Conduit
**Modern HTTP Client**: Built with `httpx` for HTTP/2 support and better performance

**MCP Integration**: Ready-to-use MCP tools for task management

**Type Safety**: Full type hints and runtime validation for better development experience

**Secure**: Token-based authentication with environment variable configuration

**Enhanced Features**:
- Advanced error handling with detailed error codes and suggestions
- Token optimization for efficient API responses
- Smart pagination and intelligent data limiting
- Runtime validation client for type safety
- Configurable client with caching and retry mechanisms

## Usage
### Via `uvx`
You need to install `uv` first. If it is not installed, run the following command:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
After installation, restart your shell or terminal to apply the environment variable changes.

Then run:
```bash
uvx --from git+https://github.com/mcpnow-io/conduit conduit-mcp
```

### From Source
To install from source for development or contribution:

```bash
# Clone the repository
git clone https://github.com/mcpnow-io/conduit.git
cd conduit

# Install in development mode with all dependencies
pip install -e .[dev]
```

This will install the package in editable mode with all development dependencies.

### Docker
We are still working on Docker support. We estimate it will be available soon.

### As an HTTP Server

Conduit can run as an HTTP server for multi-user scenarios, allowing multiple
clients to connect simultaneously, each using their own identity. Select the
transport with `--transport`:

| Transport | Flag | Notes |
|---|---|---|
| stdio | `--transport stdio` (default) | Local, single user. |
| **Streamable HTTP** | `--transport http` | Modern MCP HTTP transport. Recommended. |
| SSE | `--transport sse` | **Deprecated.** Kept for backward compatibility. |

For backward compatibility, passing `--host`/`--port` without `--transport`
still selects the deprecated SSE transport.

#### Streamable HTTP with header authentication

```bash
conduit-mcp --transport http --host 127.0.0.1 --port 8000
```

The server listens at `http://<host>:<port>/mcp` and runs in **stateless** mode
by default (toggle with `--no-stateless`). Each request supplies its own token
via an HTTP header:

```
X-PHABRICATOR-TOKEN: your-32-character-token-here
```

#### Streamable HTTP with OAuth2 (MCP-spec)

In this mode Conduit acts as an OAuth2 Authorization Server to the MCP client
(the way public MCP servers like GitHub's work): the client points at the server
URL, the user is redirected in a browser to authenticate against Phabricator, a
token is issued, and it authenticates every subsequent request. Conduit proxies
the flow upstream to Phabricator's OAuth server.

Register an OAuth2 application in Phabricator with the **Redirect URI** set to
`<server-url>/auth/callback` (e.g. `http://localhost:8000/auth/callback`), then:

```bash
conduit-mcp --transport http --host 127.0.0.1 --port 8000 \
  --url https://your-phabricator-instance.com/api/ \
  --server-url http://localhost:8000 \
  --client-id <client-id> \
  --client-secret <client-secret> \
  --scope "whoami maniphest"
```

`--server-url` (or `PHABRICATOR_MCP_SERVER_URL`) is the public base URL of *this*
MCP server, used to advertise the issuer and redirect URI. The client ID/secret
may also be supplied via `PHABRICATOR_OAUTH_CLIENT_ID` / `PHABRICATOR_OAUTH_CLIENT_SECRET`.

## Configuration

### Authentication — OAuth2 (Recommended for shared deployments)

OAuth2 lets users authenticate through their browser without manually managing API tokens. This is the recommended approach when deploying Conduit for a team, as it removes the need for users to provision or rotate Conduit API keys.

#### Prerequisites

An administrator must register an OAuth2 application in Phabricator once:

1. Go to **Settings → OAuth Server Applications → Create Application**
2. Set the **Redirect URI** to `http://localhost:<port>` where `<port>` is a fixed port your users will have free on their machine (e.g. `http://localhost:8889`)
3. Note the **Client ID** and **Client Secret** from the application settings

#### Usage

```bash
uvx --from git+https://github.com/your-org/conduit conduit-mcp \
  --url https://your-phabricator-instance.com/api/ \
  --client-id <client-id> \
  --client-secret <client-secret> \
  --oauth-redirect-port 8889 \
  --scope "whoami maniphest"
```

`--scope` is optional and defaults to `whoami maniphest`. Add further space-separated scopes if your usage requires access to other Phabricator APIs (e.g. `differential`, `diffusion`).

On first run, a browser window opens for the user to log in. The resulting token is cached in `~/.conduit/oauth_tokens.json` (readable only by the current user) and reused on subsequent runs — no further browser prompts until the token expires.

To log out and force re-authentication:

```bash
conduit-mcp --url https://your-phabricator-instance.com/api/ \
  --client-id <client-id> \
  --logout
```

#### OAuth2 environment variables

| Variable | Description |
|---|---|
| `PHABRICATOR_URL` | Phabricator instance URL |
| `PHABRICATOR_OAUTH_CLIENT_ID` | OAuth2 Application Client ID |
| `PHABRICATOR_OAUTH_CLIENT_SECRET` | OAuth2 Application Client Secret |

Prefer environment variables over CLI arguments for the client secret to avoid it appearing in process listings:

```bash
export PHABRICATOR_URL="https://your-phabricator-instance.com/api/"
export PHABRICATOR_OAUTH_CLIENT_ID="your-client-id"
export PHABRICATOR_OAUTH_CLIENT_SECRET="your-client-secret"
conduit-mcp --oauth-redirect-port 8889
```

---

### Authentication — Conduit API Token

Before running the server, you need to set up the following environment variables:

### Environment Variables
```bash
export PHABRICATOR_TOKEN=your-api-token-here
export PHABRICATOR_URL="https://your-phabricator-instance.com/api/"

export PHABRICATOR_PROXY="socks5://127.0.0.1:1080"  # Optional, if your network is behind a firewall
export PHABRICATOR_DISABLE_CERT_VERIFY=1  # Optional, if your network is under HTTPS filter (WARNING: Disabling certificate verification can expose you to security risks. Only set this if you trust your network environment.)
```
Do note that in HTTPS/SSE mode, `PHABRICATOR_TOKEN` is NOT needed.

### Getting Your API Token
1. Log into your Phabricator instance
2. Go to Settings > API Tokens
3. Generate a new token
4. Copy the 32-character token and use it as `PHABRICATOR_TOKEN`

## Contributing
There are many ways in which you can participate in this project, for example:
* Submit [bugs and feature requests](https://github.com/mcpnow-io/conduit/issues), and help us verify as they are checked in
* Review [source code changes](https://github.com/mcpnow-io/conduit/pulls)
* Review the [wiki](https://github.com/mcpnow-io/conduit/wiki) and make pull requests for anything from typos to additional and new content

If you are interested in fixing issues and contributing directly to the code base, please see the document [How to Contribute](https://github.com/mcpnow-io/conduit/wiki/How-to-Contribute)：
* [First-Time Setup](https://github.com/mcpnow-io/conduit/wiki/How-to-Contribute#first-time-setup)
* [Submitting a Pull Request](https://github.com/mcpnow-io/conduit/wiki/How-to-Contribute#submitting-a-pull-request)
* [Running Unittests](https://github.com/mcpnow-io/conduit/wiki/How-to-Contribute#running-unittests)

## License
Copyright (c) 2025 mpcnow.io. all rights reserved.

Licensed under the [MIT](LICENSE) license.
