"""Run from this directory: python3 -m unittest"""
import io
import json
import unittest
from unittest import mock

# First: points the config at the fixtures before any module reads it.
import fakes  # noqa: F401
import gerrit


class RestGetTest(unittest.TestCase):
    def setUp(self):
        gerrit._rest_auth = None
        self.addCleanup(setattr, gerrit, "_rest_auth", None)

    @staticmethod
    def response(body):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = f")]}}'\n{json.dumps(body)}".encode()
        return response

    def test_a_rotated_token_is_reread_once(self):
        # Given
        unauthorized = gerrit.urllib.error.HTTPError("url", 401, "Unauthorized", {}, io.BytesIO())
        credentials = mock.Mock(side_effect=[("me", "old"), ("me", "new")])
        # When
        with mock.patch.object(gerrit, "http_credentials", credentials), \
                mock.patch.object(gerrit.urllib.request, "urlopen",
                                  side_effect=[unauthorized, self.response({"ok": 1})]) as urlopen:
            found = gerrit.rest_get("/changes/1/comments")
        # Then
        self.assertEqual({"ok": 1}, found)
        self.assertEqual(2, credentials.call_count)
        self.assertIn("me:new".encode(), [gerrit.base64.b64decode(c.args[0].get_header("Authorization")[6:])
                                           for c in urlopen.call_args_list])

    def test_a_token_still_refused_after_rereading_raises(self):
        # Given
        unauthorized = gerrit.urllib.error.HTTPError("url", 401, "Unauthorized", {}, io.BytesIO())
        self.addCleanup(unauthorized.close)
        # When / Then
        with mock.patch.object(gerrit, "http_credentials", return_value=("me", "bad")), \
                mock.patch.object(gerrit.urllib.request, "urlopen", side_effect=unauthorized) as urlopen, \
                self.assertRaises(gerrit.urllib.error.HTTPError):
            gerrit.rest_get("/changes/1/comments")
        self.assertEqual(2, urlopen.call_count)


if __name__ == "__main__":
    unittest.main()
