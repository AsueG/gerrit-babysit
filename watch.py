#!/usr/bin/env python3
"""Block until one of my open Gerrit changes gets a new actionable event, print it as JSON, exit."""
import argparse
import base64
import datetime
import json
import netrc
import os
import pathlib
import re
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

import ci
import snooze
from config import (CACHE, CI_STUCK_S, CONFIG, REPO, STALE_AFTER_S, STATUS, SWIFTBAR_PLUGIN, atomic_write,
                    gerrit_user, t)

HOST = CONFIG["gerrit_host"]
PORT = str(CONFIG["ssh_port"])
USER = gerrit_user()
QUERY = "owner:self status:open"
REVIEW_QUERY = "reviewer:self status:open -owner:self"
ATTENTION_QUERY = "attention:self status:open"
# Group additions put dozens of people on a change: not a personal review request.
MAX_REVIEWERS = CONFIG["max_reviewers"]
STATE = CACHE / "seen.json"
CI_USER = CONFIG["ci_user"]
# "" = Gerrit itself (auto-abandon notices carry no username).
BOT_USERS = {CI_USER, "", *CONFIG["bot_users"]}
CI_LABELS = tuple(CONFIG["ci_labels"])
PROTECTED_BRANCHES = set(CONFIG["protected_branches"])
# Private namespace: never moves a ref another session relies on (branches, FETCH_HEAD).
FETCH_NAMESPACE = "refs/gerrit-babysit"
DAEMON_STATE = CACHE / "daemon-seen.json"
DAEMON_POLL = CACHE / "daemon-poll.json"
SESSION = CACHE / "session.json"
MAX_THREADS = 20
REST = f"https://{HOST}/a"
# Longer than the daemon's interval, so the session sees at least one newer poll before reporting.
SETTLE_S = 90
MAX_BACKOFF_S = 600
SEEN_RETENTION_S = 30 * 86400
WORK_HOURS = tuple(CONFIG["work_hours"])
UNREVIEWED_WORKING_DAYS = 2
# Past this many notifications in one poll (typically the morning flush), a single summary replaces them.
DIGEST_OVER = 3
DASHBOARD = f"https://{HOST}/dashboard/self"


