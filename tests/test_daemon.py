"""Run from the skill directory: python3 -m unittest"""

import contextlib
import io
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

# First: points the config at the fixtures before any module reads it.
from fakes import NOW, change, message, poll_result
import daemon
import events
import gerrit
import snooze
import watch


class DaemonTest(unittest.TestCase):
    class Stop(Exception):
        pass

    def run_daemon(self, rounds, **patches):
        sleeps = self.sleeps = mock.Mock(side_effect=[None] * (rounds - 1) + [self.Stop()])
        stderr = io.StringIO()
        with (
            mock.patch.object(daemon.time, "sleep", sleeps),
            mock.patch.object(daemon, "swiftbar"),
            mock.patch.object(daemon, "write_daemon_poll"),
            mock.patch("sys.stderr", stderr),
            contextlib.ExitStack() as stack,
        ):
            for name, value in patches.items():
                # The daemon's own names on daemon, the shared ones (write_status…) on watch.
                stack.enter_context(mock.patch.object(daemon if hasattr(daemon, name) else watch, name, value))
            with self.assertRaises(self.Stop):
                daemon.run(60)
        return stderr.getvalue()

    def test_a_bug_after_the_poll_is_logged_once_and_the_daemon_keeps_going(self):
        # Given
        status = mock.Mock()
        # When
        log = self.run_daemon(
            3, poll=mock.Mock(return_value={}), write_status=status, daemon_round=mock.Mock(side_effect=KeyError("number"))
        )
        # Then
        self.assertEqual(1, log.count("Traceback"))
        self.assertEqual([mock.call(error="KeyError: 'number'")] * 3, status.call_args_list)

    def test_a_plugin_update_ends_the_daemon_after_its_round(self):
        # Given
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        launcher = pathlib.Path(tmp.name) / "launch.py"
        launcher.write_text("import pathlib\ndef skill_dir():\n    return pathlib.Path('/elsewhere/1.4.2')\n")
        round_ = mock.Mock()
        # When
        with (
            mock.patch.dict(daemon.os.environ, {"GERRIT_BABYSIT_LAUNCHER": str(launcher)}),
            mock.patch.object(daemon, "swiftbar"),
            mock.patch.object(daemon, "write_daemon_poll"),
            mock.patch.object(daemon, "poll", return_value={}),
            mock.patch.object(daemon, "daemon_round", round_),
            mock.patch("sys.stderr", io.StringIO()) as stderr,
        ):
            code = daemon.run(60)
        # Then
        self.assertEqual((0, 1), (code, round_.call_count))
        self.assertIn("restarting", stderr.getvalue())

    def test_a_repeated_poll_failure_is_logged_once(self):
        # Given
        down = OSError("Could not resolve hostname")
        # When
        log = self.run_daemon(3, poll=mock.Mock(side_effect=[down, down, {}]), write_status=mock.Mock(), daemon_round=mock.Mock())
        # Then
        self.assertEqual(1, log.count("poll failed"))
        self.assertIn("recovered", log)

    def outage(self, rounds, first_run=False, quiet=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache = pathlib.Path(tmp.name)
        state = cache / "daemon-seen.json"
        if not first_run:
            state.write_text("{}")
        (cache / "status.json").write_text(json.dumps({"updated": 1000.0}))
        notify = mock.Mock()
        down = OSError("Could not resolve hostname")
        with (
            mock.patch.object(daemon, "DAEMON_STATE", state),
            mock.patch.object(watch, "STATUS", cache / "status.json"),
            mock.patch.object(daemon.events, "is_quiet", return_value=quiet),
        ):
            self.run_daemon(
                rounds,
                poll=mock.Mock(side_effect=down),
                write_status=mock.Mock(),
                swiftbar=mock.Mock(side_effect=lambda action, **p: action == "notify" and notify(p)),
            )
        return notify

    def test_a_lasting_outage_notifies_once_pointing_at_the_vpn(self):
        # When
        notify = self.outage(6)
        # Then
        notify.assert_called_once()
        self.assertIn("VPN", notify.call_args.args[0]["body"])

    def test_no_outage_notification_on_a_first_install_or_at_night(self):
        # When
        silent = [self.outage(4, first_run=True), self.outage(4, quiet=True), self.outage(2)]
        # Then
        self.assertEqual([0, 0, 0], [n.call_count for n in silent])

    def test_the_heartbeat_names_the_daemons_version(self):
        # Given
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        heartbeat = pathlib.Path(tmp.name) / "daemon-poll.json"
        # When
        with mock.patch.object(daemon, "DAEMON_POLL", heartbeat):
            daemon.write_daemon_poll({"changes": []})
        # Then
        self.assertEqual(
            (daemon.VERSION, str(daemon.SKILL_DIR)),
            (json.loads(heartbeat.read_text())["version"], json.loads(heartbeat.read_text())["skill_dir"]),
        )

    def test_the_poll_duration_comes_off_the_wait(self):
        # Given
        sleep = mock.Mock(side_effect=self.Stop())
        clock = mock.Mock(side_effect=[100.0, 125.0])
        # When
        with (
            mock.patch.object(daemon.time, "sleep", sleep),
            mock.patch.object(daemon.time, "monotonic", clock),
            mock.patch.object(daemon, "poll", return_value={}),
            mock.patch.object(daemon, "daemon_round"),
            mock.patch.object(daemon, "write_daemon_poll"),
            self.assertRaises(self.Stop),
        ):
            daemon.run(60)
        # Then
        sleep.assert_called_once_with(35.0)

    def test_the_ssh_session_cap_waits_for_the_lingering_sessions_to_time_out(self):
        # Given
        stderr = "Received disconnect from x port 29418:12: Too many concurrent connections (10)\nDisconnected\n"
        capped = subprocess.CalledProcessError(255, ["git", "fetch"], stderr=stderr)
        # When
        self.run_daemon(2, poll=mock.Mock(side_effect=capped), write_status=mock.Mock())
        # Then
        self.assertTrue(all(c.args[0] > 60 * 9 for c in self.sleeps.call_args_list))

    def test_another_failure_retries_on_the_next_round(self):
        # Given
        down = subprocess.CalledProcessError(255, ["ssh"], stderr="ssh: Could not resolve hostname\n")
        # When
        self.run_daemon(2, poll=mock.Mock(side_effect=down), write_status=mock.Mock())
        # Then
        self.assertTrue(all(c.args[0] <= 60 for c in self.sleeps.call_args_list))

    def test_a_bug_inside_the_poll_does_not_crash_the_daemon(self):
        # Given
        status = mock.Mock()
        # When
        log = self.run_daemon(2, poll=mock.Mock(side_effect=KeyError("status")), write_status=status)
        # Then
        self.assertEqual(1, log.count("Traceback"))
        self.assertEqual([mock.call(error="KeyError: 'status'")] * 2, status.call_args_list)

    def test_the_poll_is_published_even_if_the_status_write_fails(self):
        # Given
        published = mock.Mock()
        # When
        with (
            mock.patch.object(daemon, "write_daemon_poll", published),
            mock.patch.object(daemon, "write_status", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            daemon.daemon_round({"changes": []}, first_run=True)
        # Then
        published.assert_called_once_with({"changes": []})


class SwiftbarTest(unittest.TestCase):
    def test_a_hung_swiftbar_does_not_kill_the_daemon(self):
        # Given
        hung = subprocess.TimeoutExpired("open", 10)
        # When
        with mock.patch.object(daemon.subprocess, "run", side_effect=hung) as run:
            daemon.swiftbar("refreshplugin", name="gerrit")
        # Then
        run.assert_called_once()


class AutoSubmitAllTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tagged = pathlib.Path(tmp.name) / "tagged.json"
        for patcher in (
            mock.patch.object(daemon, "AUTO_SUBMIT_TAGGED", self.tagged),
            mock.patch.dict(daemon.CONFIG, auto_submit_all=True, auto_submit_hashtag="auto-submit"),
            mock.patch.object(daemon, "log"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_tagging(self, *changes, fail=None):
        with mock.patch.object(gerrit, "rest_post", side_effect=fail) as post:
            daemon.tag_auto_submit({"changes": list(changes)})
        return [c.args[0] for c in post.call_args_list]

    def test_ready_changes_get_the_hashtag_but_not_wip_or_already_tagged_ones(self):
        # When
        posted = self.run_tagging(change(1), change(2, wip=True), change(3, hashtags=["auto-submit"]))
        # Then
        self.assertEqual(["/changes/1/hashtags"], posted)
        self.assertEqual({"1": True, "3": True}, json.loads(self.tagged.read_text()))

    def test_a_hashtag_removed_by_hand_is_not_added_again(self):
        # Given
        self.run_tagging(change(1))
        # When
        posted = self.run_tagging(change(1))
        # Then
        self.assertEqual([], posted)

    def test_a_change_out_of_wip_gets_it(self):
        # Given
        self.run_tagging(change(1, wip=True))
        # When
        posted = self.run_tagging(change(1))
        # Then
        self.assertEqual(["/changes/1/hashtags"], posted)

    def test_a_failed_post_is_retried_on_the_next_round(self):
        # Given
        self.run_tagging(change(1), fail=OSError("down"))
        # When
        posted = self.run_tagging(change(1))
        # Then
        self.assertEqual(["/changes/1/hashtags"], posted)

    def test_merged_changes_leave_the_memory(self):
        # Given
        self.run_tagging(change(1), change(2))
        # When
        self.run_tagging(change(2))
        # Then
        self.assertEqual({"2": True}, json.loads(self.tagged.read_text()))

    def test_off_by_default(self):
        # When
        with mock.patch.dict(daemon.CONFIG, auto_submit_all=False):
            posted = self.run_tagging(change(1))
        # Then
        self.assertEqual([], posted)


class SnoozedRoundTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(snooze, "SNOOZE", pathlib.Path(tmp.name) / "snooze.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_daemon_neither_notifies_nor_marks_a_snoozed_change_seen(self):
        # Given
        snooze.set_snooze(1, until=NOW + 3600)
        result = poll_result([change(comments=[message("reviewer", "Patch Set 1:\n\nwhy?", 10)])])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = pathlib.Path(tmp.name) / "daemon-seen.json"
        # When
        with (
            mock.patch.object(daemon, "DAEMON_STATE", state),
            mock.patch.object(daemon, "write_daemon_poll"),
            mock.patch.object(daemon, "write_status"),
            mock.patch.object(daemon, "record_flaky"),
            mock.patch.object(events, "is_quiet", return_value=False),
            mock.patch.object(daemon, "swiftbar") as swiftbar,
        ):
            daemon.daemon_round(result, first_run=False)
        # Then
        self.assertEqual([], [c for c in swiftbar.call_args_list if c.args[0] == "notify"])
        self.assertEqual({}, json.loads(state.read_text()))
