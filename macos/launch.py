#!/usr/bin/python3
"""Stable entry point for launchd, SwiftBar and the applet, copied to ~/.config/gerrit-babysit by install.sh.

Each plugin version lands in its own folder: this runs `<file> [args…]` from the one Claude Code installed last."""
import json
import os
import pathlib
import sys

PLUGIN_CACHE = pathlib.Path.home() / ".claude" / "plugins" / "cache"
INSTALLED = pathlib.Path.home() / ".claude" / "plugins" / "installed_plugins.json"
# Set by install.sh: the skill it ran from. A cloned skill never moves; a plugin install is looked up again.
FALLBACK = ""


def skill_dir(fallback=None):
    fallback = pathlib.Path(fallback or FALLBACK)
    if PLUGIN_CACHE not in fallback.parents:
        return fallback
    # cache/<marketplace>/<plugin>/<version>
    marketplace, plugin = fallback.relative_to(PLUGIN_CACHE).parts[:2]
    try:
        entries = json.loads(INSTALLED.read_text())["plugins"][f"{plugin}@{marketplace}"]
    except (OSError, ValueError, KeyError):
        return fallback
    # The user-scope install first, then the latest update.
    entries = sorted(entries, key=lambda e: (e.get("scope") == "user", e.get("lastUpdated", "")), reverse=True)
    return next((path for e in entries if e.get("installPath") and (path := pathlib.Path(e["installPath"])).is_dir()),
                fallback)


def main():
    file, *args = sys.argv[1:]
    target = str(skill_dir() / file)
    # Read by the daemon: it exits once a newer install supersedes it, and launchd restarts it through here.
    os.environ["GERRIT_BABYSIT_LAUNCHER"] = str(pathlib.Path(__file__).resolve())
    command = [sys.executable, target] if target.endswith(".py") else [target]
    os.execv(command[0], [*command, *args])


if __name__ == "__main__":
    main()
