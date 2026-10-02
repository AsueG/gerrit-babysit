"""Run from this directory: python3 -m unittest"""
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

# First: points the config at the fixtures before any module reads it.
import fakes  # noqa: F401
import stop_hook
import watch

BABYSIT, OTHER, HOOK = 100, 200, 150
WATCHER = f"python3 {stop_hook.WATCH_PY}"


class StopHookTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.session = pathlib.Path(tmp.name) / "session.json"
        self.session.write_text(json.dumps({"claude_pid": BABYSIT}))

    def run_hook(self, extra_processes=(), hook_input=None, hook_parent=HOOK):
        table = {BABYSIT: (1, "claude"), OTHER: (1, "claude"), HOOK: (BABYSIT, "zsh"), 250: (OTHER, "zsh"),
                 **dict(extra_processes)}
        out = io.StringIO()
        with mock.patch.object(stop_hook, "SESSION", self.session), \
                mock.patch.object(stop_hook, "processes", return_value=table), \
                mock.patch.object(stop_hook.os, "getppid", return_value=hook_parent), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(hook_input or {}))), \
                mock.patch("sys.stdout", out):
            stop_hook.main()
        return json.loads(out.getvalue()) if out.getvalue() else None

    def test_reads_the_session_lock_where_the_watcher_writes_it(self):
        # Given
        written = watch.SESSION
        # When
        read = stop_hook.SESSION
        # Then
        self.assertEqual(written, read)
        self.assertIn(str(read), stop_hook.reason())

    def test_blocks_the_babysit_session_without_a_watcher(self):
        # Given
        processes = {300: (HOOK, f"{WATCHER} --daemon"), 301: (HOOK, f"{WATCHER} --pending")}
        # When
        verdict = self.run_hook(processes)
        # Then
        self.assertEqual("block", verdict["decision"])

    def test_a_corrupt_session_lock_lets_the_turn_end(self):
        # Given
        self.session.write_text('{"claude_pid": 1')
        # When
        with mock.patch("sys.stderr", io.StringIO()):
            verdict = self.run_hook()
        # Then
        self.assertIsNone(verdict)

    def test_lets_the_turn_end_while_the_watcher_runs(self):
        # Given
        processes = {300: (HOOK, WATCHER)}
        # When
        verdict = self.run_hook(processes)
        # Then
        self.assertIsNone(verdict)

    def test_the_skill_folder_can_have_any_name(self):
        # Given
        link = pathlib.Path(self.session.parent) / "renamed-skill"
        link.symlink_to(stop_hook.WATCH_PY.parent)
        processes = {300: (HOOK, f"python3 {link}/watch.py")}
        # When
        verdict = self.run_hook(processes)
        # Then
        self.assertIsNone(verdict)

    def test_the_skill_path_can_contain_spaces(self):
        # Given
        link = pathlib.Path(self.session.parent) / "my skills" / "babysit"
        link.parent.mkdir()
        link.symlink_to(stop_hook.WATCH_PY.parent)
        processes = {300: (HOOK, f"/usr/bin/python3 {link}/watch.py --interval 60")}
        # When
        verdict = self.run_hook(processes)
        # Then
        self.assertIsNone(verdict)

    def test_an_unrelated_watch_py_does_not_count(self):
        # Given
        processes = {300: (HOOK, "python3 /elsewhere/gerrit-babysit/watch.py")}
        # When
        verdict = self.run_hook(processes)
        # Then
        self.assertEqual("block", verdict["decision"])

    def plugin_versions(self):
        """cache/<marketplace>/<plugin>/{1.0.0,1.1.0}/watch.py, the hook running from 1.0.0."""
        cache = pathlib.Path(self.session.parent).resolve() / "cache"
        old, new = (cache / "market" / "gerrit-babysit" / version / "watch.py" for version in ("1.0.0", "1.1.0"))
        for path in (old, new):
            path.parent.mkdir(parents=True)
            path.touch()
        for name, value in (("PLUGIN_CACHE", cache), ("WATCH_PY", old)):
            patcher = mock.patch.object(stop_hook, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return old, new

    def test_a_watcher_of_a_newer_plugin_version_counts(self):
        # Given
        _, new = self.plugin_versions()
        processes = {300: (HOOK, f"python3 {new}")}
        # When
        verdict = self.run_hook(processes)
        # Then
        self.assertIsNone(verdict)

    def test_a_sibling_skill_of_a_cloned_install_does_not_count(self):
        # Given
        _, new = self.plugin_versions()
        stop_hook_outside_cache = mock.patch.object(stop_hook, "PLUGIN_CACHE", pathlib.Path("/nowhere"))
        processes = {300: (HOOK, f"python3 {new}")}
        # When
        with stop_hook_outside_cache:
            verdict = self.run_hook(processes)
        # Then
        self.assertEqual("block", verdict["decision"])

    def test_the_relaunch_points_at_the_latest_install(self):
        # Given
        _, new = self.plugin_versions()
        # When
        with mock.patch.object(stop_hook.launch, "skill_dir", return_value=new.parent):
            verdict = self.run_hook()
        # Then
        self.assertIn(f"python3 {new}`", verdict["reason"])

    def test_a_watcher_of_another_session_does_not_count(self):
        # Given
        processes = {300: (250, WATCHER)}
        # When
        verdict = self.run_hook(processes)
        # Then
        self.assertEqual("block", verdict["decision"])

    def test_other_sessions_and_a_second_stop_are_left_alone(self):
        # Given
        cases = [{"hook_parent": 250}, {"hook_input": {"stop_hook_active": True}}]
        # When
        verdicts = [self.run_hook(**case) for case in cases]
        # Then
        self.assertEqual([None, None], verdicts)

    def test_no_babysit_session(self):
        # Given
        self.session.unlink()
        # When
        verdict = self.run_hook()
        # Then
        self.assertIsNone(verdict)


if __name__ == "__main__":
    unittest.main()
