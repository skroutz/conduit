import statistics
import time
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

from fastmcp import FastMCP

from conduit.client.types import (
    ManiphestSearchAttachments,
    ManiphestSearchConstraints,
    ManiphestTaskTransaction,
    ManiphestTaskTransactionColumn,
    ManiphestTaskTransactionComment,
    ManiphestTaskTransactionDescription,
    ManiphestTaskTransactionDueDate,
    ManiphestTaskTransactionOwner,
    ManiphestTaskTransactionPoints,
    ManiphestTaskTransactionPriority,
    ManiphestTaskTransactionProjectsAdd,
    ManiphestTaskTransactionProjectsRemove,
    ManiphestTaskTransactionProjectsSet,
    ManiphestTaskTransactionReference,
    ManiphestTaskTransactionStatus,
    ManiphestTaskTransactionTitle,
    UserSearchAttachments,
    UserSearchConstraints,
)
from conduit.client.unified import PhabricatorClient


from conduit.tools.handlers import handle_api_errors


# Pagination Functions


def _apply_smart_pagination(data: List[Any], limit: int = None) -> dict:
    """
    Apply smart pagination to data.

    Args:
        data: List of data items
        limit: Maximum number of items to return (optional)

    Returns:
        Paginated response with metadata
    """
    if limit is None:
        limit = 100  # Default limit

    # Apply limit if data is larger than limit
    if len(data) > limit:
        paginated_data = data[:limit]
        has_more = True
        total_count = len(data)
        suggestion = f"Use pagination to retrieve remaining {total_count - limit} items"
    else:
        paginated_data = data
        has_more = False
        total_count = len(data)
        suggestion = None

    return {
        "data": paginated_data,
        "pagination": {
            "total": total_count,
            "returned": len(paginated_data),
            "has_more": has_more,
        },
        "suggestion": suggestion,
    }


def optimize_token_usage(func: Callable) -> Callable:
    """
    Decorator to apply smart limits and truncation to search results.
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        result = func(*args, **kwargs)

        # Apply smart limits to search results
        if isinstance(result, dict) and "data" in result:
            # Check if this is a search result that needs limits
            data = result["data"]
            if isinstance(data, list) and len(data) > 50:
                # Apply smart pagination
                optimized_result = _apply_smart_pagination(
                    data, kwargs.get("limit", 100)
                )
                result.update(optimized_result)

        return result

    return wrapper


def _truncate_text_response(text: str, max_length: int = 2000) -> dict:
    """
    Truncate long text responses with helpful guidance.

    Args:
        text: The text to truncate
        max_length: Maximum allowed length

    Returns:
        Truncated response with guidance
    """
    if len(text) <= max_length:
        return {"content": text, "truncated": False}

    truncated_text = text[:max_length]
    remaining_length = len(text) - max_length

    return {
        "content": truncated_text,
        "truncated": True,
        "original_length": len(text),
        "remaining_length": remaining_length,
        "suggestion": f"Content was truncated. {remaining_length} characters remaining. Use specific search parameters to reduce results.",
    }


def _add_pagination_metadata(result: dict, cursor: dict = None) -> dict:
    """
    Add pagination metadata to search results.

    Args:
        result: Original search result
        cursor: Pagination cursor from API

    Returns:
        Result with enhanced pagination metadata
    """
    if cursor:
        result["pagination"] = {
            "cursor": cursor,
            "has_more": cursor.get("after") is not None,
            "limit": cursor.get("limit", 100),
        }

    return result


def _validate_task_page_request(
    limit: int,
    before: Optional[str],
    after: Optional[str],
) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer between 1 and 1000")

    if before is not None and after is not None:
        raise ValueError("before and after may not be provided together")

    if before is not None and not before:
        raise ValueError("before must be a non-empty opaque cursor")

    if after is not None and not after:
        raise ValueError("after must be a non-empty opaque cursor")


def _add_task_enumeration_metadata(result: dict, *, reverse: bool = False) -> dict:
    data = result.get("data")
    if not isinstance(data, list):
        raise ValueError("maniphest.search response must contain a data list")

    cursor = result.get("cursor")
    if not isinstance(cursor, dict):
        raise ValueError("maniphest.search response must contain a cursor object")

    for field in ("after", "before", "limit", "order"):
        if field not in cursor:
            raise ValueError(f"maniphest.search cursor.{field} is required")

    for field in ("after", "before"):
        value = cursor[field]
        if value is not None and not isinstance(value, str):
            raise ValueError(
                f"maniphest.search cursor.{field} must be a string or null"
            )

    cursor_limit = cursor["limit"]
    if isinstance(cursor_limit, bool):
        raise ValueError(
            "maniphest.search cursor.limit must be an integer between 1 and 1000"
        )
    if isinstance(cursor_limit, str) and cursor_limit.isdecimal():
        pagination_limit = int(cursor_limit)
    elif isinstance(cursor_limit, int):
        pagination_limit = cursor_limit
    else:
        raise ValueError(
            "maniphest.search cursor.limit must be an integer between 1 and 1000"
        )
    if not 1 <= pagination_limit <= 1000:
        raise ValueError(
            "maniphest.search cursor.limit must be an integer between 1 and 1000"
        )

    cursor_order = cursor["order"]
    if cursor_order is not None and not isinstance(cursor_order, str):
        raise ValueError("maniphest.search cursor.order must be a string or null")

    has_next = cursor["after"] is not None
    has_previous = cursor["before"] is not None
    result["pagination"] = {
        "cursor": cursor,
        "limit": cursor_limit,
        "returned": len(data),
        "has_more": has_next,
        "has_next": has_next,
        "has_previous": has_previous,
        "complete": not has_previous if reverse else not has_next,
    }
    return result


def _build_task_transactions(
    title: Optional[str] = None,
    description: Optional[str] = None,
    priority: Optional[str] = None,
    status: Optional[str] = None,
    owner_phid: Optional[str] = None,
    projects_add: Optional[List[str]] = None,
    projects_remove: Optional[List[str]] = None,
    projects_set: Optional[List[str]] = None,
    reference: Optional[str] = None,
    points: Optional[float] = None,
    column_phid: Optional[str] = None,
    comment: Optional[str] = None,
    due_date: Optional[int] = None,
) -> List[ManiphestTaskTransaction]:
    """Build maniphest.edit transactions for the fields that were provided."""
    transactions: List[ManiphestTaskTransaction] = []
    if title is not None:
        transactions.append(ManiphestTaskTransactionTitle(type="title", value=title))
    if description is not None:
        transactions.append(
            ManiphestTaskTransactionDescription(type="description", value=description)
        )
    if priority is not None:
        transactions.append(
            ManiphestTaskTransactionPriority(type="priority", value=priority)
        )
    if status is not None:
        transactions.append(ManiphestTaskTransactionStatus(type="status", value=status))
    if owner_phid is not None:
        transactions.append(
            ManiphestTaskTransactionOwner(type="owner", value=owner_phid)
        )
    if projects_add is not None:
        transactions.append(
            ManiphestTaskTransactionProjectsAdd(type="projects.add", value=projects_add)
        )
    if projects_remove is not None:
        transactions.append(
            ManiphestTaskTransactionProjectsRemove(
                type="projects.remove", value=projects_remove
            )
        )
    if projects_set is not None:
        transactions.append(
            ManiphestTaskTransactionProjectsSet(type="projects.set", value=projects_set)
        )
    if reference is not None:
        transactions.append(
            ManiphestTaskTransactionReference(
                type="custom.skroutz:reference", value=reference
            )
        )
    if due_date is not None:
        transactions.append(
            ManiphestTaskTransactionDueDate(
                type="custom.skroutz:due-date", value=due_date
            )
        )
    if points is not None:
        transactions.append(ManiphestTaskTransactionPoints(type="points", value=points))
    if column_phid is not None:
        transactions.append(
            ManiphestTaskTransactionColumn(type="column", value=column_phid)
        )
    if comment is not None:
        transactions.append(
            ManiphestTaskTransactionComment(type="comment", value=comment)
        )
    return transactions


def _normalize_task_id(task_id: str) -> str:
    """Strip a leading T from a task monogram ("T1234" -> "1234")."""
    if task_id and task_id[0] in ("T", "t") and task_id[1:].isdigit():
        return task_id[1:]
    return task_id


def _fetch_tasks(client: PhabricatorClient, task_ids: List[str]) -> Dict[str, dict]:
    """Load tasks by monogram, numeric ID or PHID, keyed by the given identifier."""
    ids = {}
    phids = {}
    for task_id in task_ids:
        normalized = _normalize_task_id(task_id)
        if normalized.isdigit():
            ids[int(normalized)] = task_id
        else:
            phids[normalized] = task_id

    tasks = []
    for key, values in (("ids", list(ids)), ("phids", list(phids))):
        for offset in range(0, len(values), 100):
            page = values[offset : offset + 100]
            after = None
            while True:
                result = client.maniphest.search_tasks(
                    constraints={key: page}, after=after, limit=100
                )
                tasks.extend(result.get("data", []))
                after = (result.get("cursor") or {}).get("after")
                if not after:
                    break

    found = {}
    for task in tasks:
        if task["id"] in ids:
            found[ids[task["id"]]] = task
        if task["phid"] in phids:
            found[phids[task["phid"]]] = task
    return found


def _task_field_diff(task: dict, changes: Dict[str, Any]) -> List[dict]:
    """Compare requested changes against a task's current field values."""
    fields = task.get("fields") or {}
    current = {
        "title": fields.get("name"),
        "description": (fields.get("description") or {}).get("raw"),
        "status": (fields.get("status") or {}).get("value"),
        "priority": (fields.get("priority") or {}).get("name"),
        "owner_phid": fields.get("ownerPHID"),
        "points": fields.get("points"),
    }

    diff = []
    for field, new_value in changes.items():
        old_value = current.get(field)
        # Priority is read back as a display name ("High") but written as a
        # keyword ("high"), so compare case-insensitively.
        if field == "priority" and isinstance(old_value, str):
            if old_value.lower() == str(new_value).lower():
                continue
        elif field in current and old_value == new_value:
            continue

        diff.append(
            {
                "task_id": "T{}".format(task["id"]),
                "field": field,
                "from": old_value,
                "to": new_value,
            }
        )
    return diff


