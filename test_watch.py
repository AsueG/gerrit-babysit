"""Run from this directory: python3 -m unittest"""
import concurrent.futures
import contextlib
import io
import json
import pathlib
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

# First: points the config at the fixtures before any module reads it.
from fakes import ZUUL, NOW, GREEN_CI, approval, patch_set, message, change, poll_result, inline, zuul_verdict
import ci
import events
import gerrit
import repo
import watch


def done(value):
    future = concurrent.futures.Future()
    future.set_result(value)
    return future


class OpenThreadsTest(unittest.TestCase):
    def setUp(self):
        watch._threads_memo = watch.PollMemo()

    def test_rest_is_only_called_when_the_change_moves(self):
        # Given
        comments = {"A.kt": [inline("a1", "reviewer", "why?", 1)]}
        with mock.patch.object(gerrit, "rest_get", return_value=comments) as rest:
            # When
            first = watch.open_threads([change(1, lastUpdated=10)])
            second = watch.open_threads([change(1, lastUpdated=10)])
            watch.open_threads([change(1, lastUpdated=11)])
        # Then
        self.assertEqual(first, second)
        self.assertEqual(1, len(first[1]))
        self.assertEqual(2, rest.call_count)

    def test_requirements_are_only_asked_for_voted_changes_and_again_when_they_move(self):
        # Given
        watch._requirements_memo = watch.PollMemo()
        voted = patch_set(1, approval("Code-Review", 2), *GREEN_CI)
        row = {"submit_requirements": [{"name": "Code-Owners", "status": "UNSATISFIED"}]}
        with mock.patch.object(gerrit, "rest_get", return_value=row) as rest:
            # When
            first = watch.submit_blockers([change(1, current=voted, lastUpdated=10), change(2, lastUpdated=10)])
            watch.submit_blockers([change(1, current=voted, lastUpdated=10)])
            watch.submit_blockers([change(1, current=voted, lastUpdated=11)])
        # Then
        self.assertEqual({1: ["Code-Owners"]}, first)
        self.assertEqual(2, rest.call_count)
        rest.assert_called_with("/changes/1?o=SUBMIT_REQUIREMENTS")

    def test_unreadable_requirements_are_unknown(self):
        # Given
        watch._requirements_memo = watch.PollMemo()
        voted = patch_set(1, approval("Code-Review", 2), *GREEN_CI)
        with mock.patch.object(gerrit, "rest_get", side_effect=OSError("HTTP Error 401: Unauthorized")):
            # When
            found = watch.submit_blockers([change(1, current=voted)])
        # Then
        self.assertIsNone(found)

    def test_an_expired_rest_token_does_not_fail_the_poll(self):
        # Given
        ps = patch_set(1, approval("Code-Review", 2), *GREEN_CI)
        with mock.patch.object(watch, "fetch_changes", return_value=[change(current=ps)]), \
                mock.patch.object(watch, "parent_statuses", return_value={}), \
                mock.patch.object(repo, "merge_conflicts", return_value={}), \
                mock.patch.object(repo, "stale_parents", return_value={}), \
                mock.patch.object(watch, "fetch_reviews", return_value=[]), \
                mock.patch.object(watch, "fetch_attention", return_value=[]), \
                mock.patch.object(gerrit, "rest_get", side_effect=OSError("HTTP Error 401: Unauthorized")):
            # When
            result = watch.poll()
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
            watch.open_threads([change(1, lastUpdated=10)])
            watch.open_threads([change(1, lastUpdated=10)])
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
        self.assertEqual({"1:10:reviewer": NOW, "1:20:reviewer": NOW, "2:10:reviewer": NOW - 3600,
                          "cleanup:live:sha": NOW}, kept)

    def test_a_restored_change_does_not_replay(self):
        # Given
        c = change(2, comments=[message("reviewer", "old remark", timestamp=10)])
        seen = watch.remember({}, dict(events.events(poll_result([c]))), poll_result([c]), {}, NOW - 86400)
        seen = watch.remember(seen, (), poll_result([]), {}, NOW - 3600)
        # When
        fresh = dict(events.events(poll_result([c]))).keys() - seen.keys()
        # Then
        self.assertEqual(set(), fresh)


