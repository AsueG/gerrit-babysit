"""Local git work on the repo: fetches into a private namespace, conflict checks, rebases, branch cleanup."""
import os
import pathlib
import re
import subprocess

import gerrit
from config import CONFIG, REPO
from memo import PollMemo

PROTECTED_BRANCHES = set(CONFIG["protected_branches"])
# Private namespace: never moves a ref another session relies on (branches, FETCH_HEAD).
FETCH_NAMESPACE = "refs/gerrit-babysit"


def git_run(*args, cwd=None, timeout=60, input=None, env=None):
    return subprocess.run(["git", "-C", str(cwd or REPO), *args], capture_output=True, text=True, timeout=timeout,
                          input=input, env=env)


def git(*args, cwd=None):
    return git_run(*args, cwd=cwd).stdout


def missing_objects(shas):
    """One `cat-file` for all, instead of a process per change on every poll."""
    if not shas:
        return set()
    out = git_run("cat-file", "--batch-check", input="".join(f"{sha}\n" for sha in shas)).stdout
    return {line.split()[0] for line in out.splitlines() if line.endswith(" missing")}


def fetch_refs(refspecs):
    """One fetch for all; when a ref vanished, one per refspec, so it does not blind the whole poll.

    Any other failure (network, auth) would only repeat itself once per refspec, each paying the same timeout."""
    command = ["fetch", "--quiet", "--no-write-fetch-head", "origin"]
    # Untranslated stderr: the vanished-ref message is matched below.
    env = {**os.environ, "LC_ALL": "C"}
    result = git_run(*command, *refspecs, timeout=180, env=env)
    if result.returncode == 0:
        return
    if "couldn't find remote ref" not in result.stderr:
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
    results = [git_run(*command, refspec, timeout=180, env=env) for refspec in refspecs]
    if all(r.returncode for r in results):
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)


def branch_tips(branches):
    """{branch: tip SHA} of the branches fetched into the namespace; the others are left out."""
    tips = {branch: git("rev-parse", "--verify", "--quiet", f"{FETCH_NAMESPACE}/{branch}").strip()
            for branch in branches}
    return {branch: sha for branch, sha in tips.items() if sha}


_conflicts_memo = PollMemo()


def merge_conflicts(changes):
    """{change number: conflicting files} — `is:mergeable` is often disabled server-side, so merge locally."""
    candidates = [(c, c["currentPatchSet"]) for c in changes if c.get("currentPatchSet", {}).get("revision")]
    if not candidates:
        return {}
    missing = missing_objects([ps["revision"] for _, ps in candidates])
    refspecs = {f"+refs/heads/{c['branch']}:{FETCH_NAMESPACE}/{c['branch']}" for c, _ in candidates}
    refspecs |= {f"+{ps['ref']}:{FETCH_NAMESPACE}/changes/{c['number']}"
                 for c, ps in candidates if ps["revision"] in missing}
    fetch_refs(sorted(refspecs))

    tips = branch_tips({c["branch"] for c, _ in candidates})
    conflicts = {}
    with _conflicts_memo.poll() as memo:
        for change, patch_set in candidates:
            target = tips.get(change["branch"])
            if not target:
                continue
            content_merge = gerrit.uses_content_merge(change["project"])
            merge = merged_with_content if content_merge else changed_on_both_sides
            # None when the patch set is not fetched (its ref failed): unknown, not clean.
            files = memo.get((target, patch_set["revision"], content_merge), merge, target, patch_set["revision"])
            if files:
                conflicts[change["number"]] = files
    return conflicts


def merged_with_content(target, revision):
    """A 3-way merge of the whole patch set onto the branch tip: conflicts iff Gerrit's rebase-on-submit would."""
    result = git_run("merge-tree", "--write-tree", "--name-only", "--no-messages", target, revision)
    if result.returncode not in (0, 1):
        return None
    return result.stdout.splitlines()[1:] if result.returncode == 1 else []


def changed_on_both_sides(target, revision):
    """Gerrit's merge with content merge off: a path both sides changed, each its own way, is a conflict."""
    base = git_run("merge-base", target, revision)
    if base.returncode:
        return None
    diffs = [git_run("diff", "--no-renames", "--name-only", a, b)
             for a, b in ((base.stdout.strip(), target), (base.stdout.strip(), revision), (target, revision))]
    if any(d.returncode for d in diffs):
        return None
    theirs, mine, different = (set(d.stdout.splitlines()) for d in diffs)
    return sorted(theirs & mine & different)


_ancestry_memo = PollMemo()


def is_ancestor(base, target):
    """None when the base is not fetched yet: unknown, asked again on the next poll."""
    result = git_run("merge-base", "--is-ancestor", base, target).returncode
    return {0: True, 1: False}.get(result)


