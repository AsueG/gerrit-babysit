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
            gerrit.ssh_query(text)
        # Then
        self.assertEqual([text], shlex.split(ssh.call_args.args[-1]))

    def test_with_an_http_password_no_ssh_session_is_opened(self):
        # Given
        with mock.patch.object(gerrit, "rest_auth", return_value="token"), \
                mock.patch.object(gerrit, "rest_get", return_value=[rest_change()]) as rest_get, \
                mock.patch.object(gerrit, "ssh") as ssh:
            # When
            rows = gerrit.query("owner:self status:open", "--current-patch-set")
        # Then
        ssh.assert_not_called()
        self.assertEqual([7], [row["number"] for row in rows])
        self.assertIn("o=CURRENT_REVISION", rest_get.call_args.args[0])

    def test_without_an_http_password_the_query_goes_over_ssh(self):
        # Given
        with mock.patch.object(gerrit, "rest_auth", side_effect=KeyError("no ~/.netrc entry")), \
                mock.patch.object(gerrit, "ssh", return_value=mock.Mock(stdout='{"number": 7}\n')) as ssh:
            # When
            rows = gerrit.query("owner:self status:open")
        # Then
        ssh.assert_called_once()
        self.assertEqual([{"number": 7}], rows)

    def test_a_refused_http_password_falls_back_on_ssh(self):
        # Given
        refused = gerrit.urllib.error.HTTPError("url", 401, "Unauthorized", {}, io.BytesIO())
        with mock.patch.object(gerrit, "rest_auth", return_value="token"), \
                mock.patch.object(gerrit, "rest_get", side_effect=refused), \
                mock.patch.object(gerrit, "ssh", return_value=mock.Mock(stdout='{"number": 7}\n')) as ssh:
            # When
            rows = gerrit.query("owner:self status:open")
        # Then
        ssh.assert_called_once()
        self.assertEqual([{"number": 7}], rows)

def rest_account(username, account_id=1):
    return {"_account_id": account_id, "name": username.title(), "email": f"{username}@example.com",
            "username": username, "avatars": []}


def rest_change(number=7, **extra):
    """A ChangeInfo as `/changes/?q=…` returns it with the options the SSH flags map to."""
    return {"_number": number, "change_id": f"I{number}", "project": "app", "branch": "main", "subject": "fix: it",
            "status": "NEW", "owner": rest_account("owner"), "hashtags": [], "work_in_progress": True,
            "created": "2026-10-01 10:00:00.000000000", "updated": "2026-10-01 12:00:00.000000000",
            "current_revision": "sha2",
            "revisions": {
                "sha1": {"_number": 1, "ref": "refs/changes/07/7/1", "created": "2026-10-01 10:00:00.000000000"},
                "sha2": {"_number": 2, "ref": "refs/changes/07/7/2", "created": "2026-10-01 11:00:00.000000000",
                         "kind": "REWORK", "commit": {"parents": [{"commit": "base"}], "author": rest_account("owner")}},
            },
            "labels": {"Code-Review": {"all": [{**rest_account("me", 2), "value": 2,
                                                "date": "2026-10-01 11:30:00.000000000"},
                                               {**rest_account("idle", 3), "value": 0}]},
                       "Build": {"all": [{**rest_account("zuul", 4), "value": -1,
                                          "date": "2026-10-01 11:10:00.000000000"}]}},
            "reviewers": {"REVIEWER": [rest_account("me", 2), rest_account("idle", 3)], "CC": [rest_account("cc", 5)]},
            "messages": [{"author": rest_account("me", 2), "date": "2026-10-01 10:30:00.000000000",
                          "_revision_number": 1, "message": "Patch Set 1: Code-Review-1\n\nNot yet."},
                         {"author": rest_account("zuul", 4), "date": "2026-10-01 11:10:00.000000000",
                          "_revision_number": 2, "message": "Patch Set 2: Build-1 -Quality\n\nBuild failed."}],
            "submit_records": [{"status": "NOT_READY", "labels": [{"label": "Code-Review", "status": "MAY"},
                                                                  {"label": "Build", "status": "REJECT"}]}],
            **extra}


class SshRowTest(unittest.TestCase):
    ALL = ("--current-patch-set", "--all-approvals", "--comments", "--all-reviewers", "--submit-records")

    def test_the_current_patch_set_carries_the_votes_and_not_the_unvoted_reviewers(self):
        # Given
        change = rest_change()
        # When
        row = gerrit.ssh_row(change, ("--current-patch-set",))
        # Then
        current = row["currentPatchSet"]
        self.assertEqual((2, "sha2", "refs/changes/07/7/2", ["base"]),
                         (current["number"], current["revision"], current["ref"], current["parents"]))
        self.assertEqual([("Code-Review", "2", "me", gerrit.epoch("2026-10-01 11:30:00")), ("Build", "-1", "zuul",
                                                                                         gerrit.epoch("2026-10-01 11:10:00"))],
                         [(a["type"], a["value"], a["by"]["username"], a["grantedOn"]) for a in current["approvals"]])

    def test_the_row_keeps_the_ssh_field_names(self):
        # Given
        change = rest_change()
        # When
        row = gerrit.ssh_row(change, self.ALL)
        # Then
        self.assertEqual(("I7", 7, True, "NEW", True), (row["id"], row["number"], row["wip"], row["status"], row["open"]))
        self.assertEqual(f"https://{gerrit.HOST}/c/app/+/7", row["url"])
        self.assertEqual({"name": "Owner", "email": "owner@example.com", "username": "owner"}, row["owner"])
        self.assertEqual(1790856000, row["lastUpdated"])
        self.assertEqual([("me", 1790850600, "Patch Set 1: Code-Review-1\n\nNot yet.")],
                         [(m["reviewer"]["username"], m["timestamp"], m["message"]) for m in row["comments"][:1]])
        self.assertEqual(["me", "idle", "cc"], [r["username"] for r in row["allReviewers"]])
        self.assertEqual([{"label": "Code-Review", "status": "MAY"}, {"label": "Build", "status": "REJECT"}],
                         row["submitRecords"][0]["labels"])

    def test_older_patch_sets_get_their_votes_back_from_the_messages(self):
        # Given
        change = rest_change()
        # When
        row = gerrit.ssh_row(change, self.ALL)
        # Then
        self.assertEqual([(1, [("Code-Review", "-1", "me")]), (2, [("Code-Review", "2", "me"), ("Build", "-1", "zuul")])],
                         [(ps["number"], [(a["type"], a["value"], a["by"]["username"]) for a in ps["approvals"]])
                          for ps in row["patchSets"]])

    def test_flags_not_asked_for_leave_their_fields_out(self):
        # Given
        change = rest_change(work_in_progress=False)
        # When
        row = gerrit.ssh_row(change, ())
        # Then
        self.assertFalse({"currentPatchSet", "patchSets", "comments", "allReviewers", "submitRecords", "wip"} & row.keys())


