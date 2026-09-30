"""Run from this directory: python3 -m unittest"""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
os.environ["GERRIT_BABYSIT_CONFIG"] = str(FIXTURES / "config.json")
os.environ.setdefault("GERRIT_BABYSIT_CACHE", tempfile.mkdtemp(prefix="gerrit-babysit-test-"))

import config  # noqa: E402
import statusline_segment  # noqa: E402

# The plugin's file name (SwiftBar's refresh interval is in it) is not an importable module name.
_spec = importlib.util.spec_from_file_location("swiftbar_plugin", pathlib.Path(__file__).parent / "macos" / "gerrit.30s.py")
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)


def row(number=1, **extra):
    return {"number": number, "patch_set": 2, "subject": "feat(inbox): x", "url": f"https://review/{number}",
            "wip": False, "code_review": 0, "ci": "passed", "ci_failed": [], "ci_stuck": False, "threads": 0,
            "conflict": False, "open_parent": None, "ready": False, "worktree": None, **extra}


class SnapshotTest(unittest.TestCase):
    def write(self, changes, age=0, **extra):
        now = time.time() - age
        self.status.write_text(json.dumps({"updated": now, "last_attempt": now, "last_error": None,
                                           "changes": changes, **extra}))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.status = pathlib.Path(tmp.name) / "status.json"


class StatusLineTest(SnapshotTest):
    def segment(self):
        out = io.StringIO()
        with mock.patch.object(statusline_segment, "STATUS", self.status), contextlib.redirect_stdout(out):
            statusline_segment.main()
        return out.getvalue()

    def test_counts_problems_and_ready_changes_without_wip(self):
        # Given
        self.write([row(1, ci="failed"), row(2, ready=True), row(3, ready=True, open_parent=9), row(4, wip=True)])
        # When
        found = self.segment()
        # Then
        self.assertEqual("⎇ 3 · 1⚠ · 1✓", found)

    def test_stale_snapshot_shows_a_question_mark(self):
        # Given
        self.write([row(1, ci="failed")], age=config.STALE_AFTER_S + 1)
        # When
        found = self.segment()
        # Then
        self.assertEqual("⎇ 1 · ?", found)

    def test_no_language_lookup_without_a_string_to_translate(self):
        # Given
        self.write([row(1)])
        # When
        with mock.patch.object(config, "_language", None), mock.patch.object(config, "system_language") as lookup:
            self.segment()
        # Then
        lookup.assert_not_called()


class MenuStateTest(unittest.TestCase):
    def colors(self, *changes):
        return [plugin.state_of(c)[2] for c in changes]

    def test_worst_problem_wins(self):
        # Given
        changes = [row(wip=True, conflict=True), row(conflict=True, ci="failed"), row(ci="stale_base", code_review=-1),
                   row(code_review=-1, ci_stuck=True)]
        # When
        symbols = [plugin.state_of(c)[1] for c in changes]
        # Then
        self.assertEqual(["pencil", "exclamationmark.triangle.fill", "arrow.triangle.branch", "hand.thumbsdown.fill"],
                         symbols)

    def test_ready_is_green_only_without_an_open_parent(self):
        # Given
        ready, blocked = row(ready=True), row(ready=True, open_parent=9)
        # When
        found = self.colors(ready, blocked)
        # Then
        self.assertEqual(["green", "orange"], found)

    def test_waiting_states(self):
        # Given
        changes = [row(ci_stuck=True, ci="running"), row(ci="running"), row(ci="passed", code_review=1)]
        # When
        found = self.colors(*changes)
        # Then
        self.assertEqual(["orange", "orange", None], found)


class OpenWorktreeTest(SnapshotTest):
    def test_the_path_comes_from_the_snapshot_not_the_menu(self):
        # Given
        self.write([row(7, worktree='/tmp/odd "name"')])
        # When
        with mock.patch.object(plugin, "STATUS", self.status), mock.patch.object(plugin, "ORCA", "/nonexistent"), \
                mock.patch.object(plugin.subprocess, "run") as run:
            plugin.handle(["worktree", "7"])
            plugin.handle(["worktree", "8"])
        # Then
        run.assert_called_once_with(["open", "-a", "Terminal", '/tmp/odd "name"'], capture_output=True)

    def test_menu_actions_carry_only_the_change_number(self):
        # Given
        change = row(7, worktree='/tmp/odd "name"')
        out = io.StringIO()
        # When
        with contextlib.redirect_stdout(out):
            plugin.print_change(change, "label", "clock", None, fresh=True)
        # Then
        self.assertNotIn("odd", out.getvalue())


