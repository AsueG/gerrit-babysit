"""Run from this directory: python3 -m unittest"""
import pathlib
import subprocess
import unittest
from unittest import mock

# First: points the config at the fixtures before any module reads it.
from fakes import CHANGE_ID, GitRepoTest, change, patch_set
import gerrit
import repo


class StaleParentsTest(GitRepoTest):
    def setUp(self):
        super().setUp()
        repo._ancestry_memo = repo.PollMemo()

    def test_parent_landed_under_another_sha(self):
        # Given
        self.git("checkout", "-q", "-b", "stack")
        old_parent = self.commit("parent")
        self.git("checkout", "-q", "main")
        self.commit("parent, rebased on submit")
        self.git("update-ref", f"{repo.FETCH_NAMESPACE}/main", "main")
        c = change(current=patch_set(1, parents=(old_parent,)))
        # When
        stale = repo.stale_parents([c], {1: (99, "MERGED", {})})
        # Then
        self.assertEqual({1: {"parent": 99, "old_parent_sha": old_parent}}, stale)

    def test_parent_landed_as_is_or_still_open(self):
        # Given
        parent = self.commit("parent")
        self.git("update-ref", f"{repo.FETCH_NAMESPACE}/main", "main")
        c = change(current=patch_set(1, parents=(parent,)))
        # When
        results = [repo.stale_parents([c], {1: (99, status, {})}) for status in ("MERGED", "NEW")]
        # Then
        self.assertEqual([{}, {}], results)

    def test_the_ancestry_check_only_reruns_when_the_branch_moves(self):
        # Given
        parent = self.commit("parent")
        self.git("update-ref", f"{repo.FETCH_NAMESPACE}/main", "main")
        c = change(current=patch_set(1, parents=(parent,)))
        repo.stale_parents([c], {1: (99, "MERGED", {})})
        # When
        with mock.patch.object(repo, "git_run", wraps=repo.git_run) as git_run:
            repo.stale_parents([c], {1: (99, "MERGED", {})})
        # Then
        self.assertEqual([], [call for call in git_run.call_args_list if call.args[0] == "merge-base"])


