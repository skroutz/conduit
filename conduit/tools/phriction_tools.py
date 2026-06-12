from fastmcp import FastMCP

from conduit.tools.handlers import handle_api_errors
from conduit.tools.pagination import _add_pagination_metadata


def _normalize_slug(slug: str) -> str:
    """Strip UI prefix and ensure trailing slash."""
    normalized = slug.lstrip("/")
    if normalized.startswith("w/"):
        normalized = normalized[2:]
    if not normalized.endswith("/"):
        normalized += "/"
    return normalized


def register_phriction_tools(
    mcp: FastMCP,
    get_client_func: callable,
    enable_type_safety: bool = False,
) -> None:
    """Register Phriction (wiki) MCP tools."""

    @mcp.tool()
    @handle_api_errors
    def pha_wiki_search(
        query: str = "",
        phids: list[str] = None,
        ids: list[int] = None,
        limit: int = 50,
    ) -> dict:
        """
        Search for Phriction wiki pages by full-text query, PHIDs, or IDs.
        To retrieve a specific page by path, use pha_wiki_get instead.

        Args:
            query: Full-text search query
            phids: Filter by page PHIDs
            ids: Filter by page IDs
            limit: Maximum number of results to return (default: 50)

        Returns:
            List of matching wiki pages with pagination metadata
        """
        client = get_client_func()

        constraints = {}
        if query:
            constraints["query"] = query
        if phids:
            constraints["phids"] = phids
        if ids:
            constraints["ids"] = ids

        result = client.phriction.search_documents(
            constraints=constraints if constraints else None,
            limit=limit,
        )

        result = _add_pagination_metadata(result, result.get("cursor"))

        return {"success": True, "pages": result}

    @mcp.tool()
    @handle_api_errors
    def pha_wiki_get(slug: str) -> dict:
        """
        Get a specific Phriction wiki page by its path/slug.

        Args:
            slug: The wiki page path, e.g. "engineering/oncall/" or "w/engineering/oncall/"

        Returns:
            Wiki page data including title, content, and metadata
        """
        client = get_client_func()

        normalized = _normalize_slug(slug)
        page = client.phriction.get_document_by_slug(normalized)

        if not page:
            return {"success": False, "error": f"Wiki page '{slug}' not found"}

        return {"success": True, "page": page}

    @mcp.tool()
    @handle_api_errors
    def pha_wiki_create(slug: str, title: str, content: str) -> dict:
        """
        Create a new Phriction wiki page.

        Args:
            slug: The wiki page path/slug, e.g. "engineering/oncall/"
            title: Page title
            content: Page content in Remarkup format

        Returns:
            Created page data
        """
        client = get_client_func()

        result = client.phriction.create_document(
            path=slug,
            title=title,
            content=content,
        )

        return {"success": True, "page": result}

    @mcp.tool()
    @handle_api_errors
    def pha_wiki_edit(
        slug: str,
        title: str = "",
        content: str = "",
    ) -> dict:
        """
        Edit an existing Phriction wiki page.

        Args:
            slug: The wiki page path/slug to edit, e.g. "engineering/oncall/"
            title: New page title (omit to leave unchanged)
            content: New page content in Remarkup format (omit to leave unchanged)

        Returns:
            Updated page data
        """
        client = get_client_func()

        result = client.phriction.edit_document(
            path=slug,
            title=title if title else None,
            content=content if content else None,
        )

        return {"success": True, "page": result}

    @mcp.tool()
    @handle_api_errors
    def pha_wiki_history(slug: str, limit: int = 20) -> dict:
        """
        Get the edit history of a Phriction wiki page.

        Args:
            slug: The wiki page path/slug, e.g. "engineering/oncall/"
            limit: Maximum number of history entries to return (default: 20)

        Returns:
            List of content revisions ordered newest-first
        """
        client = get_client_func()

        normalized = _normalize_slug(slug)

        # Resolve PHID via phriction.info, then fetch content history
        page = client.phriction.get_document_by_slug(normalized)
        if not page:
            return {"success": False, "error": f"Wiki page '{slug}' not found"}

        page_phid = page.get("phid")

        history_result = client.phriction.search_content(
            constraints={"documentPHIDs": [page_phid]},
            limit=limit,
        )

        history_result = _add_pagination_metadata(
            history_result, history_result.get("cursor")
        )

        return {"success": True, "history": history_result, "page_phid": page_phid}
