"""Run from the skill directory: python3 -m unittest"""

import concurrent.futures
import json
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

# First: points the config at the fixtures before any module reads it.
from fakes import NOW, GREEN_CI, approval, patch_set, message, change, poll_result, inline
import ci
import events
import gerrit
import poll
import daemon
import repo
import status_writer
import watch


def done(value):
    future = concurrent.futures.Future()
    future.set_result(value)
    return future


class OpenThreadsTest(unittest.TestCase):
    def setUp(self):
        poll._threads_memo = poll.PollMemo()

    def test_rest_is_only_called_when_the_change_moves(self):
        # Given
        comments = {"A.kt": [inline("a1", "reviewer", "why?", 1)]}
        with mock.patch.object(gerrit, "rest_get", return_value=comments) as rest:
            # When
            first = poll.open_threads([change(1, lastUpdated=10)])
            second = poll.open_threads([change(1, lastUpdated=10)])
            poll.open_threads([change(1, lastUpdated=11)])
        # Then
        self.assertEqual(first, second)
        self.assertEqual(1, len(first[1]))
        self.assertEqual(2, rest.call_count)

    def test_requirements_are_only_asked_for_voted_changes_and_again_when_they_move(self):
        # Given
        poll._requirements_memo = poll.PollMemo()
        voted = patch_set(1, approval("Code-Review", 2), *GREEN_CI)
        row = {"submit_requirements": [{"name": "Code-Owners", "status": "UNSATISFIED"}]}
        with mock.patch.object(gerrit, "rest_get", return_value=row) as rest:
            # When
            first = poll.submit_blockers([change(1, current=voted, lastUpdated=10), change(2, lastUpdated=10)])
            poll.submit_blockers([change(1, current=voted, lastUpdated=10)])
            poll.submit_blockers([change(1, current=voted, lastUpdated=11)])
        # Then
        self.assertEqual({1: ["Code-Owners"]}, first)
        self.assertEqual(2, rest.call_count)
        rest.assert_called_with("/changes/1?o=SUBMIT_REQUIREMENTS")

    def test_unreadable_requirements_are_unknown(self):
        # Given
        poll._requirements_memo = poll.PollMemo()
        voted = patch_set(1, approval("Code-Review", 2), *GREEN_CI)
        with mock.patch.object(gerrit, "rest_get", side_effect=OSError("HTTP Error 401: Unauthorized")):
            # When
            found = poll.submit_blockers([change(1, current=voted)])
        # Then
        self.assertIsNone(found)

    def test_an_expired_rest_token_does_not_fail_the_poll(self):
        # Given
        ps = patch_set(1, approval("Code-Review", 2), *GREEN_CI)
        with (
            mock.patch.object(poll, "fetch_changes", return_value=[change(current=ps)]),
            mock.patch.object(poll, "parent_statuses", return_value={}),
            mock.patch.object(repo, "merge_conflicts", return_value={}),
            mock.patch.object(repo, "stale_parents", return_value={}),
            mock.patch.object(poll, "fetch_reviews", return_value=[]),
            mock.patch.object(poll, "fetch_attention", return_value=[]),
            mock.patch.object(gerrit, "rest_get", side_effect=OSError("HTTP Error 401: Unauthorized")),
        ):
            # When
            result = poll.poll()
        # Then
        self.assertEqual("HTTP Error 401: Unauthorized", result["threads_error"])
        self.assertEqual([], [e["kind"] for _, e in events.events(result)])

    def test_unknown_threads_leave_human_messages_without_threads(self):
        # Given
        c = change(comments=[message("reviewer", "Patch Set 1:\n\nwhy?", 10)])
        result = {**poll_result([c]), "threads_error": "offline"}
        # When
        found = [e for _, e in events.events(result)]
        # Then
        self.assertEqual([None], [e["threads_awaiting_me"] for e in found])

    def test_changes_without_threads_are_memoized_too(self):
        # Given
        with mock.patch.object(gerrit, "rest_get", return_value={}) as rest:
            # When
            poll.open_threads([change(1, lastUpdated=10)])
            poll.open_threads([change(1, lastUpdated=10)])
        # Then
        rest.assert_called_once_with("/changes/1/comments")