class MergeConflictsTest(GitRepoTest):
    def setUp(self):
        super().setUp()
        self.git("remote", "add", "origin", str(self.repo))
        self.content_merge = True
        patcher = mock.patch.object(gerrit, "uses_content_merge", lambda project: self.content_merge)
        patcher.start()
        self.addCleanup(patcher.stop)

    def conflicting_change(self, number, branch, base="base\n", theirs="theirs\n", mine="mine\n"):
        (self.repo / "A.kt").write_text(base)
        self.git("add", "A.kt")
        self.git("commit", "-q", "-m", "base")
        self.git("branch", "-f", branch)
        (self.repo / "A.kt").write_text(theirs)
        self.git("commit", "-q", "-am", "theirs")
        self.git("checkout", "-q", "--detach", branch)
        (self.repo / "A.kt").write_text(mine)
        self.git("commit", "-q", "-am", "mine")
        ref = f"refs/changes/0{number}/{number}/1"
        self.git("update-ref", ref, "HEAD")
        self.git("checkout", "-q", "main")
        self.git("branch", "-f", branch, "main")
        return change(number, current={**patch_set(1), "revision": self.git("rev-parse", ref), "ref": ref},
                      branch=branch)

    def test_the_memo_only_keeps_the_current_branch_tip(self):
        # Given
        c = self.conflicting_change(1, "release")
        repo.merge_conflicts([c])
        self.git("commit", "-q", "--allow-empty", "-m", "moved")
        self.git("branch", "-f", "release", "main")
        # When
        conflicts = repo.merge_conflicts([c])
        # Then
        self.assertEqual({1: ["A.kt"]}, conflicts)
        self.assertEqual([(self.git("rev-parse", "release"), c["currentPatchSet"]["revision"], True)],
                         list(repo._conflicts_memo.kept))

    def test_without_content_merge_edits_far_apart_in_one_file_conflict(self):
        # Given
        lines = [f"line {i}\n" for i in range(20)]
        base, theirs, mine = "".join(lines), "".join(["top\n", *lines[1:]]), "".join([*lines[:-1], "bottom\n"])
        c = self.conflicting_change(1, "release", base, theirs, mine)
        # When
        verdicts = []
        for self.content_merge in (True, False):
            verdicts.append(repo.merge_conflicts([c]))
        # Then
        self.assertEqual([{}, {1: ["A.kt"]}], verdicts)

    def test_without_content_merge_the_same_edit_on_both_sides_is_clean(self):
        # Given
        self.content_merge = False
        c = self.conflicting_change(1, "release", theirs="same\n", mine="same\n")
        (self.repo / "B.kt").write_text("theirs only\n")
        self.git("add", "B.kt")
        self.git("commit", "-q", "-m", "theirs only")
        self.git("branch", "-f", "release", "main")
        # When
        conflicts = repo.merge_conflicts([c])
        # Then
        self.assertEqual({}, conflicts)

    def test_a_vanished_branch_does_not_hide_other_conflicts(self):
        # Given
        live = self.conflicting_change(1, "release")
        gone = change(2, current={**patch_set(1), "revision": "0" * 40, "ref": "refs/changes/02/2/1"},
                      branch="deleted")
        # When
        conflicts = repo.merge_conflicts([live, gone])
        # Then
        self.assertEqual({1: ["A.kt"]}, conflicts)

    def test_only_absent_patch_sets_are_fetched(self):
        # Given
        present = self.git("rev-parse", "HEAD")
        # When
        missing = repo.missing_objects([present, "0" * 40])
        # Then
        self.assertEqual({"0" * 40}, missing)

    def test_fetch_failing_for_every_ref_raises(self):
        # Given
        self.git("remote", "set-url", "origin", str(self.repo / "missing"))
        c = change(1, current={**patch_set(1), "revision": "0" * 40})
        # When
        with mock.patch.object(repo, "git_run", wraps=repo.git_run) as git_run, \
                self.assertRaises(subprocess.CalledProcessError):
            repo.merge_conflicts([c])
        # Then: an unreachable remote is not retried once per refspec
        self.assertEqual(1, sum(call.args[0] == "fetch" for call in git_run.call_args_list))


class InterdiffTest(GitRepoTest):
    def publish(self, patch_set, files):
        """Writes `files` on a commit off `main` and publishes it as refs/changes/01/1/<patch_set>."""
        self.git("checkout", "-q", "--detach", "main")
        for name, text in files.items():
            (self.repo / name).write_text(text)
        self.git("add", *files)
        self.git("commit", "-q", "-m", f"ps{patch_set}")
        self.git("update-ref", f"refs/changes/01/1/{patch_set}", "HEAD")
        self.git("checkout", "-q", "main")

    def event(self):
        return {"change": 1, "since_ref": "refs/changes/01/1/1", "current_ref": "refs/changes/01/1/2"}

    def setUp(self):
        super().setUp()
        self.git("remote", "add", "origin", str(self.repo))

    def test_rebased_patch_set_only_shows_the_change_files(self):
        # Given
        self.publish(1, {"A.kt": "one\n"})
        (self.repo / "Upstream.kt").write_text("x\n")
        self.git("add", "Upstream.kt")
        self.git("commit", "-q", "-m", "upstream")
        self.publish(2, {"A.kt": "two\n"})
        # When
        found = repo.interdiff(self.event())
        # Then
        self.assertTrue(found["rebased"])
        self.assertIn("A.kt", found["stat"])
        self.assertNotIn("Upstream.kt", found["stat"])
        self.assertEqual("", self.git("for-each-ref", f"{repo.FETCH_NAMESPACE}/review"))

    def test_same_base(self):
        # Given
        self.publish(1, {"A.kt": "one\n"})
        self.publish(2, {"A.kt": "one\n", "B.kt": "new\n"})
        # When
        found = repo.interdiff(self.event())
        # Then
        self.assertFalse(found["rebased"])
        self.assertIn("B.kt", found["stat"])

    def test_a_git_timeout_is_reported_and_the_refs_are_removed(self):
        # Given
        self.publish(1, {"A.kt": "one\n"})
        self.publish(2, {"A.kt": "two\n"})
        real = repo.git_run

        def git_run(*args, **kwargs):
            if args[0] == "diff":
                raise subprocess.TimeoutExpired("git diff", 60)
            return real(*args, **kwargs)
        # When
        with mock.patch.object(repo, "git_run", side_effect=git_run):
            found = repo.interdiff(self.event())
        # Then
        self.assertIn("timed out", found["error"])
        self.assertEqual("", self.git("for-each-ref", f"{repo.FETCH_NAMESPACE}/review"))


