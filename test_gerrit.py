"""Run from this directory: python3 -m unittest"""
import io
import json
import shlex
import unittest
from unittest import mock

# First: points the config at the fixtures before any module reads it.
import fakes  # noqa: F401
import gerrit


class QueryTest(unittest.TestCase):
    def test_the_query_reaches_the_remote_shell_as_one_word(self):
        # Given
        text = "message:\"it's done\" status:open"
        # When
        with mock.patch.object(gerrit, "ssh", return_value=mock.Mock(stdout="")) as ssh:
            gerrit.query(text)
        # Then
        self.assertEqual([text], shlex.split(ssh.call_args.args[-1]))


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


class UsesContentMergeTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(gerrit._content_merge.clear)

    def test_reads_the_project_setting_once(self):
        # Given
        config = {"use_content_merge": {"configured_value": "FALSE", "inherited_value": False}}
        # When
        with mock.patch.object(gerrit, "rest_get", return_value=config) as rest_get:
            answers = [gerrit.uses_content_merge("android/app") for _ in range(2)]
        # Then
        self.assertEqual([False, False], answers)
        rest_get.assert_called_once_with("/projects/android%2Fapp/config")

    def test_false_booleans_missing_from_the_json_read_as_false(self):
        # Given
        settings = [{"configured_value": "TRUE"}, {"configured_value": "INHERIT"},
                    {"configured_value": "INHERIT", "inherited_value": True}, {"configured_value": "FALSE"}, None]
        # When
        answers = [gerrit.content_merge_of(setting) for setting in settings]
        # Then
        self.assertEqual([True, False, True, False, True], answers)

    def test_an_unreadable_setting_counts_as_on_and_is_asked_again(self):
        # Given
        failures = [KeyError("no ~/.netrc entry"), OSError("offline")]
        # When
        with mock.patch.object(gerrit, "rest_get", side_effect=failures) as rest_get:
            answers = [gerrit.uses_content_merge("app") for _ in range(2)]
        # Then
        self.assertEqual([True, True], answers)
        self.assertEqual(2, rest_get.call_count)


if __name__ == "__main__":
    unittest.main()