class EnrichTest(unittest.TestCase):
    def test_stuck_ci_gets_the_zuul_queue_and_requests_the_prereview_path(self):
        # Given
        stuck = {"kind": "ci_stuck", "change": 1, "patch_set": 2}
        requested = {"kind": "review_requested", "change": 3, "patch_set": 4, "participated": False}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # When
        with mock.patch.object(ci, "zuul_queue", return_value=[]), \
                mock.patch.object(watch, "CACHE", pathlib.Path(tmp.name)):
            watch.enrich([stuck, requested])
        # Then
        self.assertEqual([], stuck["zuul_queue"])
        self.assertEqual({"path": f"{tmp.name}/prereview/3-4.md", "done": False}, requested["prereview"])

    def test_a_job_red_on_several_changes_fetches_its_history_once(self):
        # Given
        events = [{"kind": "message", "change": n, "branch": "main", "author_username": "zuul",
                   "message": ZUUL["failed"]["message"]} for n in (1, 2)]
        build = {"log_url": "https://logs/x/", "end_time": "2026-09-29T12:00:00", "artifacts": []}
        answers = {"/build/": json.dumps(build), "job-output.txt": "", "zuul-file-comments.json": "{}",
                   "/builds?": "[]"}
        get = lambda url: next(body for marker, body in answers.items() if marker in url)
        jobs = {j["job"] for j in ci.failed_jobs(ZUUL["failed"]["message"])}
        # When
        with mock.patch.object(ci, "http_get", side_effect=get) as http, \
                mock.patch.object(ci, "ZUUL_API", "https://zuul/api"), \
                mock.patch.object(ci, "PERIODIC_BUILD", None):
            watch.enrich(events)
        # Then
        self.assertEqual(len(jobs), sum("/builds?" in c.args[0] for c in http.call_args_list))

    def test_failures_reuse_the_polled_base_health(self):
        # Given
        failures = [{"kind": "message", "change": n, "branch": branch, "author_username": "zuul", "message": "m"}
                    for n, branch in ((1, "main"), (2, "main"), (3, "release"))]
        base = {"main": {"result": "FAILURE"}, "release": {"result": "SUCCESS"}}
        # When
        with mock.patch.object(ci, "ZUUL_API", "https://zuul/api"), \
                mock.patch.object(ci, "PERIODIC_BUILD", {"pipeline": "periodic", "job": "build"}), \
                mock.patch.object(ci, "submit_diagnosis", side_effect=lambda _, event, *__: [done(event["change"])]), \
                mock.patch.object(ci, "base_health") as base_health:
            watch.enrich(failures, base)
        # Then
        base_health.assert_not_called()
        self.assertEqual([([1], "FAILURE"), ([2], "FAILURE"), ([3], "SUCCESS")],
                         [(f["ci_diagnosis"], f["base_build"]["result"]) for f in failures])

    def test_only_ci_failures_and_new_patch_sets_get_extras(self):
        # Given
        failure = {"kind": "message", "change": 1, "branch": "main", "author_username": "zuul", "message": "m"}
        human = {"kind": "message", "change": 1, "branch": "main", "author_username": "reviewer", "message": "m"}
        patch = {"kind": "review_new_patch_set", "change": 2, "since_ref": "a", "current_ref": "b"}
        # When
        with mock.patch.object(ci, "submit_diagnosis", return_value=[done("d")]), \
                mock.patch.object(repo, "interdiff", return_value={"stat": "s"}):
            watch.enrich([failure, human, patch], {"main": {"result": "SUCCESS"}})
        # Then
        self.assertEqual((["d"], {"result": "SUCCESS"}), (failure["ci_diagnosis"], failure["base_build"]))
        self.assertNotIn("ci_diagnosis", human)
        self.assertEqual({"stat": "s"}, patch["interdiff"])