class InvestigateTest(SnapshotTest):
    def menu(self, change):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            plugin.print_change(change, "label", "clock", None, fresh=True)
        return out.getvalue()

    def test_offered_only_for_changes_in_trouble(self):
        # Given
        changes = [row(ci="failed"), row(threads=2), row(ci="running"), row(wip=True, ci="failed")]
        # When
        offered = ['"investigate"' in self.menu(c) for c in changes]
        # Then
        self.assertEqual([True, True, False, False], offered)

    def test_opens_claude_in_the_worktree_with_the_state_in_the_prompt(self):
        # Given
        self.write([row(7, ci="failed", ci_failed=["Quality"], worktree='/tmp/odd "name"')])
        # When
        with mock.patch.object(plugin, "STATUS", self.status), mock.patch.object(plugin, "ORCA", "/nonexistent"), \
                mock.patch.object(plugin, "osascript") as osascript:
            plugin.handle(["investigate", "7"])
            plugin.handle(["investigate", "8"])
        # Then
        osascript.assert_called_once()
        path, command = osascript.call_args.args[1:]
        self.assertEqual('/tmp/odd "name"', path)
        self.assertIn("7", command)
        self.assertIn("Quality", command)


FLAKY_RED = {"ci": "failed", "ci_failed_jobs": [{"job": "unit", "result": "FAILURE"}],
             "flaky": {"unit": {"week": 3, "month": 4, "last": 1}}, "rechecks": 0}


class RecheckTest(SnapshotTest):
    def menu(self, change, fresh=True):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            plugin.print_change(change, "label", "clock", None, fresh=fresh)
        return out.getvalue()

    def test_offered_when_every_failed_job_is_a_known_flake_not_yet_rechecked(self):
        # Given
        changes = [row(**FLAKY_RED), row(**{**FLAKY_RED, "rechecks": 1}),
                   row(**{**FLAKY_RED, "flaky": {"unit": {"week": 1, "month": 1, "last": 1}}}),
                   row(**{**FLAKY_RED, "ci_failed_jobs": [{"job": "unit", "result": "FAILURE"},
                                                          {"job": "lint", "result": "FAILURE"}]})]
        # When
        offered = ['"recheck"' in self.menu(c) for c in changes] + ['"recheck"' in self.menu(row(**FLAKY_RED), False)]
        # Then
        self.assertEqual([True, False, False, False, False], offered)
        self.assertIn("unit flaky 3× this week", self.menu(row(**FLAKY_RED)))

    def test_posts_the_configured_recheck_on_the_confirmed_patch_set_only(self):
        # Given
        self.write([row(7, **FLAKY_RED)])
        confirm = mock.Mock(returncode=0)
        # When
        with mock.patch.object(plugin, "STATUS", self.status), \
                mock.patch.object(plugin, "osascript", return_value=confirm), \
                mock.patch.object(plugin.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            plugin.handle(["recheck", "7", "1"])
            plugin.handle(["recheck", "7", "2"])
        # Then
        ssh = [c.args[0] for c in run.call_args_list if c.args[0][0] == "ssh"]
        self.assertEqual([[*plugin.SSH, "gerrit", "review", "--message", "recheck", "7,2"]], ssh)


class SnoozeMenuTest(SnapshotTest):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(plugin.snooze, "SNOOZE", self.status.parent / "snooze.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def menu(self):
        out = io.StringIO()
        with mock.patch.object(plugin, "STATUS", self.status), contextlib.redirect_stdout(out):
            plugin.main()
        return out.getvalue()

    def test_a_snoozed_change_leaves_the_counts_for_its_own_section(self):
        # Given
        self.write([row(1, ci="failed"), row(2, ci="failed")])
        plugin.snooze.set_snooze(2, patch_set=2)
        # When
        found = self.menu()
        # Then
        title, rest = found.split("\n", 1)
        self.assertTrue(title.startswith("1 · "))
        self.assertIn("Snoozed | disabled=true", rest)
        self.assertIn('param1="wake" param2="2"', rest)

    def test_snoozing_until_the_next_patch_set_from_the_menu(self):
        # Given
        self.write([row(3)])
        # When
        with mock.patch.object(plugin, "STATUS", self.status), mock.patch.object(plugin, "notify"), \
                mock.patch.object(plugin, "refresh"):
            plugin.handle(["snooze", "3", "ps", "2"])
        # Then
        self.assertEqual({3: {"patch_set": 2}}, plugin.snooze.load())
        with mock.patch.object(statusline_segment, "STATUS", self.status), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            statusline_segment.main()
        self.assertEqual("⎇ 0", out.getvalue())


if __name__ == "__main__":
    unittest.main()