class RememberTest(unittest.TestCase):
    def test_vanished_changes_are_kept_for_a_while_then_forgotten(self):
        # Given
        old = NOW - watch.SEEN_RETENTION_S - 1
        seen = {"1:10:reviewer": old, "2:10:reviewer": NOW - 3600, "3:10:reviewer": old, "cleanup:live:sha": old}
        result = poll_result([change(1)])
        current = {"cleanup:live:sha": {}, "1:20:reviewer": {}}
        # When
        kept = watch.remember(seen, ["1:20:reviewer"], result, current, NOW)
        # Then
        self.assertEqual({"1:10:reviewer": NOW, "1:20:reviewer": NOW, "2:10:reviewer": NOW - 3600, "cleanup:live:sha": NOW}, kept)

    def test_a_restored_change_does_not_replay(self):
        # Given
        c = change(2, comments=[message("reviewer", "old remark", timestamp=10)])
        seen = watch.remember({}, dict(events.events(poll_result([c]))), poll_result([c]), {}, NOW - 86400)
        seen = watch.remember(seen, (), poll_result([]), {}, NOW - 3600)
        # When
        fresh = dict(events.events(poll_result([c]))).keys() - seen.keys()
        # Then
        self.assertEqual(set(), fresh)


class SettleTest(unittest.TestCase):
    def test_a_burst_waits_once_then_goes_out_whole_with_the_latest_versions(self):
        # Given
        reply = {"kind": "message", "body": "v1"}
        verdict = {"kind": "ci_red"}
        # When
        first = watch.settle({}, {"a": reply}, {"a": reply})
        second = watch.settle(first[1], {"b": verdict}, {"a": {**reply, "body": "v2"}, "b": verdict})
        # Then
        self.assertEqual((None, {"a": reply}), first)
        self.assertEqual(({"a": {"kind": "message", "body": "v2"}, "b": verdict}, {}), second)

    def test_other_kinds_go_at_once_and_nothing_new_is_nothing_due(self):
        # When
        found = [watch.settle({}, {"b": {"kind": "ci_red"}}, {}), watch.settle({}, {}, {})]
        # Then
        self.assertEqual([({"b": {"kind": "ci_red"}}, {}), ({}, {})], found)

    def test_events_a_minute_apart_wake_the_session_once(self):
        # Given
        first = poll_result([change(comments=[message("reviewer", "Patch Set 1:\n\nwhy?", 10)])])
        second = poll_result(
            [change(comments=[message("reviewer", "Patch Set 1:\n\nwhy?", 10), message("reviewer", "Patch Set 1: Code-Review-1", 20)])]
        )
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = []
        with (
            mock.patch.object(watch, "STATE", pathlib.Path(tmp.name) / "seen.json"),
            mock.patch.object(watch, "INBOX", pathlib.Path(tmp.name) / "inbox.json"),
            mock.patch.object(watch, "CACHE", pathlib.Path(tmp.name)),
            mock.patch.object(watch, "write_session_lock"),
            mock.patch.object(watch, "daemon_poll", side_effect=[first, second]),
            mock.patch.object(repo, "cleanup_candidates", return_value=[]),
            mock.patch.object(watch.time, "sleep") as sleep,
            mock.patch("sys.argv", ["watch.py"]),
            mock.patch("builtins.print", side_effect=out.append),
        ):
            # When
            watch.main()
        # Then
        sleep.assert_called_once_with(watch.SETTLE_S)
        self.assertEqual(["1:10:reviewer", "1:20:reviewer"], sorted(json.loads((pathlib.Path(tmp.name) / "seen.json").read_text())))
        self.assertEqual(2, len(json.loads(out[0])["events"]))

    def test_a_crash_while_enriching_leaves_the_events_unseen(self):
        # Given
        result = poll_result([change(comments=[message("reviewer", "Patch Set 1:\n\nwhy?", 10)])])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = pathlib.Path(tmp.name) / "seen.json"
        with (
            mock.patch.object(watch, "STATE", state),
            mock.patch.object(watch, "INBOX", pathlib.Path(tmp.name) / "inbox.json"),
            mock.patch.object(watch, "write_session_lock"),
            mock.patch.object(watch, "daemon_poll", return_value=result),
            mock.patch.object(repo, "cleanup_candidates", return_value=[]),
            mock.patch.object(watch, "enrich", side_effect=RuntimeError("boom")),
            mock.patch.object(watch.time, "sleep"),
            mock.patch("sys.argv", ["watch.py"]),
        ):
            # When
            with self.assertRaises(RuntimeError):
                watch.main()
        # Then
        self.assertFalse(state.exists())


