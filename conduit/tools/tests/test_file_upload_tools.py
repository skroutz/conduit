"""Tests for the prepare-upload tool and the HTTP endpoint that stores the file."""

import asyncio
import base64
from unittest.mock import Mock, patch

import fastmcp
import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from conduit.auth import build_phabricator_auth_provider
from conduit.client import PhabricatorAPIError
from conduit.client.file import FileClient
from conduit.conduit import ConduitApp, PhabricatorConfig
from conduit.tools import file_upload_tools
from conduit.tools.file_upload_tools import (
    DEFAULT_VIEW_POLICY,
    MAX_ACTIVE_UPLOADS_PER_USER,
    MAX_PENDING_UPLOADS_PER_USER,
    UPLOAD_PATH,
    UPLOAD_TOKEN_HEADER,
    register_file_upload_tools,
)

SERVER_URL = "https://phabricator.example.com/ai"
MAX_BYTES = 1024
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
REJECTED = {
    "success": False,
    "error": "Unknown, expired or already used upload token",
}


class _StubMCP:
    """Stand-in for FastMCP that captures the tool and the HTTP route."""

    def __init__(self):
        self.tools = {}
        self.routes = []

    def tool(self, *args, **kwargs):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator

    def custom_route(self, path, methods):
        def decorator(func):
            self.routes.append(Route(path, endpoint=func, methods=methods))
            return func

        return decorator


def _make_client(user_phid="PHID-USER-alice", file_id=42):
    client = Mock()
    client.user.whoami.return_value = {"phid": user_phid}
    client.file.upload_file.return_value = f"PHID-FILE-{file_id}"
    client.file.get_file_info.return_value = {
        "id": file_id,
        "phid": f"PHID-FILE-{file_id}",
        "fields": {
            "name": "shot.png",
            "uri": f"https://phabricator.example.com/F{file_id}",
        },
    }
    return client


class _Harness:
    """The registered tool plus an ASGI app serving the upload endpoint."""

    def __init__(self, **options):
        self.client = _make_client()
        mcp = _StubMCP()
        register_file_upload_tools(
            mcp,
            lambda: self.client,
            SERVER_URL,
            max_upload_bytes=MAX_BYTES,
            **options,
        )
        self.prepare = mcp.tools["pha_file_prepare_upload"]
        self.app = Starlette(routes=mcp.routes)

    def token(self, name="shot.png"):
        return self.prepare(name)["headers"][UPLOAD_TOKEN_HEADER]

    def upload(self, token=None, content=PNG, method="POST", headers=None):
        return _request(self.app, token, content, method, headers)


def _request(app, token=None, content=PNG, method="POST", headers=None):
    headers = dict(headers or {})
    if token is not None:
        headers[UPLOAD_TOKEN_HEADER] = token

    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://conduit"
        ) as http:
            return await http.request(
                method, UPLOAD_PATH, content=content, headers=headers
            )

    return asyncio.run(send())


@pytest.fixture
def harness():
    return _Harness()


