from typing import Any, Dict, List, Optional

from conduit.client.base import BasePhabricatorClient
from conduit.utils import build_transaction_params, flatten_params


class PassphraseClient(BasePhabricatorClient):
    """Metadata-only access to Passphrase credentials.

    This client intentionally has no operation which requests, accepts, or
    updates private credential material.  In particular, ``needSecrets`` is
    always sent as ``False`` and Passphrase query responses are projected onto
    an explicit metadata allowlist before they leave this module.
    """

    _METADATA_FIELDS = (
        "id",
        "phid",
        "type",
        "name",
        "description",
        "uri",
        "monogram",
        "username",
        "viewPolicy",
        "editPolicy",
        "isDestroyed",
        "isLocked",
        "allowConduit",
        "authorPHID",
        "spacePHID",
        "dateCreated",
        "dateModified",
    )
    _ACCESS_TRANSACTION_TYPES = {
        "passphrase:lookedAtSecret",
        "passphrase.looked-at-secret",
    }

    @staticmethod
    def _parse_identifier(identifier: str) -> Dict[str, List[Any]]:
        if not isinstance(identifier, str):
            raise ValueError("Passphrase identifier must be a string")

        if identifier.startswith("PHID-CDTL-") and len(identifier) > len("PHID-CDTL-"):
            return {"phids": [identifier]}

        numeric_id = identifier[1:] if identifier[:1] in ("K", "k") else identifier
        if numeric_id.isdigit() and int(numeric_id) > 0:
            return {"ids": [int(numeric_id)]}

        raise ValueError(
            f"Invalid Passphrase identifier '{identifier}' -- expected a positive "
            'numeric ID ("123"), a monogram ("K123"), or a credential PHID '
            '("PHID-CDTL-...")'
        )

    @classmethod
    def _object_identifier(cls, identifier: str) -> str:
        parsed = cls._parse_identifier(identifier)
        if "phids" in parsed:
            return parsed["phids"][0]
        return f"K{parsed['ids'][0]}"

    @classmethod
    def _sanitize_credential(cls, credential: Any) -> Dict[str, Any]:
        if not isinstance(credential, dict):
            raise ValueError(
                "Unexpected response shape from passphrase.query: expected a "
                f"credential record, got {type(credential).__name__}"
            )
        return {
            field: credential[field]
            for field in cls._METADATA_FIELDS
            if field in credential
        }

    def get_credential(self, identifier: str) -> Optional[Dict[str, Any]]:
        """Return one credential's non-secret metadata, or ``None``.

        ``passphrase.query`` is an older endpoint which is technically capable
        of returning plaintext.  Keep the opt-in flag explicitly false, do not
        request public key material, and allowlist the response fields as a
        second independent boundary.
        """
        params: Dict[str, Any] = dict(
            flatten_params(self._parse_identifier(identifier))
        )
        params.update(
            {
                "needSecrets": False,
                "needPublicKeys": False,
                "limit": 1,
            }
        )
        result = self._make_request("passphrase.query", params)
        data = result.get("data", {})

        if isinstance(data, dict):
            records = list(data.values())
        elif isinstance(data, list):
            records = data
        else:
            raise ValueError(
                "Unexpected response shape from passphrase.query: expected "
                f"data to be a map or list, got {type(data).__name__}"
            )

        if not records:
            return None
        return self._sanitize_credential(records[0])

    def edit_policies(
        self,
        identifier: str,
        *,
        view_policy: Optional[str] = None,
        edit_policy: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update only the view and/or edit policy of a credential.

        This uses the conventional ``passphrase.edit`` transaction endpoint.
        Stock Phorge does not currently provide that endpoint, so installations
        must expose a compatible metadata-only Passphrase edit method.  The
        narrow signature deliberately makes secret-related transactions
        impossible to express through this client.
        """
        object_identifier = self._object_identifier(identifier)
        transactions = []

        if view_policy is not None:
            if not isinstance(view_policy, str) or not view_policy:
                raise ValueError("view_policy must be a non-empty policy identifier")
            transactions.append({"type": "view", "value": view_policy})
        if edit_policy is not None:
            if not isinstance(edit_policy, str) or not edit_policy:
                raise ValueError("edit_policy must be a non-empty policy identifier")
            transactions.append({"type": "edit", "value": edit_policy})

        if not transactions:
            raise ValueError("At least one visibility policy must be provided")

        params = build_transaction_params(
            transactions=transactions,
            object_identifier=object_identifier,
        )
        result = self._make_request("passphrase.edit", params)

        # Standard edit endpoints return only object identity. Project the
        # response anyway so a nonstandard implementation can not leak fields.
        obj = result.get("object", {}) if isinstance(result, dict) else {}
        safe_object = {}
        if isinstance(obj, dict):
            safe_object = {key: obj[key] for key in ("id", "phid") if key in obj}
        return {"object": safe_object}

    def get_access_log(
        self,
        identifier: str,
        *,
        before: Optional[str] = None,
        after: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """Return explicitly typed secret-access events for one credential.

        Events with a hidden/null transaction type are never guessed to be
        accesses.  Instead the response marks the audit result incomplete so
        callers know the Phabricator side must expose the Passphrase access
        transaction type through ``transaction.search``.
        """
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("limit must be an integer between 1 and 100")
        if before is not None and after is not None:
            raise ValueError("before and after may not be provided together")
        if before is not None and not before:
            raise ValueError("before must be a non-empty opaque cursor")
        if after is not None and not after:
            raise ValueError("after must be a non-empty opaque cursor")

        params: Dict[str, Any] = {
            "objectIdentifier": self._object_identifier(identifier),
            "limit": limit,
        }
        if before is not None:
            params["before"] = before
        if after is not None:
            params["after"] = after

        result = self._make_request("transaction.search", params)
        transactions = result.get("data", [])
        if not isinstance(transactions, list):
            raise ValueError(
                "Unexpected response shape from transaction.search: expected "
                f"a list, got {type(transactions).__name__}"
            )

        events = []
        hidden_event_count = 0
        for transaction in transactions:
            if not isinstance(transaction, dict):
                raise ValueError(
                    "Unexpected response shape from transaction.search: expected "
                    "transaction records"
                )

            transaction_type = transaction.get("type")
            if transaction_type is None:
                hidden_event_count += 1
                continue
            if transaction_type not in self._ACCESS_TRANSACTION_TYPES:
                continue

            events.append(
                {
                    key: transaction[key]
                    for key in (
                        "id",
                        "phid",
                        "authorPHID",
                        "objectPHID",
                        "dateCreated",
                        "dateModified",
                    )
                    if key in transaction
                }
            )

        response = {
            "data": events,
            "cursor": result.get("cursor", {}),
            "complete": hidden_event_count == 0,
            "hidden_event_count": hidden_event_count,
        }
        if hidden_event_count:
            response["warning"] = (
                "Phabricator hid one or more Passphrase transaction types; "
                "those events were not guessed to be credential accesses. "
                "Expose passphrase:lookedAtSecret through transaction.search "
                "for a complete audit log."
            )
        return response