def stale_parents(changes, statuses):
    """{change number: {parent, old_parent_sha}} when the parent merged under another SHA (rebase on submit).

    Must run after merge_conflicts(), which fetches the patch sets and branch tips it relies on."""
    merged = []
    for change in changes:
        parent, status, _ = statuses.get(change["number"], (None, None, None))
        base = change.get("currentPatchSet", {}).get("parents", [None])[0]
        if status == "MERGED" and base:
            merged.append((change, parent, base))
    tips = branch_tips({change["branch"] for change, _, _ in merged})
    stale = {}
    with _ancestry_memo.poll() as memo:
        for change, parent, base in merged:
            target = tips.get(change["branch"])
            if target and memo.get((base, target), is_ancestor, base, target) is False:
                stale[change["number"]] = {"parent": parent, "old_parent_sha": base}
    return stale


CHANGE_ID = re.compile(r"I[0-9a-f]{40}")


def change_id_of(sha):
    """Only a well-formed Change-Id: the value ends up inside a quoted Gerrit query."""
    value = git("log", "-1", "--format=%(trailers:key=Change-Id,valueonly)", sha).strip()
    return value if CHANGE_ID.fullmatch(value) else ""


_cleanup_memo = {}
_change_ids = PollMemo()


def cleanup_candidates(open_ids=frozenset()):
    """Read-only: local branches (and their worktree) whose tip is a pushed patch set of a merged or abandoned CL of
    mine.

    Branches of still-open CLs are skipped, so the Gerrit query only reruns when a CL leaves `open_ids` or a
    branch moves. Runs every poll: only the few candidates pay for a `git status` of their worktree."""
    blocks = [dict(line.partition(" ")[::2] for line in block.splitlines())
              for block in git("worktree", "list", "--porcelain").split("\n\n") if block.strip()]
    main_branch = blocks[0].get("branch", "").removeprefix("refs/heads/") if blocks else ""
    worktree_of = {b["branch"].removeprefix("refs/heads/"): b for b in blocks[1:] if "branch" in b}

    tips = dict(line.split(" ") for line in
                git("for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads").splitlines())
    with _change_ids.poll() as memo:
        change_ids = {sha: memo.get(sha, change_id_of, sha) for sha in set(tips.values())}
    branches = {}
    for branch, sha in tips.items():
        worktree = worktree_of.get(branch)
        if branch in PROTECTED_BRANCHES or branch == main_branch or (worktree and "locked" in worktree):
            continue
        change_id = change_ids[sha]
        if change_id and change_id not in open_ids:
            branches[branch] = (sha, change_id, worktree["worktree"] if worktree else None)
    if not branches:
        return []
    key = frozenset(branches.items())
    if key not in _cleanup_memo:
        _cleanup_memo.clear()
        _cleanup_memo[key] = list(closed_branches(branches))
    # Checked on every call, not memoized: a worktree can get dirty without its branch moving.
    return [(k, event) for k, event in _cleanup_memo[key]
            if not event["worktree"] or not git("status", "--porcelain", cwd=event["worktree"]).strip()]


def closed_branches(branches):
    ids = " OR ".join(f"change:{cid}" for cid in sorted({cid for _, cid, _ in branches.values()}))
    closed = {
        row["id"]: ({ps["revision"] for ps in row.get("patchSets", [])}, row["number"], row["subject"],
                    row.get("status", "MERGED").lower())
        for row in gerrit.query(f"owner:self (status:merged OR status:abandoned) ({ids})", "--patch-sets")
    }
    for branch, (sha, change_id, path) in branches.items():
        revisions, number, subject, status = closed.get(change_id, (set(), None, None, None))
        # Exact pushed SHA only: a local amend made after the merge or the abandon is not a candidate. An abandoned
        # change can be restored, but its pushed patch sets stay on Gerrit: deleting the branch loses nothing.
        if sha in revisions:
            yield f"cleanup:{branch}:{sha}", {"kind": "cleanup_candidate", "branch": branch, "worktree": path,
                                               "change": number, "subject": subject, "status": status}


def interdiff(event):
    """What moved since the patch set I last saw; the diff is limited to the change's files to keep rebase noise out."""
    refs = {ref: f"{FETCH_NAMESPACE}/review/{event['change']}/{ref.rsplit('/', 1)[1]}"
            for ref in (event["since_ref"], event["current_ref"])}
    try:
        fetch = git_run("fetch", "--quiet", "--no-write-fetch-head", "origin",
                        *(f"+{ref}:{local}" for ref, local in refs.items()), timeout=180)
        if fetch.returncode:
            return {"error": gerrit.error_detail(fetch)}
        old, new = refs[event["since_ref"]], refs[event["current_ref"]]
        files = sorted({f for sha in (old, new) for f in git("diff", "--name-only", f"{sha}^", sha).splitlines()})
        return {"rebased": git("rev-parse", f"{old}^") != git("rev-parse", f"{new}^"),
                "stat": git("diff", "--stat", old, new, "--", *files).strip()[-3000:] if files else ""}
    except (subprocess.SubprocessError, OSError) as error:
        return {"error": gerrit.error_detail(error)}
    finally:
        for local in refs.values():
            git_run("update-ref", "-d", local)