class TestPrepareUpload:
    def test_returns_the_upload_url_and_a_ready_curl_command(self, harness):
        result = harness.prepare("shot.png")

        token = result["headers"][UPLOAD_TOKEN_HEADER]
        assert result["success"] is True
        assert result["upload_url"] == SERVER_URL + UPLOAD_PATH
        assert result["name"] == "shot.png"
        assert result["expires_in_seconds"] == 600
        assert result["max_bytes"] == MAX_BYTES
        assert result["curl_command"] == (
            f"curl -sS -H '{UPLOAD_TOKEN_HEADER}: {token}' "
            f"--data-binary @PATH_TO_FILE {SERVER_URL}{UPLOAD_PATH}"
        )

    def test_the_token_is_long_random_and_kept_out_of_the_url(self, harness):
        first = harness.prepare("shot.png")
        second = harness.prepare("shot.png")

        token = first["headers"][UPLOAD_TOKEN_HEADER]
        assert len(token) >= 43
        assert token != second["headers"][UPLOAD_TOKEN_HEADER]
        assert token not in first["upload_url"]

    @pytest.mark.parametrize(
        "given, stored",
        [
            ("../../etc/passwd", "passwd"),
            ("C:\\Users\\alice\\shot.png", "shot.png"),
            ("/tmp/shots/shot.png", "shot.png"),
            ("sh\x00ot\n.png", "shot.png"),
            ("  my shot.png  ", "my shot.png"),
            ("a" * 300 + ".png", "a" * 255),
        ],
    )
    def test_reduces_the_name_to_a_bare_file_name(self, harness, given, stored):
        assert harness.prepare(given)["name"] == stored

    @pytest.mark.parametrize("name", ["", "   ", "/", "..", "shots/", "\x00"])
    def test_rejects_a_name_without_a_file_name(self, harness, name):
        result = harness.prepare(name)

        assert result["success"] is False
        assert "headers" not in result

    def test_refuses_a_credential_phabricator_does_not_accept(self, harness):
        harness.client.user.whoami.side_effect = PhabricatorAPIError("Invalid token")

        result = harness.prepare("shot.png")

        assert result["success"] is False
        assert "headers" not in result

    def test_caps_pending_uploads_per_user_without_blocking_others(self, harness):
        for _ in range(MAX_PENDING_UPLOADS_PER_USER):
            assert harness.prepare("shot.png")["success"] is True

        assert harness.prepare("shot.png")["success"] is False

        harness.client = _make_client(user_phid="PHID-USER-bob")
        assert harness.prepare("shot.png")["success"] is True

    def test_expired_uploads_do_not_count_against_the_cap(self, harness, monkeypatch):
        monkeypatch.setattr(file_upload_tools, "UPLOAD_TTL_SECONDS", -1)
        for _ in range(MAX_PENDING_UPLOADS_PER_USER):
            harness.prepare("shot.png")

        monkeypatch.setattr(file_upload_tools, "UPLOAD_TTL_SECONDS", 600)
        assert harness.prepare("shot.png")["success"] is True


class TestUploadEndpoint:
    def test_stores_the_bytes_under_the_prepared_name(self, harness):
        response = harness.upload(harness.token("shot.png"))

        assert response.status_code == 200
        assert response.json() == {
            "success": True,
            "file": {
                "id": "F42",
                "phid": "PHID-FILE-42",
                "name": "shot.png",
                "size": len(PNG),
                "uri": "https://phabricator.example.com/F42",
                "view_policy": "no-one",
            },
            "remarkup": "{F42}",
        }
        sent, kwargs = harness.client.file.upload_file.call_args
        assert base64.b64decode(sent[0]) == PNG
        assert kwargs == {"name": "shot.png", "view_policy": "no-one"}

    def test_accepts_put_for_curl_upload_file(self, harness):
        response = harness.upload(harness.token(), method="PUT")

        assert response.status_code == 200
        assert response.json()["remarkup"] == "{F42}"

    def test_stores_the_file_as_the_user_who_prepared_it(self, harness):
        alice = harness.client
        alice_token = harness.token()
        bob = harness.client = _make_client(user_phid="PHID-USER-bob", file_id=7)
        bob_token = harness.token()

        assert harness.upload(alice_token).json()["file"]["id"] == "F42"
        bob.file.upload_file.assert_not_called()

        assert harness.upload(bob_token).json()["file"]["id"] == "F7"
        alice.file.upload_file.assert_called_once()

    def test_a_token_works_only_once(self, harness):
        token = harness.token()
        assert harness.upload(token).status_code == 200

        response = harness.upload(token)

        assert response.status_code == 404
        assert response.json() == REJECTED
        harness.client.file.upload_file.assert_called_once()

    @pytest.mark.parametrize("token", [None, "", "not-a-token"])
    def test_rejects_an_unknown_or_missing_token(self, harness, token):
        harness.token()

        response = harness.upload(token)

        assert response.status_code == 404
        assert response.json() == REJECTED
        harness.client.file.upload_file.assert_not_called()

    def test_rejects_an_expired_token_like_an_unknown_one(self, harness, monkeypatch):
        monkeypatch.setattr(file_upload_tools, "UPLOAD_TTL_SECONDS", -1)

        response = harness.upload(harness.token())

        assert response.status_code == 404
        assert response.json() == REJECTED
        harness.client.file.upload_file.assert_not_called()

    def test_rejects_a_body_over_the_limit(self, harness):
        token = harness.token()

        response = harness.upload(token, content=b"x" * (MAX_BYTES + 1))

        assert response.status_code == 413
        assert response.json()["success"] is False
        harness.client.file.upload_file.assert_not_called()
        assert harness.upload(token).status_code == 404

    def test_accepts_a_body_exactly_at_the_limit(self, harness):
        response = harness.upload(harness.token(), content=b"x" * MAX_BYTES)

        assert response.status_code == 200

    def test_stops_reading_a_streamed_body_once_over_the_limit(self, harness):
        sent = []

        async def endless():
            for _ in range(1000):
                sent.append(1)
                yield b"x" * 512

        response = harness.upload(harness.token(), content=endless())

        assert response.status_code == 413
        assert len(sent) < 10
        harness.client.file.upload_file.assert_not_called()

    def test_refuses_a_declared_oversize_body_without_reading_it(self, harness):
        sent = []

        async def body():
            sent.append(1)
            yield b"x" * (MAX_BYTES + 1)

        response = harness.upload(
            harness.token(),
            content=body(),
            headers={"Content-Length": str(MAX_BYTES + 1)},
        )

        assert response.status_code == 413
        assert sent == []

    def test_reads_nothing_without_a_valid_token(self, harness):
        sent = []

        async def body():
            sent.append(1)
            yield b"x"

        response = harness.upload("not-a-token", content=body())

        assert response.status_code == 404
        assert sent == []

    def test_rejects_an_empty_body(self, harness):
        response = harness.upload(harness.token(), content=b"")

        assert response.status_code == 400
        harness.client.file.upload_file.assert_not_called()

    def test_reports_a_phabricator_failure_and_still_burns_the_token(self, harness):
        harness.client.file.upload_file.side_effect = PhabricatorAPIError(
            "API Error: file too large"
        )
        token = harness.token()

        response = harness.upload(token)

        assert response.status_code == 502
        assert response.json() == {
            "success": False,
            "error": "API Error: file too large",
        }
        assert harness.upload(token).status_code == 404


