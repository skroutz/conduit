from unittest import TestCase
from unittest.mock import patch

from conduit.client.maniphest import ManiphestClient
from conduit.utils import flatten_params


class TestBasePhabricatorClient(TestCase):
    def setUp(self):
        super().setUp()

    def test_flatten_params(self):
        with self.subTest("flat_params"):
            flatten = flatten_params([{"x": 1, "y": 2}, {"z": 3, "a": 4}])
            self.assertEqual(
                flatten, [("[0][x]", 1), ("[0][y]", 2), ("[1][z]", 3), ("[1][a]", 4)]
            )

        with self.subTest("flat_params_with_prefix"):
            flatten = flatten_params(
                [{"x": 1, "y": 2}, {"z": 3, "a": 4}], prefix="test"
            )
            self.assertEqual(
                flatten,
                [
                    ("test[0][x]", 1),
                    ("test[0][y]", 2),
                    ("test[1][z]", 3),
                    ("test[1][a]", 4),
                ],
            )


class TestManiphestEditComment(TestCase):
    @patch("conduit.client.base.BasePhabricatorClient._make_request")
    def test_edit_comment(self, mock_request):
        client = ManiphestClient(
            api_url="http://test.example.com/api/", api_token="test_token"
        )
        mock_request.return_value = {"transactionPHID": "PHID-XACT-TASK-abc"}

        client.edit_comment("PHID-XACT-TASK-abc", "updated text")

        mock_request.assert_called_once_with(
            "maniphest.comment.edit",
            {
                "transactionPHID": "PHID-XACT-TASK-abc",
                "content": "updated text",
            },
        )
