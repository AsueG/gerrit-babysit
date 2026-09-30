"""One reading of a snapshot row for the SwiftBar menu and the status line, so their counts never disagree.

Kept free of gerrit/events imports: the status line loads it on every refresh."""
PROBLEMS = ("conflict", "stale_base", "ci_failed", "rejected")


def state(row):
    """The worst thing about a change first; a change counts as ready only when nothing is left to do but submit."""
    if row["wip"]:
        return "wip"
    if row["conflict"]:
        return "conflict"
    if row["ci"] == "stale_base":
        return "stale_base"
    if row["ci"] == "failed":
        return "ci_failed"
    if row["code_review"] < 0:
        return "rejected"
    if row.get("outdated_parent"):
        return "parent_updated"
    if row.get("ci_stuck"):
        return "ci_stuck"
    if row["ready"]:
        return "ready_parent" if row.get("open_parent") else "ready"
    return "ci_running" if row["ci"] == "running" else "ci_passed"
