"""Unit tests for the task and workboard tools added for board management."""

from dataclasses import dataclass, field
from typing import Any, Dict, List

from conduit.main_tools import (
    _build_task_transactions,
    _project_task_fields,
    _staleness_bucket,
    _task_field_diff,
    _task_group_keys,
    register_tools,
)


class StubMCP:
    def __init__(self) -> None:
        self.tools: Dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any):
        def decorator(function: Any) -> Any:
            self.tools[function.__name__] = function
            return function

        return decorator


@dataclass
class RecordingManiphest:
    pages: List[Dict[str, Any]]
    edits: List[Dict[str, Any]] = field(default_factory=list)
    searches: List[Dict[str, Any]] = field(default_factory=list)

    def search_tasks(self, **kwargs: Any) -> Dict[str, Any]:
        self.searches.append(kwargs)
        return self.pages[min(len(self.searches) - 1, len(self.pages) - 1)]

    def edit_task(self, **kwargs: Any) -> Dict[str, Any]:
        self.edits.append(kwargs)
        return {"object": {"phid": kwargs["object_identifier"]}}


@dataclass
class RecordingProject:
    pages: List[Dict[str, Any]]
    edits: List[Dict[str, Any]] = field(default_factory=list)
    searches: List[Dict[str, Any]] = field(default_factory=list)

    def search_columns(self, **kwargs: Any) -> Dict[str, Any]:
        self.searches.append(kwargs)
        return self.pages[min(len(self.searches) - 1, len(self.pages) - 1)]

    def edit_column(self, **kwargs: Any) -> Dict[str, Any]:
        self.edits.append(kwargs)
        return {"phid": "PHID-PCOL-one", "hidden": kwargs.get("hidden")}


@dataclass
class RecordingDashboard:
    edits: List[Dict[str, Any]] = field(default_factory=list)

    def edit_panel(self, **kwargs: Any) -> Dict[str, Any]:
        self.edits.append(kwargs)
        return {"object": {"phid": kwargs["panel_phid"]}}


@dataclass
class RecordingClient:
    maniphest: RecordingManiphest
    project: RecordingProject
    dashboard: RecordingDashboard


def task(task_id, **fields):
    return {
        "id": task_id,
        "phid": "PHID-TASK-{}".format(task_id),
        "fields": fields,
    }


def build_tools(task_pages=None, column_pages=None):
    maniphest = RecordingManiphest(task_pages or [{"data": [], "cursor": {}}])
    project = RecordingProject(column_pages or [{"data": [], "cursor": {}}])
    dashboard = RecordingDashboard()
    mcp = StubMCP()
    register_tools(mcp, lambda: RecordingClient(maniphest, project, dashboard))
    return mcp.tools, maniphest, project, dashboard


class TestTaskTransactions:
    def test_builds_only_provided_fields(self) -> None:
        transactions = _build_task_transactions(
            points=3, column_phid="PHID-PCOL-one", comment="done"
        )

        assert transactions == [
            {"type": "points", "value": 3},
            {"type": "column", "value": "PHID-PCOL-one"},
            {"type": "comment", "value": "done"},
        ]

    def test_builds_nothing_when_no_fields_given(self) -> None:
        assert _build_task_transactions() == []


class TestTaskFieldDiff:
    def test_reports_only_fields_that_change(self) -> None:
        current = task(
            1,
            status={"value": "open", "name": "Open"},
            ownerPHID="PHID-USER-one",
            points=3,
        )

        diff = _task_field_diff(
            current, {"status": "resolved", "owner_phid": "PHID-USER-one"}
        )

        assert diff == [
            {"task_id": "T1", "field": "status", "from": "open", "to": "resolved"}
        ]

    def test_compares_priority_by_name_case_insensitively(self) -> None:
        current = task(2, priority={"value": 80, "name": "High"})

        assert _task_field_diff(current, {"priority": "high"}) == []
        assert len(_task_field_diff(current, {"priority": "low"})) == 1