class TestFileVisibility:
    def test_files_are_created_readable_by_their_author_only(self, harness):
        response = harness.upload(harness.token())

        assert DEFAULT_VIEW_POLICY == "no-one"
        _, kwargs = harness.client.file.upload_file.call_args
        assert kwargs["view_policy"] == "no-one"
        assert response.json()["file"]["view_policy"] == "no-one"

    def test_a_deployment_can_widen_who_sees_uploaded_files(self):
        harness = _Harness(view_policy="users")

        response = harness.upload(harness.token())

        _, kwargs = harness.client.file.upload_file.call_args
        assert kwargs["view_policy"] == "users"
        assert response.json()["file"]["view_policy"] == "users"

    def test_the_file_client_sends_the_policy_to_file_upload(self):
        client = object.__new__(FileClient)
        client._make_request = Mock(return_value="PHID-FILE-1")

        client.upload_file("QUJD", name="a.png", view_policy="no-one")

        client._make_request.assert_called_once_with(
            "file.upload",
            {"data_base64": "QUJD", "name": "a.png", "viewPolicy": "no-one"},
        )

    def test_the_file_client_leaves_the_server_default_without_a_policy(self):
        client = object.__new__(FileClient)
        client._make_request = Mock(return_value="PHID-FILE-1")

        client.upload_file("QUJD", name="a.png")

        client._make_request.assert_called_once_with(
            "file.upload", {"data_base64": "QUJD", "name": "a.png"}
        )


