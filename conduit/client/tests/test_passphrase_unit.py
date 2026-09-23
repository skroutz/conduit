from unittest.mock import Mock

import pytest

from conduit.client.passphrase import PassphraseClient


@pytest.fixture
def client():
    instance = PassphraseClient(
        "https://phorge.example/api/",
        "api-token",
        http_client=Mock(),
    )
    instance._make_request = Mock()
    return instance


class TestPassphraseGetCredential:
    def test_forces_secret_flags_off_and_allowlists_metadata(self, client):
        client._make_request.return_value = {
            "data": {
                "PHID-CDTL-abc": {
                    "id": 7,
                    "phid": "PHID-CDTL-abc",
                    "type": "password",
                    "name": "Deploy",
                    "description": "Production deploy account",
                    "username": "deploy",
                    "viewPolicy": "PHID-PROJ-viewers",
                    "editPolicy": "PHID-PROJ-editors",
                    "material": {"password": "must-not-escape"},
                    "password": "also-must-not-escape",
                    "secret": "still-must-not-escape",
                }
            }
        }

        result = client.get_credential("K7")

        client._make_request.assert_called_once_with(
            "passphrase.query",
            {
                "ids[0]": 7,
                "needSecrets": False,
                "needPublicKeys": False,
                "limit": 1,
            },
        )
        assert result == {
            "id": 7,
            "phid": "PHID-CDTL-abc",
            "type": "password",
            "name": "Deploy",
            "description": "Production deploy account",
            "username": "deploy",
            "viewPolicy": "PHID-PROJ-viewers",
            "editPolicy": "PHID-PROJ-editors",
        }

    def test_accepts_credential_phid(self, client):
        client._make_request.return_value = {"data": []}

        assert client.get_credential("PHID-CDTL-abc") is None

        params = client._make_request.call_args.args[1]
        assert params["phids[0]"] == "PHID-CDTL-abc"

    @pytest.mark.parametrize("identifier", ["", "K", "K0", "K-1", "PHID-PASS-x"])
    def test_rejects_invalid_identifiers(self, client, identifier):
        with pytest.raises(ValueError, match="Invalid Passphrase identifier"):
            client.get_credential(identifier)

        client._make_request.assert_not_called()

    def test_rejects_unexpected_data_shape(self, client):
        client._make_request.return_value = {"data": "not-records"}

        with pytest.raises(ValueError, match="expected data to be a map or list"):
            client.get_credential("K7")


class TestPassphraseEditPolicies:
    def test_only_emits_view_and_edit_transactions(self, client):
        client._make_request.return_value = {
            "object": {
                "id": 7,
                "phid": "PHID-CDTL-abc",
                "material": {"password": "must-not-escape"},
            }
        }

        result = client.edit_policies(
            "7",
            view_policy="PHID-PROJ-viewers",
            edit_policy="PHID-PROJ-editors",
        )

        method, params = client._make_request.call_args.args
        assert method == "passphrase.edit"
        assert params["objectIdentifier"] == "K7"
        assert params["transactions[0][type]"] == "view"
        assert params["transactions[0][value]"] == "PHID-PROJ-viewers"
        assert params["transactions[1][type]"] == "edit"
        assert params["transactions[1][value]"] == "PHID-PROJ-editors"
        assert result == {"object": {"id": 7, "phid": "PHID-CDTL-abc"}}

    def test_requires_a_policy(self, client):
        with pytest.raises(ValueError, match="At least one visibility policy"):
            client.edit_policies("K7")

        client._make_request.assert_not_called()

    @pytest.mark.parametrize("policy", ["", 123, False])
    def test_rejects_invalid_policy_values(self, client, policy):
        with pytest.raises(ValueError, match="view_policy"):
            client.edit_policies("K7", view_policy=policy)

        client._make_request.assert_not_called()


class TestPassphraseAccessLog:
    def test_returns_only_explicit_access_events(self, client):
        client._make_request.return_value = {
            "data": [
                {
                    "id": 10,
                    "phid": "PHID-XACT-access",
                    "type": "passphrase:lookedAtSecret",
                    "authorPHID": "PHID-USER-alice",
                    "objectPHID": "PHID-CDTL-abc",
                    "dateCreated": 1700000000,
                    "fields": {"secret": "must-not-escape"},
                },
                {
                    "id": 11,
                    "type": "core:view-policy",
                    "authorPHID": "PHID-USER-bob",
                },
            ],
            "cursor": {"after": None, "before": None},
        }

        result = client.get_access_log("K7")

        assert result == {
            "data": [
                {
                    "id": 10,
                    "phid": "PHID-XACT-access",
                    "authorPHID": "PHID-USER-alice",
                    "objectPHID": "PHID-CDTL-abc",
                    "dateCreated": 1700000000,
                }
            ],
            "cursor": {"after": None, "before": None},
            "complete": True,
            "hidden_event_count": 0,
        }

    def test_marks_hidden_transactions_incomplete_without_guessing(self, client):
        client._make_request.return_value = {
            "data": [
                {
                    "id": 12,
                    "type": None,
                    "authorPHID": "PHID-USER-unknown",
                    "dateCreated": 1700000001,
                }
            ]
        }

        result = client.get_access_log("K7")

        assert result["data"] == []
        assert result["complete"] is False
        assert result["hidden_event_count"] == 1
        assert "not guessed" in result["warning"]

    def test_validates_pagination(self, client):
        with pytest.raises(ValueError, match="between 1 and 100"):
            client.get_access_log("K7", limit=101)
        with pytest.raises(ValueError, match="may not be provided together"):
            client.get_access_log("K7", before="b", after="a")
        with pytest.raises(ValueError, match="non-empty opaque cursor"):
            client.get_access_log("K7", after="")

        client._make_request.assert_not_called()
