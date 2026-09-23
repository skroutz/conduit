from typing import Optional

from fastmcp import FastMCP

from conduit.tools.handlers import handle_api_errors
from conduit.utils import PhabricatorAPIError


def register_passphrase_tools(
    mcp: FastMCP,
    get_client_func: callable,
) -> None:
    """Register metadata-only Passphrase MCP tools."""

    @mcp.tool()
    @handle_api_errors
    def pha_passphrase_get(credential_id: str) -> dict:
        """
        Get non-secret details for one Passphrase credential.

        Args:
            credential_id: Numeric ID ("123"), monogram ("K123"), or
                credential PHID ("PHID-CDTL-...").

        Returns:
            Credential metadata such as type, name, description, URI, and
            username. Private material, passwords, tokens, private keys, and
            public keys are never requested or returned.
        """
        client = get_client_func()
        credential = client.passphrase.get_credential(credential_id)
        if credential is None:
            return {
                "success": False,
                "error": f"Passphrase credential '{credential_id}' not found",
            }
        return {"success": True, "credential": credential}

    @mcp.tool()
    @handle_api_errors
    def pha_passphrase_edit_policies(
        credential_id: str,
        view_policy: Optional[str] = None,
        edit_policy: Optional[str] = None,
    ) -> dict:
        """
        Edit only a Passphrase credential's visibility policies.

        Args:
            credential_id: Numeric ID, K-monogram, or credential PHID.
            view_policy: New policy identifier controlling who can view the
                credential entry. Omit to leave unchanged.
            edit_policy: New policy identifier controlling who can edit the
                credential entry. Omit to leave unchanged.

        Returns:
            The updated object's ID and PHID. This tool has no argument or
            transaction path for changing credential material. The connected
            Phabricator must provide a compatible passphrase.edit endpoint;
            stock Phorge does not currently expose policy edits via Conduit.
        """
        client = get_client_func()
        result = client.passphrase.edit_policies(
            credential_id,
            view_policy=view_policy,
            edit_policy=edit_policy,
        )
        return {"success": True, "credential": result}

    @mcp.tool()
    @handle_api_errors
    def pha_passphrase_get_access_log(
        credential_id: str,
        limit: int = 100,
        before: Optional[str] = None,
        after: Optional[str] = None,
    ) -> dict:
        """
        Get the audit log of plaintext access to a Passphrase credential.

        Args:
            credential_id: Numeric ID, K-monogram, or credential PHID.
            limit: Maximum upstream transactions to inspect (1-100).
            before: Opaque cursor for the previous page.
            after: Opaque cursor for the next page.

        Returns:
            Explicitly typed access events with actor PHID and timestamps.
            ``complete`` is false if Phabricator hid any Passphrase transaction
            types. Hidden events are never guessed to be accesses.
        """
        client = get_client_func()
        result = client.passphrase.get_access_log(
            credential_id,
            limit=limit,
            before=before,
            after=after,
        )

        author_phids = sorted(
            {
                event["authorPHID"]
                for event in result.get("data", [])
                if isinstance(event, dict) and event.get("authorPHID")
            }
        )
        actors = {}
        if author_phids:
            try:
                user_result = client.user.search(
                    constraints={"phids": author_phids},
                    limit=len(author_phids),
                )
                for user in user_result.get("data", []):
                    if not isinstance(user, dict) or not user.get("phid"):
                        continue
                    fields = user.get("fields", {})
                    if not isinstance(fields, dict):
                        fields = {}
                    actors[user["phid"]] = {
                        key: fields[key]
                        for key in ("username", "realName")
                        if key in fields
                    }
            except PhabricatorAPIError as error:
                result["actor_resolution_warning"] = str(error)
        result["actors"] = actors
        return {"success": True, "access_log": result}
