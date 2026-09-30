#!/usr/bin/env python3
"""Claude Code status line segment, e.g. `⎇ 2 · 1⚠`, read from the watcher snapshot (never hits Gerrit)."""
import time

import snapshot
import snooze
from config import STATUS, read_json
from i18n import t


def main():
    if not STATUS.exists():
        return
    status = read_json(STATUS)
    changes = status.get("changes", [])
    snoozed = snooze.snapshot_active(status)
    states = [snapshot.state(c) for c in changes if not c["wip"] and c["number"] not in snoozed]
    segment = f"⎇ {len(states)}"
    if status.get("last_error") or snapshot.is_stopped(status, time.time()):
        segment += " · ?"
    else:
        problems = sum(1 for s in states if s in snapshot.PROBLEMS)
        ready = states.count("ready")
        if problems:
            segment += f" · {problems}⚠"
        if ready:
            segment += f" · {ready}✓"
        if status.get("threads_error"):
            segment += f" · {t('threads_unknown')}"
    print(segment, end="")


if __name__ == "__main__":
    main()