class SettleTest(unittest.TestCase):
    def test_events_a_minute_apart_wake_the_session_once(self):
        # Given
        first = poll_result([change(comments=[message("reviewer", "Patch Set 1:\n\nwhy?", 10)])])
        second = poll_result([change(comments=[message("reviewer", "Patch Set 1:\n\nwhy?", 10),
                                               message("reviewer", "Patch Set 1: Code-Review-1", 20)])])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = []
        with mock.patch.object(watch, "STATE", pathlib.Path(tmp.name) / "seen.json"), \
                mock.patch.object(watch, "CACHE", pathlib.Path(tmp.name)), \
                mock.patch.object(watch, "write_session_lock"), \
                mock.patch.object(watch, "daemon_poll", side_effect=[first, second]), \
                mock.patch.object(repo, "cleanup_candidates", return_value=[]), \
                mock.patch.object(watch.time, "sleep") as sleep, \
                mock.patch("sys.argv", ["watch.py"]), \
                mock.patch("builtins.print", side_effect=out.append):
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
        with mock.patch.object(watch, "STATE", state), \
                mock.patch.object(watch, "write_session_lock"), \
                mock.patch.object(watch, "daemon_poll", return_value=result), \
                mock.patch.object(repo, "cleanup_candidates", return_value=[]), \
                mock.patch.object(watch, "enrich", side_effect=RuntimeError("boom")), \
                mock.patch.object(watch.time, "sleep"), \
                mock.patch("sys.argv", ["watch.py"]):
            # When
            with self.assertRaises(RuntimeError):
                watch.main()
        # Then
        self.assertFalse(state.exists())


class SessionRetryTest(unittest.TestCase):
    def run_main(self, argv, polls, unfinished=()):
        out = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(watch, "STATE", pathlib.Path(tmp.name) / "seen.json"), \
                mock.patch.object(watch, "write_session_lock"), \
                mock.patch.object(watch, "write_status"), \
                mock.patch.object(watch, "daemon_poll", side_effect=polls), \
                mock.patch.object(repo, "cleanup_candidates", return_value=[]), \
                mock.patch.object(watch, "unfinished_events", return_value=list(unfinished)), \
                mock.patch.object(watch, "fetch_drafts", return_value={}), \
                mock.patch.object(watch, "enrich", side_effect=lambda events, *_: events), \
                mock.patch.object(watch.time, "sleep") as sleep, \
                mock.patch("sys.argv", ["watch.py", *argv]), \
                mock.patch("builtins.print", side_effect=out.append):
            code = watch.main()
        return code, [json.loads(line) for line in out], [c.args[0] for c in sleep.call_args_list]

    def test_the_watcher_outlasts_a_long_outage_with_a_capped_backoff(self):
        # Given
        down = watch.DaemonError("Could not resolve hostname")
        back = poll_result([change(comments=[message("reviewer", "Patch Set 1:\n\nwhy?", 10)])])
        # When
        code, out, sleeps = self.run_main([], [down] * 15 + [back, back])
        # Then
        self.assertEqual((0, "events"), (code, out[0]["status"]))
        self.assertEqual(watch.MAX_BACKOFF_S, max(sleeps[:15]))

    def test_the_pending_sweep_reports_the_first_failure(self):
        # Given
        down = OSError("Could not resolve hostname")
        # When
        code, out, _ = self.run_main(["--pending"], [down])
        # Then
        self.assertEqual((1, "error"), (code, out[0]["status"]))

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


