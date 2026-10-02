#!/usr/bin/env python3
"""Claude Code Stop hook: in the /gerrit-babysit session, refuse to end a turn while no session watcher runs."""
import importlib.util
import json
import os
import pathlib
import sys

from config import INBOX, SESSION, read_json
from procs import descends_from, processes

WATCH_PY = pathlib.Path(__file__).resolve().parent / "watch.py"
PLUGIN_CACHE = pathlib.Path.home() / ".claude" / "plugins" / "cache"
_spec = importlib.util.spec_from_file_location("launch", WATCH_PY.parent / "macos" / "launch.py")
assert _spec and _spec.loader
launch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launch)


INBOX_SUMMARY_MAX = 10


def unread_report():
    """A line naming what the watcher's last report holds, when no `--ack` confirmed it was handled."""
    inbox = read_json(INBOX)
    if not inbox:
        return ""
    events = inbox.get("report", {}).get("events", [])
    items = [f"{e['change']} {e['kind']}" if e.get("change") else e["kind"] for e in events[:INBOX_SUMMARY_MAX]]
    more = f" and {len(events) - INBOX_SUMMARY_MAX} more" if len(events) > INBOX_SUMMARY_MAX else ""
    return (f" Its last report (id {inbox['id']}: {', '.join(items) or inbox['report'].get('status')}{more}) is not "
            f"acknowledged yet: once handled, relaunch with `--ack {inbox['id']}`; without it the relaunch prints "
            "that report again at once.")


def reason():
    """Points at the latest install: a session keeps the hook of the plugin version it started on."""
    watch_py = launch.skill_dir(WATCH_PY.parent) / "watch.py"
    return (f"The gerrit-babysit watcher is no longer running in this session. Relaunch `python3 {watch_py}` "
            f"with run_in_background before ending the turn.{unread_report()} If watching should stop (stop "
            f"requested), delete {SESSION} instead.")


def is_watch_py(path):
    """This copy's watch.py or, for a plugin install, the one of any other version folder of the same plugin: after
    an update the session's watcher runs from the new folder while this hook stays on the old one."""
    if path == WATCH_PY:
        return True
    return PLUGIN_CACHE in WATCH_PY.parents and path.name == WATCH_PY.name and path.parent.parent == WATCH_PY.parent.parent


def is_session_watcher(command):
    """Matched on the resolved path, not the folder name: the skill may be cloned or symlinked under any name.

    `ps` joins argv with spaces, so a path containing spaces is rebuilt from consecutive words."""
    args = command.split()
    if "--daemon" in args or "--pending" in args:
        return False
    return any(is_watch_py(pathlib.Path(" ".join(args[start:end + 1])).expanduser().resolve())
               for end, arg in enumerate(args) if arg.endswith("watch.py")
               for start in range(end + 1))


def main():
    hook = json.load(sys.stdin)
    # Blocked once already: let the turn end rather than loop if the relaunch keeps failing.
    if hook.get("stop_hook_active") or not SESSION.exists():
        return 0
    babysit_pid = read_json(SESSION).get("claude_pid")
    table = processes()
    if not babysit_pid or not descends_from(os.getppid(), babysit_pid, table):
        return 0
    watching = any(is_session_watcher(command) and descends_from(pid, babysit_pid, table)
                   for pid, (_, command) in table.items())
    if not watching:
        print(json.dumps({"decision": "block", "reason": reason()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
