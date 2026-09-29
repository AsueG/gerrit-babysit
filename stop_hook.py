#!/usr/bin/env python3
"""Claude Code Stop hook: in the /gerrit-babysit session, refuse to end a turn while no session watcher runs."""
import json
import os
import pathlib
import subprocess
import sys

SESSION = pathlib.Path.home() / ".cache" / "gerrit-babysit" / "session.json"
WATCH_PY = pathlib.Path(__file__).resolve().parent / "watch.py"
REASON = (f"The gerrit-babysit watcher is no longer running in this session. Relaunch `python3 {WATCH_PY}` "
          "with run_in_background before ending the turn. If watching should stop (stop requested, "
          "status: error), delete ~/.cache/gerrit-babysit/session.json instead.")


def processes():
    """{pid: (ppid, command)}"""
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True).stdout
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            table[int(parts[0])] = (int(parts[1]), parts[2])
    return table


def descends_from(pid, ancestor, table):
    while pid > 1 and pid in table:
        if pid == ancestor:
            return True
        pid = table[pid][0]
    return False


def is_session_watcher(command):
    """Matched on the resolved path, not the folder name: the skill may be cloned or symlinked under any name.

    `ps` joins argv with spaces, so a path containing spaces is rebuilt from consecutive words."""
    args = command.split()
    if "--daemon" in args or "--pending" in args:
        return False
    return any(pathlib.Path(" ".join(args[start:end + 1])).expanduser().resolve() == WATCH_PY
               for end, arg in enumerate(args) if arg.endswith("watch.py")
               for start in range(end + 1))


def main():
    hook = json.load(sys.stdin)
    # Blocked once already: let the turn end rather than loop if the relaunch keeps failing.
    if hook.get("stop_hook_active") or not SESSION.exists():
        return 0
    babysit_pid = json.loads(SESSION.read_text()).get("claude_pid")
    table = processes()
    if not babysit_pid or not descends_from(os.getppid(), babysit_pid, table):
        return 0
    watching = any(is_session_watcher(command) and descends_from(pid, babysit_pid, table)
                   for pid, (_, command) in table.items())
    if not watching:
        print(json.dumps({"decision": "block", "reason": REASON}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