NETWORK = {"dns": {"gerrit.example.com": False}, "vpn_tunnel": False, "globalprotect_running": True}


class SessionRetryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = pathlib.Path(tmp.name)
        (self.cache / "status.json").write_text(json.dumps({"updated": 1000.0}))

    def run_main(self, argv, polls, unfinished=()):
        out = []
        with (
            mock.patch.object(watch, "STATE", self.cache / "seen.json"),
            mock.patch.object(watch, "STATUS", self.cache / "status.json"),
            mock.patch.object(status_writer, "STATUS", self.cache / "status.json"),
            mock.patch.object(watch.network, "checks", return_value=NETWORK),
            mock.patch.object(watch, "INBOX", self.cache / "inbox.json"),
            mock.patch.object(watch, "write_session_lock"),
            mock.patch.object(watch, "write_status"),
            mock.patch.object(watch, "daemon_poll", side_effect=polls),
            mock.patch.object(repo, "cleanup_candidates", return_value=[]),
            mock.patch.object(watch, "unfinished_events", return_value=list(unfinished)),
            mock.patch.object(watch, "fetch_drafts", return_value={}),
            mock.patch.object(watch, "enrich", side_effect=lambda events, *_: events),
            mock.patch.object(watch.time, "sleep") as sleep,
            mock.patch("sys.argv", ["watch.py", *argv]),
            mock.patch("builtins.print", side_effect=out.append),
        ):
            code = watch.main()
        return code, [json.loads(line) for line in out], [c.args[0] for c in sleep.call_args_list]

    def test_the_watcher_outlasts_a_long_outage_with_a_capped_backoff(self):
        # Given
        down = watch.DaemonError("Could not resolve hostname")
        back = poll_result([change(comments=[message("reviewer", "Patch Set 1:\n\nwhy?", 10)])])
        (self.cache / "seen.json").write_text(json.dumps({"unreachable:1000.0": 1}))
        # When
        code, out, sleeps = self.run_main([], [down] * 15 + [back, back])
        # Then
        self.assertEqual((0, "events"), (code, out[0]["status"]))
        self.assertEqual(["message"], [e["kind"] for e in out[0]["events"]])
        self.assertEqual(watch.MAX_BACKOFF_S, max(sleeps[:15]))

    def test_a_lasting_outage_wakes_the_session_once_with_the_network_checks(self):
        # Given
        down = watch.DaemonError("Could not resolve hostname")
        # When
        code, (first,), sleeps = self.run_main([], [down] * 3)
        back = poll_result([change(comments=[message("r", "Patch Set 1:\n\n?", 5)])])
        _, (after,), _ = self.run_main(["--ack", first["id"]], [down] * 4 + [back] * 2)
        # Then
        self.assertEqual((0, 2), (code, len(sleeps)))
        self.assertEqual(
            {"kind": "unreachable", "detail": "Could not resolve hostname", "failures": 3, "since": 1000.0, "network": NETWORK},
            first["events"][0],
        )
        self.assertEqual(["message"], [e["kind"] for e in after["events"]])

    def test_a_new_outage_is_reported_again(self):
        # Given
        down = watch.DaemonError("Could not resolve hostname")
        _, (first,), _ = self.run_main([], [down] * 3)
        (self.cache / "status.json").write_text(json.dumps({"updated": 2000.0}))
        # When
        _, (again,), _ = self.run_main(["--ack", first["id"]], [down] * 3)
        # Then
        self.assertEqual(("unreachable", 2000.0), (again["events"][0]["kind"], again["events"][0]["since"]))

    def test_a_lone_event_that_nothing_follows_is_reported_without_waiting(self):
        # Given
        ps = patch_set(1, approval("Code-Review", 2), *GREEN_CI)
        # When
        _, (out,), sleeps = self.run_main([], [poll_result([change(current=ps)], threads={1: []})])
        # Then
        self.assertEqual((["ready_to_submit"], []), ([e["kind"] for e in out["events"]], sleeps))

    def test_a_report_comes_back_until_it_is_acknowledged(self):
        # Given
        why = message("reviewer", "Patch Set 1:\n\nwhy?", 10)
        later = poll_result([change(comments=[why, message("reviewer", "Patch Set 1:\n\nok?", 20)])])
        _, (first,), _ = self.run_main([], [poll_result([change(comments=[why])])] * 2)
        # When
        _, (again,), _ = self.run_main([], [])
        _, (after_ack,), _ = self.run_main(["--ack", first["id"]], [later, later])
        # Then
        self.assertEqual((first["id"], True, first["events"]), (again["id"], again["replayed"], again["events"]))
        self.assertEqual(["Patch Set 1:\n\nok?"], [e["message"] for e in after_ack["events"]])
        self.assertNotIn("replayed", after_ack)

    def test_the_pending_sweep_reports_the_first_failure(self):
        # Given
        down = OSError("Could not resolve hostname")
        # When
        code, out, _ = self.run_main(["--pending"], [down])
        # Then
        self.assertEqual((1, "error", NETWORK), (code, out[0]["status"], out[0]["network"]))

    def test_the_pending_sweep_checks_the_http_password_when_no_change_needs_it(self):
        # Given
        with mock.patch.object(gerrit, "http_error", return_value="HTTP Error 401: Unauthorized") as http_error:
            # When
            code, out, _ = self.run_main(["--pending"], [poll_result([])])
        # Then
        http_error.assert_called_once()
        self.assertEqual((0, "HTTP Error 401: Unauthorized"), (code, out[0]["threads_error"]))

    def test_the_pending_sweep_opens_on_the_work_left_halfway(self):
        # Given
        c = change(comments=[message("reviewer", "Patch Set 1:\n\nwhy?", 10)])
        ps = patch_set(1, approval("Code-Review", 2), *GREEN_CI)
        result = poll_result([c, change(2, current=ps)], threads={1: [{"file": "A.kt"}]})
        unfinished = [("1:unfinished", {"kind": "unfinished", "change": 1, "unpushed": True})]
        # When
        code, out, _ = self.run_main(["--pending"], [result], unfinished)
        # Then
        self.assertEqual(["unfinished", "pending", "ready_to_submit"], [e["kind"] for e in out[0]["events"]])


