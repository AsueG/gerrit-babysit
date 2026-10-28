"""Writes the status snapshot snapshot.py reads, and keeps what it needs between polls: the diagnosis of each red
change, the rebases prepared for the menu, the cleanup candidates."""

import sys
import time

import ci
import events
import extras
import known_failures
import repo
from config import CACHE, STATUS, atomic_write, read_json

DIAGNOSES = CACHE / "diagnoses.json"
EXCERPT_CHARS = 300


def job_summary(entry):
    """What the menu and the Investigate prompt show of one failed job's diagnosis."""
    summary = {key: entry[key] for key in ("job", "category", "log_url") if entry.get(key)}
    excerpt = (entry.get("failure") or entry.get("log_tail") or "").strip()
    if excerpt:
        summary["excerpt"] = excerpt[:EXCERPT_CHARS]
    if entry.get("resembles"):
        summary["resembles"] = [{key: r.get(key) for key in ("change", "cause", "fix")} for r in entry["resembles"]]
    return summary


def red_verdicts(changes):
    """{"<change>:<patch set>:<verdict time>": (change, verdict)} of my changes whose current patch set failed: a
    recheck's new verdict is diagnosed again."""
    red = {}
    for change in changes:
        patch_set, _, _, verdict = events.ci_reading(change)
        if verdict:
            red[f"{change['number']}:{patch_set.get('number')}:{verdict['timestamp']}"] = (change, verdict)
    return red


def diagnose_red(result):
    """{change number: [job summary]} of my red changes, each verdict diagnosed once and kept while the change is
    open: the snapshot then tells why a change is red, not only that it is."""
    if not ci.ZUUL_API:
        return {}
    stored = read_json(DIAGNOSES)
    red = red_verdicts(result["changes"])
    missing = [
        (key, {**events.event_base(change), "kind": "ci_red", "ci_verdict": verdict["message"]})
        for key, (change, verdict) in red.items()
        if key not in stored
    ]
    extras.enrich([event for _, event in missing], result.get("base_health"))
    found = {
        key: {"diagnosed_at": time.time(), "jobs": [job_summary(d) for d in event.get("ci_diagnosis", [])]} for key, event in missing
    }
    open_changes = {str(c["number"]) for c in result["changes"]}
    kept = {key: entry for key, entry in {**stored, **found}.items() if key.split(":", 1)[0] in open_changes}
    if kept != stored:
        atomic_write(DIAGNOSES, dict(sorted(kept.items())))
    return {int(key.split(":", 1)[0]): kept[key]["jobs"] for key in red}


def rebase_candidates(result):
    """{change number: (change, onto SHA, parent number or None)} for my changes a plain rebase would unblock: a
    parent with a new patch set (onto it), or a merged parent, Merge Failed or a conflict (onto the branch tip). A
    change stacked on an open parent is left to the session: a branch-tip rebase would drop that parent."""
    tips = repo.branch_tips({c["branch"] for c in result["changes"]})
    outdated = result.get("outdated_parents", {})
    found = {}
    for change in result["changes"]:
        number = change["number"]
        if number in outdated:
            found[number] = (change, outdated[number]["new_parent_sha"], outdated[number]["parent"])
        elif number not in result["parents"] and (
            number in result.get("stale_parents", {}) or number in result["conflicts"] or events.ci_reading(change)[2] == "stale_base"
        ):
            if tips.get(change["branch"]):
                found[number] = (change, tips[change["branch"]], None)
    return found