class UnfinishedEventsTest(unittest.TestCase):
    def test_local_work_and_drafts_merge_per_change(self):
        # Given
        local = {1: {"worktree": "/wt/1", "unpushed": True, "busy": False}}
        drafts = [change(1), change(5, subject="someone else's")]
        with mock.patch.object(repo, "unfinished_work", return_value=local), \
                mock.patch.object(gerrit, "query", return_value=drafts) as query:
            # When
            found = dict(watch.unfinished_events([change(1)], watch.fetch_drafts()))
        # Then
        query.assert_called_once_with(watch.DRAFTS_QUERY)
        self.assertEqual([("/wt/1", True, True), (None, False, True)],
                         [(e["worktree"], e["unpushed"], e["drafts"]) for e in found.values()])
        self.assertEqual(["1:unfinished", "5:unfinished"], list(found))

    def test_an_unreachable_gerrit_still_reports_the_local_work(self):
        # Given
        local = {1: {"worktree": "/wt/1", "unpushed": False, "busy": True}}
        with mock.patch.object(repo, "unfinished_work", return_value=local), \
                mock.patch.object(gerrit, "query", side_effect=OSError("offline")):
            # When
            found = dict(watch.unfinished_events([change(1)], watch.fetch_drafts()))
        # Then
        self.assertEqual([(True, False)], [(e["busy"], e["drafts"]) for e in found.values()])


class SwiftbarTest(unittest.TestCase):
    def test_a_hung_swiftbar_does_not_kill_the_daemon(self):
        # Given
        hung = subprocess.TimeoutExpired("open", 10)
        # When
        with mock.patch.object(watch.subprocess, "run", side_effect=hung) as run:
            watch.swiftbar("refreshplugin", name="gerrit")
        # Then
        run.assert_called_once()


class ParentStatusesTest(unittest.TestCase):
    def test_mine_are_open_and_others_are_queried(self):
        # Given
        changes = [change(1, dependsOn=[{"number": 2}]), change(2, dependsOn=[{"number": 50}]), change(3)]
        merged = {"number": 50, "status": "MERGED", "currentPatchSet": {"revision": "r50"}}
        # When
        with mock.patch.object(gerrit, "query", return_value=[merged]) as query:
            statuses = watch.parent_statuses(changes)
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
        outdated = watch.outdated_parents([child, on_top], {1: (9, "NEW", parent_ps), 2: (9, "NEW", parent_ps)})
        # Then
        self.assertEqual({1: {"parent": 9, "parent_patch_set": 3, "parent_ref": "refs/changes/09/9/3",
                              "old_parent_sha": "old", "new_parent_sha": "new"}}, outdated)

    def test_root_commit_has_no_parent(self):
        # Given: Gerrit sends parents: [] for a root commit, not a missing key.
        child = change(1, current=patch_set(1, parents=()))
        # When
        outdated = watch.outdated_parents([child], {1: (9, "NEW", {"revision": "new"})})
        # Then
        self.assertEqual({}, outdated)

    def test_merged_parents_are_left_to_stale_parents(self):
        # Given
        child = change(1, current=patch_set(1, parents=("old",)))
        # When
        outdated = watch.outdated_parents([child], {1: (9, "MERGED", {"revision": "new"})})
        # Then
        self.assertEqual({}, outdated)

    def test_parent_updated_once_per_parent_patch_set_and_before_conflicts(self):
        # Given
        outdated = {1: {"parent": 9, "parent_patch_set": 3, "parent_ref": "r", "old_parent_sha": "old",
                        "new_parent_sha": "new"}}
        result = {**poll_result([change()], conflicts={1: ["A.kt"]}), "outdated_parents": outdated}
        # When
        found = dict(events.events(result))
        # Then
        self.assertEqual(["1:parent_updated:1:new"], [k for k, e in found.items() if e["kind"] != "message"])
        self.assertEqual("1 · parent 9 has a new patch set", events.notification(found["1:parent_updated:1:new"])[0])


