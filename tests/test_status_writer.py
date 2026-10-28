"""Run from the skill directory: python3 -m unittest"""

import json
import pathlib
import tempfile
import unittest
from unittest import mock

# First: points the config at the fixtures before any module reads it.
from fakes import NOW, GREEN_CI, approval, patch_set, message, change, poll_result, zuul_verdict
import ci
import events
import extras
import repo
import snooze
import status_writer


class PreparedRebasesTest(unittest.TestCase):
    def test_candidates_are_what_a_plain_rebase_unblocks(self):
        # Given
        merge_failed = patch_set(1, *[approval(label, -1, "zuul") for label in events.CI_LABELS])
        verdict = message("zuul", "Patch Set 1: Verified-1\n\nMerge Failed.", 5)
        changes = [change(1), change(2), change(3), change(4), change(5, current=merge_failed, comments=[verdict])]
        result = {
            **poll_result(
                changes, conflicts={3: ["A.kt"], 4: ["B.kt"]}, parents={1: 9, 4: 8}, stale={2: {"parent": 7, "old_parent_sha": "old"}}
            ),
            "outdated_parents": {1: {"parent": 9, "new_parent_sha": "p9"}},
        }
        # When
        with mock.patch.object(repo, "branch_tips", return_value={"main": "tip"}):
            found = {n: (onto, parent) for n, (_, onto, parent) in status_writer.rebase_candidates(result).items()}
        # Then
        self.assertEqual({1: ("p9", 9), 2: ("tip", None), 3: ("tip", None), 5: ("tip", None)}, found)

    def test_a_change_with_work_left_in_its_worktree_is_not_prepared(self):
        # Given
        result = poll_result([change(1), change(2)], conflicts={1: ["A.kt"], 2: ["A.kt"]})
        with (
            mock.patch.object(repo, "branch_tips", return_value={"main": "tip"}),
            mock.patch.object(repo, "unfinished_work", return_value={1: {"worktree": "/wt1"}}),
            mock.patch.object(repo, "worktrees_by_change_id", return_value={"I1": "/wt1", "I2": "/wt2"}),
            mock.patch.object(repo, "prepare_replays", side_effect=lambda wanted: {n: "new" for n in wanted}) as replays,
        ):
            # When
            found = status_writer.prepared_rebases(result)
        # Then
        replays.assert_called_once_with({2: ("rev1", "tip")})
        self.assertEqual({2: {"sha": "new", "patch_set": 1, "parent": None}}, found)

    def test_the_worktree_listing_is_reused(self):
        # Given
        result = poll_result([change(1)], conflicts={1: ["A.kt"]})
        with (
            mock.patch.object(repo, "branch_tips", return_value={"main": "tip"}),
            mock.patch.object(repo, "worktree_listing") as listing,
            mock.patch.object(repo, "change_ids_by_worktree", return_value={}),
            mock.patch.object(repo, "prepare_replays", return_value={}),
        ):
            # When
            status_writer.prepared_rebases(result, "worktree /repo\n")
        # Then
        listing.assert_not_called()

    def test_without_candidates_the_old_rebases_are_still_dropped(self):
        # Given
        result = poll_result([change(1)])
        with (
            mock.patch.object(repo, "branch_tips", return_value={}),
            mock.patch.object(repo, "prepare_replays", return_value={}) as replays,
        ):
            # When
            found = status_writer.prepared_rebases(result)
        # Then
        self.assertEqual({}, found)
        replays.assert_called_once_with({})


class DiagnoseRedTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(status_writer, "DIAGNOSES", pathlib.Path(tmp.name) / "diagnoses.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def red(self, number, verdict_at):
        return change(
            number,
            current=patch_set(1, *[approval(label, -1, "zuul") for label in events.CI_LABELS]),
            comments=[zuul_verdict(1, verdict_at, "FAILURE")],
        )

    def test_each_verdict_is_diagnosed_once_and_kept_while_the_change_is_open(self):
        # Given
        def diagnose(reported, base=None):
            for event in reported:
                event["ci_diagnosis"] = [
                    {"job": "unit", "category": "unknown", "log_url": "https://logs/u", "log_tail": "x" * 1000, "others_failing": [3]}
                ]
            return reported

        with (
            mock.patch.object(ci, "ZUUL_API", "https://zuul/api"),
            mock.patch.object(extras, "enrich", side_effect=diagnose) as enrich,
        ):
            # When
            first = status_writer.diagnose_red(poll_result([self.red(1, 10), change(2)]))
            again = status_writer.diagnose_red(poll_result([self.red(1, 10)]))
            rechecked = status_writer.diagnose_red(poll_result([self.red(1, 20)]))
            closed = status_writer.diagnose_red(poll_result([change(2)]))
        # Then
        self.assertEqual([1, 0, 1, 0], [len(c.args[0]) for c in enrich.call_args_list])
        self.assertEqual(
            {1: [{"job": "unit", "category": "unknown", "log_url": "https://logs/u", "excerpt": "x" * status_writer.EXCERPT_CHARS}]},
            first,
        )
        self.assertEqual((first, first), (again, rechecked))
        self.assertEqual(({}, {}), (closed, json.loads(status_writer.DIAGNOSES.read_text())))

    def test_nothing_without_zuul(self):
        # Given / When
        with mock.patch.object(ci, "ZUUL_API", None):
            found = status_writer.diagnose_red(poll_result([self.red(1, 10)]))
        # Then
        self.assertEqual({}, found)

    def test_an_unknown_failure_a_newer_patch_set_got_past_is_offered_for_the_memory(self):
        # Given
        jobs = [
            {"job": "unit", "category": "unknown", "log_url": "https://logs/u", "excerpt": "boom"},
            {"job": "lint", "category": "lint"},
        ]
        status_writer.DIAGNOSES.write_text(
            json.dumps(
                {"101:1:10": {"jobs": jobs}, "102:1:10": {"jobs": jobs}, "103:1:10": {"jobs": jobs}, "104:3:10": {"jobs": jobs}}
            )
        )
        green = patch_set(2, *GREEN_CI)
        changes = [
            change(101, current=green),
            change(102, current=green),
            self.red(103, 20),
            change(104, current=patch_set(3, *GREEN_CI)),
        ]
        with mock.patch.object(status_writer.known_failures, "load", return_value={"102:1:unit": {}}):
            # When
            found = dict(status_writer.fixed_failures(poll_result(changes)))
        # Then
        self.assertEqual(["101:fixed:1:unit"], list(found))
        self.assertEqual(
            {
                "kind": "failure_fixed",
                "job": "unit",
                "failed_patch_set": 1,
                "failed_ref": "refs/changes/01/101/1",
                "fixed_ref": "refs/changes/01/1/2",
                "log_url": "https://logs/u",
                "excerpt": "boom",
            },
            {k: v for k, v in found["101:fixed:1:unit"].items() if k not in events.event_base(changes[0])},
        )


RED = {
    "result": "FAILURE",
    "end_time": "2026-09-29T12:00:00",
    "log_url": "https://logs/p/",
    "red_since": "2026-09-29T10:00:00",
    "failures": 2,
    "last_green": "2026-09-29T09:00:00",
}


class WriteStatusTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(snooze, "SNOOZE", pathlib.Path(tmp.name) / "snooze.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_snapshot_flags_rows_on_a_red_base(self):
        # Given
        result = {**poll_result([change(1), change(2, branch="release")]), "base_health": {"main": RED}}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        status = pathlib.Path(tmp.name) / "status.json"
        snooze.set_snooze(1, base_green=True)
        # When
        with (
            mock.patch.object(status_writer, "STATUS", status),
            mock.patch.object(repo, "worktrees_by_change_id", return_value={}),
            mock.patch.object(repo, "cleanup_candidates", return_value=[]),
        ):
            status_writer.write_status(result)
        snapshot = json.loads(status.read_text())
        # Then
        self.assertEqual([True, False], [row["base_red"] for row in snapshot["changes"]])
        self.assertEqual([1], list(snooze.snapshot_active(snapshot, NOW)))

    def test_the_snapshot_lists_the_branches_to_clean_up(self):
        # Given
        candidate = {
            "kind": "cleanup_candidate",
            "branch": "old",
            "worktree": "/wt/old",
            "change": 7,
            "subject": "fix: x",
            "status": "merged",
        }
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        status = pathlib.Path(tmp.name) / "status.json"
        # When
        with (
            mock.patch.object(status_writer, "STATUS", status),
            mock.patch.object(repo, "worktrees_by_change_id", return_value={}),
            mock.patch.object(repo, "cleanup_candidates", return_value=[("cleanup:old:sha", candidate)]) as found,
        ):
            status_writer.write_status(poll_result([change(1)]), listing="listing")
        # Then
        self.assertEqual([{"key": "cleanup:old:sha", **candidate}], json.loads(status.read_text())["cleanup"])
        self.assertEqual("listing", found.call_args.args[1])
        with mock.patch.object(status_writer, "STATUS", status):
            self.assertEqual({"cleanup:old:sha": candidate}, status_writer.snapshot_cleanup())


if __name__ == "__main__":
    unittest.main()
