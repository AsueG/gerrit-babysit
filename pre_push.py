#!/usr/bin/env python3
"""Git pre-push hook: a push for review from a babysit worktree must keep the Change-Id of a change of mine.

An amend that dropped the trailer gets a fresh one from Gerrit's commit-msg hook, and the push opens a new change
instead of updating the old one. Only linked worktrees holding one of my open changes are checked; the main checkout
and the other worktrees push as before. `--install` adds the hook to the watched repo, keeping any existing one."""
import os
import pathlib
import shlex
import sys

import repo
from config import LAUNCHER, REPO, SKILL_DIR, STATUS, read_json

ZERO = "0" * 40
OVERRIDE = "GERRIT_BABYSIT_NEW_CHANGE"
MARKER = "# gerrit-babysit pre-push"


def babysit_worktree(cwd):
    """The toplevel of `cwd` when it is a linked worktree holding one of my open changes, else None."""
    git_dir, common_dir, top = repo.git("rev-parse", "--absolute-git-dir", "--git-common-dir", "--show-toplevel",
                                        cwd=cwd).split("\n")[:3]
    if pathlib.Path(cwd, common_dir).resolve() == pathlib.Path(git_dir).resolve():
        return None
    worktrees = {pathlib.Path(c["worktree"]).resolve() for c in read_json(STATUS).get("changes", []) if c.get("worktree")}
    return top if pathlib.Path(top).resolve() in worktrees else None


def problems(updates, cwd, open_ids, me):
    """(sha, subject, why) for each commit sent for review that would not land on one of `open_ids`.

    Commits authored by someone else (an open parent I build on) carry their own Change-Id and are left alone."""
    for line in updates:
        parts = line.split()
        if len(parts) != 4 or parts[1] == ZERO or not parts[2].startswith("refs/for/"):
            continue
        for sha in repo.git("rev-list", parts[1], "--not", "--remotes", cwd=cwd).split():
            author, subject, trailers = repo.git(
                "log", "-1", "--format=%ae%n%s%n%(trailers:key=Change-Id,valueonly,separator=%x20)", sha,
                cwd=cwd).split("\n")[:3]
            ids = trailers.split()
            if not ids:
                yield sha, subject, "no Change-Id"
            elif len(ids) > 1:
                yield sha, subject, f"{len(ids)} Change-Ids"
            elif ids[0] not in open_ids and author == me:
                yield sha, subject, f"Change-Id {ids[0]} is none of my open changes: the push would open a new one"


def check(updates, cwd):
    if os.environ.get(OVERRIDE) or not babysit_worktree(cwd):
        return 0
    open_ids = {c["change_id"] for c in read_json(STATUS).get("changes", []) if c.get("change_id")}
    me = repo.git("config", "user.email", cwd=cwd).strip()
    found = list(problems(updates, cwd, open_ids, me))
    for sha, subject, why in found:
        print(f"gerrit-babysit: {sha[:10]} {subject}: {why}", file=sys.stderr)
    if found:
        print("gerrit-babysit: push stopped. Restore the Change-Id trailer (git commit --amend), or push a new change "
              f"on purpose with {OVERRIDE}=1 git push …", file=sys.stderr)
        return 1
    return 0


def hook_script():
    """Runs whatever install is current through the launcher, and the hook it replaced first."""
    target = [str(LAUNCHER), "pre_push.py"] if LAUNCHER.is_file() else [str(SKILL_DIR / "pre_push.py")]
    return (f"#!/bin/sh\n{MARKER}: written by pre_push.py --install\n"
            "input=$(cat)\n"
            'if [ -x "$0.local" ]; then printf \'%s\\n\' "$input" | "$0.local" "$@" || exit $?; fi\n'
            # An uninstalled skill must not block every push.
            f"[ -f {shlex.quote(target[0])} ] || exit 0\n"
            f"printf '%s\\n' \"$input\" | /usr/bin/python3 {shlex.join(target)} \"$@\"\n")


def hook_path():
    return pathlib.Path(REPO, repo.git("rev-parse", "--git-path", "hooks").strip()) / "pre-push"


def installed():
    path = hook_path()
    return path.is_file() and MARKER in path.read_text(errors="replace")


def install():
    path = hook_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not installed():
        kept = path.with_name("pre-push.local")
        if kept.exists():
            sys.exit(f"gerrit-babysit: {path} and {kept} both exist, merge them by hand first")
        path.rename(kept)
    path.write_text(hook_script())
    path.chmod(0o755)
    print(f"pre-push hook: {path}")


def main():
    if sys.argv[1:] == ["--install"]:
        return install()
    return check(sys.stdin.read().splitlines(), os.getcwd())


if __name__ == "__main__":
    sys.exit(main())