class DaemonTest(unittest.TestCase):
    class Stop(Exception):
        pass

    def run_daemon(self, rounds, **patches):
        sleeps = mock.Mock(side_effect=[None] * (rounds - 1) + [self.Stop()])
        stderr = io.StringIO()
        with mock.patch.object(watch.time, "sleep", sleeps), \
                mock.patch.object(watch, "swiftbar"), \
                mock.patch.object(watch, "write_daemon_poll"), \
                mock.patch("sys.stderr", stderr), \
                contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(watch, name, value))
            with self.assertRaises(self.Stop):
                watch.daemon(60)
        return stderr.getvalue()

    def test_a_bug_after_the_poll_is_logged_once_and_the_daemon_keeps_going(self):
        # Given
        status = mock.Mock()
        # When
        log = self.run_daemon(3, poll=mock.Mock(return_value={}), write_status=status,
                              daemon_round=mock.Mock(side_effect=KeyError("number")))
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
        with mock.patch.dict(watch.os.environ, {"GERRIT_BABYSIT_LAUNCHER": str(launcher)}), \
                mock.patch.object(watch, "swiftbar"), mock.patch.object(watch, "write_daemon_poll"), \
                mock.patch.object(watch, "poll", return_value={}), mock.patch.object(watch, "daemon_round", round_), \
                mock.patch("sys.stderr", io.StringIO()) as stderr:
            code = watch.daemon(60)
        # Then
        self.assertEqual((0, 1), (code, round_.call_count))
        self.assertIn("restarting", stderr.getvalue())

    def test_a_repeated_poll_failure_is_logged_once(self):
        # Given
        down = OSError("Could not resolve hostname")
        # When
        log = self.run_daemon(3, poll=mock.Mock(side_effect=[down, down, {}]), write_status=mock.Mock(),
                              daemon_round=mock.Mock())
        # Then
        self.assertEqual(1, log.count("poll failed"))
        self.assertIn("recovered", log)

    def test_the_poll_duration_comes_off_the_wait(self):
        # Given
        sleep = mock.Mock(side_effect=self.Stop())
        clock = mock.Mock(side_effect=[100.0, 125.0])
        # When
        with mock.patch.object(watch.time, "sleep", sleep), mock.patch.object(watch.time, "monotonic", clock), \
                mock.patch.object(watch, "poll", return_value={}), mock.patch.object(watch, "daemon_round"), \
                mock.patch.object(watch, "write_daemon_poll"), self.assertRaises(self.Stop):
            watch.daemon(60)
        # Then
        sleep.assert_called_once_with(35.0)

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
        with mock.patch.object(watch, "write_daemon_poll", published), \
                mock.patch.object(watch, "write_status", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            watch.daemon_round({"changes": []}, first_run=True)
        # Then
        published.assert_called_once_with({"changes": []})


class FlakyMemoryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(watch, "FLAKY", pathlib.Path(tmp.name) / "flaky.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_flakes_outlive_the_change_and_expire(self):
        # Given
        flaked = change(comments=[zuul_verdict(1, NOW - 20, "FAILURE"), zuul_verdict(1, NOW - 10, "SUCCESS")])
        watch.record_flaky(poll_result([flaked]), NOW)
        # When
        watch.record_flaky(poll_result([]), NOW)
        kept = watch.load_flaky()
        watch.record_flaky(poll_result([]), NOW + watch.FLAKY_RETENTION_S)
        # Then
        self.assertEqual(["unit"], [r["job"] for r in kept.values()])
        self.assertEqual({}, watch.load_flaky())

    def test_a_failure_carries_the_local_flake_count_even_without_zuul_api(self):
        # Given
        watch.record_flaky(poll_result([change(comments=[zuul_verdict(1, NOW - 20, "FAILURE"),
                                                         zuul_verdict(1, NOW - 10, "SUCCESS")])]), NOW)
        event = {"kind": "message", "change": 2, "branch": "main", "author_username": "zuul",
                 "message": zuul_verdict(3, NOW, "FAILURE")["message"]}
        # When
        with mock.patch.object(ci, "ZUUL_API", None):
            watch.enrich([event])
        # Then
        self.assertEqual({"unit": {"week": 1, "month": 1, "last": NOW - 10}}, event["known_flaky"])

    def test_the_status_row_lists_failed_jobs_their_flakes_and_my_rechecks(self):
        # Given
        watch.record_flaky(poll_result([change(2, comments=[zuul_verdict(1, NOW - 20, "FAILURE"),
                                                            zuul_verdict(1, NOW - 10, "SUCCESS")])]), NOW)
        red = change(current=patch_set(1, *[approval(label, -1, "zuul") for label in events.CI_LABELS]),
                     comments=[zuul_verdict(1, NOW - 5, "FAILURE"), message(gerrit.USER, "Patch Set 1:\n\nrecheck", NOW)])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        status = pathlib.Path(tmp.name) / "status.json"
        # When
        with mock.patch.object(watch, "STATUS", status), mock.patch.object(repo, "worktrees_by_change_id", return_value={}):
            watch.write_status(poll_result([red]))
        # Then
        row = json.loads(status.read_text())["changes"][0]
        self.assertEqual(([{"job": "unit", "result": "FAILURE"}], 1, 1),
                         (row["ci_failed_jobs"], row["flaky"]["unit"]["month"], row["rechecks"]))


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

    def test_the_daemon_neither_notifies_nor_marks_a_snoozed_change_seen(self):
        # Given
        watch.snooze.set_snooze(1, until=NOW + 3600)
        result = poll_result([change(comments=[message("reviewer", "Patch Set 1:\n\nwhy?", 10)])])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = pathlib.Path(tmp.name) / "daemon-seen.json"
        # When
        with mock.patch.object(watch, "DAEMON_STATE", state), mock.patch.object(watch, "write_daemon_poll"), \
                mock.patch.object(watch, "write_status"), mock.patch.object(watch, "record_flaky"), \
                mock.patch.object(events, "is_quiet", return_value=False), \
                mock.patch.object(watch, "swiftbar") as swiftbar:
            watch.daemon_round(result, first_run=False)
        # Then
        self.assertEqual([], [c for c in swiftbar.call_args_list if c.args[0] == "notify"])
        self.assertEqual({}, json.loads(state.read_text()))


class MyAttentionSetsTest(unittest.TestCase):
    def test_my_changes_attention_sets_by_number_and_none_without_rest(self):
        # Given
        alice = {"account": {"username": "alice", "name": "Alice"}, "last_update": "2026-09-29 08:00:00.000000000"}
        rows = [{"_number": 7, "attention_set": {"1": alice}}]
        # When
        with mock.patch.object(gerrit, "rest_get", return_value=rows) as rest:
            found = watch.my_attention_sets()
        with mock.patch.object(gerrit, "rest_get", side_effect=OSError("HTTP Error 401: Unauthorized")):
            failed = watch.my_attention_sets()
        # Then
        rest.assert_called_once_with("/changes/?q=owner%3Aself%20status%3Aopen&o=DETAILED_ACCOUNTS")
        self.assertEqual(({7: {"holders": [{"username": "alice", "name": "Alice", "since": 1790668800.0, "reason": ""}],
                               "removed": []}}, None), (found, failed))


RED = {"result": "FAILURE", "end_time": "2026-09-29T12:00:00", "log_url": "https://logs/p/",
       "red_since": "2026-09-29T10:00:00", "failures": 2, "last_green": "2026-09-29T09:00:00"}


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
        with mock.patch.object(ci, "ZUUL_API", "https://zuul/api"), \
                mock.patch.object(ci, "PERIODIC_BUILD", {"pipeline": "periodic", "job": "build"}), \
                mock.patch.object(ci, "base_health", side_effect=lambda branch: {"branch": branch}) as base_health:
            found = watch.base_health(changes)
        with mock.patch.object(ci, "PERIODIC_BUILD", None):
            unset = watch.base_health(changes)
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
        with mock.patch.object(ci, "ZUUL_API", "https://zuul/api"), \
                mock.patch.object(ci, "PERIODIC_BUILD", {"pipeline": "periodic", "job": "build"}), \
                mock.patch.object(ci, "base_health", side_effect=fetch):
            found = watch.base_health(changes)
        # Then: both branches reached the barrier, so neither call waited on the other to start.
        self.assertEqual({"main": {"branch": "main"}, "release": {"branch": "release"}}, found)

    def test_a_red_base_is_announced_once_for_all_its_changes(self):
        # Given
        red = {**poll_result([change(1), change(2), change(3, branch="release")]),
               "base_health": {"main": RED, "release": {"result": "SUCCESS"}}}
        longer = {**red, "base_health": {"main": {**RED, "end_time": "2026-09-29T14:00:00", "failures": 3}}}
        again = {**red, "base_health": {"main": {**RED, "last_green": "2026-09-29T13:00:00"}}}
        # When
        found = [{k: e for k, e in events.events(result, NOW) if e["kind"] == "base_red"}
                 for result in (red, longer, again)]
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
        polls = [{**poll_result([change(1)]), "base_health": {"main": health}}
                 for health in (RED, {"error": "offline"}, {"result": "SUCCESS"})]
        # When
        found = [bool(watch.snoozed_changes(result, NOW)) for result in polls]
        # Then
        self.assertEqual([True, True, False], found)

    def test_the_snapshot_flags_rows_on_a_red_base(self):
        # Given
        result = {**poll_result([change(1), change(2, branch="release")]), "base_health": {"main": RED}}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        status = pathlib.Path(tmp.name) / "status.json"
        watch.snooze.set_snooze(1, base_green=True)
        # When
        with mock.patch.object(watch, "STATUS", status), mock.patch.object(repo, "worktrees_by_change_id", return_value={}):
            watch.write_status(result)
        snapshot = json.loads(status.read_text())
        # Then
        self.assertEqual([True, False], [row["base_red"] for row in snapshot["changes"]])
        self.assertEqual([1], list(watch.snooze.snapshot_active(snapshot, NOW)))


class WithIntKeysTest(unittest.TestCase):
    def test_attention_sets_come_back_with_int_keys_and_none_stays_none(self):
        # Given
        polls = [{**poll_result(), "attention_sets": {7: {"holders": [], "removed": []}}},
                 {**poll_result(), "attention_sets": None}]
        # When
        found = [watch.with_int_keys(json.loads(json.dumps(p))) for p in polls]
        # Then
        self.assertEqual(polls, found)

    def test_change_numbers_come_back_as_ints(self):
        # Given
        poll = {**poll_result(conflicts={1: ["A.kt"]}, parents={2: 3}), "outdated_parents": {}, "attention": [4]}
        payload = json.loads(json.dumps(poll))
        # When
        result = watch.with_int_keys(payload)
        # Then
        self.assertEqual(poll, result)


class ClaudePidTest(unittest.TestCase):
    def test_the_closest_claude_ancestor_in_one_ps_call(self):
        # Given
        table = {40: (30, "/bin/zsh"), 30: (20, "/Users/me/.local/bin/claude"), 20: (1, "claude"), 50: (1, "claude")}
        # When
        with mock.patch.object(watch.procs, "processes", return_value=table) as processes, \
                mock.patch.object(watch.os, "getppid", return_value=40):
            pid = watch.claude_pid()
        # Then
        self.assertEqual(30, pid)
        processes.assert_called_once_with("comm")

    def test_none_outside_claude(self):
        # Given
        table = {40: (1, "/bin/zsh")}
        # When
        with mock.patch.object(watch.procs, "processes", return_value=table), \
                mock.patch.object(watch.os, "getppid", return_value=40):
            pid = watch.claude_pid()
        # Then
        self.assertIsNone(pid)


if __name__ == "__main__":
    unittest.main()