class SessionPollTest(unittest.TestCase):
    def test_a_slow_but_alive_daemon_wins_the_write(self):
        # Given
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        result = poll_result([change()])

        def slow_poll():
            # The daemon's own round, stuck in fetch retries past STALE_AFTER_S, finishes and publishes while
            # the session is still running its fallback poll.
            daemon.write_daemon_poll(result)
            return result

        heartbeat = pathlib.Path(tmp.name) / "daemon-poll.json"
        with (
            mock.patch.object(watch, "DAEMON_POLL", heartbeat),
            mock.patch.object(daemon, "DAEMON_POLL", heartbeat),
            mock.patch.object(watch, "daemon_poll", return_value=None),
            mock.patch.object(watch, "poll", side_effect=slow_poll),
            mock.patch.object(repo, "worktree_listing", return_value=[]),
            mock.patch.object(repo, "cleanup_candidates", return_value=[]),
            mock.patch.object(watch, "write_status") as write_status,
            mock.patch.object(watch, "record_flaky") as record_flaky,
        ):
            # When
            watch.session_poll()
        # Then
        write_status.assert_not_called()
        record_flaky.assert_not_called()

    def test_the_daemon_cleanup_check_is_read_from_the_snapshot(self):
        # Given
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        status = pathlib.Path(tmp.name) / "status.json"
        candidate = {
            "kind": "cleanup_candidate",
            "branch": "old",
            "worktree": None,
            "change": 7,
            "subject": "fix: x",
            "status": "merged",
        }
        status.write_text(
            json.dumps({"cleanup": [{"key": "cleanup:old:sha", **candidate}, {k: v for k, v in candidate.items() if k != "kind"}]})
        )
        result = poll_result([change()])
        with (
            mock.patch.object(status_writer, "STATUS", status),
            mock.patch.object(watch, "daemon_poll", return_value=result),
            mock.patch.object(repo, "worktree_listing") as listing,
            mock.patch.object(repo, "cleanup_candidates") as check,
        ):
            # When
            found, _, _, cleanup = watch.session_poll()
        # Then
        self.assertEqual((result, {"cleanup:old:sha": candidate}), (found, cleanup))
        listing.assert_not_called()
        check.assert_not_called()

    def test_a_genuinely_dead_daemon_still_falls_back(self):
        # Given
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        result = poll_result([change()])
        with (
            mock.patch.object(watch, "DAEMON_POLL", pathlib.Path(tmp.name) / "daemon-poll.json"),
            mock.patch.object(watch, "daemon_poll", return_value=None),
            mock.patch.object(watch, "poll", return_value=result),
            mock.patch.object(repo, "worktree_listing", return_value=[]),
            mock.patch.object(repo, "cleanup_candidates", return_value=[]),
            mock.patch.object(watch, "write_status") as write_status,
            mock.patch.object(watch, "record_flaky") as record_flaky,
        ):
            # When
            watch.session_poll()
        # Then
        write_status.assert_called_once_with(result, listing=[])
        record_flaky.assert_called_once_with(result)