class TestBulkUpdate:
    def test_dry_run_writes_nothing(self) -> None:
        tools, maniphest, _, _ = build_tools(
            [
                {
                    "data": [task(1, status={"value": "open", "name": "Open"})],
                    "cursor": {},
                }
            ]
        )

        result = tools["pha_task_bulk_update"](task_ids=["T1"], status="resolved")

        assert result["applied"] is False
        assert result["would_change"] == [
            {"task_id": "T1", "field": "status", "from": "open", "to": "resolved"}
        ]
        assert maniphest.edits == []

    def test_applies_edits_when_dry_run_is_off(self) -> None:
        tools, maniphest, _, _ = build_tools(
            [
                {
                    "data": [task(1, status={"value": "open", "name": "Open"})],
                    "cursor": {},
                }
            ]
        )

        result = tools["pha_task_bulk_update"](
            task_ids=["T1"], status="resolved", comment="stale", dry_run=False
        )

        assert result["applied"] is True
        assert result["applied_to"] == ["T1"]
        assert maniphest.edits[0]["object_identifier"] == "PHID-TASK-1"
        assert maniphest.edits[0]["transactions"] == [
            {"type": "status", "value": "resolved"},
            {"type": "comment", "value": "stale"},
        ]

    def test_reports_tasks_that_do_not_exist(self) -> None:
        tools, _, _, _ = build_tools([{"data": [], "cursor": {}}])

        result = tools["pha_task_bulk_update"](task_ids=["T404"], status="resolved")

        assert result["errors"] == [{"task_id": "T404", "error": "task not found"}]

    def test_rejects_an_empty_change_set(self) -> None:
        tools, _, _, _ = build_tools()

        result = tools["pha_task_bulk_update"](task_ids=["T1"])

        assert result["success"] is False
        assert "no fields to update" in result["error"]

    def test_rejects_more_than_five_hundred_tasks(self) -> None:
        tools, maniphest, _, _ = build_tools()

        result = tools["pha_task_bulk_update"](
            task_ids=[str(i) for i in range(501)], status="resolved"
        )

        assert result["success"] is False
        assert "at most 500" in result["error"]
        assert maniphest.searches == []


class TestGrouping:
    def test_buckets_staleness_by_day_boundaries(self) -> None:
        buckets = [30, 90]

        assert _staleness_bucket(10, buckets) == "<30d"
        assert _staleness_bucket(30, buckets) == "<90d"
        assert _staleness_bucket(365, buckets) == ">=90d"

    def test_counts_a_task_once_per_column(self) -> None:
        board_task = {
            "id": 1,
            "phid": "PHID-TASK-1",
            "fields": {},
            "attachments": {
                "columns": {
                    "boards": {
                        "PHID-PROJ-a": {
                            "columns": [
                                {"phid": "PHID-PCOL-a", "name": "Backlog"},
                                {"phid": "PHID-PCOL-b", "name": "Doing"},
                            ]
                        }
                    }
                }
            },
        }

        keys = _task_group_keys(board_task, "column", [30], now=0)

        assert keys == [("PHID-PCOL-a", "Backlog"), ("PHID-PCOL-b", "Doing")]

    def test_groups_tasks_without_an_owner_as_unassigned(self) -> None:
        assert _task_group_keys(task(1), "owner", [30], now=0) == [
            ("unassigned", "Unassigned")
        ]


class TestAggregate:
    def test_counts_and_summarises_each_group(self) -> None:
        day = 86400
        now = 100 * day
        tools, _, _, _ = build_tools(
            [
                {
                    "data": [
                        task(
                            1,
                            status={"value": "open", "name": "Open"},
                            dateCreated=now - 10 * day,
                            points=3,
                            ownerPHID="PHID-USER-one",
                        ),
                        task(
                            2,
                            status={"value": "open", "name": "Open"},
                            dateCreated=now - 30 * day,
                        ),
                        task(
                            3,
                            status={"value": "resolved", "name": "Resolved"},
                            dateCreated=now - 50 * day,
                        ),
                    ],
                    "cursor": {},
                }
            ]
        )

        result = tools["pha_task_aggregate"](group_by="status")

        assert result["total"] == 3
        assert result["truncated"] is False
        open_group = result["groups"][0]
        assert open_group["key"] == "open"
        assert open_group["count"] == 2
        assert open_group["unassigned"] == 1
        assert open_group["with_points"] == 1

    def test_stops_at_max_tasks(self) -> None:
        page = {
            "data": [task(i) for i in range(100)],
            "cursor": {"after": "more"},
        }
        tools, maniphest, _, _ = build_tools([page])

        result = tools["pha_task_aggregate"](group_by="status", max_tasks=200)

        assert result["total"] == 200
        assert result["truncated"] is True
        assert len(maniphest.searches) == 2


