#!/usr/bin/env python3
"""Claude Code status line segment, e.g. `⎇ 2 · 1⚠`, read from the watcher snapshot (never hits Gerrit)."""
import json
import time

from config import CACHE, t

STATUS = CACHE / "status.json"
STALE_AFTER_S = 360


def is_problem(change):
    return change["conflict"] or change["ci"] in ("failed", "stale_base") or change["code_review"] < 0


def main():
    if not STATUS.exists():
        return
    snapshot = json.loads(STATUS.read_text())
    active = [c for c in snapshot.get("changes", []) if not c["wip"]]
    last_attempt = snapshot.get("last_attempt", snapshot.get("updated", 0))
    segment = f"⎇ {len(active)}"
    if snapshot.get("last_error") or time.time() - last_attempt > STALE_AFTER_S:
        segment += " · ?"
    else:
        problems = sum(1 for c in active if is_problem(c))
        ready = sum(1 for c in active if c["ready"] and not c.get("open_parent") and not is_problem(c))
        if problems:
            segment += f" · {problems}⚠"
        if ready:
            segment += f" · {ready}✓"
        if snapshot.get("threads_error"):
            segment += f" · {t('threads_unknown')}"
    print(segment, end="")


if __name__ == "__main__":
    main()
