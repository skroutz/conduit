"""Relationship safety at the MCP boundary and against a live Phorge instance."""

import asyncio
from uuid import uuid4

import pytest
from fastmcp import Client, FastMCP

from conduit.client.unified import PhabricatorClient
from conduit.conduit import get_config
from conduit.main_tools import register_tools


TOOL_NAME = "pha_task_update_relationships"
BASE_ARGUMENTS = {
    "task_id": "PHID-TASK-source",
    "relationship_type": "subtask",
    "target_ids": "PHID-TASK-target",
}


@pytest.fixture
def validation_server():
    client_requests = []

    def get_client():
        client_requests.append(True)
        raise AssertionError("Invalid requests must not acquire an API client")

    mcp = FastMCP("relationship-validation")
    register_tools(mcp, get_client)
    return mcp, client_requests


def test_schema_requires_operation_and_warns_about_replacement(validation_server):
    mcp, client_requests = validation_server

    async def check_schema():
        async with Client(mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            tool = tools[TOOL_NAME]
            schema = tool.input_schema
            assert "operation" in schema["required"]
            assert schema["properties"]["operation"]["enum"] == [
                "add",
                "remove",
                "set",
            ]
            assert "default" not in schema["properties"]["operation"]
            assert 'operation="add"' in tool.description
            assert 'operation="remove"' in tool.description
            assert 'operation="set"' in tool.description
            assert "replaces the entire list" in tool.description
            assert (
                "removing omitted relationships"
                in schema["properties"]["operation"]["description"]
            )

    asyncio.run(check_schema())
    assert client_requests == []


@pytest.mark.parametrize(
    "arguments",
    [
        BASE_ARGUMENTS,  # Legacy calls must not silently replace relationships.
        {**BASE_ARGUMENTS, "operation": "replace"},
        {**BASE_ARGUMENTS, "operation": None},
        {**BASE_ARGUMENTS, "operation": "add", "relationship_type": "other"},
    ],
)
def test_invalid_contract_is_rejected_before_api_access(validation_server, arguments):
    mcp, client_requests = validation_server

    async def call_tool():
        async with Client(mcp) as client:
            result = await client.call_tool(TOOL_NAME, arguments, raise_on_error=False)
            assert result.is_error

    asyncio.run(call_tool())
    assert client_requests == []


@pytest.mark.parametrize("operation", ["add", "remove", "set"])
@pytest.mark.parametrize("target_ids", ["", " , , "])
def test_empty_targets_never_write(validation_server, operation, target_ids):
    mcp, client_requests = validation_server

    async def call_tool():
        async with Client(mcp) as client:
            result = await client.call_tool(
                TOOL_NAME,
                {**BASE_ARGUMENTS, "operation": operation, "target_ids": target_ids},
            )
            assert result.data["success"] is False
            assert result.data["error_code"] == "VALIDATION_ERROR"

    asyncio.run(call_tool())
    assert client_requests == []


@pytest.fixture
def live_relationship_tasks():
    config = get_config()
    with PhabricatorClient(config.url, config.token, enable_cache=False) as client:
        tasks = []
        try:
            for index in range(4):
                tasks.append(
                    client.maniphest.create_task(
                        f"Conduit relationship regression {uuid4().hex} {index}"
                    )
                )
            yield client, tasks
        finally:
            for task in tasks:
                client.maniphest.edit_task(
                    object_identifier=task["phid"],
                    transactions=[{"type": "status", "value": "resolved"}],
                )


@pytest.mark.integration
@pytest.mark.parametrize("relationship_type", ["subtask", "parent"])
def test_native_relationship_operations_preserve_unrelated_links(
    live_relationship_tasks, relationship_type
):
    upstream, tasks = live_relationship_tasks
    source, first, second, third = tasks
    targets = [first, second, third]
    mcp = FastMCP("relationship-integration")
    register_tools(mcp, lambda: upstream)

    def assert_relationships(expected):
        if relationship_type == "subtask":
            actual = upstream.maniphest.get_task(int(source["id"]))[
                "dependsOnTaskPHIDs"
            ]
            assert set(actual) == set(expected)
        else:
            # Each parent depends on the source task; check both inverse edges
            # and the absence of an incorrectly reversed subtask relationship.
            for target in targets:
                dependencies = upstream.maniphest.get_task(int(target["id"]))[
                    "dependsOnTaskPHIDs"
                ]
                assert (source["phid"] in dependencies) == (target["phid"] in expected)
            assert (
                upstream.maniphest.get_task(int(source["id"]))["dependsOnTaskPHIDs"]
                == []
            )

    async def exercise_operations():
        async with Client(mcp) as client:

            async def update(operation, selected):
                result = await client.call_tool(
                    TOOL_NAME,
                    {
                        "task_id": source["phid"],
                        "relationship_type": relationship_type,
                        "target_ids": " , ".join(task["phid"] for task in selected),
                        "operation": operation,
                    },
                )
                assert result.data == {
                    "success": True,
                    "operation": operation,
                    "relationship_type": relationship_type,
                }

            await update("add", [first, second])
            assert_relationships([first["phid"], second["phid"]])

            # Reproduce adding one task to a non-empty list, then retry it.
            await update("add", [third])
            await update("add", [third])
            assert_relationships([task["phid"] for task in targets])

            await update("remove", [second])
            await update("remove", [second])
            assert_relationships([first["phid"], third["phid"]])

            # Replacement is possible only when the caller explicitly selects it.
            await update("set", [second])
            assert_relationships([second["phid"]])

    asyncio.run(exercise_operations())