class TestFieldProjection:
    def test_keeps_only_requested_fields(self) -> None:
        result = {"data": [task(1, name="Fix it", points=3, ownerPHID="PHID-USER-one")]}

        projected = _project_task_fields(result, ["points"])

        assert projected["data"] == [{"id": 1, "phid": "PHID-TASK-1", "points": 3}]

    def test_returns_the_full_payload_when_no_fields_given(self) -> None:
        result = {"data": [task(1, name="Fix it")]}

        assert _project_task_fields(result, None) == result


class TestColumnTools:
    def test_pages_past_the_first_hundred_columns(self) -> None:
        first = {
            "data": [
                {"phid": "PHID-PCOL-{}".format(i), "fields": {}} for i in range(100)
            ],
            "cursor": {"after": "page-2"},
        }
        second = {
            "data": [{"phid": "PHID-PCOL-x", "fields": {}}],
            "cursor": {"after": None},
        }
        tools, _, project, _ = build_tools(column_pages=[first, second])

        result = tools["pha_workboard_search_columns"](limit=150)

        assert result["returned"] == 101
        assert result["has_more"] is False
        assert project.searches[1]["after"] == "page-2"

    def test_drops_hidden_columns_when_asked(self) -> None:
        page = {
            "data": [
                {"phid": "PHID-PCOL-a", "fields": {"isHidden": False}},
                {"phid": "PHID-PCOL-b", "fields": {"isHidden": True}},
            ],
            "cursor": {},
        }
        tools, _, _, _ = build_tools(column_pages=[page])

        result = tools["pha_workboard_search_columns"](include_hidden=False)

        assert [column["phid"] for column in result["columns"]] == ["PHID-PCOL-a"]

    def test_edit_column_dry_run_writes_nothing(self) -> None:
        tools, _, project, _ = build_tools()

        result = tools["pha_workboard_edit_column"](
            column_phid="PHID-PCOL-one", hidden=True
        )

        assert result == {
            "success": True,
            "applied": False,
            "creates_column": False,
            "column_phid": "PHID-PCOL-one",
            "would_change": {"hidden": True},
        }
        assert project.edits == []

    def test_edit_column_applies_the_change(self) -> None:
        tools, _, project, _ = build_tools()

        result = tools["pha_workboard_edit_column"](
            column_phid="PHID-PCOL-one", hidden=True, dry_run=False
        )

        assert result["applied"] is True
        assert project.edits == [
            {
                "column_phid": "PHID-PCOL-one",
                "project_phid": None,
                "name": None,
                "hidden": True,
                "limit": None,
                "sequence": None,
            }
        ]

    def test_creating_a_column_requires_a_project_and_name(self) -> None:
        tools, _, project, _ = build_tools()

        result = tools["pha_workboard_edit_column"](name="Done")

        assert result["success"] is False
        assert "project_phid and name" in result["error"]
        assert project.edits == []


class TestDashboardPanelTool:
    def test_dry_run_writes_nothing(self) -> None:
        tools, _, _, dashboard = build_tools()

        result = tools["pha_dashboard_edit_panel"](
            panel_phid="PHID-DSHP-one", name="Board health"
        )

        assert result["applied"] is False
        assert result["would_apply"] == [{"type": "name", "value": "Board health"}]
        assert dashboard.edits == []

    def test_maps_panel_fields_to_transactions(self) -> None:
        tools, _, _, dashboard = build_tools()

        tools["pha_dashboard_edit_panel"](
            panel_phid="PHID-DSHP-one",
            text="The task standard",
            query_key="assigned",
            item_limit=10,
            dry_run=False,
        )

        assert dashboard.edits == [
            {
                "panel_phid": "PHID-DSHP-one",
                "transactions": [
                    {"type": "custom.text", "value": "The task standard"},
                    {"type": "custom.key", "value": "assigned"},
                    {"type": "custom.limit", "value": 10},
                ],
            }
        ]

    def test_rejects_an_empty_change_set(self) -> None:
        tools, _, _, _ = build_tools()

        result = tools["pha_dashboard_edit_panel"](panel_phid="PHID-DSHP-one")

        assert result["success"] is False
        assert "no panel fields to change" in result["error"]
