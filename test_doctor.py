"""Run from this directory: python3 -m unittest"""
import json
import os
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest import mock

# First: points the config at the fixtures before any module reads it.
from fakes import GitRepoTest
import ci
import doctor
import gerrit
import repo
import snooze


class DoctorStateTest(GitRepoTest):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = pathlib.Path(tmp.name)
        (self.cache / "prereview").mkdir()
        for patcher in (mock.patch.object(doctor, "CACHE", self.cache),
                        mock.patch.object(snooze, "SNOOZE", self.cache / "snooze.json")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def touch(self, name, age_s=0):
        path = self.cache / name
        path.write_text("{}")
        os.utime(path, (time.time() - age_s,) * 2)
        return str(path)

    def test_what_closed_changes_left_behind_is_prunable(self):
        # Given
        head = self.git("rev-parse", "HEAD")
        for ref in ("changes/7", "changes/8", "review/8/2", "review/x"):
            self.git("update-ref", f"{repo.FETCH_NAMESPACE}/{ref}", head)
        current, outdated, closed = (self.touch(f"prereview/{n}.md") for n in ("7-3", "7-2", "8-1"))
        snooze.set_snooze(7, patch_set=3)
        snooze.set_snooze(8, patch_set=1)
        # When
        found = doctor.prunable(time.time(), {7: 3})
        # Then
        self.assertEqual([outdated, closed], found["prereviews"])
        self.assertEqual([f"{repo.FETCH_NAMESPACE}/changes/8", f"{repo.FETCH_NAMESPACE}/review/8/2"], found["refs"])
        self.assertEqual([8], found["snoozes"])
        self.assertNotIn(current, found["prereviews"])

    def test_prune_deletes_what_was_found_and_nothing_else(self):
        # Given
        head = self.git("rev-parse", "HEAD")
        for ref in ("changes/7", "changes/8", "main"):
            self.git("update-ref", f"{repo.FETCH_NAMESPACE}/{ref}", head)
        kept = self.touch("prereview/7-3.md")
        self.touch("prereview/8-1.md")
        snooze.set_snooze(8, patch_set=1)
        # When
        doctor.prune(doctor.prunable(time.time(), {7: 3}))
        # Then
        refs = self.git("for-each-ref", "--format=%(refname)", repo.FETCH_NAMESPACE).split()
        self.assertEqual([f"{repo.FETCH_NAMESPACE}/changes/7", f"{repo.FETCH_NAMESPACE}/main"], refs)
        self.assertEqual([kept], [str(p) for p in (self.cache / "prereview").iterdir()])
        self.assertEqual({}, snooze.load())

    def test_only_old_temp_files_are_prunable_without_gerrit(self):
        # Given
        crashed = self.touch(".seen.json.abc123", age_s=2 * doctor.TEMP_GRACE_S)
        self.touch(".seen.json.def456")
        self.touch("prereview/8-1.md")
        # When
        found = doctor.prunable(time.time(), None)
        # Then
        self.assertEqual({"temp_files": [crashed]}, found)

    def test_a_corrupt_state_file_is_flagged(self):
        # Given
        (self.cache / "seen.json").write_text('{"a": 1, "b": 2}')
        (self.cache / "flaky.json").write_text('{"trunc')
        # When
        files = {f["file"]: f for f in doctor.state_files(time.time())}
        # Then
        self.assertEqual(2, files["seen.json"]["entries"])
        self.assertTrue(files["flaky.json"]["corrupt"])


class CheckHttpTest(unittest.TestCase):
    def test_a_password_of_another_account_fails(self):
        # Given
        with mock.patch.object(gerrit, "rest_get", return_value={"username": "someone-else"}):
            # When
            check = doctor.check_http()
        # Then
        self.assertEqual((False, "someone-else"), (check["ok"], check["username"]))

    def test_an_unreadable_password_says_why(self):
        # Given
        with mock.patch.object(gerrit, "rest_get", side_effect=KeyError("no ~/.netrc entry for gerrit")):
            # When
            check = doctor.check_http()
        # Then
        self.assertFalse(check["ok"])
        self.assertIn("no ~/.netrc entry", check["detail"])


class CheckSshTest(unittest.TestCase):
    def test_a_reachable_gerrit_reports_its_version(self):
        # Given
        with mock.patch.object(gerrit, "ssh", return_value=mock.Mock(stdout="2026.1.0\n")) as ssh:
            # When
            check = doctor.check_ssh()
        # Then
        self.assertEqual({"ok": True, "detail": "2026.1.0"}, check)
        ssh.assert_called_once_with("gerrit", "version", timeout=30, check=True)

    def test_an_unreachable_gerrit_says_why(self):
        # Given
        error = subprocess.CalledProcessError(255, ["ssh"], stderr="ssh: Could not resolve hostname\n")
        with mock.patch.object(gerrit, "ssh", side_effect=error):
            # When
            check = doctor.check_ssh()
        # Then
        self.assertEqual({"ok": False, "detail": "ssh: Could not resolve hostname"}, check)


class CheckZuulTest(unittest.TestCase):
    def test_a_reachable_zuul_is_ok(self):
        # Given
        with mock.patch.object(ci, "http_get", return_value="[]") as http_get:
            # When
            check = doctor.check_zuul()
        # Then
        self.assertEqual({"ok": True}, check)
        http_get.assert_called_once_with(f"{ci.ZUUL_API}/builds?limit=1")

    def test_an_unreachable_zuul_says_why(self):
        # Given
        with mock.patch.object(ci, "http_get", side_effect=OSError("Could not resolve hostname")):
            # When
            check = doctor.check_zuul()
        # Then
        self.assertEqual({"ok": False, "detail": "Could not resolve hostname"}, check)


class MainTest(unittest.TestCase):
    def test_a_failing_ssh_exits_1_and_prunes_no_gerrit_backed_state(self):
        # Given
        out = []
        with mock.patch.object(doctor, "check_ssh", return_value={"ok": False, "detail": "Connection refused"}), \
                mock.patch.object(doctor, "check_http", return_value={"ok": True}), \
                mock.patch.object(doctor, "open_patch_sets") as open_patch_sets, \
                mock.patch.object(doctor, "prunable", return_value={"temp_files": []}) as prunable, \
                mock.patch("sys.argv", ["doctor.py"]), \
                mock.patch("builtins.print", side_effect=out.append):
            # When
            code = doctor.main()
        # Then
        self.assertEqual(1, code)
        open_patch_sets.assert_not_called()
        self.assertIsNone(prunable.call_args.args[1])
        self.assertIn("prunable", json.loads(out[0]))


if __name__ == "__main__":
    unittest.main()