def _project_task_fields(result: dict, fields: Optional[List[str]]) -> dict:
    """Keep only the named fields on each task, dropping the rest of the payload."""
    if not fields:
        return result

    projected = []
    for task in result.get("data") or []:
        task_fields = task.get("fields") or {}
        kept = {"id": task.get("id"), "phid": task.get("phid")}
        for field in fields:
            if field in ("id", "phid"):
                continue
            if field in task_fields:
                kept[field] = task_fields[field]
        projected.append(kept)

    result["data"] = projected
    return result


TaskGrouping = Literal[
    "column",
    "priority",
    "status",
    "owner",
    "project",
    "points",
    "staleness",
    "created_month",
    "closed_month",
]

DEFAULT_STALENESS_BUCKETS = [30, 90, 180, 365, 730]


def _epoch_month(timestamp: Optional[int]) -> Optional[str]:
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m")


def _staleness_bucket(days: float, buckets: List[int]) -> str:
    for bucket in buckets:
        if days < bucket:
            return "<{}d".format(bucket)
    return ">={}d".format(buckets[-1]) if buckets else "all"


def _task_group_keys(
    task: dict,
    group_by: TaskGrouping,
    buckets: List[int],
    now: float,
) -> List[Tuple[str, str]]:
    """Return the (key, label) pairs a task contributes to, usually just one."""
    fields = task.get("fields") or {}
    attachments = task.get("attachments") or {}

    if group_by == "status":
        status = fields.get("status") or {}
        return [(status.get("value") or "unknown", status.get("name") or "Unknown")]

    if group_by == "priority":
        priority = fields.get("priority") or {}
        return [
            (str(priority.get("value")), priority.get("name") or "Unknown"),
        ]

    if group_by == "owner":
        owner = fields.get("ownerPHID")
        return [(owner or "unassigned", owner or "Unassigned")]

    if group_by == "points":
        points = fields.get("points")
        return [("none" if points is None else str(points), str(points))]

    if group_by == "staleness":
        modified = fields.get("dateModified")
        if not modified:
            return [("unknown", "Unknown")]
        label = _staleness_bucket((now - modified) / 86400, buckets)
        return [(label, label)]

    if group_by == "created_month":
        month = _epoch_month(fields.get("dateCreated"))
        return [(month or "unknown", month or "Unknown")]

    if group_by == "closed_month":
        month = _epoch_month(fields.get("dateClosed"))
        return [(month or "open", month or "Still open")]

    if group_by == "project":
        phids = ((attachments.get("projects") or {}).get("projectPHIDs")) or []
        return [(phid, phid) for phid in phids] or [("none", "No projects")]

    if group_by == "column":
        boards = ((attachments.get("columns") or {}).get("boards")) or {}
        keys = []
        for board in boards.values():
            for column in board.get("columns") or []:
                keys.append((column.get("phid"), column.get("name") or "Unnamed"))
        return keys or [("none", "Not on a board")]

    raise ValueError("unsupported group_by: {}".format(group_by))


def _summarize_group(tasks: List[dict], now: float) -> dict:
    ages = []
    unassigned = 0
    with_points = 0
    for task in tasks:
        fields = task.get("fields") or {}
        if not fields.get("ownerPHID"):
            unassigned += 1
        if fields.get("points") is not None:
            with_points += 1
        created = fields.get("dateCreated")
        if created:
            ages.append((now - created) / 86400)

    return {
        "count": len(tasks),
        "unassigned": unassigned,
        "with_points": with_points,
        "median_age_days": round(statistics.median(ages), 1) if ages else None,
    }