def prepared_rebases(result, listing=None):
    """{change number: {sha, patch_set, parent}}: each candidate replayed onto its new base without a worktree, for
    the menu to push after a confirmation. A change whose worktree holds unpushed or half-done work is skipped:
    pushing past it would leave that work on the old patch set."""
    candidates = rebase_candidates(result)
    if not candidates:
        # Still called: it deletes the rebases prepared earlier.
        return repo.prepare_replays({})
    busy = {w["worktree"] for w in repo.unfinished_work([c for c, _, _ in candidates.values()], listing).values()}
    worktrees = repo.worktrees_by_change_id(listing)
    wanted = {
        n: (c["currentPatchSet"]["revision"], onto)
        for n, (c, onto, _) in candidates.items()
        if c.get("currentPatchSet", {}).get("revision") and worktrees.get(c["id"]) not in busy
    }
    return {
        n: {"sha": sha, "patch_set": candidates[n][0]["currentPatchSet"].get("number"), "parent": candidates[n][2]}
        for n, sha in repo.prepare_replays(wanted).items()
    }


def change_ref(number, patch_set):
    return f"refs/changes/{number % 100:02d}/{number}/{patch_set}"


def fixed_failures(result):
    """(key, event) per `unknown` failure a newer patch set of the change got past: the diff between the two is how
    it was fixed, worth remembering for its next look-alike. Read from the diagnoses kept in diagnoses.json."""
    known = known_failures.load()
    changes = {c["number"]: c for c in result["changes"]}
    for key, entry in sorted(read_json(DIAGNOSES).items()):
        number, failed, _ = (int(part) if part.isdigit() else part for part in key.split(":", 2))
        change = changes.get(number)
        if not change:
            continue
        patch_set, _, state, _ = events.ci_reading(change)
        current = int(patch_set.get("number") or 0)
        if state != "passed" or current <= failed:
            continue
        for job in entry.get("jobs", []):
            if job.get("category") == "unknown" and f"{number}:{failed}:{job['job']}" not in known:
                yield (
                    f"{number}:fixed:{failed}:{job['job']}",
                    {
                        **events.event_base(change),
                        "kind": "failure_fixed",
                        "job": job["job"],
                        "failed_patch_set": failed,
                        "failed_ref": change_ref(number, failed),
                        "fixed_ref": patch_set.get("ref"),
                        "log_url": job.get("log_url"),
                        "excerpt": job.get("excerpt"),
                    },
                )


def bonus(compute, *args, what):
    """{} when `compute` fails: the why of a red change, a prepared rebase or a cleanup must not fail the whole
    poll."""
    try:
        return compute(*args)
    except Exception as failure:  # noqa: BLE001
        print(f"gerrit-babysit: {what} skipped: {type(failure).__name__}: {failure}", file=sys.stderr)
        return {}


def write_status(result=None, error=None, listing=None):
    """Snapshot read by the SwiftBar plugin and the Claude Code status line, so they never hit Gerrit themselves.

    On error the last good rows are kept and `updated` stays at the last success. `listing` is `git worktree list`
    when the caller already ran it."""
    now = time.time()
    if error is not None:
        previous = read_json(STATUS)
        atomic_write(STATUS, {**previous, "last_attempt": now, "last_error": error})
        return
    diagnoses = bonus(diagnose_red, result, what="CI diagnosis")
    rebases = bonus(prepared_rebases, result, listing, what="rebase preparation")
    rows = events.status_rows(result, extras.load_flaky(), repo.worktrees_by_change_id(listing), now, diagnoses)
    for row in rows:
        row["rebase_ready"] = rebases.get(row["number"])
    cleanup = bonus(repo.cleanup_candidates, frozenset(c["id"] for c in result["changes"]), listing, what="cleanup check")
    atomic_write(
        STATUS,
        {
            "updated": now,
            "last_attempt": now,
            "last_error": None,
            "threads_error": result.get("threads_error"),
            "changes": rows,
            "reviews": events.review_rows(result),
            "base_health": result.get("base_health", {}),
            "cleanup": [{"key": key, **event} for key, event in cleanup],
        },
    )


def snapshot_cleanup():
    """{key: event} of the cleanup check the last write_status() made. Entries without a key come from a daemon older
    than this session, still running until its restart."""
    return {
        entry["key"]: {k: v for k, v in entry.items() if k != "key"}
        for entry in read_json(STATUS).get("cleanup", [])
        if "key" in entry
    }