class TestActiveUploadLimits:
    def _run_with_stalled_uploads(self, harness, users, extra_user):
        """Start one stalled upload per user, then try one more, then finish."""

        async def scenario():
            gate = asyncio.Event()

            async def stalled():
                yield b"x"
                await gate.wait()

            tokens = []
            for user in users + [extra_user]:
                harness.client = _make_client(user_phid=user)
                tokens.append(harness.token())

            transport = httpx.ASGITransport(app=harness.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://conduit"
            ) as http:

                def post(token, content):
                    return http.post(
                        UPLOAD_PATH,
                        content=content,
                        headers={UPLOAD_TOKEN_HEADER: token},
                    )

                running = [
                    asyncio.create_task(post(token, stalled())) for token in tokens[:-1]
                ]
                await asyncio.sleep(0.05)
                over = await post(tokens[-1], PNG)
                gate.set()
                finished = await asyncio.gather(*running)
                retried = await post(tokens[-1], PNG)
            return over, finished, retried

        return asyncio.run(scenario())

    def test_a_user_cannot_run_more_uploads_than_the_cap_at_once(self, harness):
        users = ["PHID-USER-alice"] * MAX_ACTIVE_UPLOADS_PER_USER

        over, finished, _ = self._run_with_stalled_uploads(
            harness, users, "PHID-USER-alice"
        )

        assert over.status_code == 429
        assert over.json()["success"] is False
        assert [r.status_code for r in finished] == [200] * len(users)

    def test_a_refused_upload_keeps_its_token_for_a_retry(self, harness):
        users = ["PHID-USER-alice"] * MAX_ACTIVE_UPLOADS_PER_USER

        over, _, retried = self._run_with_stalled_uploads(
            harness, users, "PHID-USER-alice"
        )

        assert over.status_code == 429
        assert retried.status_code == 200

    def test_other_users_are_not_blocked_by_one_users_uploads(self, harness):
        users = ["PHID-USER-alice"] * MAX_ACTIVE_UPLOADS_PER_USER

        over, _, _ = self._run_with_stalled_uploads(harness, users, "PHID-USER-bob")

        assert over.status_code == 200

    def test_the_server_wide_cap_applies_across_users(self, harness, monkeypatch):
        monkeypatch.setattr(file_upload_tools, "MAX_ACTIVE_UPLOADS", 2)
        users = ["PHID-USER-alice", "PHID-USER-bob"]

        over, _, retried = self._run_with_stalled_uploads(
            harness, users, "PHID-USER-carol"
        )

        assert over.status_code == 429
        assert retried.status_code == 200

    @pytest.mark.parametrize(
        "content, status",
        [(b"x" * (MAX_BYTES + 1), 413), (b"", 400)],
    )
    def test_a_failed_upload_gives_its_slot_back(
        self, harness, monkeypatch, content, status
    ):
        monkeypatch.setattr(file_upload_tools, "MAX_ACTIVE_UPLOADS_PER_USER", 1)

        assert harness.upload(harness.token(), content=content).status_code == status

        assert harness.upload(harness.token()).status_code == 200

    def test_an_upload_phabricator_refuses_gives_its_slot_back(
        self, harness, monkeypatch
    ):
        monkeypatch.setattr(file_upload_tools, "MAX_ACTIVE_UPLOADS_PER_USER", 1)
        harness.client.file.upload_file.side_effect = PhabricatorAPIError("nope")

        assert harness.upload(harness.token()).status_code == 502

        harness.client.file.upload_file.side_effect = None
        assert harness.upload(harness.token()).status_code == 200

    def test_a_stalled_body_times_out_and_gives_its_slot_back(
        self, harness, monkeypatch
    ):
        monkeypatch.setattr(file_upload_tools, "MAX_ACTIVE_UPLOADS_PER_USER", 1)
        monkeypatch.setattr(file_upload_tools, "UPLOAD_BODY_TIMEOUT_SECONDS", 0.05)

        async def never_ends():
            yield b"x"
            await asyncio.sleep(30)

        response = harness.upload(harness.token(), content=never_ends())

        assert response.status_code == 408
        harness.client.file.upload_file.assert_not_called()
        monkeypatch.setattr(file_upload_tools, "UPLOAD_BODY_TIMEOUT_SECONDS", 300)
        assert harness.upload(harness.token()).status_code == 200


