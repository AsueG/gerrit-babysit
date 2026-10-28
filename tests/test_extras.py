"""Run from the skill directory: python3 -m unittest"""

import concurrent.futures
import json
import pathlib
import tempfile
import unittest
from unittest import mock

# First: points the config at the fixtures before any module reads it.
from fakes import ZUUL, NOW, approval, patch_set, message, change, poll_result, zuul_verdict
import ci
import events
import extras
import gerrit
import repo
import status_writer


def done(value):
    future = concurrent.futures.Future()
    future.set_result(value)
    return future


class EnrichTest(unittest.TestCase):
    def test_stuck_ci_gets_the_zuul_queue_and_requests_the_prereview_path(self):
        # Given
        stuck = {"kind": "ci_stuck", "change": 1, "patch_set": 2}
        requested = {"kind": "review_requested", "change": 3, "patch_set": 4, "participated": False}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # When
        with mock.patch.object(ci, "zuul_queue", return_value=[]), mock.patch.object(extras, "CACHE", pathlib.Path(tmp.name)):
            extras.enrich([stuck, requested])
        # Then
        self.assertEqual([], stuck["zuul_queue"])
        self.assertEqual({"path": f"{tmp.name}/prereview/3-4.md", "done": False}, requested["prereview"])

    def test_a_job_red_on_several_changes_fetches_its_history_once(self):
        # Given
        events = [
            {"kind": "message", "change": n, "branch": "main", "author_username": "zuul", "message": ZUUL["failed"]["message"]}
            for n in (1, 2)
        ]
        build = {"log_url": "https://logs/x/", "end_time": "2026-09-29T12:00:00", "artifacts": []}
        answers = {"/build/": json.dumps(build), "job-output.txt": "", "zuul-file-comments.json": "{}", "/builds?": "[]"}
        get = lambda url: next(body for marker, body in answers.items() if marker in url)
        jobs = {j["job"] for j in ci.failed_jobs(ZUUL["failed"]["message"])}
        # When
        with (
            mock.patch.object(ci, "http_get", side_effect=get) as http,
            mock.patch.object(ci, "ZUUL_API", "https://zuul/api"),
            mock.patch.object(ci, "PERIODIC_BUILD", None),
        ):
            extras.enrich(events)
        # Then
        self.assertEqual(len(jobs), sum("/builds?" in c.args[0] for c in http.call_args_list))

    def test_failures_reuse_the_polled_base_health(self):
        # Given
        failures = [
            {"kind": "message", "change": n, "branch": branch, "author_username": "zuul", "message": "m"}
            for n, branch in ((1, "main"), (2, "main"), (3, "release"))
        ]
        base = {"main": {"result": "FAILURE"}, "release": {"result": "SUCCESS"}}
        # When
        with (
            mock.patch.object(ci, "ZUUL_API", "https://zuul/api"),
            mock.patch.object(ci, "PERIODIC_BUILD", {"pipeline": "periodic", "job": "build"}),
            mock.patch.object(ci, "submit_diagnosis", side_effect=lambda _, event, *__: [done(event["change"])]),
            mock.patch.object(ci, "base_health") as base_health,
        ):
            extras.enrich(failures, base)
        # Then
        base_health.assert_not_called()
        self.assertEqual(
            [([1], "FAILURE"), ([2], "FAILURE"), ([3], "SUCCESS")], [(f["ci_diagnosis"], f["base_build"]["result"]) for f in failures]
        )

    def test_only_ci_failures_and_new_patch_sets_get_extras(self):
        # Given
        failure = {"kind": "message", "change": 1, "branch": "main", "author_username": "zuul", "message": "m"}
        human = {"kind": "message", "change": 1, "branch": "main", "author_username": "reviewer", "message": "m"}
        patch = {"kind": "review_new_patch_set", "change": 2, "since_ref": "a", "current_ref": "b"}
        # When
        with (
            mock.patch.object(ci, "submit_diagnosis", return_value=[done("d")]),
            mock.patch.object(repo, "interdiff", return_value={"stat": "s"}),
        ):
            extras.enrich([failure, human, patch], {"main": {"result": "SUCCESS"}})
        # Then
        self.assertEqual((["d"], {"result": "SUCCESS"}), (failure["ci_diagnosis"], failure["base_build"]))
        self.assertNotIn("ci_diagnosis", human)
        self.assertEqual({"stat": "s"}, patch["interdiff"])


class FlakyMemoryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(extras, "FLAKY", pathlib.Path(tmp.name) / "flaky.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_flakes_outlive_the_change_and_expire(self):
        # Given
        flaked = change(comments=[zuul_verdict(1, NOW - 20, "FAILURE"), zuul_verdict(1, NOW - 10, "SUCCESS")])
        extras.record_flaky(poll_result([flaked]), NOW)
        # When
        extras.record_flaky(poll_result([]), NOW)
        kept = extras.load_flaky()
        extras.record_flaky(poll_result([]), NOW + extras.FLAKY_RETENTION_S)
        # Then
        self.assertEqual(["unit"], [r["job"] for r in kept.values()])
        self.assertEqual({}, extras.load_flaky())

    def test_a_failure_carries_the_local_flake_count_even_without_zuul_api(self):
        # Given
        extras.record_flaky(
            poll_result([change(comments=[zuul_verdict(1, NOW - 20, "FAILURE"), zuul_verdict(1, NOW - 10, "SUCCESS")])]), NOW
        )
        event = {
            "kind": "message",
            "change": 2,
            "branch": "main",
            "author_username": "zuul",
            "message": zuul_verdict(3, NOW, "FAILURE")["message"],
        }
        # When
        with mock.patch.object(ci, "ZUUL_API", None):
            extras.enrich([event])
        # Then
        self.assertEqual({"unit": {"week": 1, "month": 1, "last": NOW - 10}}, event["known_flaky"])

    def test_the_status_row_lists_failed_jobs_their_flakes_and_my_rechecks(self):
        # Given
        extras.record_flaky(
            poll_result([change(2, comments=[zuul_verdict(1, NOW - 20, "FAILURE"), zuul_verdict(1, NOW - 10, "SUCCESS")])]), NOW
        )
        red = change(
            current=patch_set(1, *[approval(label, -1, "zuul") for label in events.CI_LABELS]),
            comments=[zuul_verdict(1, NOW - 5, "FAILURE"), message(gerrit.USER, "Patch Set 1:\n\nrecheck", NOW)],
        )
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        status = pathlib.Path(tmp.name) / "status.json"
        # When
        with (
            mock.patch.object(status_writer, "STATUS", status),
            mock.patch.object(repo, "worktrees_by_change_id", return_value={}),
            mock.patch.object(repo, "cleanup_candidates", return_value=[]),
        ):
            status_writer.write_status(poll_result([red]))
        # Then
        row = json.loads(status.read_text())["changes"][0]
        self.assertEqual(
            ([{"job": "unit", "result": "FAILURE"}], 1, 1), (row["ci_failed_jobs"], row["flaky"]["unit"]["month"], row["rechecks"])
        )


if __name__ == "__main__":
    unittest.main()