class PrepareRebaseTest(GitRepoTest):
    """A stack parent → child in a linked worktree; the parent then gets a new patch set on `main`."""

    def setUp(self):
        super().setUp()
        self.git("remote", "add", "origin", str(self.repo))
        (self.repo / "A.kt").write_text("base\n")
        self.git("add", "A.kt")
        self.git("commit", "-q", "-m", "base A")
        self.git("checkout", "-q", "-b", "feature")
        self.old_parent = self.commit("parent")
        (self.repo / "B.kt").write_text("child\n")
        self.git("add", "B.kt")
        self.git("commit", "-q", "-m", f"child\n\nChange-Id: {CHANGE_ID}")
        self.git("checkout", "-q", "main")
        self.worktree = pathlib.Path(self.tmp.name + "-wt")
        self.git("worktree", "add", "-q", str(self.worktree), "feature")
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.worktree)])

    def new_parent(self, a_text=None):
        """Parent patch set 2, based on `main` like the first one; optionally rewriting A.kt."""
        if a_text:
            (self.repo / "A.kt").write_text(a_text)
            self.git("add", "A.kt")
        self.git("commit", "-q", "--allow-empty", "-m", "parent v2")
        sha = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/changes/09/9/2", sha)
        self.git("reset", "-q", "--hard", "HEAD^")
        return sha

    def event(self, new_sha):
        return {"kind": "parent_updated", "change": 1, "change_id": CHANGE_ID, "branch": "main", "parent": 9,
                "parent_ref": "refs/changes/09/9/2", "old_parent_sha": self.old_parent, "new_parent_sha": new_sha}

    def in_worktree(self, *args):
        return subprocess.run(["git", "-C", str(self.worktree), *args], capture_output=True, text=True).stdout.strip()

    def test_the_child_moves_onto_the_new_parent_patch_set(self):
        # Given
        new = self.new_parent()
        # When
        found = repo.prepare_rebase(self.event(new))
        # Then
        self.assertEqual(("rebased", 1), (found["status"], found["commits"]))
        self.assertEqual(new, self.in_worktree("rev-parse", "HEAD^"))
        self.assertEqual(CHANGE_ID, self.in_worktree("log", "-1", "--format=%(trailers:key=Change-Id,valueonly)"))

    def test_a_conflict_is_aborted_and_reported(self):
        # Given
        (self.worktree / "A.kt").write_text("child\n")
        subprocess.run(["git", "-C", str(self.worktree), "commit", "-q", "--amend", "-am",
                        f"child\n\nChange-Id: {CHANGE_ID}"], check=True)
        head = self.in_worktree("rev-parse", "HEAD")
        new = self.new_parent("parent\n")
        # When
        found = repo.prepare_rebase(self.event(new))
        # Then
        self.assertEqual(("conflict", ["A.kt"]), (found["status"], found["files"]))
        self.assertEqual(head, self.in_worktree("rev-parse", "HEAD"))
        self.assertEqual("", self.in_worktree("status", "--porcelain"))

    def test_a_worktree_with_edits_is_left_alone(self):
        # Given
        new = self.new_parent()
        (self.worktree / "B.kt").write_text("editing\n")
        # When
        found = repo.prepare_rebase(self.event(new))
        # Then
        self.assertEqual("busy", found["status"])

    def test_already_on_the_new_parent(self):
        # Given
        new = self.new_parent()
        repo.prepare_rebase(self.event(new))
        # When
        found = repo.prepare_rebase(self.event(new))
        # Then
        self.assertEqual("up_to_date", found["status"])

    def test_merged_parent_rebases_onto_the_branch_tip(self):
        # Given
        self.git("commit", "-q", "--allow-empty", "-m", "parent, rebased on submit")
        self.git("update-ref", f"{repo.FETCH_NAMESPACE}/main", "main")
        event = {**self.event(None), "kind": "parent_merged"}
        # When
        found = repo.prepare_rebase(event)
        # Then
        self.assertEqual("rebased", found["status"])
        self.assertEqual(self.git("rev-parse", "main"), self.in_worktree("rev-parse", "HEAD^"))

    def test_never_in_the_main_checkout(self):
        # Given
        subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(self.worktree)], check=True)
        self.git("checkout", "-q", "feature")
        # When
        found = repo.prepare_rebase(self.event(self.new_parent()))
        # Then
        self.assertEqual("no_worktree", found["status"])


