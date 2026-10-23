"""One reading of a snapshot row for the SwiftBar menu and the status line, so their counts never disagree.

Kept free of gerrit/events imports: the status line loads it on every refresh."""

from config import CI_STUCK_S, CONFIG, STALE_AFTER_S
from i18n import t

GATE_LABEL = CONFIG["gate_label"]
HASHTAGS = {"auto_submit": CONFIG["auto_submit_hashtag"], "claude_review": CONFIG["claude_review_hashtag"]}

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


# state → (sf symbol, color); red is exactly PROBLEMS.
STYLES = {
    "wip": ("pencil", "gray"),
    "conflict": ("exclamationmark.triangle.fill", "red"),
    "stale_base": ("arrow.triangle.branch", "red"),
    "ci_failed": ("xmark.octagon.fill", "red"),
    "rejected": ("hand.thumbsdown.fill", "red"),
    "parent_updated": ("square.stack.3d.up", "orange"),
    "ci_stuck": ("exclamationmark.arrow.triangle.2.circlepath", "orange"),
    "ready_parent": ("link", "orange"),
    "submit_blocked": ("lock.fill", "orange"),
    "ready": ("checkmark.seal.fill", "green"),
    "gating": ("arrow.triangle.merge", None),
    "ci_running": ("hourglass", "orange"),
    "ci_passed": ("clock", None),
}


def cr_label(change):
    return f"+{change['code_review']}" if change["code_review"] > 0 else "0"


# state → row → label; the others read `bar_<state>` as is.
def ci_failed_label(change):
    label = t("bar_ci_failed", labels=", ".join(change["ci_failed"]))
    categories = sorted({j["category"] for j in change.get("ci_diagnosis") or [] if j.get("category")})
    return f"{label} · {', '.join(categories)}" if categories else label


LABELS = {
    "ci_failed": ci_failed_label,
    "rejected": lambda c: f"CR {c['code_review']}",
    "parent_updated": lambda c: t("bar_parent_updated", parent=c["outdated_parent"]),
    "ci_stuck": lambda c: t("bar_ci_stuck", hours=CI_STUCK_S // 3600),
    "ready_parent": lambda c: t("bar_ready_parent", parent=c["open_parent"]),
    "submit_blocked": lambda c: t("bar_submit_blocked", requirements=", ".join(c["submit_blocked"])),
    "ci_running": lambda c: t("bar_ci_running", cr=cr_label(c)),
    "ci_passed": lambda c: t("bar_ci_passed", cr=cr_label(c)),
    "gating": lambda c: t("bar_gating", label=GATE_LABEL),
}


def submit_wait(state, change):
    """What the submit waits for, when the state's label tells the votes instead."""
    if state == "ci_running":
        return t("bar_submit_wait_ci")
    if state != "ci_passed":
        return None
    if change["code_review"] < 2:
        return t("bar_submit_wait_review")
    if change.get("threads") is None:
        return t("threads_unknown")
    if change["threads"]:
        return t("bar_submit_wait_threads", count=change["threads"])
    return t("bar_submit_wait_ready")


def label_of(state, change):
    if state == "wip":
        found = problem(change)
        return f"WIP · {label_of(found, change)}" if found else "WIP"
    render = LABELS.get(state)
    return render(change) if render else t(f"bar_{state}")


def display(change):
    """(label, sf symbol, color) — worst problem first."""
    found = state(change)
    return (label_of(found, change), *STYLES[found])


def rebase_onto(change):
    parent = (change.get("rebase_ready") or {}).get("parent")
    return t("bar_onto_parent", parent=parent) if parent else change.get("branch", "?")


def cleanup_line(candidate):
    line = f"{candidate['branch']} — {candidate['change']} {candidate['status']} · {candidate['subject']}"
    return line + (f"\n    {candidate['worktree']}" if candidate.get("worktree") else "")
