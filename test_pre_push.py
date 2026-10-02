"""Run from this directory: python3 -m unittest"""
import contextlib
import io
import json
import pathlib
import subprocess
from unittest import mock

# First: points the config at the fixtures before any module reads it.
from fakes import CHANGE_ID, GitRepoTest
import pre_push

OTHER_ID = "I" + "def0" * 10


class PrePushTest(GitRepoTest):
    def setUp(self):
        super().setUp()
        self.git("update-ref", "refs/remotes/origin/main", "main")
        self.git("checkout", "-q", "-b", "feature")
        self.commit("feat: mine", CHANGE_ID)
        self.git("checkout", "-q", "main")
        self.worktree = pathlib.Path(self.tmp.name) / "wt"
        self.git("worktree", "add", "-q", str(self.worktree), "feature")
        self.status = pathlib.Path(self.tmp.name) / "status.json"
        self.write_status(self.worktree)
        for patcher in (mock.patch.object(pre_push, "STATUS", self.status), mock.patch.object(pre_push, "REPO", self.repo),
                        mock.patch.dict(pre_push.os.environ, {pre_push.OVERRIDE: ""})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_status(self, worktree):
        self.status.write_text(json.dumps({"changes": [{"number": 7, "change_id": CHANGE_ID, "worktree": str(worktree)}]}))

    def in_worktree(self, *args):
        return subprocess.run(["git", "-C", str(self.worktree), *args], capture_output=True, text=True,
                              check=True).stdout.strip()

    def amend(self, message):
        self.in_worktree("commit", "-q", "--amend", "--allow-empty", "-m", message)

    def push(self, cwd=None, remote_ref="refs/for/main"):
        head = subprocess.run(["git", "-C", str(cwd or self.worktree), "rev-parse", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = pre_push.check([f"HEAD {head} {remote_ref} {'0' * 40}"], str(cwd or self.worktree))
        return code, err.getvalue()

    def test_an_amend_that_kept_the_change_id_goes_through(self):
        # Given
        self.amend(f"feat: mine, fixed\n\nChange-Id: {CHANGE_ID}")
        # When
        code, _ = self.push()
        # Then
        self.assertEqual(0, code)

    def test_a_lost_or_replaced_change_id_stops_the_push(self):
        # Given
        self.amend("feat: mine, fixed")
        lost = self.push()
        self.amend(f"feat: mine, fixed\n\nChange-Id: {OTHER_ID}")
        # When
        replaced = self.push()
        # Then
        self.assertEqual((1, 1), (lost[0], replaced[0]))
        self.assertIn("no Change-Id", lost[1])
        self.assertIn("would open a new one", replaced[1])

    def test_the_override_lets_a_new_change_through(self):
        # Given
        self.amend(f"feat: new\n\nChange-Id: {OTHER_ID}")
        # When
        with mock.patch.dict(pre_push.os.environ, {pre_push.OVERRIDE: "1"}):
            code, _ = self.push()
        # Then
        self.assertEqual(0, code)

    def test_someone_elses_parent_keeps_its_own_change_id(self):
        # Given
        self.in_worktree("reset", "-q", "--hard", "main")
        self.in_worktree("-c", "user.email=other@x", "commit", "-q", "--allow-empty", "-m",
                         f"feat: their parent\n\nChange-Id: {OTHER_ID}")
        self.in_worktree("commit", "-q", "--allow-empty", "-m", f"feat: mine\n\nChange-Id: {CHANGE_ID}")
        # When
        code, _ = self.push()
        # Then
        self.assertEqual(0, code)

    def test_the_main_checkout_other_worktrees_and_branch_pushes_are_left_alone(self):
        # Given
        self.amend("feat: mine, no id")
        self.git("commit", "-q", "--allow-empty", "-m", "wip")
        branch_push = self.push(remote_ref="refs/heads/main")
        main_checkout = self.push(cwd=self.repo)
        self.write_status(self.repo)
        # When
        other_worktree = self.push()
        # Then
        self.assertEqual([0, 0, 0], [branch_push[0], main_checkout[0], other_worktree[0]])

    def test_install_keeps_an_existing_hook_and_reinstalls_in_place(self):
        # Given
        hooks = self.repo / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        (hooks / "pre-push").write_text("#!/bin/sh\nexit 0\n")
        # When
        with contextlib.redirect_stdout(io.StringIO()):
            pre_push.install()
            pre_push.install()
        # Then
        self.assertTrue(pre_push.installed())
        self.assertEqual("#!/bin/sh\nexit 0\n", (hooks / "pre-push.local").read_text())
        self.assertIn('"$0.local"', (hooks / "pre-push").read_text())

    def test_the_installed_hook_runs_the_chained_one_and_the_check(self):
        # Given
        hooks = self.repo / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        chained = hooks / "pre-push.local"
        chained.write_text(f"#!/bin/sh\ncat > {self.tmp.name}/chained-input\n")
        chained.chmod(0o755)
        with mock.patch.object(pre_push, "LAUNCHER", pathlib.Path("/nonexistent")), \
                contextlib.redirect_stdout(io.StringIO()):
            pre_push.install()
        self.amend("feat: mine, no id")
        head = self.in_worktree("rev-parse", "HEAD")
        line = f"HEAD {head} refs/for/main {'0' * 40}\n"
        # When
        run = subprocess.run([str(hooks / "pre-push"), "origin", "ssh://x"], input=line, capture_output=True, text=True,
                             cwd=self.worktree, env={**pre_push.os.environ, "GERRIT_BABYSIT_CACHE": str(self.status.parent),
                                                     "PATH": "/usr/bin:/bin"})
        # Then
        self.assertEqual(line, (pathlib.Path(self.tmp.name) / "chained-input").read_text())
        self.assertEqual(1, run.returncode, run.stderr)
        self.assertIn("no Change-Id", run.stderr)