class DependsOnTest(unittest.TestCase):
    def test_the_parent_change_is_found_by_its_commit_on_the_same_branch(self):
        # Given
        rows = [gerrit.ssh_row(rest_change(7), ("--current-patch-set",)),
                gerrit.ssh_row(rest_change(8, current_revision="sha1"), ("--current-patch-set",))]
        parent = {"_number": 5, "change_id": "I5", "project": "app", "branch": "main", "current_revision": "base",
                  "revisions": {"base": {"_number": 3, "ref": "refs/changes/05/5/3"}}}
        elsewhere = {**parent, "_number": 6, "branch": "release"}
        # When
        with mock.patch.object(gerrit, "rest_get", return_value=[parent, elsewhere]) as rest_get:
            gerrit.add_depends_on(rows)
        # Then
        self.assertEqual([{"id": "I5", "number": 5, "revision": "base", "ref": "refs/changes/05/5/3",
                           "isCurrentPatchSet": True}], rows[0]["dependsOn"])
        self.assertNotIn("dependsOn", rows[1])
        self.assertIn("commit%3Abase", rest_get.call_args.args[0])


class ErrorDetailTest(unittest.TestCase):
    def test_the_session_cap_is_named_rather_than_the_disconnect_line(self):
        # Given
        stderr = ("Received disconnect from 10.0.0.1 port 29418:12: Too many concurrent connections (10) - max. "
                  "allowed: 10\nDisconnected from 10.0.0.1 port 29418\n")
        error = gerrit.subprocess.CalledProcessError(255, ["ssh"], stderr=stderr)
        # When
        detail = gerrit.error_detail(error)
        # Then
        self.assertIn("Too many concurrent connections (10) - max. allowed: 10", detail)
        self.assertIn(gerrit.SESSION_CAP, detail)
        self.assertNotIn("Disconnected", detail)


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

    def test_reads_the_project_setting_once_within_the_hour(self):
        # Given
        config = {"use_content_merge": {"configured_value": "FALSE", "inherited_value": False}}
        # When
        with mock.patch.object(gerrit, "rest_get", return_value=config) as rest_get:
            answers = [gerrit.uses_content_merge("android/app", now=now) for now in (0, 3599)]
        # Then
        self.assertEqual([False, False], answers)
        rest_get.assert_called_once_with("/projects/android%2Fapp/config", timeout=10)

    def test_a_setting_changed_on_gerrit_is_seen_after_the_hour(self):
        # Given
        configs = [{"use_content_merge": {"configured_value": "FALSE"}},
                   {"use_content_merge": {"configured_value": "TRUE"}}]
        # When
        with mock.patch.object(gerrit, "rest_get", side_effect=configs):
            answers = [gerrit.uses_content_merge("app", now=now) for now in (0, 3600)]
        # Then
        self.assertEqual([False, True], answers)

    def test_false_booleans_missing_from_the_json_read_as_false(self):
        # Given
        settings = [{"configured_value": "TRUE"}, {"configured_value": "INHERIT"},
                    {"configured_value": "INHERIT", "inherited_value": True}, {"configured_value": "FALSE"}, None]
        # When
        answers = [gerrit.content_merge_of(setting) for setting in settings]
        # Then
        self.assertEqual([True, False, True, False, True], answers)

    def test_an_unreadable_setting_counts_as_on_and_is_asked_again_after_a_few_minutes(self):
        # Given
        failures = [KeyError("no ~/.netrc entry"), OSError("offline")]
        # When
        with mock.patch.object(gerrit, "rest_get", side_effect=failures) as rest_get:
            answers = [gerrit.uses_content_merge("app", now=now) for now in (0, 60, 600)]
        # Then
        self.assertEqual([True, True, True], answers)
        self.assertEqual(2, rest_get.call_count)


class HttpErrorTest(unittest.TestCase):
    def test_a_working_password_reports_nothing(self):
        # Given
        with mock.patch.object(gerrit, "rest_get", return_value={"username": "me"}) as rest_get:
            # When
            error = gerrit.http_error()
        # Then
        self.assertIsNone(error)
        rest_get.assert_called_once_with("/accounts/self")

    def test_a_missing_password_says_why(self):
        # Given
        with mock.patch.object(gerrit, "rest_get", side_effect=KeyError("no ~/.netrc entry for gerrit")):
            # When
            error = gerrit.http_error()
        # Then
        self.assertIn("no ~/.netrc entry", error)


if __name__ == "__main__":
    unittest.main()
