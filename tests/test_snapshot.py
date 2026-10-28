"""Run from the skill directory: python3 -m unittest"""

import unittest

# First: points the config at the fixtures before any module reads it.
import fakes  # noqa: F401
import snapshot
from config import STALE_AFTER_S


def row(**extra):
    """A snapshot row of a change with nothing wrong and nothing done yet."""
    return {"wip": False, "conflict": [], "ci": "passed", "code_review": 0, "ready": False, **extra}


class IsStoppedTest(unittest.TestCase):
    def test_a_recent_attempt_means_the_watcher_runs_even_when_it_failed(self):
        self.assertFalse(snapshot.is_stopped({"updated": 0, "last_attempt": 1000}, 1000 + STALE_AFTER_S))

    def test_no_attempt_for_a_while_means_the_watcher_stopped(self):
        self.assertTrue(snapshot.is_stopped({"updated": 1000, "last_attempt": 1000}, 1001 + STALE_AFTER_S))

    def test_an_old_snapshot_without_last_attempt_falls_back_on_updated(self):
        self.assertFalse(snapshot.is_stopped({"updated": 1000}, 1000 + STALE_AFTER_S))

    def test_an_empty_snapshot_is_stopped(self):
        self.assertTrue(snapshot.is_stopped({}, 1000 + STALE_AFTER_S))


class StateTest(unittest.TestCase):
    def test_the_worst_problem_wins(self):
        cases = [
            (row(conflict=["a.kt"], ci="failed", code_review=-1), "conflict"),
            (row(ci="stale_base", code_review=-1), "stale_base"),
            (row(ci="failed", code_review=-1), "ci_failed"),
            (row(code_review=-1, ready=True), "rejected"),
        ]
        for change, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, snapshot.state(change))

    def test_wip_hides_the_problems_but_its_label_still_names_the_worst(self):
        change = row(wip=True, code_review=-2)
        self.assertEqual("wip", snapshot.state(change))
        self.assertEqual("WIP · CR -2", snapshot.display(change)[0])

    def test_ready_only_when_nothing_is_left_but_the_submit(self):
        cases = [
            (row(ready=True, outdated_parent=12), "parent_updated"),
            (row(ready=True, ci_stuck=True), "ci_stuck"),
            (row(ready=True, submit_blocked=["Code-Owners"]), "submit_blocked"),
            (row(ready=True, gate="voted"), "gating"),
            (row(ready=True, open_parent=12), "ready_parent"),
            (row(ready=True), "ready"),
            (row(ci="running"), "ci_running"),
            (row(), "ci_passed"),
        ]
        for change, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, snapshot.state(change))

    def test_every_state_has_a_style_and_red_is_exactly_the_problems(self):
        red = {state for state, (_, color) in snapshot.STYLES.items() if color == "red"}
        self.assertEqual(set(snapshot.PROBLEMS), red)


class RebaseOntoTest(unittest.TestCase):
    def test_a_branch_tip_rebase_names_the_branch(self):
        self.assertEqual("main", snapshot.rebase_onto(row(branch="main", rebase_ready={"parent": None})))

    def test_a_rebase_onto_a_parent_names_the_parent(self):
        self.assertIn("12", snapshot.rebase_onto(row(branch="main", rebase_ready={"parent": 12})))


if __name__ == "__main__":
    unittest.main()