class CleanupCandidatesTest(GitRepoTest):
    def setUp(self):
        super().setUp()
        repo._cleanup_memo.clear()

    def run_cleanup(self, merged_revisions, open_ids=frozenset(), status="MERGED"):
        rows = [{"id": CHANGE_ID, "number": 7, "subject": "s", "status": status,
                 "patchSets": [{"revision": r} for r in merged_revisions]}]
        with mock.patch.object(gerrit, "query", return_value=rows) as self.query:
            return [e for _, e in repo.cleanup_candidates(open_ids)]

    def test_unchanged_branches_are_not_queried_twice(self):
        # Given
        self.git("checkout", "-q", "-b", "feature")
        pushed = self.commit("feat", CHANGE_ID)
        self.git("checkout", "-q", "main")
        first = self.run_cleanup([pushed])
        # When
        second = self.run_cleanup([pushed])
        # Then
        self.assertEqual(first, second)
        self.query.assert_not_called()

    def test_branches_of_open_changes_are_skipped(self):
        # Given
        self.git("checkout", "-q", "-b", "feature")
        pushed = self.commit("feat", CHANGE_ID)
        self.git("checkout", "-q", "main")
        # When
        found = self.run_cleanup([pushed], open_ids=frozenset({CHANGE_ID}))
        # Then
        self.assertEqual([], found)
        self.query.assert_not_called()

    def test_branch_at_a_merged_patch_set_is_a_candidate(self):
        # Given
        self.git("checkout", "-q", "-b", "feature")
        pushed = self.commit("feat", CHANGE_ID)
        self.git("checkout", "-q", "main")
        # When
        found = self.run_cleanup([pushed])
        # Then
        self.assertEqual([("feature", 7, "merged")], [(e["branch"], e["change"], e["status"]) for e in found])

    def test_branch_at_an_abandoned_patch_set_is_a_candidate(self):
        # Given
        self.git("checkout", "-q", "-b", "feature")
        pushed = self.commit("feat", CHANGE_ID)
        self.git("checkout", "-q", "main")
        # When
        found = self.run_cleanup([pushed], status="ABANDONED")
        # Then
        self.assertEqual([("feature", "abandoned")], [(e["branch"], e["status"]) for e in found])
        self.assertIn("status:abandoned", self.query.call_args.args[0])

    def test_local_amend_after_merge_is_kept(self):
        # Given
        self.git("checkout", "-q", "-b", "feature")
        pushed = self.commit("feat", CHANGE_ID)
        self.git("commit", "-q", "--amend", "--allow-empty", "-m", f"feat v2\n\nChange-Id: {CHANGE_ID}")
        self.git("checkout", "-q", "main")
        # When
        found = self.run_cleanup([pushed])
        # Then
        self.assertEqual([], found)

    def test_a_dirty_worktree_is_held_back_until_clean_without_a_new_query(self):
        # Given
        self.git("checkout", "-q", "-b", "feature")
        pushed = self.commit("feat", CHANGE_ID)
        self.git("checkout", "-q", "main")
        worktree = pathlib.Path(self.tmp.name + "-wt")
        self.git("worktree", "add", "-q", str(worktree), "feature")
        self.addCleanup(subprocess.run, ["rm", "-rf", str(worktree)])
        (worktree / "scratch.txt").write_text("wip\n")
        dirty = self.run_cleanup([pushed])
        (worktree / "scratch.txt").unlink()
        # When
        clean = self.run_cleanup([pushed])
        # Then
        self.assertEqual([], dirty)
        self.assertEqual(["feature"], [e["branch"] for e in clean])
        self.query.assert_not_called()

    def test_checked_out_and_protected_branches_are_excluded(self):
        # Given
        self.git("checkout", "-q", "-b", "feature")
        pushed = self.commit("feat", CHANGE_ID)
        # When
        found = self.run_cleanup([pushed])
        # Then
        self.assertEqual([], found)

    def test_a_malformed_change_id_never_reaches_the_query(self):
        # Given
        self.git("checkout", "-q", "-b", "feature")
        pushed = self.commit("feat", "I1' OR owner:someone")
        self.git("checkout", "-q", "main")
        # When
        found = self.run_cleanup([pushed])
        # Then
        self.assertEqual([], found)
        self.query.assert_not_called()