class TestAppWiring:
    @pytest.fixture(autouse=True)
    def _phabricator_url(self, monkeypatch):
        monkeypatch.setenv("PHABRICATOR_URL", "https://phabricator.example.com/api/")
        monkeypatch.delenv("PHABRICATOR_MCP_MAX_UPLOAD_MB", raising=False)
        monkeypatch.delenv("PHABRICATOR_MCP_UPLOAD_VIEW_POLICY", raising=False)

    def _tool_names(self, **app_kwargs):
        app = ConduitApp(PhabricatorConfig(token="x" * 32), **app_kwargs)
        app.register_tools()
        return {tool.name for tool in asyncio.run(app.mcp.list_tools())}

    @pytest.mark.parametrize("transport", ["http", "sse"])
    def test_registered_on_http_transports_with_a_server_url(self, transport):
        names = self._tool_names(transport=transport, server_url=SERVER_URL)

        assert "pha_file_prepare_upload" in names

    def test_not_registered_without_a_server_url(self):
        assert "pha_file_prepare_upload" not in self._tool_names(transport="http")

    def test_not_registered_on_stdio(self):
        names = self._tool_names(transport="stdio", server_url=SERVER_URL)

        assert "pha_file_prepare_upload" not in names

    def test_header_token_mode_uploads_with_the_callers_token(self):
        app = ConduitApp(
            PhabricatorConfig(require_token=False),
            transport="http",
            server_url=SERVER_URL,
        )
        app.register_tools()
        caller_token = "a" * 32

        sent = {}

        def conduit_api(self, method, params=None, **kwargs):
            assert self.api_token == caller_token
            sent[method] = params
            return {
                "user.whoami": {"phid": "PHID-USER-alice"},
                "file.upload": "PHID-FILE-42",
                "file.search": {"data": [{"id": 42, "fields": {"name": "shot.png"}}]},
            }[method]

        async def prepare():
            return await app.mcp.call_tool(
                "pha_file_prepare_upload", {"name": "shot.png"}
            )

        with patch(
            "conduit.client.base.BasePhabricatorClient._make_request", conduit_api
        ):
            with patch(
                "conduit.conduit.get_http_headers",
                return_value={"x-phabricator-token": caller_token},
            ):
                prepared = asyncio.run(prepare()).structured_content
            response = _request(
                app.mcp.http_app(path="/mcp", stateless_http=True),
                prepared["headers"][UPLOAD_TOKEN_HEADER],
            )

        assert response.status_code == 200
        assert response.json()["remarkup"] == "{F42}"
        assert sent["file.upload"]["viewPolicy"] == "no-one"

    def test_oauth_mode_serves_the_endpoint_without_a_bearer_token(
        self, tmp_path, monkeypatch
    ):
        # The OAuth proxy persists client registrations under FastMCP's home.
        monkeypatch.setattr(fastmcp.settings, "home", tmp_path)
        provider = build_phabricator_auth_provider(
            base_url="https://phabricator.example.com",
            api_url="https://phabricator.example.com/api/",
            server_url=SERVER_URL,
            client_id="PHID-OASC-12345",
            client_secret="secret",
        )
        app = ConduitApp(
            PhabricatorConfig(require_token=False),
            transport="http",
            auth_provider=provider,
            server_url=SERVER_URL,
        )
        app.register_tools()

        response = _request(
            app.mcp.http_app(path="/mcp", stateless_http=True), "not-a-token"
        )

        assert response.status_code == 404
        assert response.json() == REJECTED


class TestUploadLimitConfig:
    @pytest.fixture(autouse=True)
    def _phabricator_url(self, monkeypatch):
        monkeypatch.setenv("PHABRICATOR_URL", "https://phabricator.example.com/api/")

    def test_defaults_to_16_megabytes(self, monkeypatch):
        monkeypatch.delenv("PHABRICATOR_MCP_MAX_UPLOAD_MB", raising=False)

        config = PhabricatorConfig(require_token=False)

        assert config.max_upload_bytes == 16 * 1024 * 1024

    def test_reads_the_limit_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("PHABRICATOR_MCP_MAX_UPLOAD_MB", "5")

        config = PhabricatorConfig(require_token=False)

        assert config.max_upload_bytes == 5 * 1024 * 1024

    def test_a_blank_value_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("PHABRICATOR_MCP_MAX_UPLOAD_MB", "")

        config = PhabricatorConfig(require_token=False)

        assert config.max_upload_bytes == 16 * 1024 * 1024

    @pytest.mark.parametrize("value", ["0", "-1", "ten", "1.5"])
    def test_rejects_a_limit_that_is_not_a_positive_integer(self, monkeypatch, value):
        monkeypatch.setenv("PHABRICATOR_MCP_MAX_UPLOAD_MB", value)

        with pytest.raises(ValueError, match="PHABRICATOR_MCP_MAX_UPLOAD_MB"):
            PhabricatorConfig(require_token=False)


class TestUploadViewPolicyConfig:
    @pytest.fixture(autouse=True)
    def _phabricator_url(self, monkeypatch):
        monkeypatch.setenv("PHABRICATOR_URL", "https://phabricator.example.com/api/")

    @pytest.mark.parametrize("value", [None, ""])
    def test_defaults_to_author_only(self, monkeypatch, value):
        monkeypatch.delenv("PHABRICATOR_MCP_UPLOAD_VIEW_POLICY", raising=False)
        if value is not None:
            monkeypatch.setenv("PHABRICATOR_MCP_UPLOAD_VIEW_POLICY", value)

        assert PhabricatorConfig(require_token=False).upload_view_policy == "no-one"

    def test_reads_the_policy_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("PHABRICATOR_MCP_UPLOAD_VIEW_POLICY", "users")

        assert PhabricatorConfig(require_token=False).upload_view_policy == "users"