def register_tools(  # noqa: C901
    mcp: FastMCP,
    get_client_func: Callable[[], PhabricatorClient],
) -> None:
    """
    Register all MCP tools with the FastMCP instance.

    Args:
        mcp: FastMCP instance to register tools with
        get_client_func: Function to get Phabricator client instance
    """

    @mcp.tool()
    @handle_api_errors
    def pha_user_whoami() -> dict:
        """
        Get the current user's information.

        Returns:
            User information
        """
        client = get_client_func()
        result = client.user.whoami()

        return {"success": True, "user": result}

    @mcp.tool()
    @handle_api_errors
    @optimize_token_usage
    def pha_user_search(
        query_key: str = "",
        ids: Optional[List[int]] = None,
        phids: Optional[List[str]] = None,
        usernames: Optional[List[str]] = None,
        name_like: str = "",
        is_admin: bool = None,
        is_disabled: bool = None,
        is_bot: bool = None,
        is_mailing_list: bool = None,
        needs_approval: bool = None,
        mfa: bool = None,
        created_start: int = None,
        created_end: int = None,
        fulltext_query: str = "",
        order: str = "",
        include_availability: bool = False,
        limit: int = 100,
    ) -> dict:
        """
        Search for users with advanced filtering capabilities.

        Args:
            query_key: Builtin query ("active", "admin", "all", "approval")
            ids: List of specific user IDs to search for
            phids: List of specific user PHIDs to search for
            usernames: List of exact usernames to find
            name_like: Find users whose usernames or real names contain this substring
            is_admin: Pass true to find only administrators, or false to omit administrators
            is_disabled: Pass true to find only disabled users, or false to omit disabled users
            is_bot: Pass true to find only bots, or false to omit bots
            is_mailing_list: Pass true to find only mailing lists, or false to omit mailing lists
            needs_approval: Pass true to find only users awaiting approval, or false to omit these users
            mfa: Pass true to find only users enrolled in MFA, or false to omit these users
            created_start: Unix timestamp - find users created after this time
            created_end: Unix timestamp - find users created before this time
            fulltext_query: Full-text search query string
            order: Result ordering ("newest", "oldest", "relevance")
            include_availability: Include user availability information in results
            limit: Maximum number of results to return (default: 100, max: 1000)

        Returns:
            Search results with user data and pagination metadata
        """
        # Initialize None parameters to empty lists
        if ids is None:
            ids = []
        if phids is None:
            phids = []
        if usernames is None:
            usernames = []

        client = get_client_func()

        # Build constraints
        constraints: UserSearchConstraints = {}

        if ids:
            constraints["ids"] = ids
        if phids:
            constraints["phids"] = phids
        if usernames:
            constraints["usernames"] = usernames
        if name_like:
            constraints["nameLike"] = name_like
        if is_admin is not None:
            constraints["isAdmin"] = is_admin
        if is_disabled is not None:
            constraints["isDisabled"] = is_disabled
        if is_bot is not None:
            constraints["isBot"] = is_bot
        if is_mailing_list is not None:
            constraints["isMailingList"] = is_mailing_list
        if needs_approval is not None:
            constraints["needsApproval"] = needs_approval
        if mfa is not None:
            constraints["mfa"] = mfa
        if created_start is not None:
            constraints["createdStart"] = created_start
        if created_end is not None:
            constraints["createdEnd"] = created_end
        if fulltext_query:
            constraints["query"] = fulltext_query

        # Build attachments
        attachments: UserSearchAttachments = {}
        if include_availability:
            attachments["availability"] = True

        # Call the search API
        result = client.user.search(
            query_key=query_key or None,
            constraints=constraints if constraints else None,
            attachments=attachments if attachments else None,
            order=order or None,
            limit=limit,
        )

        # Add pagination metadata
        result = _add_pagination_metadata(result, result.get("cursor"))

        return {"success": True, "users": result["data"], "cursor": result["cursor"]}

    @mcp.tool()
    @handle_api_errors
    def pha_task_create(
        title: str, description: str = "", owner_phid: str = ""
    ) -> dict:
        """
        Create a new Phabricator task.

        Args:
            title: Task title
            description: Task description
            owner_phid: PHID of the user to assign this task to

        Returns:
            Created task information
        """
        client = get_client_func()
        result = client.maniphest.create_task(
            title=title,
            description=description,
            owner_phid=owner_phid,
        )
        return {"success": True, "task": result}

    @mcp.tool()
    @handle_api_errors
    def pha_task_get(task_id: str) -> dict:
        """
        Get details of a specific Phabricator task

        Args:
            task_id: The numeric ID of the task to retrieve (e.g., 1234 or T1234)

        Returns:
            Task details
        """
        client = get_client_func()
        numeric_id = int(task_id.lstrip("Tt"))
        result = client.maniphest.get_task(numeric_id)
        return {"success": True, "task": result}

    @mcp.tool()
    @handle_api_errors
    def pha_task_update(
        task_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        priority: Optional[str] = None,
        status: Optional[str] = None,
        owner_phid: Optional[str] = None,
        projects_add: Optional[List[str]] = None,
        projects_remove: Optional[List[str]] = None,
        projects_set: Optional[List[str]] = None,
        reference: Optional[str] = None,
        due_date: Optional[int] = None,
        points: Optional[float] = None,
        column_phid: Optional[str] = None,
        comment: Optional[str] = None,
    ) -> dict:
        """
        Update the metadata of a Phabricator task.

        Args:
            task_id: The ID, PHID of the task to update.
            title: The new title for the task.
            description: The new description for the task.
            priority: The new priority for the task.
            status: The new status for the task.
            owner_phid: The PHID of the new owner for the task.
            projects_add: List of project PHIDs to add the task to.
            projects_remove: List of project PHIDs to remove the task from.
            projects_set: List of project PHIDs to set (overwrites current projects).
            reference: The new value for the task's Reference custom field.
            due_date: The new value for the task's Due Date custom field, as a Unix epoch timestamp.
            points: The new story point value for the task.
            column_phid: PHID of the workboard column to move the task into.
            comment: A comment to post along with the update.

        Returns:
            Success status.
        """
        client = get_client_func()

        transactions = _build_task_transactions(
            title=title,
            description=description,
            priority=priority,
            status=status,
            owner_phid=owner_phid,
            projects_add=projects_add,
            projects_remove=projects_remove,
            projects_set=projects_set,
            reference=reference,
            points=points,
            column_phid=column_phid,
            comment=comment,
            due_date=due_date,
        )

        client.maniphest.edit_task(
            object_identifier=task_id,
            transactions=transactions,
        )
        return {"success": True}

    @mcp.tool()
    @handle_api_errors
    def pha_task_aggregate(
        projects: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        group_by: TaskGrouping = "status",
        staleness_buckets: Optional[List[int]] = None,
        created_after: Optional[int] = None,
        created_before: Optional[int] = None,
        modified_after: Optional[int] = None,
        modified_before: Optional[int] = None,
        max_tasks: int = 5000,
    ) -> dict:
        """
        Count tasks by group without returning the tasks themselves.

        Args:
            projects: Project slugs or PHIDs. Descendant projects match too,
                as they do in Conduit.
            statuses: Task statuses to include, such as ["open"].
            group_by: What to count by. "column" and "project" read the
                board and project attachments, so a task on several boards is
                counted once per column.
            staleness_buckets: Day boundaries for group_by="staleness",
                defaulting to [30, 90, 180, 365, 730].
            created_after: Unix timestamp; tasks created at or after it.
            created_before: Unix timestamp; tasks created at or before it.
            modified_after: Unix timestamp; tasks modified at or after it.
            modified_before: Unix timestamp; tasks modified at or before it.
            max_tasks: Stop after reading this many tasks, so an unbounded
                query cannot page forever.

        Returns:
            The total task count and per-group counts, each with how many are
            unassigned, how many carry points, and the median task age in days.
            Owner and project groups are keyed by PHID, not by name.
        """
        buckets = sorted(staleness_buckets or DEFAULT_STALENESS_BUCKETS)

        constraints: ManiphestSearchConstraints = {}
        if projects:
            constraints["projects"] = projects
        if statuses:
            constraints["statuses"] = statuses
        if created_after is not None:
            constraints["createdStart"] = created_after
        if created_before is not None:
            constraints["createdEnd"] = created_before
        if modified_after is not None:
            constraints["modifiedStart"] = modified_after
        if modified_before is not None:
            constraints["modifiedEnd"] = modified_before

        attachments: ManiphestSearchAttachments = {}
        if group_by == "column":
            attachments["columns"] = True
        if group_by == "project":
            attachments["projects"] = True

        client = get_client_func()

        tasks = []
        after = None
        while len(tasks) < max_tasks:
            result = client.maniphest.search_tasks(
                constraints=constraints or None,
                attachments=attachments or None,
                after=after,
                limit=min(100, max_tasks - len(tasks)),
            )
            tasks.extend(result.get("data") or [])

            after = (result.get("cursor") or {}).get("after")
            if not after:
                break

        now = time.time()
        grouped: Dict[str, List[dict]] = {}
        labels: Dict[str, str] = {}
        for task in tasks:
            for key, label in _task_group_keys(task, group_by, buckets, now):
                grouped.setdefault(key, []).append(task)
                labels[key] = label

        groups = [
            dict(key=key, label=labels[key], **_summarize_group(members, now))
            for key, members in grouped.items()
        ]
        groups.sort(key=lambda group: group["count"], reverse=True)

        return {
            "success": True,
            "total": len(tasks),
            "truncated": after is not None,
            "group_by": group_by,
            "groups": groups,
        }

    @mcp.tool()
    @handle_api_errors
    def pha_task_bulk_update(
        task_ids: List[str],
        dry_run: bool = True,
        priority: Optional[str] = None,
        status: Optional[str] = None,
        owner_phid: Optional[str] = None,
        projects_add: Optional[List[str]] = None,
        projects_remove: Optional[List[str]] = None,
        projects_set: Optional[List[str]] = None,
        points: Optional[float] = None,
        column_phid: Optional[str] = None,
        comment: Optional[str] = None,
    ) -> dict:
        """
        Apply the same update to many Phabricator tasks, previewing by default.

        Args:
            task_ids: Task monograms, numeric IDs or PHIDs, at most 500.
            dry_run: When true (the default) nothing is written: the change set
                is computed and returned for review.
            priority: The new priority for every task.
            status: The new status for every task.
            owner_phid: The PHID of the new owner for every task.
            projects_add: List of project PHIDs to add to every task.
            projects_remove: List of project PHIDs to remove from every task.
            projects_set: List of project PHIDs to set on every task.
            points: The new story point value for every task.
            column_phid: PHID of the workboard column to move every task into.
            comment: A comment to post on every task.

        Returns:
            The change set as would_change, whether it was applied, and per-task
            errors. Tasks that could not be loaded are reported in errors.
        """
        if not task_ids:
            raise ValueError("task_ids must not be empty")
        if len(task_ids) > 500:
            raise ValueError("task_ids accepts at most 500 tasks per call")

        transactions = _build_task_transactions(
            priority=priority,
            status=status,
            owner_phid=owner_phid,
            projects_add=projects_add,
            projects_remove=projects_remove,
            projects_set=projects_set,
            points=points,
            column_phid=column_phid,
            comment=comment,
        )
        if not transactions:
            raise ValueError("no fields to update were provided")

        client = get_client_func()
        tasks = _fetch_tasks(client, task_ids)

        # Fields whose current value the search response exposes, so a dry run
        # can show what the edit would actually change.
        changes = {
            field: value
            for field, value in (
                ("priority", priority),
                ("status", status),
                ("owner_phid", owner_phid),
                ("points", points),
            )
            if value is not None
        }

        would_change = []
        errors = []
        for task_id in task_ids:
            task = tasks.get(task_id)
            if task is None:
                errors.append({"task_id": task_id, "error": "task not found"})
                continue
            would_change.extend(_task_field_diff(task, changes))

        if dry_run:
            return {
                "success": True,
                "applied": False,
                "would_change": would_change,
                "errors": errors,
            }

        applied_to = []
        for task_id, task in tasks.items():
            try:
                client.maniphest.edit_task(
                    object_identifier=task["phid"],
                    transactions=transactions,
                )
                applied_to.append(task_id)
            except Exception as exc:  # noqa: BLE001 - reported per task
                errors.append({"task_id": task_id, "error": str(exc)})

        return {
            "success": not errors,
            "applied": True,
            "applied_to": applied_to,
            "would_change": would_change,
            "errors": errors,
        }

    @mcp.tool()
    @handle_api_errors
    def pha_task_add_comment(task_id: str, comment: str) -> dict:
        """
        Add a comment to a Phabricator task.

        Args:
            task_id: The ID, PHID of the task to add the comment to.
            comment: The content of the comment.

        Returns:
            Success status.
        """
        client = get_client_func()
        client.maniphest.edit_task(
            object_identifier=task_id,
            transactions=[
                ManiphestTaskTransactionComment(
                    type="comment",
                    value=comment,
                )
            ],
        )
        return {"success": True}

    @mcp.tool()
    @handle_api_errors
    def pha_task_edit_comment(transaction_phid: str, comment: str) -> dict:
        """
        Edit an existing comment on a Phabricator task.

        Args:
            transaction_phid: The PHID of the comment transaction to edit
                (e.g. "PHID-XACT-TASK-xxx"), as returned by
                pha_task_get_transactions.
            comment: The new content of the comment.

        Returns:
            Success status.
        """
        client = get_client_func()
        client.maniphest.edit_comment(
            transaction_phid=transaction_phid,
            content=comment,
        )
        return {"success": True}

    @mcp.tool()
    @handle_api_errors
    @optimize_token_usage
    def pha_task_get_transactions(task_id: str) -> dict:
        """
        Get transaction history for a Phabricator task, including comments.

        Args:
            task_id: The numeric ID or PHID of the task to retrieve transactions for (e.g., "1234" or "PHID-TASK-xxx")

        Returns:
            Transaction history with all changes and comments for the task
        """
        client = get_client_func()

        # Strip T/t prefix if present (e.g. "T244969" → "244969")
        if task_id and task_id[0] in ("T", "t") and task_id[1:].isdigit():
            task_id = task_id[1:]

        # If task_id is a numeric ID, get PHID first
        if task_id.isdigit():
            task_id_int = int(task_id)
            # Search for task to get PHID
            task_result = client.maniphest.search_tasks(
                constraints={"ids": [task_id_int]}, limit=1
            )

            if not task_result.get("data"):
                return {"success": False, "error": f"Task with ID {task_id} not found"}

            task_phid = task_result["data"][0]["phid"]
        else:
            # Assume it's already a PHID
            task_phid = task_id

        # Use modern transaction.search API
        result = client.maniphest.search_task_transactions(
            task_phid=task_phid, limit=100
        )

        return {"success": True, "transactions": result}

    @mcp.tool()
    @handle_api_errors
    def pha_task_get_personal(
        task_type: Literal["assigned", "authored"] = "assigned",
        include_projects: bool = True,
        include_subscribers: bool = False,
        limit: int = 50,
    ) -> dict:
        """
        Get personal tasks assigned to or authored by the current user.

        Args:
            task_type: Type of tasks to retrieve ("assigned" or "authored")
            include_projects: Include project information in results
            include_subscribers: Include subscriber information in results
            limit: Maximum number of results to return

        Returns:
            Personal tasks based on the specified type
        """
        client = get_client_func()

        attachments: ManiphestSearchAttachments = {}
        if include_projects:
            attachments["projects"] = True
        if include_subscribers:
            attachments["subscribers"] = True

        if task_type == "assigned":
            result = client.maniphest.search_assigned_tasks(
                attachments=attachments if attachments else None, limit=limit
            )
            return {"success": True, "assigned_tasks": result}
        elif task_type == "authored":
            result = client.maniphest.search_authored_tasks(
                attachments=attachments if attachments else None, limit=limit
            )
            return {"success": True, "authored_tasks": result}
        else:
            return {
                "success": False,
                "error": "Invalid task_type. Use 'assigned' or 'authored'",
            }

    @mcp.tool()
    @handle_api_errors
    def pha_task_update_relationships(
        task_id: str,
        relationship_type: Literal["subtask", "parent"],
        target_ids: str,
    ) -> dict:
        """
        Update task relationships (subtasks or parents).

        Args:
            task_id: The PHID of the task to update (must be PHID format, not numeric ID)
            relationship_type: Type of relationship ("subtask" or "parent")
            target_ids: Comma-separated list of target task PHIDs (must be PHID format, not numeric IDs)

        Returns:
            Success status
        """
        client = get_client_func()

        # Parse comma-separated target IDs
        target_list = [
            target.strip() for target in target_ids.split(",") if target.strip()
        ]

        if not target_list:
            return {"success": False, "error": "No valid target IDs provided"}

        if relationship_type == "subtask":
            transaction_type = "subtasks.set"
        elif relationship_type == "parent":
            transaction_type = "parents.set"
        else:
            return {
                "success": False,
                "error": "Invalid relationship_type. Use 'subtask' or 'parent'",
            }

        client.maniphest.edit_task(
            object_identifier=task_id,
            transactions=[
                {
                    "type": transaction_type,
                    "value": target_list,
                }
            ],
        )
        return {"success": True}

    @mcp.tool()
    @handle_api_errors
    @optimize_token_usage
    def pha_task_search_advanced(
        query_key: str = "",
        assigned: Optional[List[str]] = None,
        author_phids: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        priorities: Optional[List[int]] = None,
        projects: Optional[List[str]] = None,
        subscribers: Optional[List[str]] = None,
        fulltext_query: str = "",
        has_parents: bool = None,
        has_subtasks: bool = None,
        created_after: int = None,
        created_before: int = None,
        modified_after: int = None,
        modified_before: int = None,
        order: str = "",
        include_subscribers: bool = False,
        include_projects: bool = False,
        include_columns: bool = False,
        limit: int = 100,
        preset: Literal[
            "all", "assigned", "authored", "open", "high_priority", "recent"
        ] = None,
        closed_after: Optional[int] = None,
        closed_before: Optional[int] = None,
        before: Optional[str] = None,
        after: Optional[str] = None,
        fields: Optional[List[str]] = None,
    ) -> dict:
        """
        Advanced task search with filtering and preset options. For a cursor page,
        repeat all query-defining arguments unchanged and replace only the cursor.
        Pagination preserves the upstream cursor limit as an integer or decimal string.
        Complete is true when no page remains in the requested direction.

        Args:
            query_key: Builtin query ("assigned", "authored", "subscribed", "open", "all")
            assigned: List of usernames or PHIDs of assignees
            author_phids: List of PHIDs of task authors
            statuses: List of task statuses to filter by
            priorities: List of priority levels to filter by
            projects: List of project names or PHIDs to filter by
            subscribers: List of subscriber usernames or PHIDs
            fulltext_query: Full-text search query string
            has_parents: Filter by whether tasks have parent tasks
            has_subtasks: Filter by whether tasks have subtasks
            created_after: Unix timestamp - tasks created after this time
            created_before: Unix timestamp - tasks created before this time
            modified_after: Unix timestamp - tasks modified after this time
            modified_before: Unix timestamp - tasks modified before this time
            order: Result ordering ("priority", "updated", "newest", "oldest", "closed", "title", "relevance")
            include_subscribers: Include subscriber information in results
            include_projects: Include project information in results
            include_columns: Include workboard column information in results
            limit: Number of tasks to request, from 1 through 1000. The configured
                Phabricator server may enforce a lower maximum.
            preset: Preset search configurations for common use cases. The
                dynamic "recent" preset cannot be combined with a cursor.
            closed_after: Unix timestamp; include tasks closed at or after it.
            closed_before: Unix timestamp; include tasks closed at or before it.
            before: Opaque cursor from cursor.before for the previous page. Repeat
                all query-defining arguments unchanged from the prior request.
            after: Opaque cursor from cursor.after for the next page. Repeat all
                query-defining arguments unchanged from the prior request.
            fields: Task fields to keep, such as ["ownerPHID", "points",
                "dateModified"]. Omit to return the full task payload. Each
                task always keeps its id and phid.

        Returns:
            Search results with task data and pagination metadata
        """
        _validate_task_page_request(limit, before, after)
        if preset == "recent" and (before is not None or after is not None):
            raise ValueError(
                'preset "recent" cannot be combined with before or after cursors'
            )

        # Initialize None parameters to empty lists
        if assigned is None:
            assigned = []
        if author_phids is None:
            author_phids = []
        if statuses is None:
            statuses = []
        if priorities is None:
            priorities = []
        if projects is None:
            projects = []
        if subscribers is None:
            subscribers = []

        client = get_client_func()

        # Handle preset configurations
        if preset:
            if preset == "assigned":
                query_key = "assigned"
                if not assigned:
                    # Get current user for assigned tasks
                    user_info = client.user.whoami()
                    assigned = [user_info["phid"]]
            elif preset == "authored":
                query_key = "authored"
                if not author_phids:
                    # Get current user for authored tasks
                    user_info = client.user.whoami()
                    author_phids = [user_info["phid"]]
            elif preset == "high_priority":
                priorities = [90, 100]  # High and Unbreak Now priorities
                order = "priority"
            elif preset == "recent":
                import time

                modified_after = int(time.time()) - (7 * 24 * 60 * 60)  # Last 7 days
                order = "updated"
            elif preset == "open":
                statuses = ["open"]
            elif preset == "all":
                query_key = "all"

        # Build constraints
        constraints: ManiphestSearchConstraints = {}

        if assigned:
            constraints["assigned"] = assigned
        if author_phids:
            constraints["authorPHIDs"] = author_phids
        if statuses:
            constraints["statuses"] = statuses
        if priorities:
            constraints["priorities"] = priorities
        if projects:
            constraints["projects"] = projects
        if subscribers:
            constraints["subscribers"] = subscribers
        if fulltext_query:
            constraints["query"] = fulltext_query
        if has_parents is not None:
            constraints["hasParents"] = has_parents
        if has_subtasks is not None:
            constraints["hasSubtasks"] = has_subtasks
        if created_after:
            constraints["createdStart"] = created_after
        if created_before:
            constraints["createdEnd"] = created_before
        if modified_after:
            constraints["modifiedStart"] = modified_after
        if modified_before:
            constraints["modifiedEnd"] = modified_before
        if closed_after is not None:
            constraints["closedStart"] = closed_after
        if closed_before is not None:
            constraints["closedEnd"] = closed_before

        # Build attachments
        attachments: ManiphestSearchAttachments = {}
        if include_subscribers:
            attachments["subscribers"] = True
        if include_projects:
            attachments["projects"] = True
        if include_columns:
            attachments["columns"] = True

        result = client.maniphest.search_tasks(
            query_key=query_key or None,
            constraints=constraints if constraints else None,
            attachments=attachments if attachments else None,
            order=order or None,
            before=before,
            after=after,
            limit=limit,
        )

        result = _add_task_enumeration_metadata(result, reverse=before is not None)
        result = _project_task_fields(result, fields)

        return {"success": True, "results": result}

    # Diffusion (Repository) Tools

    @mcp.tool()
    @handle_api_errors
    @optimize_token_usage
    def pha_repository_search(
        constraints: Dict[str, Any] = None,
        limit: int = 50,
    ) -> dict:
        """
        Search for repositories in Phabricator.

        Args:
            constraints: Search constraints dictionary (e.g., {"query": "repo_name", "vcs": "git"})
            limit: Maximum number of results to return (default: 50, max: 500)

        Returns:
            Repository search results with data list and pagination metadata
        """
        client = get_client_func()

        if constraints is None:
            constraints = {}

        result = client.diffusion.search_repositories(
            constraints=constraints if constraints else None, limit=limit
        )

        # Add pagination metadata
        result = _add_pagination_metadata(result, result.get("cursor"))

        return {"success": True, "repositories": result}

    @mcp.tool()
    @handle_api_errors
    def pha_repository_create(
        name: str,
        vcs_type: str = "git",
        description: str = "",
        callsign: str = "",
    ) -> dict:
        """
        Create a new repository in Phabricator.

        Args:
            name: Repository name
            vcs_type: Version control system type ("git", "hg", "svn")
            description: Repository description
            callsign: Optional repository callsign

        Returns:
            Created repository information
        """
        client = get_client_func()

        result = client.diffusion.create_repository(
            name=name,
            vcs_type=vcs_type,
            description=description,
            callsign=callsign if callsign else None,
        )

        return {"success": True, "repository": result}

    @mcp.tool()
    @handle_api_errors
    def pha_repository_info(repository_identifier: str) -> dict:
        """
        Get detailed information about a specific repository.

        Args:
            repository_identifier: Repository ID (numeric or string), PHID, callsign, or name

        Returns:
            Repository information
        """
        client = get_client_func()

        # Try different search strategies based on identifier format
        result = None

        # 1. If it looks like a PHID, search by PHID
        if repository_identifier.startswith("PHID-REPO-"):
            result = client.diffusion.search_repositories(
                constraints={"phids": [repository_identifier]},
                limit=1,
            )

        # 2. If it's numeric, search by ID
        elif repository_identifier.isdigit():
            result = client.diffusion.search_repositories(
                constraints={"ids": [int(repository_identifier)]},
                limit=1,
            )

        # 3. If it's all uppercase, likely a callsign
        elif repository_identifier.isupper() and repository_identifier.isalpha():
            result = client.diffusion.search_repositories(
                constraints={"callsigns": [repository_identifier]},
                limit=1,
            )

        # 4. Try searching by short name
        if not result or not result.get("data"):
            try:
                result = client.diffusion.search_repositories(
                    constraints={"shortNames": [repository_identifier]},
                    limit=1,
                )
            except Exception:  # nosec B110 - intentional fallback to next lookup strategy
                # shortNames constraint might fail, continue to next strategy
                pass

        # 5. If still no results, do a general search and filter by name
        if not result or not result.get("data"):
            # Search all repositories and find by name match
            all_repos = client.diffusion.search_repositories(limit=100)
            for repo in all_repos.get("data", []):
                fields = repo.get("fields", {})
                if (
                    fields.get("name") == repository_identifier
                    or fields.get("shortName") == repository_identifier
                    or fields.get("callsign") == repository_identifier
                ):
                    result = {"data": [repo]}
                    break

        if result and result.get("data"):
            return {"success": True, "repository": result["data"][0]}
        else:
            return {
                "success": False,
                "error": f"Repository '{repository_identifier}' not found",
            }

    @mcp.tool()
    @handle_api_errors
    @optimize_token_usage
    def pha_repository_browse(
        repository: str,
        path: str = "/",
        commit: str = "",
    ) -> dict:
        """
        Browse files and directories in a repository.

        Args:
            repository: Repository identifier (PHID, callsign, or name)
            path: Path to browse (default: root "/")
            commit: Specific commit to browse (default: latest)

        Returns:
            List of files and directories at the specified path with pagination metadata
        """
        client = get_client_func()

        result = client.diffusion.browse_query(
            repository=repository,
            path=path if path else "/",
            commit=commit if commit else None,
        )

        # Add pagination metadata
        result = _add_pagination_metadata(result, result.get("cursor"))

        return {"success": True, "browse_result": result}

    @mcp.tool()
    @handle_api_errors
    def pha_repository_file_content(
        repository: str,
        file_path: str,
        commit: str = "",
    ) -> dict:
        """
        Get the content of a specific file from a repository.

        Args:
            repository: Repository identifier (PHID, callsign, or name)
            file_path: Path to the file
            commit: Specific commit (default: latest)

        Returns:
            File content and metadata
        """
        import base64

        client = get_client_func()

        # Step 1: Get file PHID from repository
        file_info = client.diffusion.file_content_query(
            repository=repository, path=file_path, commit=commit if commit else None
        )

        # Step 2: Download actual file content using the file PHID
        file_phid = file_info.get("filePHID")
        if not file_phid:
            return {
                "success": False,
                "error": "File PHID not found in repository query result",
                "file_info": file_info,
            }

        # Download the actual file content
        download_result = client.file.download_file(file_phid=file_phid)

        # Step 3: Decode base64 content if returned
        file_content = download_result
        if isinstance(download_result, str) and download_result:
            try:
                file_content = base64.b64decode(download_result).decode("utf-8")
            except Exception:
                # If decoding fails, keep original content
                file_content = download_result

        # Combine metadata with actual decoded content
        return {"success": True, "file_content": file_content, "metadata": file_info}

    @mcp.tool()
    @handle_api_errors
    def pha_file_download(file_identifier: str) -> dict:
        """
        Download a file attachment from Phabricator.

        Useful for retrieving files attached to Maniphest tasks. The content is
        returned base64-encoded so binary files (images, PDFs, archives) are
        preserved intact; the caller is responsible for decoding it.

        Args:
            file_identifier: File PHID (PHID-FILE-...) or monogram/ID (F123 or 123)

        Returns:
            File metadata and base64-encoded content
        """
        client = get_client_func()

        file_phid = client.file.resolve_file_phid(file_identifier)
        info = client.file.get_file_info(file_phid)
        download_result = client.file.download_file(file_phid=file_phid)

        # file.download returns the raw base64 payload, either as a bare string
        # or wrapped in a dict under "data_base64" depending on the endpoint.
        if isinstance(download_result, dict):
            data_base64 = download_result.get("data_base64")
        else:
            data_base64 = download_result

        return {
            "success": True,
            "file": {
                "phid": file_phid,
                "name": info.get("name"),
                "size": info.get("size"),
                "mimeType": info.get("mimeType"),
                "data_base64": data_base64,
            },
        }

    @mcp.tool()
    @handle_api_errors
    @optimize_token_usage
    def pha_repository_history(
        repository: str,
        path: str = "",
        commit: str = "",
        limit: int = 20,
    ) -> dict:
        """
        Get commit history for a repository or specific path.

        Args:
            repository: Repository identifier (PHID, callsign, or name)
            path: Specific path to get history for (optional)
            commit: Starting commit (default: latest)
            limit: Maximum number of commits to return (default: 20, max: 100)

        Returns:
            Commit history with pagination metadata
        """
        client = get_client_func()

        result = client.diffusion.history_query(
            repository=repository,
            path=path if path else None,
            commit=commit if commit else None,
            limit=limit,
        )

        # Add pagination metadata
        result = _add_pagination_metadata(result, result.get("cursor"))

        return {"success": True, "history": result}

    @mcp.tool()
    @handle_api_errors
    def pha_repository_branches(repository: str) -> dict:
        """
        Get all branches in a repository.

        Args:
            repository: Repository identifier (PHID, callsign, or name)

        Returns:
            List of branches
        """
        client = get_client_func()

        result = client.diffusion.branch_query(repository=repository)

        return {"success": True, "branches": result}

    @mcp.tool()
    @handle_api_errors
    def pha_repository_commits_search(
        repository: str = "",
        author: str = "",
        message_contains: str = "",
        limit: int = 20,
    ) -> dict:
        """
        Search for commits across repositories.

        Args:
            repository: Repository identifier to search in (optional)
            author: Filter by commit author
            message_contains: Filter by commit message containing this text
            limit: Maximum number of results to return

        Returns:
            List of matching commits
        """
        client = get_client_func()

        constraints = {}
        if repository:
            constraints["repositories"] = [repository]
        if author:
            constraints["authors"] = [author]
        if message_contains:
            constraints["query"] = message_contains

        result = client.diffusion.search_commits(
            constraints=constraints if constraints else None, limit=limit
        )

        return {"success": True, "commits": result}

    # Differential (Code Review) Tools

    @mcp.tool()
    @handle_api_errors
    def pha_diff_create_from_content(
        diff_content: str,
        repository: str = "",
    ) -> dict:
        """
        Create a diff from raw diff content.

        Args:
            diff_content: Raw unified diff content
            repository: Repository identifier to associate with (optional)

        Returns:
            Created diff information
        """
        client = get_client_func()

        repository_phid = None
        if repository:
            # Try to resolve repository to PHID
            try:
                repos = client.diffusion.search_repositories(
                    constraints={"query": repository},
                    limit=1,
                )
                if repos.get("data"):
                    repository_phid = repos["data"][0]["phid"]
            except Exception:
                # If query search fails, try direct PHID
                if repository.startswith("PHID-"):
                    repository_phid = repository

        result = client.differential.create_raw_diff(
            diff=diff_content, repository_phid=repository_phid
        )

        return {"success": True, "diff": result}

    @mcp.tool()
    @handle_api_errors
    def pha_diff_create(
        diff_id: str,
        title: str,
        summary: str = "",
        test_plan: str = "",
        reviewers: Optional[List[str]] = None,
    ) -> dict:
        """
        Create a new code review (Differential revision).

        Args:
            diff_id: PHID of the diff to review (use pha_diff_create_from_content to create a diff first)
            title: Review title
            summary: Detailed description of the changes
            test_plan: How the changes were tested
            reviewers: List of reviewer usernames or PHIDs

        Returns:
            Created revision information
        """
        # Initialize None parameters to empty lists
        if reviewers is None:
            reviewers = []

        client = get_client_func()

        transactions = [
            {"type": "title", "value": title},
            {"type": "update", "value": diff_id},
        ]

        if summary:
            transactions.append({"type": "summary", "value": summary})
        if test_plan:
            transactions.append({"type": "testPlan", "value": test_plan})
        if reviewers:
            transactions.append({"type": "reviewers.add", "value": reviewers})

        result = client.differential.edit_revision(transactions=transactions)

        return {"success": True, "revision": result}

    @mcp.tool()
    @handle_api_errors
    @optimize_token_usage
    def pha_diff_search(
        author: str = "",
        reviewer: str = "",
        status: str = "",
        repository: str = "",
        title_contains: str = "",
        limit: int = 50,
    ) -> dict:
        """
        Search for code reviews (Differential revisions).

        Args:
            author: Filter by author username or PHID
            reviewer: Filter by reviewer username or PHID
            status: Filter by status ("open", "closed", "abandoned", "accepted")
            repository: Filter by repository PHID (recommended) or name
            title_contains: Filter by title containing this text
            limit: Maximum number of results to return (default: 50, max: 500)

        Returns:
            List of matching code reviews with pagination metadata
        """
        client = get_client_func()

        constraints = {}
        if author:
            constraints["authorPHIDs"] = [author]
        if reviewer:
            constraints["reviewerPHIDs"] = [reviewer]
        if status:
            constraints["statuses"] = [status]
        if repository:
            constraints["repositoryPHIDs"] = [repository]
        if title_contains:
            constraints["query"] = title_contains

        result = client.differential.search_revisions(
            constraints=constraints if constraints else None, limit=limit
        )

        # Add pagination metadata
        result = _add_pagination_metadata(result, result.get("cursor"))

        return {"success": True, "revisions": result}

    @mcp.tool()
    @handle_api_errors
    def pha_diff_get(revision_id: str) -> dict:
        """
        Get detailed information about a specific code review including all diffs.

        Args:
            revision_id: Revision ID (e.g., "D123") or PHID

        Returns:
            Detailed revision information with all associated diffs
        """
        client = get_client_func()

        # Parse revision ID if in "D123" format
        if revision_id.startswith("D"):
            revision_id = revision_id[1:]

        result = client.differential.search_revisions(
            constraints={"ids": [int(revision_id)]}, limit=1
        )

        if result.get("data"):
            revision = result["data"][0]

            # Get all diffs associated with this revision
            diffs = client.differential.search_diffs(
                constraints={"revisionPHIDs": [revision["phid"]]},
                limit=50,  # Allow many diffs for active revisions
            )

            # Add diffs to revision information
            revision["all_diffs"] = diffs.get("data", [])

            return {"success": True, "revision": revision}
        else:
            return {"success": False, "error": f"Revision {revision_id} not found"}

    @mcp.tool()
    @handle_api_errors
    def pha_diff_add_comment(
        revision_id: str,
        comment: str,
        action: str = "comment",
    ) -> dict:
        """
        Add a comment to a code review.

        Args:
            revision_id: Revision ID (e.g., "D123") or PHID
            comment: Comment text
            action: Review action ("comment", "accept", "reject", "request-changes")

        Returns:
            Success status
        """
        client = get_client_func()

        transactions = [{"type": "comment", "value": comment}]

        if action == "accept":
            transactions.append({"type": "accept", "value": True})
        elif action == "reject":
            transactions.append({"type": "reject", "value": True})
        elif action == "request-changes":
            transactions.append({"type": "request-changes", "value": True})

        client.differential.edit_revision(
            transactions=transactions, object_identifier=revision_id
        )

        return {"success": True, "comment_added": True}

    @mcp.tool()
    @handle_api_errors
    def pha_diff_update(
        revision_id: str,
        new_diff_id: str = "",
        title: str = "",
        summary: str = "",
        test_plan: str = "",
        comment: str = "",
    ) -> dict:
        """
        Update an existing code review with new diff or metadata.

        Args:
            revision_id: Revision ID (e.g., "D123") or PHID
            new_diff_id: New diff PHID to update the review with
            title: New title (optional)
            summary: New summary (optional)
            test_plan: New test plan (optional)
            comment: Comment explaining the update

        Returns:
            Updated revision information
        """
        client = get_client_func()

        transactions = []

        if new_diff_id:
            transactions.append({"type": "update", "value": new_diff_id})
        if title:
            transactions.append({"type": "title", "value": title})
        if summary:
            transactions.append({"type": "summary", "value": summary})
        if test_plan:
            transactions.append({"type": "testPlan", "value": test_plan})
        if comment:
            transactions.append({"type": "comment", "value": comment})

        if not transactions:
            return {"success": False, "error": "No updates specified"}

        result = client.differential.edit_revision(
            transactions=transactions, object_identifier=revision_id
        )

        return {"success": True, "revision": result}

    @mcp.tool()
    @handle_api_errors
    def pha_diff_get_content(diff_phid: str) -> dict:
        """
        Get the raw content of a diff using its PHID.

        Args:
            diff_phid: Diff PHID in format "PHID-DIFF-xxxxxxxxxxxxxxxxxxxx".
                     Use `pha_diff_get` first to get revision info, then extract the diffPHID
                     from the revision.fields.diffPHID field.

        Returns:
            Raw diff content
        """
        client = get_client_func()

        # Validate PHID format
        if not diff_phid.startswith("PHID-DIFF-"):
            return {
                "success": False,
                "error": f"Invalid diff PHID format: {diff_phid}. Expected format: PHID-DIFF-xxxxxxxxxxxxxxxxxxxx",
                "error_code": "INVALID_PHID_FORMAT",
            }

        # Search for diff by PHID to get numeric ID
        diffs = client.differential.search_diffs(
            constraints={"phids": [diff_phid]}, limit=1
        )

        if not diffs.get("data"):
            return {
                "success": False,
                "error": f"Diff not found with PHID: {diff_phid}",
                "error_code": "DIFF_NOT_FOUND",
            }

        # Extract numeric ID and get raw diff content
        diff_data = diffs["data"][0]
        numeric_diff_id = diff_data["id"]

        result = client.differential.get_raw_diff(diff_id=numeric_diff_id)

        return {"success": True, "diff_content": result}

    @mcp.tool()
    @handle_api_errors
    def pha_diff_get_commit_message(revision_id: str) -> dict:
        """
        Get a commit message template for a code review.

        Args:
            revision_id: Revision ID (e.g., "D123") or PHID

        Returns:
            Formatted commit message template
        """
        client = get_client_func()

        # Parse revision ID if in "D123" format
        if revision_id.startswith("D"):
            revision_id = revision_id[1:]

        result = client.differential.get_commit_message(revision_id=int(revision_id))

        return {"success": True, "commit_message": result}

    # Project API Tools

    @mcp.tool()
    @handle_api_errors
    @optimize_token_usage
    def pha_project_search(
        query_key: str = "",
        ids: Optional[List[int]] = None,
        phids: Optional[List[str]] = None,
        names: Optional[List[str]] = None,
        name_like: str = "",
        slugs: Optional[List[str]] = None,
        ancestors: Optional[List[str]] = None,
        descendants: Optional[List[str]] = None,
        depth: int = None,
        status: str = "",
        is_milestone: bool = None,
        has_parent: bool = None,
        icon: str = "",
        color: str = "",
        limit: int = 100,
    ) -> dict:
        """
        Search for projects with advanced filtering capabilities.

        Args:
            query_key: Builtin query ("active", "all", "archived")
            ids: List of specific project IDs to search for
            phids: List of specific project PHIDs to search for
            names: List of exact project names to find
            name_like: Find projects whose names contain this substring
            slugs: List of project slugs to find
            ancestors: Find projects with these ancestors (PHIDs)
            descendants: Find projects with these descendants (PHIDs)
            depth: Maximum depth to search for ancestors/descendants
            status: Filter by project status ("active", "archived")
            is_milestone: Filter for milestone projects
            has_parent: Filter for projects with/without parents
            icon: Filter by project icon
            color: Filter by project color
            limit: Maximum number of results to return (default: 100, max: 1000)

        Returns:
            Search results with project data and pagination metadata
        """
        # Initialize None parameters to empty lists
        if ids is None:
            ids = []
        if phids is None:
            phids = []
        if names is None:
            names = []
        if slugs is None:
            slugs = []
        if ancestors is None:
            ancestors = []
        if descendants is None:
            descendants = []

        client = get_client_func()

        # Build constraints
        constraints = {}

        if ids:
            constraints["ids"] = ids
        if phids:
            constraints["phids"] = phids
        if names:
            # Use name constraint for exact matches
            if len(names) == 1:
                constraints["name"] = names[0]
            else:
                # For multiple names, use query to search
                constraints["query"] = " ".join(names)
        if name_like:
            # Use query parameter for substring search (nameLike is not supported)
            constraints["query"] = name_like
        if status:
            constraints["status"] = status
        # Note: Some constraints like ancestors, descendants, etc. may not be supported
        # by this Phorge instance. They are included for completeness.

        result = client.project.search_projects(
            constraints=constraints if constraints else None,
            limit=limit,
        )

        # Add pagination metadata
        result = _add_pagination_metadata(result, result.get("cursor"))

        return {"success": True, "projects": result}

    @mcp.tool()
    @handle_api_errors
    def pha_project_create(
        name: str,
        description: str = "",
        icon: str = "",
        color: str = "",
    ) -> dict:
        """
        Create a new project in Phabricator.

        Args:
            name: Project name (required)
            description: Project description
            icon: Project icon (e.g., "fa-briefcase", "fa-users")
            color: Project color (e.g., "red", "blue", "green")

        Returns:
            Created project information
        """
        client = get_client_func()

        result = client.project.create_project(
            name=name,
            description=description,
            icon=icon if icon else None,
            color=color if color else None,
        )

        return {"success": True, "project": result}

    @mcp.tool()
    @handle_api_errors
    def pha_project_get(project_identifier: str) -> dict:
        """
        Get detailed information about a specific project.

        Args:
            project_identifier: Project ID (e.g., 850), PHID, name, slug, or numeric ID from URL
                               (e.g., extract 850 from https://pha.example.com/project/view/850/)

        Returns:
            Project information
        """
        client = get_client_func()

        # Try different search strategies based on identifier format
        result = None

        # 1. If it looks like a PHID, search by PHID
        if project_identifier.startswith("PHID-PROJ-"):
            result = client.project.search_projects(
                constraints={"phids": [project_identifier]},
                limit=1,
            )

        # 2. If it's numeric, search by ID
        elif project_identifier.isdigit():
            result = client.project.search_projects(
                constraints={"ids": [int(project_identifier)]},
                limit=1,
            )

        # 3. Try searching by name using name parameter first, then query as fallback
        if not result or not result.get("data"):
            # First try exact name match
            result = client.project.search_projects(
                constraints={"name": project_identifier},
                limit=10,
            )

            # If no results with name, try query
            if not result.get("data"):
                result = client.project.search_projects(
                    constraints={"query": project_identifier},
                    limit=10,
                )

            # Filter for exact match
            if result.get("data"):
                exact_match = None
                for project in result["data"]:
                    fields = project.get("fields", {})
                    if (
                        fields.get("name") == project_identifier
                        or fields.get("slug") == project_identifier
                    ):
                        exact_match = project
                        break

                if exact_match:
                    result = {"data": [exact_match]}

        if result and result.get("data"):
            return {"success": True, "project": result["data"][0]}
        else:
            return {
                "success": False,
                "error": f"Project '{project_identifier}' not found",
            }

    @mcp.tool()
    @handle_api_errors
    def pha_project_update(
        project_phid: str,
        name: str = "",
        description: str = "",
        icon: str = "",
        color: str = "",
    ) -> dict:
        """
        Update an existing project in Phabricator.

        Args:
            project_phid: Project PHID to update
            name: New project name
            description: New project description
            icon: New project icon
            color: New project color

        Returns:
            Updated project information
        """
        client = get_client_func()

        # Build transactions
        transactions = []

        if name:
            transactions.append({"type": "name", "value": name})
        if description:
            transactions.append({"type": "description", "value": description})
        if icon:
            transactions.append({"type": "icon", "value": icon})
        if color:
            transactions.append({"type": "color", "value": color})

        if not transactions:
            return {"success": False, "error": "No updates specified"}

        result = client.project.edit_project(
            transactions=transactions,
            object_identifier=project_phid,
        )

        return {"success": True, "project": result}

    # Workboard Tools

    @mcp.tool()
    @handle_api_errors
    @optimize_token_usage
    def pha_workboard_search_columns(
        project_phids: Optional[List[str]] = None,
        phids: Optional[List[str]] = None,
        limit: int = 100,
        include_hidden: bool = True,
    ) -> dict:
        """
        Search for workboard columns with filtering capabilities.

        Args:
            project_phids: List of project PHIDs to search columns in
            phids: List of specific column PHIDs to search for
            limit: Maximum number of columns to return. Values above 100 are
                served by reading successive pages, as one board can hold more
                columns than a single page returns.
            include_hidden: Include columns hidden from the board. Hidden
                columns are returned by default.

        Returns:
            Column data, each with is_hidden, sequence and proxy_phid, plus the
            number of columns read and whether more remain.
        """
        if limit < 1:
            raise ValueError("limit must be a positive integer")

        client = get_client_func()

        constraints = {}
        if project_phids:
            constraints["projects"] = project_phids
        if phids:
            constraints["phids"] = phids

        columns = []
        after = None
        while len(columns) < limit:
            result = client.project.search_columns(
                constraints=constraints if constraints else None,
                limit=min(100, limit - len(columns)),
                after=after,
            )
            page = result.get("data") or []
            if not include_hidden:
                page = [
                    column
                    for column in page
                    if not (column.get("fields") or {}).get("isHidden")
                ]
            columns.extend(page)

            after = (result.get("cursor") or {}).get("after")
            if not after:
                break

        return {
            "success": True,
            "columns": columns[:limit],
            "has_more": after is not None,
            "returned": len(columns[:limit]),
        }

    @mcp.tool()
    @handle_api_errors
    def pha_workboard_edit_column(
        column_phid: Optional[str] = None,
        project_phid: Optional[str] = None,
        name: Optional[str] = None,
        hidden: Optional[bool] = None,
        sequence: Optional[int] = None,
        limit: Optional[int] = None,
        dry_run: bool = True,
    ) -> dict:
        """
        Create or edit a workboard column, previewing the change by default.

        Args:
            column_phid: PHID of the column to edit. Omit to create a column.
            project_phid: PHID of the board, required when creating a column.
            name: New name for the column, required when creating one.
            hidden: True to hide the column from the board, False to show it.
            sequence: Position of the column on the board.
            limit: Point limit for the column, 0 to remove the limit.
            dry_run: When true (the default) nothing is written: the requested
                change is returned for review.

        Returns:
            The requested change and, once applied, the column's new state.
            The default "Backlog" column cannot be hidden, and columns that
            proxy a subproject are renamed or hidden by editing the subproject.
        """
        if column_phid is None and not (project_phid and name):
            raise ValueError("creating a column requires both project_phid and name")

        change = {
            field: value
            for field, value in (
                ("name", name),
                ("hidden", hidden),
                ("sequence", sequence),
                ("limit", limit),
            )
            if value is not None
        }
        if not change:
            raise ValueError("no column fields to change were provided")

        if dry_run:
            return {
                "success": True,
                "applied": False,
                "creates_column": column_phid is None,
                "column_phid": column_phid,
                "would_change": change,
            }

        client = get_client_func()
        column = client.project.edit_column(
            column_phid=column_phid,
            project_phid=project_phid,
            name=name,
            hidden=hidden,
            limit=limit,
            sequence=sequence,
        )

        return {"success": True, "applied": True, "column": column}

    @mcp.tool()
    @handle_api_errors
    def pha_workboard_move_task(
        task_id: str,
        column_phid: str,
        before_phid: str = "",
        after_phid: str = "",
    ) -> dict:
        """
        Move a task to a specific workboard column with optional positioning.

        Args:
            task_id: Task ID or PHID to move
            column_phid: Target column PHID
            before_phid: Position before this task PHID (optional)
            after_phid: Position after this task PHID (optional)

        Returns:
            Success status and updated task information
        """
        client = get_client_func()

        # Create column transaction
        transaction = client.maniphest.create_column_transaction(
            column_phid=column_phid,
            before_phid=before_phid or None,
            after_phid=after_phid or None,
        )

        # Apply the transaction
        result = client.maniphest.edit_task(
            object_identifier=task_id,
            transactions=[transaction],
        )

        return {"success": True, "task": result}

    @mcp.tool()
    @handle_api_errors
    @optimize_token_usage
    def pha_workboard_search_tasks_by_column(
        column_phid: str,
        limit: int = 100,
        statuses: Optional[List[str]] = None,
        before: Optional[str] = None,
        after: Optional[str] = None,
    ) -> dict:
        """
        Search one page of tasks currently positioned in a workboard column. For a
        cursor page, repeat all query-defining arguments unchanged and replace only
        the cursor. Pagination preserves the upstream cursor limit as an integer or
        decimal string. Complete is true when no page remains in the requested
        direction.

        Args:
            column_phid: Column PHID to search tasks in.
            limit: Number of tasks to request, from 1 through 1000. The configured
                Phabricator server may enforce a lower maximum.
            statuses: Literal task status identifiers to include, such as "open"
                or "resolved". Omit to include all statuses.
            before: Opaque cursor from cursor.before for the previous page. Repeat
                all query-defining arguments unchanged from the prior request.
            after: Opaque cursor from cursor.after for the next page. Repeat all
                query-defining arguments unchanged from the prior request.

        Returns:
            Search results with the upstream cursor plus returned, has_next,
            has_previous, and complete pagination metadata.
        """
        _validate_task_page_request(limit, before, after)
        client = get_client_func()

        constraints: ManiphestSearchConstraints = {
            "columnPHIDs": [column_phid],
        }
        if statuses:
            constraints["statuses"] = statuses

        result = client.maniphest.search_tasks(
            constraints=constraints,
            before=before,
            after=after,
            limit=limit,
        )

        result = _add_task_enumeration_metadata(result, reverse=before is not None)
        return {"success": True, "tasks": result}

    @mcp.tool()
    @handle_api_errors
    def pha_dashboard_edit_panel(
        panel_phid: str,
        name: Optional[str] = None,
        text: Optional[str] = None,
        query_key: Optional[str] = None,
        query_engine: Optional[str] = None,
        item_limit: Optional[int] = None,
        dry_run: bool = True,
    ) -> dict:
        """
        Edit an existing dashboard panel, previewing the change by default.

        Panels are created and placed on a dashboard from the web UI: Conduit
        exposes no way to set a panel's type, and no way to read panels back,
        so this tool only updates a panel whose PHID you already have.

        Args:
            panel_phid: PHID of the panel to edit, from its UI page.
            name: New panel name.
            text: New body for a text panel, in remarkup.
            query_key: Saved query key a query panel reads, such as "assigned".
            query_engine: Search engine class a query panel searches with, such
                as "ManiphestTaskSearchEngine".
            item_limit: How many items a query panel lists.
            dry_run: When true (the default) nothing is written: the
                transactions that would be sent are returned for review.

        Returns:
            The transactions that would be, or were, applied. A field that does
            not belong to the panel's type is rejected by the server, since the
            panel type cannot be checked from here first.
        """
        transactions = [
            {"type": transaction_type, "value": value}
            for transaction_type, value in (
                ("name", name),
                ("custom.text", text),
                ("custom.key", query_key),
                ("custom.class", query_engine),
                ("custom.limit", item_limit),
            )
            if value is not None
        ]
        if not transactions:
            raise ValueError("no panel fields to change were provided")

        if dry_run:
            return {
                "success": True,
                "applied": False,
                "panel_phid": panel_phid,
                "would_apply": transactions,
            }

        client = get_client_func()
        result = client.dashboard.edit_panel(
            panel_phid=panel_phid,
            transactions=transactions,
        )

        return {
            "success": True,
            "applied": True,
            "panel_phid": panel_phid,
            "result": result,
        }

    from conduit.tools.phriction_tools import register_phriction_tools

    register_phriction_tools(mcp, get_client_func)

    from conduit.tools.paste_tools import register_paste_tools

    register_paste_tools(mcp, get_client_func)

    from conduit.tools.passphrase_tools import register_passphrase_tools

    register_passphrase_tools(mcp, get_client_func)