class WorktreesByChangeIdTest(GitRepoTest):
    def setUp(self):
        super().setUp()
        repo._worktrees_memo.clear()

    def test_recomputed_only_when_a_head_moves(self):
        # Given
        self.commit("one", CHANGE_ID)
        first = repo.worktrees_by_change_id()
        with mock.patch.object(repo, "git", wraps=repo.git) as git:
            unchanged = repo.worktrees_by_change_id()
        other = "I" + "def1" * 10
        self.commit("two", other)
        # When
        moved = repo.worktrees_by_change_id()
        # Then
        self.assertEqual(first, unchanged)
        self.assertEqual(1, git.call_count)
        self.assertEqual({CHANGE_ID, other}, set(moved))


class UnfinishedWorkTest(GitRepoTest):
    def setUp(self):
        super().setUp()
        repo._worktrees_memo.clear()

    def pushed(self, number, change_id, sha):
        return {"number": number, "id": change_id, "currentPatchSet": {"revision": sha}}

    def test_an_amend_never_pushed_is_unfinished(self):
        # Given
        pushed = self.commit("feat", CHANGE_ID)
        clean = repo.unfinished_work([self.pushed(7, CHANGE_ID, pushed)])
        self.git("commit", "-q", "--amend", "--allow-empty", "-m", f"feat, fixed\n\nChange-Id: {CHANGE_ID}")
        repo._worktrees_memo.clear()
        # When
        found = repo.unfinished_work([self.pushed(7, CHANGE_ID, pushed)])
        # Then
        self.assertEqual({}, clean)
        self.assertEqual([(7, True, False)], [(n, w["unpushed"], w["busy"]) for n, w in found.items()])

    def test_edits_in_a_stack_are_reported_once_on_its_head(self):
        # Given
        (self.repo / "A.kt").write_text("a")
        self.git("add", "A.kt")
        lower = self.commit("lower", CHANGE_ID)
        top_id = "I" + "def1" * 10
        top = self.commit("top", top_id)
        (self.repo / "A.kt").write_text("edited")
        # When
        found = repo.unfinished_work([self.pushed(7, CHANGE_ID, lower), self.pushed(8, top_id, top)])
        # Then
        self.assertEqual([(8, False, True)], [(n, w["unpushed"], w["busy"]) for n, w in found.items()])


if __name__ == "__main__":
    unittest.main()
