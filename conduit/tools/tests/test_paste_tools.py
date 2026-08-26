"""Tests for the Paste MCP tools."""

from unittest.mock import Mock

import pytest

from conduit.tools.paste_tools import _MAX_PASTE_RESPONSE_BYTES, register_paste_tools


class _StubMCP:
    """Minimal stand-in for FastMCP that captures registered tool functions."""

    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


@pytest.fixture
def paste_client():
    return Mock()


@pytest.fixture
def tools(paste_client):
    client = Mock()
    client.paste = paste_client
    mcp = _StubMCP()
    register_paste_tools(mcp, lambda: client)
    return mcp.tools


class TestPasteCreate:
    def test_passes_language_through(self, tools, paste_client):
        paste_client.create_paste.return_value = {"object": {"id": 7}}

        result = tools["pha_paste_create"]("Title", "body", language="python")

        paste_client.create_paste.assert_called_once_with(
            title="Title", content="body", language="python"
        )
        assert result == {"success": True, "paste": {"object": {"id": 7}}}

    def test_blank_language_becomes_none(self, tools, paste_client):
        paste_client.create_paste.return_value = {}

        tools["pha_paste_create"]("Title", "body")

        assert paste_client.create_paste.call_args.kwargs["language"] is None


class TestPasteUpdate:
    def test_builds_a_transaction_per_supplied_field(self, tools, paste_client):
        paste_client.edit_paste.return_value = {"object": {"id": 7}}

        result = tools["pha_paste_update"](
            "P7",
            title="New",
            content="body",
            language="python",
            status="archived",
        )

        paste_client.edit_paste.assert_called_once_with(
            [
                {"type": "title", "value": "New"},
                {"type": "text", "value": "body"},
                {"type": "language", "value": "python"},
                {"type": "status", "value": "archived"},
            ],
            object_identifier="P7",
        )
        # The edit response is returned, not discarded -- callers need the
        # object id to confirm the edit landed.
        assert result == {"success": True, "paste": {"object": {"id": 7}}}

    def test_only_sends_transactions_for_supplied_fields(self, tools, paste_client):
        paste_client.edit_paste.return_value = {}

        tools["pha_paste_update"]("P7", status="active")

        assert paste_client.edit_paste.call_args.args[0] == [
            {"type": "status", "value": "active"}
        ]

    def test_empty_string_clears_a_field_rather_than_meaning_unchanged(
        self, tools, paste_client
    ):
        paste_client.edit_paste.return_value = {}

        tools["pha_paste_update"]("P7", content="", language="")

        assert paste_client.edit_paste.call_args.args[0] == [
            {"type": "text", "value": ""},
            {"type": "language", "value": ""},
        ]

    def test_rejects_an_update_with_nothing_to_change(self, tools, paste_client):
        result = tools["pha_paste_update"]("P7")

        assert result == {"success": False, "error": "No updates specified"}
        paste_client.edit_paste.assert_not_called()