def gerrit_query(query, *options):
    out = subprocess.run(
        ["ssh", "-p", PORT, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", f"{USER}@{HOST}",
         "gerrit", "query", "--format=JSON", *options, f"'{query}'"],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    rows = [json.loads(line) for line in out.splitlines() if line.strip()]
    return [r for r in rows if r.get("type") != "stats"]


def fetch_changes():
    return gerrit_query(QUERY, "--current-patch-set", "--comments", "--dependencies", "--all-reviewers")


def fetch_attention():
    return sorted(row["number"] for row in gerrit_query(ATTENTION_QUERY))


_rest_auth = None


def http_credentials():
    """(user, HTTP password): from the Gerrit MCP server's config when set, so the token lives in one place."""
    if CONFIG["gerrit_mcp_config"]:
        hosts = json.loads(pathlib.Path(CONFIG["gerrit_mcp_config"]).expanduser().read_text())["gerrit_hosts"]
        entry = next((h for h in hosts if HOST in {urllib.parse.urlparse(h.get(key) or "").hostname
                                                   for key in ("external_url", "internal_url")}), None)
        if not entry:
            raise KeyError(f"no gerrit_hosts entry for {HOST} in {CONFIG['gerrit_mcp_config']}")
        auth = entry["authentication"]
        return auth["username"], auth["auth_token"]
    entry = netrc.netrc().authenticators(HOST)
    if not entry:
        raise KeyError(f"no ~/.netrc entry for {HOST}")
    return entry[0], entry[2]


def rest_get(path, retry=True):
    global _rest_auth
    if _rest_auth is None:
        _rest_auth = base64.b64encode(":".join(http_credentials()).encode()).decode()
    request = urllib.request.Request(REST + path, headers={"Authorization": f"Basic {_rest_auth}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            # Gerrit prefixes every JSON body with )]}' against XSSI.
            return json.loads(response.read().decode().split("\n", 1)[1])
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise
        # The token may have been rotated since the daemon started: reread it once.
        _rest_auth = None
        if not retry:
            raise
        error.close()
        return rest_get(path, retry=False)


_threads_memo = {}


def open_threads(changes):
    """{change number: threads awaiting me}; the REST call only reruns when the change moves."""
    global _threads_memo
    keys = {c["number"]: (c["number"], c.get("lastUpdated")) for c in changes}
    _threads_memo = {key: _threads_memo[key] if key in _threads_memo
                     else awaiting_threads(rest_get(f"/changes/{number}/comments"))
                     for number, key in keys.items()}
    return {number: _threads_memo[key] for number, key in keys.items()}


def fetch_reviews():
    return gerrit_query(REVIEW_QUERY, "--current-patch-set", "--all-approvals", "--all-reviewers", "--comments")


def parent_statuses(changes):
    """{change number: (parent number, parent status, parent's current patch set)} — `dependsOn` lists merged
    parents too."""
    parent_of = {c["number"]: c["dependsOn"][0]["number"] for c in changes if c.get("dependsOn")}
    parents = {c["number"]: c for c in changes}
    others = sorted(set(parent_of.values()) - parents.keys())
    if others:
        query = " OR ".join(f"change:{n}" for n in others)
        parents |= {row["number"]: row for row in gerrit_query(query, "--current-patch-set")}
    return {number: (parent, parents.get(parent, {}).get("status"), parents.get(parent, {}).get("currentPatchSet", {}))
            for number, parent in parent_of.items()}


def outdated_parents(changes, statuses):
    """{change number: {parent, parent_patch_set, parent_ref, old_parent_sha, new_parent_sha}} when the open parent
    got a patch set the change is not based on yet."""
    outdated = {}
    for change in changes:
        parent, status, parent_patch_set = statuses.get(change["number"], (None, None, {}))
        base = change.get("currentPatchSet", {}).get("parents", [None])[0]
        new = (parent_patch_set or {}).get("revision")
        if status == "NEW" and base and new and base != new:
            outdated[change["number"]] = {"parent": parent, "parent_patch_set": parent_patch_set.get("number"),
                                          "parent_ref": parent_patch_set.get("ref"), "old_parent_sha": base,
                                          "new_parent_sha": new}
    return outdated


def stale_parents(changes, statuses):
    """{change number: {parent, old_parent_sha}} when the parent merged under another SHA (rebase on submit).

    Must run after merge_conflicts(), which fetches the patch sets and branch tips it relies on."""
    stale = {}
    for change in changes:
        parent, status, _ = statuses.get(change["number"], (None, None, None))
        base = change.get("currentPatchSet", {}).get("parents", [None])[0]
        if status != "MERGED" or not base:
            continue
        target = f"{FETCH_NAMESPACE}/{change['branch']}"
        if git_run("merge-base", "--is-ancestor", base, target).returncode == 1:
            stale[change["number"]] = {"parent": parent, "old_parent_sha": base}
    return stale


def poll():
    changes = fetch_changes()
    statuses = parent_statuses(changes)
    conflicts = merge_conflicts(changes)
    # REST relies on the HTTP password, SSH does not: an expired token must not blind the whole watcher.
    threads, threads_error = {}, None
    try:
        threads = open_threads(changes)
    except (OSError, ValueError, KeyError) as error:
        threads_error = error_detail(error)
    return {
        "changes": changes,
        "conflicts": conflicts,
        "parents": {n: parent for n, (parent, status, _) in statuses.items() if status == "NEW"},
        "stale_parents": stale_parents(changes, statuses),
        "outdated_parents": outdated_parents(changes, statuses),
        "threads": threads,
        "threads_error": threads_error,
        "reviews": fetch_reviews(),
        "attention": fetch_attention(),
    }


def with_int_keys(result):
    """JSON turns the change-number keys into strings; defaults cover a daemon still running older code."""
    return {**result, "reviews": result.get("reviews", []), "attention": result.get("attention", []),
            **{key: {int(k): v for k, v in result.get(key, {}).items()}
               for key in ("conflicts", "parents", "stale_parents", "outdated_parents", "threads")}}


class DaemonError(Exception):
    pass


def error_detail(error):
    """ssh's own stderr ('Could not resolve hostname…') says far more than 'exit status 255'."""
    stderr = getattr(error, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    last_line = stderr.strip().splitlines()[-1] if stderr and stderr.strip() else ""
    return last_line or str(error)


def daemon_poll():
    """The daemon's latest poll when it is alive, so a session never hits Gerrit or the fetch refs a second time."""
    if not DAEMON_POLL.exists():
        return None
    heartbeat = json.loads(DAEMON_POLL.read_text())
    if time.time() - heartbeat["attempt"] > STALE_AFTER_S:
        return None
    if heartbeat["error"]:
        raise DaemonError(heartbeat["error"])
    return with_int_keys(heartbeat["poll"])


def write_daemon_poll(result=None, error=None):
    previous = json.loads(DAEMON_POLL.read_text()) if DAEMON_POLL.exists() else {}
    atomic_write(DAEMON_POLL, {"attempt": time.time(), "error": error,
                               "poll": result if error is None else previous.get("poll")})


def git_run(*args, cwd=None, timeout=60):
    return subprocess.run(["git", "-C", str(cwd or REPO), *args], capture_output=True, text=True, timeout=timeout)


def git(*args, cwd=None):
    return git_run(*args, cwd=cwd).stdout


def fetch_refs(refspecs):
    """One fetch for all; on failure one per refspec, so a single vanished ref does not blind the whole poll."""
    command = ["fetch", "--quiet", "--no-write-fetch-head", "origin"]
    result = git_run(*command, *refspecs, timeout=180)
    if result.returncode == 0:
        return
    results = [git_run(*command, refspec, timeout=180) for refspec in refspecs]
    if all(r.returncode for r in results):
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)


_conflicts_memo = {}


def merge_conflicts(changes):
    """{change number: conflicting files} — `is:mergeable` is often disabled server-side, so merge locally."""
    candidates = [(c, c["currentPatchSet"]) for c in changes if c.get("currentPatchSet", {}).get("revision")]
    if not candidates:
        return {}
    refspecs = {f"+refs/heads/{c['branch']}:{FETCH_NAMESPACE}/{c['branch']}" for c, _ in candidates}
    refspecs |= {f"+{ps['ref']}:{FETCH_NAMESPACE}/changes/{c['number']}"
                 for c, ps in candidates if git_run("cat-file", "-e", ps["revision"]).returncode != 0}
    fetch_refs(sorted(refspecs))

    conflicts = {}
    for change, patch_set in candidates:
        target = git("rev-parse", "--verify", "--quiet", f"{FETCH_NAMESPACE}/{change['branch']}").strip()
        if not target:
            continue
        key = (target, patch_set["revision"])
        if key not in _conflicts_memo:
            # A 3-way merge of the whole patch set onto the branch tip: conflicts iff Gerrit's rebase-on-submit would.
            result = git_run("merge-tree", "--write-tree", "--name-only", "--no-messages", target, patch_set["revision"])
            if result.returncode not in (0, 1):
                # Patch set not fetched (its ref failed): unknown, not clean, so retry on the next poll.
                continue
            _conflicts_memo[key] = result.stdout.splitlines()[1:] if result.returncode == 1 else []
        if _conflicts_memo[key]:
            conflicts[change["number"]] = _conflicts_memo[key]
    return conflicts


CHANGE_ID = re.compile(r"I[0-9a-f]{40}")


def change_id_of(sha):
    """Only a well-formed Change-Id: the value ends up inside a quoted Gerrit query."""
    value = git("log", "-1", "--format=%(trailers:key=Change-Id,valueonly)", sha).strip()
    return value if CHANGE_ID.fullmatch(value) else ""


_cleanup_memo = {}
_change_ids = {}


def cleanup_candidates(open_ids=frozenset()):
    """Read-only: local branches (and their worktree) whose tip is a pushed patch set of a merged CL of mine.

    Branches of still-open CLs are skipped, so the Gerrit query only reruns when a CL leaves `open_ids` or a
    branch moves. Runs every poll: only the few candidates pay for a `git status` of their worktree."""
    global _change_ids
    blocks = [dict(line.partition(" ")[::2] for line in block.splitlines())
              for block in git("worktree", "list", "--porcelain").split("\n\n") if block.strip()]
    main_branch = blocks[0].get("branch", "").removeprefix("refs/heads/") if blocks else ""
    worktree_of = {b["branch"].removeprefix("refs/heads/"): b for b in blocks[1:] if "branch" in b}

    tips = dict(line.split(" ") for line in
                git("for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads").splitlines())
    _change_ids = {sha: _change_ids[sha] if sha in _change_ids else change_id_of(sha) for sha in set(tips.values())}
    branches = {}
    for branch, sha in tips.items():
        worktree = worktree_of.get(branch)
        if branch in PROTECTED_BRANCHES or branch == main_branch or (worktree and "locked" in worktree):
            continue
        change_id = _change_ids[sha]
        if change_id and change_id not in open_ids:
            branches[branch] = (sha, change_id, worktree["worktree"] if worktree else None)
    if not branches:
        return []
    key = frozenset(branches.items())
    if key not in _cleanup_memo:
        _cleanup_memo.clear()
        _cleanup_memo[key] = list(merged_branches(branches))
    # Checked on every call, not memoized: a worktree can get dirty without its branch moving.
    return [(k, event) for k, event in _cleanup_memo[key]
            if not event["worktree"] or not git("status", "--porcelain", cwd=event["worktree"]).strip()]


def merged_branches(branches):
    ids = " OR ".join(f"change:{cid}" for cid in sorted({cid for _, cid, _ in branches.values()}))
    merged = {
        row["id"]: ({ps["revision"] for ps in row.get("patchSets", [])}, row["number"], row["subject"])
        for row in gerrit_query(f"owner:self status:merged ({ids})", "--patch-sets")
    }
    for branch, (sha, change_id, path) in branches.items():
        revisions, number, subject = merged.get(change_id, (set(), None, None))
        # Exact pushed SHA only: a local amend made after the merge is not a candidate.
        if sha in revisions:
            yield f"cleanup:{branch}:{sha}", {"kind": "cleanup_candidate", "branch": branch, "worktree": path,
                                               "change": number, "subject": subject}


CI_NEGATIVE_VOTE = re.compile(rf"\b(?:{'|'.join(CI_LABELS)})-[12]\b")
POSITIVE_VOTES_ONLY = re.compile(r"^Patch Set \d+:(?: [\w-]+\+[12])+$")


def is_actionable(message):
    author = message.get("reviewer", {}).get("username", "")
    if author == USER or (author in BOT_USERS and author != CI_USER):
        return False
    header, _, body = message.get("message", "").partition("\n")
    if author == CI_USER:
        # Only the header line carries zuul's votes ("Patch Set 5: Build-1"); job names and URLs below can contain "-1".
        return bool(CI_NEGATIVE_VOTE.search(header))
    # A bare +1/+2 needs nothing; a +2 that unblocks the CL comes back as ready_to_submit.
    return not (POSITIVE_VOTES_ONLY.match(header.strip()) and not body.strip())


def is_ready_to_submit(patch_set):
    # submitRecords can report OK on a change voted -1 (seen on Gerrit 3.x), so read the votes instead.
    votes = votes_of(patch_set)
    code_review = votes.get("Code-Review", [])
    return 2 in code_review and min(code_review) >= 0 and ci_passed(votes)


def ci_passed(votes):
    # Some CIs vote +2 (e.g. a gate pipeline), not only +1.
    return all(max(votes.get(label, [0])) >= 1 and min(votes[label]) >= 0 for label in CI_LABELS)


def votes_of(patch_set):
    votes = {}
    for approval in patch_set.get("approvals", []):
        votes.setdefault(approval["type"], []).append(int(approval["value"]))
    return votes


def failed_ci_labels(votes):
    return [label for label in CI_LABELS if min(votes.get(label, [0])) < 0]


def latest_ci_verdict(change, patch_set):
    prefix = f"Patch Set {patch_set.get('number')}:"
    verdicts = [m for m in change.get("comments", [])
                if m["reviewer"].get("username") == CI_USER and m["message"].startswith(prefix)]
    return max(verdicts, key=lambda m: m["timestamp"], default=None)


def ci_idle_since(change, patch_set):
    """Zuul's last sign of life on this patch set (a recheck's "Starting" counts), else the upload."""
    prefix = f"Patch Set {patch_set.get('number')}:"
    stamps = [m["timestamp"] for m in change.get("comments", [])
              if m["reviewer"].get("username") == CI_USER and m["message"].startswith(prefix)]
    return max(stamps, default=patch_set.get("createdOn", 0))


def is_ci_stuck(change, patch_set, now):
    return (ci_state(change, patch_set, votes_of(patch_set)) == "running"
            and now - ci_idle_since(change, patch_set) > CI_STUCK_S)


def is_merge_failed(change, patch_set):
    """Zuul's latest verdict on this patch set is 'Merge Failed.': no job ran, the base is stale."""
    verdict = latest_ci_verdict(change, patch_set)
    return verdict is not None and "Merge Failed." in verdict["message"]


RECHECK = re.compile(r"^recheck(?:-[\w-]+)?$")


def my_rechecks(change, patch_set, before):
    """How many times I already asked zuul to rerun this patch set before the given verdict."""
    prefix = f"Patch Set {patch_set.get('number')}:"
    return sum(1 for m in change.get("comments", [])
               if m["reviewer"].get("username") == USER and m["timestamp"] < before
               and m["message"].startswith(prefix) and RECHECK.match(m["message"].partition("\n")[2].strip()))


def prereview(event):
    path = CACHE / "prereview" / f"{event['change']}-{event['patch_set']}.md"
    return {"path": str(path), "done": path.exists()}


def interdiff(event):
    """What moved since the patch set I last saw; the diff is limited to the change's files to keep rebase noise out."""
    refs = {ref: f"{FETCH_NAMESPACE}/review/{event['change']}/{ref.rsplit('/', 1)[1]}"
            for ref in (event["since_ref"], event["current_ref"])}
    try:
        fetch = git_run("fetch", "--quiet", "--no-write-fetch-head", "origin",
                        *(f"+{ref}:{local}" for ref, local in refs.items()), timeout=180)
        if fetch.returncode:
            return {"error": error_detail(fetch)}
        old, new = refs[event["since_ref"]], refs[event["current_ref"]]
        files = sorted({f for sha in (old, new) for f in git("diff", "--name-only", f"{sha}^", sha).splitlines()})
        return {"rebased": git("rev-parse", f"{old}^") != git("rev-parse", f"{new}^"),
                "stat": git("diff", "--stat", old, new, "--", *files).strip()[-3000:] if files else ""}
    except (subprocess.SubprocessError, OSError) as error:
        return {"error": error_detail(error)}
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


FLAKY = CACHE / "flaky.json"
FLAKY_RETENTION_S = 90 * 86400


def load_flaky():
    return json.loads(FLAKY.read_text()) if FLAKY.exists() else {}


def record_flaky(result, now=None):
    """Keeps the flakes seen in the polled changes: their messages leave the query once the change merges."""
    now = now or time.time()
    runs = dict(run for c in result["changes"] + result.get("reviews", []) for run in ci.flaky_runs(c))
    stored = load_flaky()
    kept = {k: r for k, r in {**stored, **runs}.items() if now - r["passed_at"] < FLAKY_RETENTION_S}
    if kept.keys() != stored.keys():
        atomic_write(FLAKY, dict(sorted(kept.items())))


def enrich(reported):
    """Network-bound extras, only for events about to be reported, so the idle loop stays free."""
    histories = {}
    flakes = load_flaky()
    for event in reported:
        verdict = event.get("ci_verdict") or (event.get("message") if event.get("author_username") == CI_USER else None)
        if verdict:
            flaky = ci.flaky_counts(flakes, [j["job"] for j in ci.failed_jobs(verdict)])
            if flaky:
                event["known_flaky"] = flaky
        if verdict and ci.ZUUL_API:
            event["ci_diagnosis"] = ci.diagnose_ci({"change": event["change"], "message": verdict}, histories,
                                                   event.get("known_flaky"))
            if ci.PERIODIC_BUILD:
                event["base_build"] = ci.base_build(event["branch"])
        if event["kind"] in ("parent_merged", "parent_updated"):
            try:
                event["rebase"] = prepare_rebase(event)
            except (subprocess.SubprocessError, OSError) as error:
                event["rebase"] = {"status": "error", "detail": error_detail(error)}
        if event["kind"] == "review_new_patch_set" and event.get("since_ref") and event.get("current_ref"):
            event["interdiff"] = interdiff(event)
        if event["kind"] == "ci_stuck" and ci.ZUUL_API:
            event["zuul_queue"] = ci.zuul_queue(event["change"], event["patch_set"])
        if event["kind"] == "review_requested" and not event.get("participated"):
            event["prereview"] = prereview(event)
    return reported


def ci_state(change, patch_set, votes):
    if failed_ci_labels(votes):
        return "stale_base" if is_merge_failed(change, patch_set) else "failed"
    if ci_passed(votes):
        return "passed"
    return "running"


_worktrees_memo = {}


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


def write_status(result=None, error=None):
    """Snapshot read by the SwiftBar plugin and the Claude Code status line, so they never hit Gerrit themselves.

    On error the last good rows are kept and `updated` stays at the last success."""
    now = time.time()
    if error is not None:
        previous = json.loads(STATUS.read_text()) if STATUS.exists() else {}
        atomic_write(STATUS, {**previous, "last_attempt": now, "last_error": error})
        return
    worktrees = worktrees_by_change_id()
    threads_error = result.get("threads_error")
    flakes = load_flaky()
    rows = []
    for change in result["changes"]:
        patch_set = change.get("currentPatchSet", {})
        votes = votes_of(patch_set)
        code_review = votes.get("Code-Review", [])
        state = ci_state(change, patch_set, votes)
        verdict = latest_ci_verdict(change, patch_set) if state == "failed" else None
        failed_jobs = [{"job": j["job"], "result": j["result"]} for j in ci.failed_jobs(verdict["message"])] if verdict else []
        outdated = result.get("outdated_parents", {}).get(change["number"])
        rows.append({
            "number": change["number"],
            "patch_set": patch_set.get("number"),
            "subject": change["subject"],
            "url": change["url"],
            "wip": change.get("wip", False),
            "code_review": min(code_review) if code_review and min(code_review) < 0 else max(code_review, default=0),
            "ci": state,
            "ci_failed": failed_ci_labels(votes),
            "ci_failed_jobs": failed_jobs,
            "flaky": ci.flaky_counts(flakes, [j["job"] for j in failed_jobs], now),
            # Every recheck on the patch set, also after the verdict: one may already be running.
            "rechecks": my_rechecks(change, patch_set, float("inf")),
            "ci_stuck": is_ci_stuck(change, patch_set, now),
            "threads": None if threads_error else len(result.get("threads", {}).get(change["number"], [])),
            "conflict": change["number"] in result["conflicts"],
            "open_parent": result["parents"].get(change["number"]),
            "outdated_parent": outdated["parent"] if outdated else None,
            "ready": (is_ready_to_submit(patch_set) and not threads_error
                      and not result.get("threads", {}).get(change["number"])),
            "worktree": worktrees.get(change["id"]),
        })
    atomic_write(STATUS, {"updated": now, "last_attempt": now, "last_error": None, "threads_error": threads_error,
                          "changes": rows})


def event_base(change):
    return {
        "change": change["number"],
        "subject": change["subject"],
        "branch": change["branch"],
        "change_id": change["id"],
        "url": change["url"],
        "patch_set": change.get("currentPatchSet", {}).get("number"),
    }


def message_event(base, message, kind="message"):
    return {
        **base,
        "kind": kind,
        "author": message["reviewer"].get("name", ""),
        "author_username": message["reviewer"].get("username", ""),
        "message": message["message"][:1500],
    }


def awaiting_threads(comments_by_file):
    """Unresolved threads whose last word is not mine, from REST `/comments`; `reply_to` is the draft's in_reply_to."""
    by_id = {c["id"]: {**c, "file": file} for file, comments in comments_by_file.items() for c in comments}

    def root(comment):
        while comment.get("in_reply_to") in by_id:
            comment = by_id[comment["in_reply_to"]]
        return comment["id"]

    threads = {}
    for comment in sorted(by_id.values(), key=lambda c: c["updated"]):
        threads.setdefault(root(comment), []).append(comment)
    awaiting = [{"patch_set": thread[0].get("patch_set"), "file": thread[0]["file"], "line": thread[0].get("line"),
                 "author": thread[-1]["author"].get("name", ""), "reply_to": thread[-1]["id"],
                 "messages": [f'{m["author"].get("username", "")}: {m["message"][:800]}' for m in thread[-3:]]}
                for thread in threads.values()
                if thread[-1].get("unresolved") and thread[-1]["author"].get("username", "") not in {USER, *BOT_USERS}]
    return awaiting[-MAX_THREADS:]


def work_day(now):
    """Reminder bucket: flips at the start of each working day, so nothing re-fires at night or on weekends."""
    day = datetime.date.fromtimestamp(now - WORK_HOURS[0] * 3600)
    return (day - datetime.timedelta(days=max(0, day.weekday() - 4))).isoformat()


def is_quiet(now):
    local = time.localtime(now)
    return local.tm_wday >= 5 or not WORK_HOURS[0] <= local.tm_hour < WORK_HOURS[1]


def working_days_since(start, now):
    first, last = datetime.date.fromtimestamp(start), datetime.date.fromtimestamp(now)
    return sum(1 for n in range(1, (last - first).days + 1)
               if (first + datetime.timedelta(days=n)).weekday() < 5)


def unreviewed_days(change, patch_set, now):
    """Working days the current patch set has waited without a word or vote from a reviewer; None if not waiting."""
    if change.get("wip") or votes_of(patch_set).get("Code-Review"):
        return None
    uploaded = patch_set.get("createdOn", now)
    if any(m["timestamp"] >= uploaded and m["reviewer"].get("username", "") not in {USER, *BOT_USERS}
           for m in change.get("comments", [])):
        return None
    days = working_days_since(uploaded, now)
    return days if days >= UNREVIEWED_WORKING_DAYS else None


def ready_since(patch_set):
    return max((int(a.get("grantedOn", 0)) for a in patch_set.get("approvals", [])
                if a["type"] == "Code-Review" and int(a["value"]) == 2), default=None)


def events(result, now=None):
    now = now or time.time()
    day = work_day(now)
    conflicts = result["conflicts"]
    stale = result.get("stale_parents", {})
    outdated = result.get("outdated_parents", {})
    threads = result.get("threads", {})
    threads_error = result.get("threads_error")
    attention = set(result.get("attention", []))
    for change in result["changes"]:
        number = change["number"]
        patch_set = change.get("currentPatchSet", {})
        base = event_base(change)
        for message in change.get("comments", []):
            if not is_actionable(message):
                continue
            author = message["reviewer"].get("username", "")
            # A late verdict on a patch set already replaced by a push says nothing about the current one.
            if author == CI_USER and patch_set_of(message) < int(patch_set.get("number") or 0):
                continue
            event = message_event(base, message)
            if author == CI_USER:
                event["rechecks"] = my_rechecks(change, patch_set, message["timestamp"])
            else:
                event["threads_awaiting_me"] = None if threads_error else threads.get(number, [])
            yield f'{number}:{message["timestamp"]}:{author}', event
        if is_ci_stuck(change, patch_set, now):
            yield f'{number}:ci_stuck:{patch_set.get("number")}', {
                **base, "kind": "ci_stuck", "idle_since": ci_idle_since(change, patch_set)}
        if number in stale:
            # Takes over merge_conflict: the fix is a rebase --onto that drops the old parent, conflicts or not.
            yield f'{number}:parent_merged:{patch_set.get("number")}', {
                **base, "kind": "parent_merged", **stale[number], "files": conflicts.get(number, [])}
        elif number in outdated:
            # Rebasing onto the parent's new patch set comes first: a conflict with the branch may go with it.
            yield f'{number}:parent_updated:{patch_set.get("number")}:{outdated[number]["new_parent_sha"]}', {
                **base, "kind": "parent_updated", **outdated[number]}
        elif number in conflicts:
            yield f'{number}:conflict:{patch_set.get("number")}', {
                **base, "kind": "merge_conflict", "files": conflicts[number]}
        elif (is_ready_to_submit(patch_set) and number not in result["parents"] and not threads.get(number)
              and not threads_error):
            # One key per working day: a CL left unsubmitted comes back the next morning.
            yield f'{number}:ready:{patch_set.get("number")}:{day}', {
                **base, "kind": "ready_to_submit", "ready_since": ready_since(patch_set)}
        days = unreviewed_days(change, patch_set, now)
        if (days and number not in conflicts and number not in stale
                and ci_state(change, patch_set, votes_of(patch_set)) not in ("failed", "stale_base")):
            yield f'{number}:unreviewed:{patch_set.get("number")}:{day}', {
                **base, "kind": "waiting_for_review", "working_days": days,
                "reviewers": [r.get("name") or r.get("username") for r in change.get("allReviewers", [])
                              if r.get("username", "") not in {USER, *BOT_USERS}]}
    for change in result.get("reviews", []):
        yield from review_events(change, day, change["number"] in attention)


def patch_set_of(message):
    match = re.match(r"(?:Patch Set|Uploaded patch set) (\d+)", message.get("message", ""))
    return int(match.group(1)) if match else 0


def is_new_patch_set_notice(message):
    """Upload/rebase notices duplicate review_new_patch_set; anything else from the owner is a reply."""
    text = message.get("message", "")
    return text.startswith("Uploaded patch set") or " was rebased" in text.split("\n\n", 1)[0]


def review_events(change, day, needs_my_attention=False):
    """Changes I review: signal only, never fixed — they are someone else's code.

    Gerrit's attention set names me when another reviewer answers one of my threads, so their replies count too."""
    number = change["number"]
    base = {**event_base(change), "owner": change.get("owner", {}).get("name", "")}
    owner = change.get("owner", {}).get("username")
    current = change.get("currentPatchSet", {}).get("number", 0)
    comments = change.get("comments", [])
    mine = [m for m in comments if m["reviewer"].get("username") == USER]
    my_votes = [(int(ps["number"]), int(a["value"]))
                for ps in change.get("patchSets", []) for a in ps.get("approvals", [])
                if a["by"].get("username") == USER and a["type"] == "Code-Review"]
    humans = [r for r in change.get("allReviewers", []) if r.get("username", "") not in {owner, *BOT_USERS}]

    last_seen_ps = max([ps for ps, _ in my_votes] + [patch_set_of(m) for m in mine], default=0)
    if not change.get("wip") and len(humans) <= MAX_REVIEWERS:
        # Untouched requests come back each working day; once I took part, it is not a request anymore.
        key = f"{number}:review_requested" if last_seen_ps else f"{number}:review_requested:{day}"
        yield key, {**base, "kind": "review_requested", "reviewers": len(humans), "participated": bool(last_seen_ps),
                    "current_ref": change.get("currentPatchSet", {}).get("ref")}

    # A vote Gerrit copied onto the current patch set (trivial rebase) means nothing needs re-reviewing.
    if last_seen_ps and last_seen_ps < current and not any(ps == current for ps, _ in my_votes):
        last_vote = max(my_votes, default=None)
        refs = {int(ps["number"]): ps.get("ref") for ps in change.get("patchSets", [])}
        yield f"{number}:review_ps:{current}", {
            **base, "kind": "review_new_patch_set", "since_patch_set": last_seen_ps,
            "since_ref": refs.get(last_seen_ps), "current_ref": refs.get(int(current)),
            "my_last_vote": {"patch_set": last_vote[0], "value": last_vote[1]} if last_vote else None}

    if not mine:
        return
    my_last = max(m["timestamp"] for m in mine)
    for message in comments:
        author = message["reviewer"].get("username")
        replied = author == owner or (needs_my_attention and (author or "") not in {USER, *BOT_USERS})
        if message["timestamp"] > my_last and replied and not is_new_patch_set_notice(message):
            yield f'{number}:review_reply:{message["timestamp"]}', message_event(base, message, "review_reply")


def pending_events(result, now=None):
    """Start-up sweep: what needs me right now, instead of replaying every past message."""
    for change in result["changes"]:
        patch_set = change.get("currentPatchSet", {})
        votes = votes_of(patch_set)
        ci = ci_state(change, patch_set, votes)
        code_review = min(votes.get("Code-Review", [0]))
        threads = None if result.get("threads_error") else result.get("threads", {}).get(change["number"], [])
        if not (threads or code_review < 0 or ci in ("failed", "stale_base")):
            continue
        event = {**event_base(change), "kind": "pending", "wip": change.get("wip", False), "ci": ci,
                 "code_review": code_review, "threads_awaiting_me": threads}
        verdict = latest_ci_verdict(change, patch_set) if ci == "failed" else None
        if verdict:
            event["ci_verdict"] = verdict["message"][:1500]
            event["rechecks"] = my_rechecks(change, patch_set, verdict["timestamp"])
        yield event
    for _, event in events(result, now):
        if event["kind"] != "message" and not event.get("participated"):
            yield event


def snoozed_changes(result, now):
    patch_sets = {c["number"]: c.get("currentPatchSet", {}).get("number")
                  for c in result["changes"] + result.get("reviews", [])}
    return snooze.active(patch_sets, now)


def awake(pairs, snoozed):
    """Events of snoozed changes are left out, hence left unseen: they come back once the snooze ends."""
    return {key: event for key, event in pairs if event.get("change") not in snoozed}


def remember(seen, keys, result, current, now):
    """Marks `keys` seen. Keys of changes gone from both queries (merged, abandoned) are kept for a while, so a
    change restored soon after does not replay its history, then dropped so the seen files stop growing."""
    alive = {str(c["number"]) for c in result["changes"] + result.get("reviews", [])}
    live = lambda key: key in current or key.split(":", 1)[0] in alive
    return {key: now if live(key) else stamp
            for key, stamp in {**seen, **dict.fromkeys(keys, now)}.items()
            if live(key) or now - stamp < SEEN_RETENTION_S}


def claude_pid():
    """The Claude session running this watcher, found by walking up the process tree."""
    pid = os.getppid()
    while pid > 1:
        row = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True).stdout.split(None, 1)
        if len(row) < 2:
            return None
        if pathlib.Path(row[1].strip()).name == "claude":
            return pid
        pid = int(row[0])
    return None


def write_session_lock():
    """Read by macos/open-babysit.sh: a notification click focuses this session instead of opening a second one."""
    pid = claude_pid()
    if pid:
        atomic_write(SESSION, {"claude_pid": pid, "orca_terminal": os.environ.get("ORCA_TERMINAL_HANDLE", "")})


def load_seen(state=None):
    """{event key: last time its change was alive}."""
    state = state or STATE
    return json.loads(state.read_text()) if state.exists() else {}


def save_seen(seen, state=None):
    atomic_write(state or STATE, dict(sorted(seen.items())))


def swiftbar(action, **params):
    """Best effort: a hung SwiftBar must not kill the daemon before it saves what it just notified."""
    try:
        subprocess.run(["open", "-g", f"swiftbar://{action}?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"],
                       capture_output=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        pass


def notification(event):
    n = event.get("change")
    kind = event["kind"]
    if kind == "merge_conflict":
        return t("conflict", n=n), ", ".join(pathlib.Path(f).name for f in event["files"])
    if kind == "ready_to_submit":
        since = event.get("ready_since")
        days = int((time.time() - since) // 86400) if since else 0
        return t("ready", n=n) + (t("ready_for", days=days) if days else ""), event["subject"]
    if kind == "ci_stuck":
        hours = int((time.time() - event["idle_since"]) // 3600)
        return t("ci_stuck", n=n), t("ci_stuck_body", hours=hours, subject=event["subject"])
    if kind == "parent_merged":
        return t("rebase", n=n), t("rebase_body", parent=event["parent"], subject=event["subject"])
    if kind == "parent_updated":
        return t("parent_updated", n=n, parent=event["parent"]), event["subject"]
    if kind == "waiting_for_review":
        return t("unreviewed", n=n, days=event["working_days"]), event["subject"]
    if kind == "message":
        return f"{n} · {event['author']}", event["message"].split("\n\n", 1)[-1][:200]
    if kind == "review_requested":
        return t("review_requested", owner=event["owner"]), f"{n} · {event['subject']}"
    if kind == "review_new_patch_set":
        return t("new_patch_set", n=n, ps=event["patch_set"]), f"{event['owner']} · {event['subject']}"
    if kind == "review_reply":
        return t("replied", n=n, author=event["author"]), event["message"].split("\n\n", 1)[-1][:200]
    return None


def notifications(fresh):
    """(title, body, href): a click opens the change in Gerrit, or my dashboard for a summary."""
    contents = [(*content, event.get("url") or DASHBOARD) for event in fresh if (content := notification(event))]
    if len(contents) <= DIGEST_OVER:
        return contents
    return [(t("digest", count=len(contents)), " · ".join(title for title, _, _ in contents)[:200], DASHBOARD)]


def daemon(interval):
    """Status snapshot + notifications with no Claude session; clicking a notification opens one."""
    first_run = not DAEMON_STATE.exists()
    last_error = None

    def failed(error, expected):
        """Logged once per distinct error: a VPN left off overnight would otherwise add a line a minute."""
        nonlocal last_error
        detail = error_detail(error) if expected else f"{type(error).__name__}: {error}"
        if detail != last_error:
            log = f"poll failed: {detail}" if expected else traceback.format_exc()
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {log}", file=sys.stderr, flush=True)
            last_error = detail
        write_status(error=detail)
        swiftbar("refreshplugin", name=SWIFTBAR_PLUGIN)
        return detail

    while True:
        try:
            result = poll()
        # A bug on one odd change must not turn into a silent launchd crash loop.
        except Exception as error:  # noqa: BLE001
            expected = isinstance(error, (subprocess.SubprocessError, OSError, ValueError))
            write_daemon_poll(error=failed(error, expected))
            time.sleep(interval)
            continue
        try:
            daemon_round(result, first_run)
            first_run = False
            if last_error is not None:
                print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} recovered", file=sys.stderr, flush=True)
                last_error = None
        except Exception as error:  # noqa: BLE001
            failed(error, expected=False)
        time.sleep(interval)


def daemon_round(result, first_run):
    # Published first: a bug further down must not starve the session of a poll that is fine.
    write_daemon_poll(result)
    write_status(result)
    swiftbar("refreshplugin", name=SWIFTBAR_PLUGIN)

    now = time.time()
    current = awake(events(result, now), snoozed_changes(result, now))
    seen = load_seen(DAEMON_STATE)
    quiet = is_quiet(now)
    if not first_run and not quiet:
        for title, body, href in notifications(current[key] for key in sorted(current.keys() - seen.keys())):
            swiftbar("notify", plugin=SWIFTBAR_PLUGIN, title=title, body=body, href=href)
    # Held back at night and on weekends: still unseen, they notify at the start of the next working day.
    marked = () if quiet and not first_run else current.keys()
    save_seen(remember(seen, marked, result, current, now), DAEMON_STATE)
    record_flaky(result, now)


def main():
    if not HOST:
        sys.exit("gerrit-babysit: set gerrit_host in config.json (see config.example.json)")
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--pending", action="store_true",
                        help="print what needs me now, mark everything current as seen and exit")
    parser.add_argument("--daemon", action="store_true", help="run forever: status snapshot + macOS notifications")
    args = parser.parse_args()
    if args.daemon:
        return daemon(args.interval)

    write_session_lock()
    failures = 0
    settling = {}
    while True:
        try:
            result = daemon_poll()
            if result is None:
                result = poll()
                write_status(result)
                record_flaky(result)
            cleanup = dict(cleanup_candidates(frozenset(c["id"] for c in result["changes"])))
            now = time.time()
            snoozed = snoozed_changes(result, now)
            current = awake(events(result, now), snoozed) | cleanup
            failures = 0
        except (subprocess.SubprocessError, OSError, ValueError, DaemonError) as error:
            if not isinstance(error, DaemonError):
                write_status(error=error_detail(error))
            if args.pending:
                print(json.dumps({"status": "error", "detail": error_detail(error)}))
                return 1
            # Never gives up: a VPN off over lunch or overnight must not end the babysitting.
            failures += 1
            time.sleep(min(args.interval * failures, MAX_BACKOFF_S))
            continue

        seen = load_seen()
        if args.pending:
            # Same poll for the report and the seen set: nothing can slip in between.
            report = enrich([e for e in pending_events(result, now) if e.get("change") not in snoozed]
                            + list(cleanup.values()))
            save_seen(remember(seen, current.keys(), result, current, now))
            print(json.dumps({"status": "pending", "events": report, "threads_error": result.get("threads_error"),
                              "snoozed": snoozed}, ensure_ascii=False, indent=1))
            return 0

        fresh = {key: current[key] for key in current.keys() - seen.keys()}
        if fresh and not settling:
            # A reply, its vote and zuul's verdict often land a minute apart: wait once to wake for all of them.
            settling = fresh
            time.sleep(SETTLE_S)
            continue
        settling = {key: current.get(key, event) for key, event in settling.items()} | fresh
        if settling:
            # Enriched before being marked seen: a crash in the extras must not swallow the events.
            report = enrich(list(settling.values()))
            save_seen(remember(seen, settling.keys(), result, current, now))
            print(json.dumps({"status": "events", "events": report,
                              "threads_error": result.get("threads_error")}, ensure_ascii=False, indent=1))
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
