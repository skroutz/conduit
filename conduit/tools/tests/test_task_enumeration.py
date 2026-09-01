import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastmcp import FastMCP

from conduit.main_tools import (
    _add_task_enumeration_metadata,
    _validate_task_page_request,
    register_tools,
)


class StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any):
        def decorator(function: Any) -> Any:
            self.tools[function.__name__] = function
            return function

        return decorator


@dataclass
class RecordingManiphest:
    response: dict[str, Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def search_tasks(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


@dataclass
class RecordingClient:
    maniphest: RecordingManiphest


@pytest.fixture
def search_response() -> dict[str, Any]:
    return {
        "data": [{"id": 1, "phid": "PHID-TASK-one", "fields": {}}],
        "maps": {},
        "query": {"queryKey": None},
        "cursor": {
            "limit": 50,
            "after": "opaque-next",
            "before": None,
            "order": None,
        },
    }


@pytest.fixture
def registered_tools(search_response: dict[str, Any]):
    maniphest = RecordingManiphest(search_response)
    mcp = StubMCP()
    register_tools(mcp, lambda: RecordingClient(maniphest))
    return mcp.tools, maniphest


class TestTaskPageRequest:
    def test_accepts_one_opaque_cursor(self) -> None:
        _validate_task_page_request(
            1000,
            before=None,
            after="opaque/+cursor==",
        )

    @pytest.mark.parametrize("limit", [0, 1001, -1, True])
    def test_rejects_invalid_limits(self, limit: int) -> None:
        with pytest.raises(ValueError, match="between 1 and 1000"):
            _validate_task_page_request(limit, before=None, after=None)

    def test_rejects_two_cursors(self) -> None:
        with pytest.raises(ValueError, match="before and after"):
            _validate_task_page_request(50, before="previous", after="next")

    @pytest.mark.parametrize("cursor_name", ["before", "after"])
    def test_rejects_blank_cursor(self, cursor_name: str) -> None:
        cursors = {"before": None, "after": None}
        cursors[cursor_name] = ""

        with pytest.raises(ValueError, match=cursor_name):
            _validate_task_page_request(50, **cursors)


class TestTaskEnumerationMetadata:
    def test_describes_a_middle_page(self) -> None:
        result = {
            "data": [{"id": 1}],
            "cursor": {
                "limit": 50,
                "after": "opaque-next",
                "before": "opaque-previous",
                "order": None,
            },
        }

        enriched = _add_task_enumeration_metadata(result)

        assert enriched["pagination"] == {
            "cursor": result["cursor"],
            "limit": 50,
            "returned": 1,
            "has_more": True,
            "has_next": True,
            "has_previous": True,
            "complete": False,
        }

    def test_accepts_phorge_string_cursor_limit(self) -> None:
        result = {
            "data": [{"id": 1}],
            "cursor": {
                "limit": "50",
                "after": "opaque-next",
                "before": None,
                "order": None,
            },
        }

        enriched = _add_task_enumeration_metadata(result)

        assert enriched["pagination"]["cursor"] is result["cursor"]
        assert enriched["pagination"]["cursor"]["limit"] == "50"
        assert enriched["pagination"]["limit"] == "50"

    def test_accepts_legacy_maximum_cursor_limit(self) -> None:
        result = _add_task_enumeration_metadata(
            {
                "data": [{"id": 1}],
                "cursor": {
                    "limit": 1000,
                    "after": None,
                    "before": None,
                    "order": None,
                },
            }
        )

        assert result["pagination"]["limit"] == 1000

    def test_marks_the_final_empty_page_complete(self) -> None:
        result = _add_task_enumeration_metadata(
            {
                "data": [],
                "cursor": {
                    "limit": 100,
                    "after": None,
                    "before": "opaque-previous",
                    "order": None,
                },
            }
        )

        assert result["pagination"]["returned"] == 0
        assert result["pagination"]["has_next"] is False
        assert result["pagination"]["has_previous"] is True
        assert result["pagination"]["complete"] is True

    def test_marks_the_first_page_complete_during_reverse_traversal(self) -> None:
        result = _add_task_enumeration_metadata(
            {
                "data": [{"id": 1}],
                "cursor": {
                    "limit": 50,
                    "after": "opaque-next",
                    "before": None,
                    "order": None,
                },
            },
            reverse=True,
        )

        assert result["pagination"]["has_next"] is True
        assert result["pagination"]["has_previous"] is False
        assert result["pagination"]["complete"] is True

    @pytest.mark.parametrize(
        "response,error",
        [
            ({"cursor": {}}, "data"),
            ({"data": {}}, "data"),
            ({"data": []}, "cursor"),
            ({"data": [], "cursor": []}, "cursor"),
        ],
    )
    def test_rejects_malformed_search_result_structure(
        self, response: dict, error: str
    ) -> None:
        with pytest.raises(ValueError, match=error):
            _add_task_enumeration_metadata(response)

    @pytest.mark.parametrize("field", ["after", "before", "limit", "order"])
    def test_rejects_missing_cursor_control_field(self, field: str) -> None:
        cursor = {
            "limit": 50,
            "after": None,
            "before": None,
            "order": None,
        }
        del cursor[field]

        with pytest.raises(ValueError, match=rf"cursor\.{field}"):
            _add_task_enumeration_metadata({"data": [], "cursor": cursor})

    @pytest.mark.parametrize(
        "field,value",
        [
            ("after", []),
            ("before", 42),
            ("limit", "many"),
            ("limit", True),
            ("limit", 0),
            ("limit", 1001),
            ("order", []),
        ],
    )
    def test_rejects_invalid_cursor_control_field(self, field: str, value: Any) -> None:
        cursor = {
            "limit": 50,
            "after": None,
            "before": None,
            "order": None,
        }
        cursor[field] = value

        with pytest.raises(ValueError, match=rf"cursor\.{field}"):
            _add_task_enumeration_metadata({"data": [], "cursor": cursor})


class TestWorkboardTaskEnumeration:
    def test_preserves_positional_limit(
        self, registered_tools: tuple[dict[str, Any], RecordingManiphest]
    ) -> None:
        tools, maniphest = registered_tools

        tools["pha_workboard_search_tasks_by_column"](
            "PHID-PCOL-inbox",
            50,
        )

        call = maniphest.calls[0]
        assert call["constraints"] == {"columnPHIDs": ["PHID-PCOL-inbox"]}
        assert call["limit"] == 50

    def test_forwards_status_and_after_cursor(
        self, registered_tools: tuple[dict[str, Any], RecordingManiphest]
    ) -> None:
        tools, maniphest = registered_tools

        result = tools["pha_workboard_search_tasks_by_column"](
            "PHID-PCOL-inbox",
            statuses=["open"],
            after="opaque/+cursor==",
            limit=50,
        )

        assert maniphest.calls == [
            {
                "constraints": {
                    "columnPHIDs": ["PHID-PCOL-inbox"],
                    "statuses": ["open"],
                },
                "before": None,
                "after": "opaque/+cursor==",
                "limit": 50,
            }
        ]
        assert result["success"] is True
        assert result["tasks"]["pagination"]["returned"] == 1
        assert result["tasks"]["pagination"]["complete"] is False

    def test_repeats_workboard_query_arguments_for_each_cursor_page(
        self, registered_tools: tuple[dict[str, Any], RecordingManiphest]
    ) -> None:
        tools, maniphest = registered_tools

        first_page = tools["pha_workboard_search_tasks_by_column"](
            "PHID-PCOL-inbox", statuses=["open"], limit=1
        )
        tools["pha_workboard_search_tasks_by_column"](
            "PHID-PCOL-inbox",
            statuses=["open"],
            after=first_page["tasks"]["pagination"]["cursor"]["after"],
            limit=1,
        )

        assert maniphest.calls == [
            {
                "constraints": {
                    "columnPHIDs": ["PHID-PCOL-inbox"],
                    "statuses": ["open"],
                },
                "before": None,
                "after": None,
                "limit": 1,
            },
            {
                "constraints": {
                    "columnPHIDs": ["PHID-PCOL-inbox"],
                    "statuses": ["open"],
                },
                "before": None,
                "after": "opaque-next",
                "limit": 1,
            },
        ]

    def test_omits_status_constraint_and_forwards_before_cursor(
        self, registered_tools: tuple[dict[str, Any], RecordingManiphest]
    ) -> None:
        tools, maniphest = registered_tools

        result = tools["pha_workboard_search_tasks_by_column"](
            "PHID-PCOL-inbox",
            before="opaque-previous",
            limit=25,
        )

        assert result["tasks"]["pagination"]["complete"] is True
        assert maniphest.calls == [
            {
                "constraints": {"columnPHIDs": ["PHID-PCOL-inbox"]},
                "before": "opaque-previous",
                "after": None,
                "limit": 25,
            }
        ]

    def test_rejects_two_cursors_without_calling_phabricator(
        self, registered_tools: tuple[dict[str, Any], RecordingManiphest]
    ) -> None:
        tools, maniphest = registered_tools

        result = tools["pha_workboard_search_tasks_by_column"](
            "PHID-PCOL-inbox",
            before="previous",
            after="next",
        )

        assert result["success"] is False
        assert result["error_code"] == "VALIDATION_ERROR"
        assert maniphest.calls == []

    def test_forwards_legacy_limit_above_local_phorge_maximum(
        self, registered_tools: tuple[dict[str, Any], RecordingManiphest]
    ) -> None:
        tools, maniphest = registered_tools

        tools["pha_workboard_search_tasks_by_column"]("PHID-PCOL-inbox", limit=1000)

        assert maniphest.calls[0]["limit"] == 1000


class TestAdvancedTaskEnumeration:
    def test_preserves_existing_positional_parameters(
        self, registered_tools: tuple[dict[str, Any], RecordingManiphest]
    ) -> None:
        tools, maniphest = registered_tools

        tools["pha_task_search_advanced"](
            "",  # query_key
            None,  # assigned
            None,  # author_phids
            None,  # statuses
            None,  # priorities
            ["PHID-PROJ-november"],  # projects
            None,  # subscribers
            "",  # fulltext_query
            None,  # has_parents
            None,  # has_subtasks
            None,  # created_after
            None,  # created_before
            None,  # modified_after
            None,  # modified_before
            "closed",  # order
            False,  # include_subscribers
            False,  # include_projects
            False,  # include_columns
            25,  # limit
            None,  # preset
        )

        call = maniphest.calls[0]
        assert call["constraints"] == {"projects": ["PHID-PROJ-november"]}
        assert call["order"] == "closed"
        assert call["limit"] == 25

    def test_forwards_project_status_closure_dates_and_after_cursor(
        self, registered_tools: tuple[dict[str, Any], RecordingManiphest]
    ) -> None:
        tools, maniphest = registered_tools

        result = tools["pha_task_search_advanced"](
            projects=["PHID-PROJ-november"],
            statuses=["resolved"],
            closed_after=1_730_000_000,
            closed_before=1_730_600_000,
            order="closed",
            after="opaque-next",
            limit=100,
        )

        assert maniphest.calls == [
            {
                "query_key": None,
                "constraints": {
                    "statuses": ["resolved"],
                    "projects": ["PHID-PROJ-november"],
                    "closedStart": 1_730_000_000,
                    "closedEnd": 1_730_600_000,
                },
                "attachments": None,
                "order": "closed",
                "before": None,
                "after": "opaque-next",
                "limit": 100,
            }
        ]
        assert result["success"] is True
        assert result["results"]["pagination"]["returned"] == 1
        assert result["results"]["pagination"]["has_next"] is True

    def test_repeats_advanced_query_arguments_for_each_cursor_page(
        self, registered_tools: tuple[dict[str, Any], RecordingManiphest]
    ) -> None:
        tools, maniphest = registered_tools

        page_request = {
            "query_key": "all",
            "statuses": ["resolved"],
            "projects": ["PHID-PROJ-november"],
            "closed_after": 1_730_000_000,
            "closed_before": 1_730_600_000,
            "order": "closed",
            "include_projects": True,
            "limit": 1,
        }
        first_page = tools["pha_task_search_advanced"](**page_request)
        tools["pha_task_search_advanced"](
            **page_request,
            after=first_page["results"]["pagination"]["cursor"]["after"],
        )

        expected_call = {
            "query_key": "all",
            "constraints": {
                "statuses": ["resolved"],
                "projects": ["PHID-PROJ-november"],
                "closedStart": 1_730_000_000,
                "closedEnd": 1_730_600_000,
            },
            "attachments": {"projects": True},
            "order": "closed",
            "before": None,
            "limit": 1,
        }
        assert maniphest.calls == [
            {**expected_call, "after": None},
            {**expected_call, "after": "opaque-next"},
        ]

    def test_forwards_before_cursor_without_adding_date_constraints(
        self, registered_tools: tuple[dict[str, Any], RecordingManiphest]
    ) -> None:
        tools, maniphest = registered_tools

        result = tools["pha_task_search_advanced"](
            projects=["PHID-PROJ-november"],
            before="opaque-previous",
            limit=20,
        )

        assert result["results"]["pagination"]["complete"] is True
        call = maniphest.calls[0]
        assert call["before"] == "opaque-previous"
        assert call["after"] is None
        assert "closedStart" not in call["constraints"]
        assert "closedEnd" not in call["constraints"]

    def test_rejects_invalid_cursor_combination_before_upstream_call(
        self, registered_tools: tuple[dict[str, Any], RecordingManiphest]
    ) -> None:
        tools, maniphest = registered_tools

        result = tools["pha_task_search_advanced"](
            projects=["PHID-PROJ-november"],
            before="previous",
            after="next",
        )

        assert result["success"] is False
        assert result["error_code"] == "VALIDATION_ERROR"
        assert maniphest.calls == []

    @pytest.mark.parametrize("cursor", [{"before": "previous"}, {"after": "next"}])
    def test_rejects_recent_preset_with_cursor(
        self,
        cursor: dict[str, str],
        registered_tools: tuple[dict[str, Any], RecordingManiphest],
    ) -> None:
        tools, maniphest = registered_tools

        result = tools["pha_task_search_advanced"](
            preset="recent",
            **cursor,
        )

        assert result["success"] is False
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "recent" in result["error"]
        assert maniphest.calls == []

    def test_forwards_legacy_limit_above_local_phorge_maximum(
        self, registered_tools: tuple[dict[str, Any], RecordingManiphest]
    ) -> None:
        tools, maniphest = registered_tools

        tools["pha_task_search_advanced"](limit=1000)

        assert maniphest.calls[0]["limit"] == 1000


def test_fastmcp_schema_exposes_task_enumeration_arguments(
    search_response: dict[str, Any],
) -> None:
    maniphest = RecordingManiphest(search_response)
    mcp = FastMCP("task-enumeration-contract")
    register_tools(mcp, lambda: RecordingClient(maniphest))

    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    workboard_schema = tools["pha_workboard_search_tasks_by_column"].parameters
    advanced_schema = tools["pha_task_search_advanced"].parameters

    assert set(workboard_schema["properties"]) == {
        "column_phid",
        "statuses",
        "before",
        "after",
        "limit",
    }
    assert workboard_schema["required"] == ["column_phid"]

    advanced_properties = set(advanced_schema["properties"])
    assert {
        "closed_after",
        "closed_before",
        "before",
        "after",
        "limit",
    } <= advanced_properties
    assert not {
        "closed_after",
        "closed_before",
        "before",
        "after",
    }.intersection(advanced_schema.get("required", []))

    for tool_name in (
        "pha_workboard_search_tasks_by_column",
        "pha_task_search_advanced",
    ):
        assert (
            "repeat all query-defining arguments unchanged"
            in tools[tool_name].description
        )
        assert "integer or decimal string" in tools[tool_name].description.replace(
            "\n", " "
        )
        assert "requested direction" in tools[tool_name].description.replace("\n", " ")

    for field_name in ("closed_after", "closed_before"):
        field_schema = advanced_schema["properties"][field_name]
        assert {"type": "integer"} in field_schema["anyOf"]
        assert {"type": "null"} in field_schema["anyOf"]
        assert field_schema["default"] is None
