import inspect
from unittest.mock import Mock

import pytest

from conduit.tools.passphrase_tools import register_passphrase_tools
from conduit.utils import PhabricatorAPIError


class _StubMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


@pytest.fixture
def passphrase_client():
    return Mock()


@pytest.fixture
def user_client():
    client = Mock()
    client.search.return_value = {"data": []}
    return client


@pytest.fixture
def tools(passphrase_client, user_client):
    client = Mock()
    client.passphrase = passphrase_client
    client.user = user_client
    mcp = _StubMCP()
    register_passphrase_tools(mcp, lambda: client)
    return mcp.tools


def test_registers_only_the_narrow_passphrase_tools(tools):
    assert set(tools) == {
        "pha_passphrase_get",
        "pha_passphrase_edit_policies",
        "pha_passphrase_get_access_log",
    }


def test_get_returns_sanitized_client_metadata(tools, passphrase_client):
    passphrase_client.get_credential.return_value = {
        "id": 7,
        "name": "Deploy",
    }

    result = tools["pha_passphrase_get"]("K7")

    passphrase_client.get_credential.assert_called_once_with("K7")
    assert result == {
        "success": True,
        "credential": {"id": 7, "name": "Deploy"},
    }


def test_policy_tool_has_no_credential_material_argument(tools, passphrase_client):
    passphrase_client.edit_policies.return_value = {
        "object": {"id": 7, "phid": "PHID-CDTL-abc"}
    }

    result = tools["pha_passphrase_edit_policies"](
        "K7",
        view_policy="PHID-PROJ-viewers",
    )

    passphrase_client.edit_policies.assert_called_once_with(
        "K7",
        view_policy="PHID-PROJ-viewers",
        edit_policy=None,
    )
    assert result["success"] is True


def test_policy_tool_schema_has_no_secret_or_material_input(tools):
    parameters = inspect.signature(tools["pha_passphrase_edit_policies"]).parameters

    assert set(parameters) == {"credential_id", "view_policy", "edit_policy"}


def test_access_log_forwards_bounded_pagination(tools, passphrase_client):
    passphrase_client.get_access_log.return_value = {
        "data": [],
        "complete": True,
    }

    result = tools["pha_passphrase_get_access_log"](
        "K7",
        limit=20,
        after="cursor",
    )

    passphrase_client.get_access_log.assert_called_once_with(
        "K7",
        limit=20,
        before=None,
        after="cursor",
    )
    assert result == {
        "success": True,
        "access_log": {"data": [], "complete": True, "actors": {}},
    }


def test_access_log_resolves_actor_phids(tools, passphrase_client, user_client):
    passphrase_client.get_access_log.return_value = {
        "data": [
            {
                "authorPHID": "PHID-USER-alice",
                "dateCreated": 1700000000,
            }
        ],
        "complete": True,
    }
    user_client.search.return_value = {
        "data": [
            {
                "phid": "PHID-USER-alice",
                "fields": {"username": "alice", "realName": "Alice Example"},
            }
        ]
    }

    result = tools["pha_passphrase_get_access_log"]("K7")

    user_client.search.assert_called_once_with(
        constraints={"phids": ["PHID-USER-alice"]},
        limit=1,
    )
    assert result["access_log"]["actors"] == {
        "PHID-USER-alice": {
            "username": "alice",
            "realName": "Alice Example",
        }
    }


def test_access_log_survives_actor_resolution_failure(
    tools, passphrase_client, user_client
):
    passphrase_client.get_access_log.return_value = {
        "data": [{"authorPHID": "PHID-USER-alice"}],
        "complete": True,
    }
    user_client.search.side_effect = PhabricatorAPIError("user.search unavailable")

    result = tools["pha_passphrase_get_access_log"]("K7")

    assert result["success"] is True
    assert result["access_log"]["actors"] == {}
    assert "user.search unavailable" in result["access_log"]["actor_resolution_warning"]
