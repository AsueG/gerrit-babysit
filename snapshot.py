"""One reading of a snapshot row for the SwiftBar menu and the status line, so their counts never disagree.

Kept free of gerrit/events imports: the status line loads it on every refresh."""
from config import STALE_AFTER_S

PROBLEMS = ("conflict", "stale_base", "ci_failed", "rejected")
# Even a draft needs these fixed before anyone can review it.
NEEDS_REBASE = ("conflict", "stale_base")


def is_stopped(status, now):
    """No poll attempt for a while: the watcher is not running. A snapshot written only by failed polls has no
    `updated`."""
    return now - (status.get("last_attempt") or status.get("updated") or 0) > STALE_AFTER_S


def problem(row):
    """The worst of PROBLEMS, WIP or not."""
    if row["conflict"]:
        return "conflict"
    if row["ci"] == "stale_base":
        return "stale_base"
    if row["ci"] == "failed":
        return "ci_failed"
    if row["code_review"] < 0:
        return "rejected"
    return None


def state(row):
    """The worst thing about a change first; a change counts as ready only when nothing is left to do but submit."""
    if row["wip"]:
        return "wip"
    found = problem(row)
    if found:
        return found
    if row.get("outdated_parent"):
        return "parent_updated"
    if row.get("ci_stuck"):
        return "ci_stuck"
    if row.get("submit_blocked"):
        return "submit_blocked"
    if row.get("gate") == "voted":
        return "gating"
    if row["ready"]:
        return "ready_parent" if row.get("open_parent") else "ready"
    return "ci_running" if row["ci"] == "running" else "ci_passed"
