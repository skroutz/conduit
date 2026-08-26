from typing import List, Optional

from fastmcp import FastMCP

from conduit.tools.handlers import handle_api_errors
from conduit.tools.optimization import optimize_token_usage
from conduit.tools.pagination import _add_pagination_metadata, _truncate_text_response

# Pastes have no size limit on Phabricator's side. pha_paste_search never
# returns content at all (metadata only -- see its docstring), so the only
# place a paste body reaches this module is pha_paste_get, fetching exactly
# one paste. This caps what that single body looks like in the *emitted*
# result.
_MAX_PASTE_CONTENT_CHARS = 20_000

# Pre-decode ceiling on the paste.search response fetching that one body,
# passed to BasePhabricatorClient._make_request() so it's enforced while
# streaming the HTTP response, before it's buffered or JSON-decoded -- not
# just on the already-decoded result _cap_content() truncates above. Scoped
# to this one content-bearing call rather than applied as a blanket default
# in _make_request(), since other clients (file.download, diffusion raw
# diffs, wiki content, ...) can legitimately return responses well past this.
_MAX_PASTE_RESPONSE_BYTES = 25_000_000


def _cap_content(paste: dict) -> dict:
    """Truncate an oversized attached paste body in place, if present."""
    attachments = paste.get("attachments")
    if not isinstance(attachments, dict):
        return paste

    content_attachment = attachments.get("content")
    if not isinstance(content_attachment, dict):
        return paste

    text = content_attachment.get("content")
    if not isinstance(text, str) or len(text) <= _MAX_PASTE_CONTENT_CHARS:
        return paste

    truncated = _truncate_text_response(text, _MAX_PASTE_CONTENT_CHARS)
    content_attachment["content"] = truncated["content"]
    content_attachment["truncated"] = True
    content_attachment["original_length"] = truncated["original_length"]
    return paste


