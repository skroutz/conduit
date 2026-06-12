from fastmcp import FastMCP

from conduit.tools.handlers import handle_api_errors
from conduit.tools.pagination import _add_pagination_metadata


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
        slugs: list[str] = None,
        phids: list[str] = None,
        ids: list[int] = None,
        limit: int = 50,
    ) -> dict:
        """
        Search for Phriction wiki pages.

        Args:
            query: Full-text search query
            slugs: Filter by exact page paths/slugs (e.g. ["engineering/oncall/"])
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
        if slugs:
            constraints["slugs"] = slugs
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

        # Normalize slug: strip leading "w/" prefix if present (UI uses it, API does not)
        normalized = slug.lstrip("/")
        if normalized.startswith("w/"):
            normalized = normalized[2:]
        # Ensure trailing slash as Phabricator slugs are always terminated with "/"
        if not normalized.endswith("/"):
            normalized += "/"

        result = client.phriction.search_documents(
            constraints={"slugs": [normalized]},
            limit=1,
        )

        data = result.get("data", [])
        if not data:
            return {
                "success": False,
                "error": f"Wiki page '{slug}' not found",
            }

        page = data[0]
        page_phid = page.get("phid")

        # Fetch the current content via content search
        content_result = client.phriction.search_content(
            constraints={"documentPHIDs": [page_phid]},
            limit=1,
        )
        content_data = content_result.get("data", [])
        if content_data:
            page["current_content"] = content_data[0]

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

        # Resolve the document PHID first
        normalized = slug.lstrip("/")
        if normalized.startswith("w/"):
            normalized = normalized[2:]
        if not normalized.endswith("/"):
            normalized += "/"

        doc_result = client.phriction.search_documents(
            constraints={"slugs": [normalized]},
            limit=1,
        )
        data = doc_result.get("data", [])
        if not data:
            return {
                "success": False,
                "error": f"Wiki page '{slug}' not found",
            }

        page_phid = data[0].get("phid")

        history_result = client.phriction.search_content(
            constraints={"documentPHIDs": [page_phid]},
            limit=limit,
        )

        history_result = _add_pagination_metadata(
            history_result, history_result.get("cursor")
        )

        return {"success": True, "history": history_result, "page_phid": page_phid}