IN_PROGRESS = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")


def busy(worktree):
    """Tracked edits or an operation halfway through: someone is working there, a rebase would get in the way."""
    if git("status", "--porcelain", "--untracked-files=no", cwd=worktree).strip():
        return True
    return any(pathlib.Path(worktree, git("rev-parse", "--git-path", name, cwd=worktree).strip()).exists()
               for name in IN_PROGRESS)


def rebase_target(event):
    """The commit the change must sit on now, fetched if needed; None when it cannot be had."""
    if event["kind"] == "parent_merged":
        target = f"{FETCH_NAMESPACE}/{event['branch']}"
        return git("rev-parse", "--verify", "--quiet", target).strip() or None
    sha = event["new_parent_sha"]
    if git_run("cat-file", "-e", f"{sha}^{{commit}}").returncode and event.get("parent_ref"):
        git_run("fetch", "--quiet", "--no-write-fetch-head", "origin",
                f"+{event['parent_ref']}:{FETCH_NAMESPACE}/changes/{event['parent']}", timeout=180)
    return sha if git_run("cat-file", "-e", f"{sha}^{{commit}}").returncode == 0 else None


def prepare_rebase(event):
    """Local only, never pushed: moves the change (and what is stacked on it) onto its new base in its worktree.

    A conflict is aborted, leaving the worktree as it was, and reported with its files and the command to rerun."""
    main_checkout = git("worktree", "list", "--porcelain").partition("\n")[0].removeprefix("worktree ")
    worktree = worktrees_by_change_id().get(event["change_id"])
    if not worktree or worktree == main_checkout:
        return {"status": "no_worktree"}
    if busy(worktree):
        return {"status": "busy", "worktree": worktree}
    onto = rebase_target(event)
    if not onto:
        return {"status": "error", "worktree": worktree, "detail": "new base not fetched"}
    old = event["old_parent_sha"]
    head = git("rev-parse", "HEAD", cwd=worktree).strip()
    if git_run("merge-base", "--is-ancestor", onto, head, cwd=worktree).returncode == 0:
        return {"status": "up_to_date", "worktree": worktree}
    if git_run("merge-base", "--is-ancestor", old, head, cwd=worktree).returncode != 0:
        # HEAD was rebased or reset by hand since the push: guessing what to replay could lose work.
        return {"status": "diverged", "worktree": worktree, "head": head}
    command = ["rebase", "--onto", onto, old]
    try:
        result = git_run(*command, cwd=worktree, timeout=300)
    except subprocess.TimeoutExpired:
        git_run("rebase", "--abort", cwd=worktree)
        raise
    report = {"worktree": worktree, "onto": onto, "previous_head": head,
              "command": " ".join(["git", *command])}
    if result.returncode:
        files = git("diff", "--name-only", "--diff-filter=U", cwd=worktree).split()
        git_run("rebase", "--abort", cwd=worktree)
        return {**report, "status": "conflict", "files": files}
    return {**report, "status": "rebased", "head": git("rev-parse", "HEAD", cwd=worktree).strip(),
            "commits": int(git("rev-list", "--count", f"{onto}..HEAD", cwd=worktree).strip() or 0)}


_worktrees_memo = {}


def unfinished_work(changes):
    """{change number: {worktree, unpushed, busy}} for my open changes whose worktree holds work Gerrit has not
    seen: a commit never pushed (an amend, a prepared rebase) or edits and an operation halfway through. A worktree
    holding a stack is reported once, on the change at its HEAD when there is one."""
    by_worktree = {}
    for change in changes:
        path = worktrees_by_change_id().get(change["id"])
        if path:
            by_worktree.setdefault(path, []).append(change)
    found = {}
    for path, stack in by_worktree.items():
        head = git("rev-parse", "HEAD", cwd=path).strip()
        top = next((c for c in stack if change_id_of(head) == c["id"]), None)
        unpushed = top is not None and head != top.get("currentPatchSet", {}).get("revision")
        in_progress = busy(path)
        if unpushed or in_progress:
            found[(top or stack[0])["number"]] = {"worktree": path, "unpushed": unpushed, "busy": in_progress}
    return found


def worktrees_by_change_id():
    """Change-Id → worktree path; linked worktrees win over the main checkout, whose branch keeps moving.

    Only recomputed when a worktree appears, disappears or moves its HEAD."""
    listing = git("worktree", "list", "--porcelain")
    if listing not in _worktrees_memo:
        paths = [line.removeprefix("worktree ") for line in listing.splitlines() if line.startswith("worktree ")]
        found = {}
        for path in reversed(paths):
            trailers = git("log", "-10", "--format=%(trailers:key=Change-Id,valueonly)", "HEAD", cwd=path)
            for change_id in trailers.split():
                found.setdefault(change_id, path)
        _worktrees_memo.clear()
        _worktrees_memo[listing] = found
    return _worktrees_memo[listing]
