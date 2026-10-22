"""Builders shared by the test modules. Imported first: it points the config at the fixtures."""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
os.environ["GERRIT_BABYSIT_CONFIG"] = str(FIXTURES / "config.json")
os.environ.setdefault("GERRIT_BABYSIT_CACHE", tempfile.mkdtemp(prefix="gerrit-babysit-test-"))

import events  # noqa: E402
import gerrit  # noqa: E402
import repo  # noqa: E402

ZUUL = json.loads((FIXTURES / "zuul_messages.json").read_text())


def approval(label, value, by="someone"):
    return {"type": label, "value": str(value), "by": {"username": by}}


NOW = time.time()
DAY = "2026-09-29"
CHANGE_ID = "I" + "abc0" * 10


def patch_set(number=1, *approvals, parents=("base",), created=NOW):
    return {"number": number, "revision": f"rev{number}", "ref": f"refs/changes/01/1/{number}",
            "parents": list(parents), "approvals": list(approvals), "createdOn": created}


def message(author, text, timestamp=1):
    return {"reviewer": {"username": author, "name": author.title()}, "message": text, "timestamp": timestamp}


def change(number=1, current=None, comments=(), **extra):
    return {"number": number, "id": f"I{number}", "subject": f"subject {number}", "project": "app", "branch": "main",
            "url": f"https://review/{number}", "status": "NEW", "currentPatchSet": current or patch_set(),
            "comments": list(comments), **extra}


GREEN_CI = [approval(label, 1, "zuul") for label in events.CI_LABELS]


def poll_result(changes=(), reviews=(), conflicts=None, parents=None, stale=None, threads=None):
    return {"changes": list(changes), "reviews": list(reviews), "conflicts": conflicts or {},
            "parents": parents or {}, "stale_parents": stale or {}, "threads": threads or {}}


def inline(id_, author, text, updated, reply_to=None, unresolved=True, line=3, ps=1):
    """A comment as REST `/changes/<n>/comments` returns it."""
    return {"id": id_, "in_reply_to": reply_to, "unresolved": unresolved, "patch_set": ps, "line": line,
            "updated": f"2026-09-29 10:00:{updated:02d}.000000000", "message": text,
            "author": {"username": author, "name": author.title()}}


def review(current=1, patch_sets=None, comments=(), reviewers=1, **extra):
    owner = {"username": "owner", "name": "Owner"}
    people = [{"username": gerrit.USER}, {"username": gerrit.CI_USER}, owner]
    people += [{"username": f"r{i}"} for i in range(reviewers - 1)]
    sets = patch_sets or [patch_set(n) for n in range(1, current + 1)]
    return change(current=sets[-1], comments=comments, owner=owner, allReviewers=people, patchSets=sets, **extra)


def zuul_verdict(ps, timestamp, result):
    return message("zuul", f"Patch Set {ps}: Build{'+1' if result == 'SUCCESS' else '-1'}\n\n"
                           f"- unit https://zuul/build/u{int(timestamp)} : {result}", timestamp)


_template = None


def template_repo():
    """Built once per run, then copied: a git process costs ~25 ms, and every repo test needs the same start."""
    global _template
    if _template is None:
        _template = tempfile.TemporaryDirectory()
        run = lambda *args: subprocess.run(["git", "-C", _template.name, *args], capture_output=True, check=True)
        run("init", "-q", "-b", "main")
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        run("commit", "-q", "--allow-empty", "-m", "base")
    return pathlib.Path(_template.name)


class GitRepoTest(unittest.TestCase):
    """A throwaway repo standing in for the checkout: REPO is resolved at call time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self.tmp.name) / "repo"
        shutil.copytree(template_repo(), self.repo, symlinks=True)
        patcher = mock.patch.object(repo, "REPO", self.repo)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True,
                              check=True).stdout.strip()

    def commit(self, subject, change_id=None):
        message = f"{subject}\n\nChange-Id: {change_id}" if change_id else subject
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")