def register_paste_tools(
    mcp: FastMCP,
    get_client_func: callable,
    enable_type_safety: bool = False,
) -> None:
    """Register Paste (code snippets) MCP tools."""

    @mcp.tool()
    @handle_api_errors
    def pha_paste_create(title: str, content: str, language: str = "") -> dict:
        """
        Create a new Phabricator paste.

        Args:
            title: Paste title, shown in Paste listings and search results.
            content: The raw text/code to store as the paste's body.
            language: Syntax-highlighting language identifier (e.g.
                "python", "javascript", "yaml", "bash", "diff", "json").
                Matches your Phabricator instance's highlighter lexer
                names; not validated here -- an unrecognized value
                typically just renders as plain text rather than erroring.
                Omit for no highlighting.

        Returns:
            Created paste data
        """
        client = get_client_func()

        result = client.paste.create_paste(
            title=title,
            content=content,
            language=language or None,
        )

        return {"success": True, "paste": result}

    @mcp.tool()
    @handle_api_errors
    def pha_paste_update(
        paste_id: str,
        title: Optional[str] = None,
        content: Optional[str] = None,
        language: Optional[str] = None,
        status: Optional[str] = None,
    ) -> dict:
        """
        Update an existing Phabricator paste.

        Args:
            paste_id: The paste to update -- accepts a numeric ID ("123"),
                a monogram ("P123"), or a full PHID ("PHID-PSTE-...").
            title: New title for the paste. Omit to keep the current
                title unchanged.
            content: New content for the paste -- replaces it entirely,
                there is no partial/append edit. Omit to keep unchanged;
                pass "" to empty the paste out.
            language: New syntax-highlighting language identifier (same
                format as pha_paste_create's language argument). Omit to
                keep unchanged; pass "" to drop highlighting.
            status: "archived" to archive the paste, or "active" to restore
                an archived one. Omit to keep unchanged. Phabricator has no
                true delete for pastes -- archiving is the closest
                equivalent.

        Returns:
            Updated paste data. Returns {"success": False, "error": ...} if
            every field was left blank, since there would be nothing to
            update.
        """
        client = get_client_func()

        # `is not None` rather than truthiness: "" is a meaningful value
        # here (empty the paste, drop highlighting), not "leave unchanged".
        transactions = []
        if title is not None:
            transactions.append({"type": "title", "value": title})
        if content is not None:
            transactions.append({"type": "text", "value": content})
        if language is not None:
            transactions.append({"type": "language", "value": language})
        if status is not None:
            transactions.append({"type": "status", "value": status})

        if not transactions:
            return {"success": False, "error": "No updates specified"}

        result = client.paste.edit_paste(transactions, object_identifier=paste_id)

        return {"success": True, "paste": result}

    @mcp.tool()
    @handle_api_errors
    def pha_paste_get(paste_id: str) -> dict:
        """
        Get a specific Phabricator paste by ID, monogram, or PHID.

        Args:
            paste_id: The paste to fetch -- accepts a numeric ID ("123"),
                a monogram ("P123"), or a full PHID ("PHID-PSTE-...").

        Returns:
            The raw paste.search record, which is nested -- metadata lives
            under "fields" and the body under "attachments":

                {"id": 2256, "phid": "PHID-PSTE-...",
                 "fields": {"title": ..., "language": ..., "status": ...,
                            "authorPHID": ..., "dateCreated": ...},
                 "attachments": {"content": {"content": "<paste body>"}}}

            Content over roughly 20,000 characters is truncated -- check
            attachments.content.truncated/original_length to see if that
            happened.

            Returns {"success": False, "error": ...} if no paste matches or
            Phabricator's response doesn't have the shape this expects.
        """
        client = get_client_func()

        if paste_id.startswith("PHID-"):
            constraints = {"phids": [paste_id]}
        else:
            numeric_id = paste_id[1:] if paste_id[:1] in ("P", "p") else paste_id
            if not numeric_id.isdigit():
                return {
                    "success": False,
                    "error": (
                        f"Invalid paste identifier '{paste_id}' -- expected a "
                        'numeric ID ("123"), a monogram ("P123"), or a PHID '
                        '("PHID-PSTE-...")'
                    ),
                }
            constraints = {"ids": [int(numeric_id)]}

        result = client.paste.search_pastes(
            constraints=constraints,
            attachments={"content": True},
            limit=1,
            max_response_bytes=_MAX_PASTE_RESPONSE_BYTES,
        )

        data = result.get("data", [])
        if not isinstance(data, list):
            return {
                "success": False,
                "error": (
                    "Unexpected response shape from Phabricator: expected a "
                    f"list of pastes, got {type(data).__name__}"
                ),
            }
        if not data:
            return {"success": False, "error": f"Paste '{paste_id}' not found"}

        paste = data[0]
        if not isinstance(paste, dict):
            return {
                "success": False,
                "error": (
                    "Unexpected response shape from Phabricator: expected a "
                    f"paste record, got {type(paste).__name__}"
                ),
            }

        return {"success": True, "paste": _cap_content(paste)}

    @mcp.tool()
    @handle_api_errors
    @optimize_token_usage
    def pha_paste_search(
        ids: Optional[List[int]] = None,
        phids: Optional[List[str]] = None,
        authors: Optional[List[str]] = None,
        languages: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        order: str = "",
        after: str = "",
        limit: int = 50,
    ) -> dict:
        """
        Search for Phabricator pastes by metadata. Paste has no full-text
        search endpoint -- filter by author, language, or status instead.

        Metadata only -- this never returns paste content/body, by design,
        to keep bulk search results small regardless of how large individual
        pastes are. Use pha_paste_get on a specific result's id/phid to read
        that paste's content.

        Args:
            ids: Filter by numeric paste IDs, e.g. [123, 456].
            phids: Filter by exact paste PHIDs, e.g. ["PHID-PSTE-abc123"].
            authors: Filter by paste author -- accepts usernames (e.g.
                "sbarousis") or user PHIDs, and both can be mixed in the
                same list.
            languages: Filter by the paste's stored syntax-highlighting
                language, matched exactly against what was set on
                creation/update (e.g. "python" will not match a paste
                saved as "Python"). See pha_paste_create for the format.
            statuses: Filter by status -- valid values are "active" and
                "archived". Omit to include both.
            order: Result ordering -- "newest" or "oldest". Omit for
                Phabricator's default, which is newest-first. paste.search
                rejects any other value, including "relevance" (there is
                no full-text query to rank by).
            after: Pagination cursor from a previous response's
                pastes.pagination.cursor.after field, to fetch the next page.
            limit: Maximum results per page (default: 50). Phabricator caps
                this at 100 regardless of what's requested -- use `after`
                to page through more.

        Returns:
            {"success": True, "pastes": {"data": [...], "pagination": {...}}}
        """
        client = get_client_func()

        constraints = {}
        if ids:
            constraints["ids"] = ids
        if phids:
            constraints["phids"] = phids
        if authors:
            # paste.search names this constraint "authors" -- not the
            # "authorPHIDs" that maniphest/differential search use. Verified
            # against a live instance; don't "fix" this for consistency.
            constraints["authors"] = authors
        if languages:
            constraints["languages"] = languages
        if statuses:
            constraints["statuses"] = statuses

        result = client.paste.search_pastes(
            constraints=constraints if constraints else None,
            order=order or None,
            after=after or None,
            limit=limit,
        )

        result = _add_pagination_metadata(result, result.get("cursor"))

        return {"success": True, "pastes": result}