class TestPasteGet:
    @pytest.mark.parametrize("paste_id", ["123", "P123", "p123"])
    def test_resolves_numeric_ids_and_monograms(self, tools, paste_client, paste_id):
        paste_client.search_pastes.return_value = {"data": [{"id": 123}]}

        result = tools["pha_paste_get"](paste_id)

        paste_client.search_pastes.assert_called_once_with(
            constraints={"ids": [123]},
            attachments={"content": True},
            limit=1,
            max_response_bytes=_MAX_PASTE_RESPONSE_BYTES,
        )
        assert result == {"success": True, "paste": {"id": 123}}

    def test_bounds_the_response_before_decoding(self, tools, paste_client):
        # The size cap must reach the HTTP layer as an explicit, scoped
        # policy for this call -- not rely on a blanket default in
        # BasePhabricatorClient._make_request(), which would also apply to
        # unrelated large-but-legitimate responses like file.download.
        paste_client.search_pastes.return_value = {"data": [{"id": 123}]}

        tools["pha_paste_get"]("P123")

        assert (
            paste_client.search_pastes.call_args.kwargs["max_response_bytes"]
            == _MAX_PASTE_RESPONSE_BYTES
        )

    def test_resolves_phids(self, tools, paste_client):
        paste_client.search_pastes.return_value = {"data": [{"id": 123}]}

        tools["pha_paste_get"]("PHID-PSTE-abc")

        assert paste_client.search_pastes.call_args.kwargs["constraints"] == {
            "phids": ["PHID-PSTE-abc"]
        }

    @pytest.mark.parametrize("paste_id", ["", "not-a-paste", "P", "12a"])
    def test_rejects_malformed_identifiers_without_calling_the_api(
        self, tools, paste_client, paste_id
    ):
        result = tools["pha_paste_get"](paste_id)

        assert result["success"] is False
        assert "Invalid paste identifier" in result["error"]
        paste_client.search_pastes.assert_not_called()

    def test_reports_a_missing_paste(self, tools, paste_client):
        paste_client.search_pastes.return_value = {"data": []}

        result = tools["pha_paste_get"]("P404")

        assert result == {"success": False, "error": "Paste 'P404' not found"}

    def test_truncates_oversized_content(self, tools, paste_client):
        huge_text = "x" * 25_000
        paste_client.search_pastes.return_value = {
            "data": [{"id": 123, "attachments": {"content": {"content": huge_text}}}]
        }

        result = tools["pha_paste_get"]("P123")

        content_attachment = result["paste"]["attachments"]["content"]
        assert len(content_attachment["content"]) == 20_000
        assert content_attachment["truncated"] is True
        assert content_attachment["original_length"] == 25_000

    def test_leaves_content_under_the_cap_untouched(self, tools, paste_client):
        paste_client.search_pastes.return_value = {
            "data": [{"id": 123, "attachments": {"content": {"content": "short"}}}]
        }

        result = tools["pha_paste_get"]("P123")

        content_attachment = result["paste"]["attachments"]["content"]
        assert content_attachment["content"] == "short"
        assert "truncated" not in content_attachment

    def test_rejects_a_non_list_data_field(self, tools, paste_client):
        paste_client.search_pastes.return_value = {"data": "oops"}

        result = tools["pha_paste_get"]("P123")

        assert result["success"] is False
        assert "expected a list" in result["error"]

    def test_rejects_a_non_dict_paste_record(self, tools, paste_client):
        paste_client.search_pastes.return_value = {"data": ["oops"]}

        result = tools["pha_paste_get"]("P123")

        assert result["success"] is False
        assert "expected a paste record" in result["error"]


def test_registers_all_paste_tools(tools):
    assert set(tools) == {
        "pha_paste_create",
        "pha_paste_update",
        "pha_paste_get",
        "pha_paste_search",
    }


class TestPasteSearch:
    def test_maps_every_filter_to_its_constraint_key(self, tools, paste_client):
        paste_client.search_pastes.return_value = {"data": []}

        tools["pha_paste_search"](
            ids=[1],
            phids=["PHID-PSTE-abc"],
            authors=["sbarousis"],
            languages=["python"],
            statuses=["active"],
        )

        # "authors" is deliberate: paste.search does not accept the
        # "authorPHIDs" key that maniphest/differential search use.
        assert paste_client.search_pastes.call_args.kwargs["constraints"] == {
            "ids": [1],
            "phids": ["PHID-PSTE-abc"],
            "authors": ["sbarousis"],
            "languages": ["python"],
            "statuses": ["active"],
        }

    def test_sends_no_constraints_when_unfiltered(self, tools, paste_client):
        paste_client.search_pastes.return_value = {"data": []}

        tools["pha_paste_search"]()

        kwargs = paste_client.search_pastes.call_args.kwargs
        assert kwargs["constraints"] is None
        assert kwargs["order"] is None
        assert kwargs["after"] is None
        assert kwargs["limit"] == 50

    def test_never_requests_content(self, tools, paste_client):
        # Bulk search deliberately never fetches paste bodies -- an
        # unbounded number of arbitrarily large pastes in one response is
        # exactly the resource risk pha_paste_get's per-item cap doesn't
        # protect against. Fetch content per-paste via pha_paste_get instead.
        paste_client.search_pastes.return_value = {"data": []}

        tools["pha_paste_search"]()

        assert "attachments" not in paste_client.search_pastes.call_args.kwargs

    def test_passes_order_and_cursor_through(self, tools, paste_client):
        paste_client.search_pastes.return_value = {"data": []}

        tools["pha_paste_search"](order="oldest", after="cursor-1")

        kwargs = paste_client.search_pastes.call_args.kwargs
        assert kwargs["order"] == "oldest"
        assert kwargs["after"] == "cursor-1"

    def test_adds_pagination_metadata_from_the_cursor(self, tools, paste_client):
        paste_client.search_pastes.return_value = {
            "data": [{"id": 1}],
            "cursor": {"after": "next-page", "limit": 50},
        }

        result = tools["pha_paste_search"]()

        assert result["pastes"]["pagination"] == {
            "cursor": {"after": "next-page", "limit": 50},
            "has_more": True,
            "limit": 50,
        }
