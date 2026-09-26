from typing import Any, Dict, List, Optional

from conduit.client.base import BasePhabricatorClient
from conduit.utils import build_search_params, build_transaction_params


class ProjectClient(BasePhabricatorClient):
    def search_projects(
        self, constraints: Dict[str, Any] = None, limit: int = 100
    ) -> Dict[str, Any]:
        """
        Search for projects.

        Args:
            constraints: Search constraints
            limit: Maximum number of results to return

        Returns:
            Search results with project data
        """
        params = build_search_params(
            constraints=constraints,
            limit=limit,
        )
        return self._make_request("project.search", params)

    def edit_project(
        self, transactions: List[Dict[str, Any]], object_identifier: str = None
    ) -> Dict[str, Any]:
        """
        Apply transactions to create a new project or edit an existing one.

        Args:
            transactions: List of transaction objects
            object_identifier: Existing project identifier to update

        Returns:
            Project data
        """
        params = build_transaction_params(
            transactions=transactions,
            object_identifier=object_identifier,
        )
        return self._make_request("project.edit", params)

    def create_project(
        self, name: str, description: str = "", icon: str = None, color: str = None
    ) -> Dict[str, Any]:
        """
        Create a new project.

        Args:
            name: Project name
            description: Project description
            icon: Project icon
            color: Project color

        Returns:
            Created project data
        """
        transactions = [{"type": "name", "value": name}]

        if description:
            transactions.append({"type": "description", "value": description})
        if icon:
            transactions.append({"type": "icon", "value": icon})
        if color:
            transactions.append({"type": "color", "value": color})

        return self.edit_project(transactions)

    def search_columns(
        self,
        constraints: Dict[str, Any] = None,
        limit: int = 100,
        after: Optional[str] = None,
        before: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Read information about workboard columns.

        Args:
            constraints: Search constraints
            limit: Maximum number of results to return
            after: Cursor for the next page
            before: Cursor for the previous page

        Returns:
            Column information
        """
        params = build_search_params(
            constraints=constraints,
            limit=limit,
            after=after,
            before=before,
        )
        return self._make_request("project.column.search", params)

    def query_projects(self, constraints: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Execute searches for Projects (legacy method).

        Args:
            constraints: Query constraints

        Returns:
            Query results
        """
        params = constraints or {}
        return self._make_request("project.query", params)

    def edit_column(
        self,
        column_phid: Optional[str] = None,
        project_phid: Optional[str] = None,
        name: Optional[str] = None,
        hidden: Optional[bool] = None,
        limit: Optional[int] = None,
        sequence: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Create or edit a workboard column.

        Args:
            column_phid: PHID of the column to edit. Omit to create a column.
            project_phid: PHID of the board, required when creating.
            name: New column name, required when creating.
            hidden: Whether the column is hidden from the board.
            limit: Point limit for the column, 0 to remove it.
            sequence: Position of the column on the board.

        Returns:
            The column's id, phid, name, hidden, sequence, pointLimit,
            isDefault and proxyPHID.
        """
        params = {
            "columnPHID": column_phid,
            "projectPHID": project_phid,
            "name": name,
            "hidden": hidden,
            "limit": limit,
            "sequence": sequence,
        }
        params = {k: v for k, v in params.items() if v is not None}

        return self._make_request("project.column.edit", params)