class UnfinishedEventsTest(unittest.TestCase):
    def test_local_work_and_drafts_merge_per_change(self):
        # Given
        local = {1: {"worktree": "/wt/1", "unpushed": True, "busy": False}}
        drafts = [change(1), change(5, subject="someone else's")]
        with (
            mock.patch.object(repo, "unfinished_work", return_value=local),
            mock.patch.object(gerrit, "query", return_value=drafts) as query,
        ):
            # When
            found = dict(watch.unfinished_events([change(1)], poll.fetch_drafts()))
        # Then
        query.assert_called_once_with(poll.DRAFTS_QUERY)
        self.assertEqual(
            [("/wt/1", True, True), (None, False, True)], [(e["worktree"], e["unpushed"], e["drafts"]) for e in found.values()]
        )
        self.assertEqual(["1:unfinished", "5:unfinished"], list(found))

    def test_an_unreachable_gerrit_still_reports_the_local_work(self):
        # Given
        local = {1: {"worktree": "/wt/1", "unpushed": False, "busy": True}}
        with (
            mock.patch.object(repo, "unfinished_work", return_value=local),
            mock.patch.object(gerrit, "query", side_effect=OSError("offline")),
        ):
            # When
            found = dict(watch.unfinished_events([change(1)], poll.fetch_drafts()))
        # Then
        self.assertEqual([(True, False)], [(e["busy"], e["drafts"]) for e in found.values()])


class ParentStatusesTest(unittest.TestCase):
    def test_mine_are_open_and_others_are_queried(self):
        # Given
        changes = [change(1, dependsOn=[{"number": 2}]), change(2, dependsOn=[{"number": 50}]), change(3)]
        merged = {"number": 50, "status": "MERGED", "currentPatchSet": {"revision": "r50"}}
        # When
        with mock.patch.object(gerrit, "query", return_value=[merged]) as query:
            statuses = poll.parent_statuses(changes)
        # Then
        self.assertEqual({1: (2, "NEW", patch_set()), 2: (50, "MERGED", {"revision": "r50"})}, statuses)
        query.assert_called_once_with("change:50", "--current-patch-set")


class OutdatedParentsTest(unittest.TestCase):
    def test_open_parent_with_a_newer_patch_set(self):
        # Given
        child = change(1, current=patch_set(1, parents=("old",)))
        on_top = change(2, current=patch_set(1, parents=("new",)))
        parent_ps = {"number": 3, "revision": "new", "ref": "refs/changes/09/9/3"}
        # When
        outdated = poll.outdated_parents([child, on_top], {1: (9, "NEW", parent_ps), 2: (9, "NEW", parent_ps)})
        # Then
        self.assertEqual(
            {
                1: {
                    "parent": 9,
                    "parent_patch_set": 3,
                    "parent_ref": "refs/changes/09/9/3",
                    "old_parent_sha": "old",
                    "new_parent_sha": "new",
                }
            },
            outdated,
        )

    def test_root_commit_has_no_parent(self):
        # Given: Gerrit sends parents: [] for a root commit, not a missing key.
        child = change(1, current=patch_set(1, parents=()))
        # When
        outdated = poll.outdated_parents([child], {1: (9, "NEW", {"revision": "new"})})
        # Then
        self.assertEqual({}, outdated)

    def test_merged_parents_are_left_to_stale_parents(self):
        # Given
        child = change(1, current=patch_set(1, parents=("old",)))
        # When
        outdated = poll.outdated_parents([child], {1: (9, "MERGED", {"revision": "new"})})
        # Then
        self.assertEqual({}, outdated)

    def test_parent_updated_once_per_parent_patch_set_and_before_conflicts(self):
        # Given
        outdated = {1: {"parent": 9, "parent_patch_set": 3, "parent_ref": "r", "old_parent_sha": "old", "new_parent_sha": "new"}}
        result = {**poll_result([change()], conflicts={1: ["A.kt"]}), "outdated_parents": outdated}
        # When
        found = dict(events.events(result))
        # Then
        self.assertEqual(["1:parent_updated:1:new"], [k for k, e in found.items() if e["kind"] != "message"])
        self.assertEqual("1 · parent 9 has a new patch set", events.notification(found["1:parent_updated:1:new"])[0])


class SnoozeFilterTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(watch.snooze, "SNOOZE", pathlib.Path(tmp.name) / "snooze.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_events_of_a_snoozed_change_are_held_back_until_the_next_patch_set(self):
        # Given
        comments = [message("reviewer", "Patch Set 1:\n\nwhy?", 10)]
        watch.snooze.set_snooze(1, patch_set=1)
        before = poll_result([change(comments=comments), change(2, comments=comments)])
        after = poll_result([change(current=patch_set(2), comments=comments)])
        # When
        held = watch.awake(events.events(before, NOW), watch.snoozed_changes(before, NOW))
        back = watch.awake(events.events(after, NOW), watch.snoozed_changes(after, NOW))
        # Then
        self.assertEqual(["2:10:reviewer"], list(held))
        self.assertIn("1:10:reviewer", back)


class MyAttentionSetsTest(unittest.TestCase):
    def test_my_changes_attention_sets_by_number_and_none_without_rest(self):
        # Given
        alice = {"account": {"username": "alice", "name": "Alice"}, "last_update": "2026-09-29 08:00:00.000000000"}
        rows = [{"_number": 7, "attention_set": {"1": alice}}]
        # When
        with mock.patch.object(gerrit, "rest_get", return_value=rows) as rest:
            found = poll.my_attention_sets()
        with mock.patch.object(gerrit, "rest_get", side_effect=OSError("HTTP Error 401: Unauthorized")):
            failed = poll.my_attention_sets()
        # Then
        rest.assert_called_once_with("/changes/?q=owner%3Aself%20status%3Aopen&o=DETAILED_ACCOUNTS")
        self.assertEqual(
            ({7: {"holders": [{"username": "alice", "name": "Alice", "since": 1790668800.0, "reason": ""}], "removed": []}}, None),
            (found, failed),
        )


RED = {
    "result": "FAILURE",
    "end_time": "2026-09-29T12:00:00",
    "log_url": "https://logs/p/",
    "red_since": "2026-09-29T10:00:00",
    "failures": 2,
    "last_green": "2026-09-29T09:00:00",
}


class BaseHealthTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(watch.snooze, "SNOOZE", pathlib.Path(tmp.name) / "snooze.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_target_branch_is_asked_once_per_poll(self):
        # Given
        changes = [change(1), change(2), change(3, branch="release")]
        # When
        with (
            mock.patch.object(ci, "ZUUL_API", "https://zuul/api"),
            mock.patch.object(ci, "PERIODIC_BUILD", {"pipeline": "periodic", "job": "build"}),
            mock.patch.object(ci, "base_health", side_effect=lambda branch: {"branch": branch}) as base_health,
        ):
            found = poll.base_health(changes)
        with mock.patch.object(ci, "PERIODIC_BUILD", None):
            unset = poll.base_health(changes)
        # Then
        self.assertEqual({"main": {"branch": "main"}, "release": {"branch": "release"}}, found)
        self.assertEqual(2, base_health.call_count)
        self.assertEqual({}, unset)

    def test_target_branches_are_asked_side_by_side(self):
        # Given
        changes = [change(1), change(2), change(3, branch="release")]
        barrier = threading.Barrier(2, timeout=5)

        def fetch(branch):
            barrier.wait()
            return {"branch": branch}

        # When
        with (
            mock.patch.object(ci, "ZUUL_API", "https://zuul/api"),
            mock.patch.object(ci, "PERIODIC_BUILD", {"pipeline": "periodic", "job": "build"}),
            mock.patch.object(ci, "base_health", side_effect=fetch),
        ):
            found = poll.base_health(changes)
        # Then: both branches reached the barrier, so neither call waited on the other to start.
        self.assertEqual({"main": {"branch": "main"}, "release": {"branch": "release"}}, found)

    def test_a_red_base_is_announced_once_for_all_its_changes(self):
        # Given
        red = {
            **poll_result([change(1), change(2), change(3, branch="release")]),
            "base_health": {"main": RED, "release": {"result": "SUCCESS"}},
        }
        longer = {**red, "base_health": {"main": {**RED, "end_time": "2026-09-29T14:00:00", "failures": 3}}}
        again = {**red, "base_health": {"main": {**RED, "last_green": "2026-09-29T13:00:00"}}}
        # When
        found = [{k: e for k, e in events.events(result, NOW) if e["kind"] == "base_red"} for result in (red, longer, again)]
        # Then
        self.assertEqual(["base_red:main:2026-09-29T09:00:00"], list(found[0]))
        self.assertEqual([1, 2], found[0]["base_red:main:2026-09-29T09:00:00"]["changes"])
        self.assertEqual(found[0].keys(), found[1].keys())
        self.assertNotEqual(found[0].keys(), found[2].keys())

    def test_the_notification_says_since_when(self):
        # Given
        event = {"kind": "base_red", "branch": "main", "changes": [1, 2], **RED}
        # When
        title, body = events.notification(event, ci.iso_to_epoch("2026-09-29T15:30:00"))
        # Then
        self.assertEqual("main red for 5 h", title)
        self.assertIn("1, 2", body)

    def test_snoozed_changes_leave_the_announcement(self):
        # Given
        watch.snooze.set_snooze(1, patch_set=1)
        result = {**poll_result([change(1), change(2)]), "base_health": {"main": RED}}
        watch.snooze.set_snooze(2, until=NOW + 3600)
        both = watch.awake(events.events(result, NOW), watch.snoozed_changes(result, NOW))
        watch.snooze.wake(2)
        # When
        one = watch.awake(events.events(result, NOW), watch.snoozed_changes(result, NOW))
        # Then
        self.assertEqual([], [e for e in both.values() if e["kind"] == "base_red"])
        self.assertEqual([[2]], [e["changes"] for e in one.values() if e["kind"] == "base_red"])

    def test_a_base_green_snooze_holds_while_red_or_unknown(self):
        # Given
        watch.snooze.set_snooze(1, base_green=True)
        polls = [
            {**poll_result([change(1)]), "base_health": {"main": health}}
            for health in (RED, {"error": "offline"}, {"result": "SUCCESS"})
        ]
        # When
        found = [bool(watch.snoozed_changes(result, NOW)) for result in polls]
        # Then
        self.assertEqual([True, True, False], found)


class WithIntKeysTest(unittest.TestCase):
    def test_attention_sets_come_back_with_int_keys_and_none_stays_none(self):
        # Given
        polls = [{**poll_result(), "attention_sets": {7: {"holders": [], "removed": []}}}, {**poll_result(), "attention_sets": None}]
        # When
        found = [poll.with_int_keys(json.loads(json.dumps(p))) for p in polls]
        # Then
        self.assertEqual(polls, found)

    def test_change_numbers_come_back_as_ints(self):
        # Given
        polled = {**poll_result(conflicts={1: ["A.kt"]}, parents={2: 3}), "outdated_parents": {}, "attention": [4]}
        payload = json.loads(json.dumps(polled))
        # When
        result = poll.with_int_keys(payload)
        # Then
        self.assertEqual(polled, result)


class ClaudePidTest(unittest.TestCase):
    def test_the_closest_claude_ancestor_in_one_ps_call(self):
        # Given
        table = {40: (30, "/bin/zsh"), 30: (20, "/Users/me/.local/bin/claude"), 20: (1, "claude"), 50: (1, "claude")}
        # When
        with (
            mock.patch.object(watch.procs, "processes", return_value=table) as processes,
            mock.patch.object(watch.os, "getppid", return_value=40),
        ):
            pid = watch.claude_pid()
        # Then
        self.assertEqual(30, pid)
        processes.assert_called_once_with("comm")

    def test_none_outside_claude(self):
        # Given
        table = {40: (1, "/bin/zsh")}
        # When
        with mock.patch.object(watch.procs, "processes", return_value=table), mock.patch.object(watch.os, "getppid", return_value=40):
            pid = watch.claude_pid()
        # Then
        self.assertIsNone(pid)


if __name__ == "__main__":
    unittest.main()
