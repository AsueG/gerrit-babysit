#!/usr/bin/env python3
"""Health check of the setup and the local state, as JSON. `--prune` drops what closed changes left behind."""
import argparse
import json
import pathlib
import subprocess
import sys
import time

import ci
import gerrit
import network
import pre_push
import repo
import snooze
import watch
from config import (CACHE, CONFIG, LAUNCHER, REPO, SKILL_DIR, SWIFTBAR_PLUGIN, VERSION, config_path,
                    launched_skill_dir, read_json, version_of)

# An older temp file is a write that crashed halfway, not one in flight.
TEMP_GRACE_S = 3600
OPEN_QUERY = "(owner:self OR reviewer:self) status:open"
# Fetched per change and never reused once it closes; the branch tips next to them are reused by every poll.
CHANGE_REFS = [f"{repo.FETCH_NAMESPACE}/{kind}/" for kind in ("changes", "review")]


def check_ssh():
    try:
        return {"ok": True, "detail": gerrit.ssh("gerrit", "version", timeout=30, check=True).stdout.strip()}
    except (subprocess.SubprocessError, OSError) as error:
        return {"ok": False, "detail": gerrit.error_detail(error)}


def check_http():
    source = "gerrit_mcp_config" if CONFIG["gerrit_mcp_config"] else "netrc"
    try:
        username = gerrit.rest_get("/accounts/self").get("username")
    except (OSError, ValueError, KeyError) as error:
        return {"ok": False, "source": source, "detail": gerrit.error_detail(error)}
    # Another account's password works, but reads Gerrit as someone else than the SSH queries.
    return {"ok": username == gerrit.USER, "source": source, "username": username, "ssh_user": gerrit.USER}


def check_zuul():
    if not ci.ZUUL_API:
        return None
    try:
        ci.http_get(f"{ci.ZUUL_API}/builds?limit=1")
        return {"ok": True}
    except (OSError, ValueError) as error:
        return {"ok": False, "detail": gerrit.error_detail(error)}


def daemon_state(now):
    heartbeat = read_json(watch.DAEMON_POLL)
    if not heartbeat:
        return {"running": False}
    age = int(now - heartbeat.get("attempt", 0))
    return {"running": age <= watch.STALE_AFTER_S, "last_poll_age_s": age, "error": heartbeat.get("error"),
            "version": heartbeat.get("version")}


def swiftbar_install(installed):
    """The install the SwiftBar menu runs: the launcher's for the stub install.sh writes, the target of an older
    symlink; None without SwiftBar or the plugin."""
    try:
        folder = subprocess.run(["defaults", "read", "com.ameba.SwiftBar", "PluginDirectory"], capture_output=True,
                                text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    stub = pathlib.Path(folder).expanduser() / f"{SWIFTBAR_PLUGIN}.30s.py" if folder else None
    if not stub or not (stub.is_file() or stub.is_symlink()):
        return None
    if stub.is_symlink():
        return stub.resolve().parents[1]
    return installed if str(LAUNCHER) in stub.read_text(errors="replace") else None


def versions(daemon):
    """Each piece's version; `match` is false when the daemon or the menu runs another install than Claude Code."""
    installed = launched_skill_dir(LAUNCHER) if LAUNCHER.is_file() else SKILL_DIR
    menu = swiftbar_install(installed)
    found = {"doctor": VERSION, "plugin": version_of(installed),
             # A running daemon that does not say predates the versioned heartbeat.
             "daemon": (daemon.get("version") or "?") if daemon["running"] else None,
             "swiftbar": version_of(menu) if menu else None}
    found["match"] = len({v for v in found.values() if v is not None}) <= 1
    return found


def state_files(now):
    files = []
    for path in sorted(CACHE.glob("*.json")) + sorted(CACHE.glob("*.log")):
        stat = path.stat()
        entry = {"file": path.name, "bytes": stat.st_size, "age_s": int(now - stat.st_mtime)}
        if path.suffix == ".json":
            try:
                content = json.loads(path.read_text())
                entry["entries"] = len(content) if isinstance(content, (dict, list)) else None
            except ValueError:
                entry["corrupt"] = True
        files.append(entry)
    return files


def open_patch_sets():
    rows = gerrit.query(OPEN_QUERY, "--current-patch-set")
    return {row["number"]: row.get("currentPatchSet", {}).get("number") for row in rows}


def prunable(now, open_changes):
    """What nothing will read again. Without the open changes (Gerrit unreachable), only the crashed temp files."""
    found = {"temp_files": sorted(str(p) for p in CACHE.glob(".*.json.*") if now - p.stat().st_mtime > TEMP_GRACE_S)}
    if open_changes is None:
        return found
    found["prereviews"] = sorted(str(p) for p in (CACHE / "prereview").glob("*.md")
                                 if not is_current(p.stem.split("-"), open_changes))
    refs = repo.git("for-each-ref", "--format=%(refname)", *CHANGE_REFS).split()
    # Only numbered refs: a Gerrit branch named review/x has its tip under the same prefix.
    found["refs"] = [ref for ref in refs
                     if ref.split("/")[3].isdigit() and int(ref.split("/")[3]) not in open_changes]
    found["snoozes"] = sorted(n for n in snooze.load() if n not in open_changes)
    return found


def is_current(parts, open_changes):
    """A prereview of the change's current patch set; a name it does not recognize is kept."""
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return True
    change, patch_set = map(int, parts)
    return change in open_changes and open_changes[change] in {patch_set, str(patch_set)}


def prune(found):
    for path in found["temp_files"] + found.get("prereviews", []):
        pathlib.Path(path).unlink(missing_ok=True)
    if found.get("refs"):
        repo.git_run("update-ref", "--stdin", input="".join(f"delete {ref}\n" for ref in found["refs"]))
    if found.get("snoozes"):
        snooze.forget(set(found["snoozes"]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prune", action="store_true", help="delete what the report lists under `prunable`")
    args = parser.parse_args()
    now = time.time()
    report = {"config": {"path": str(config_path()) if config_path() else None, "gerrit_host": gerrit.HOST,
                         "user": gerrit.USER, "repo": str(REPO), "repo_ok": (REPO / ".git").exists()},
              "ssh": check_ssh(), "http": check_http(), "zuul": check_zuul(), "daemon": daemon_state(now),
              "pre_push_hook": pre_push.installed(), "state": state_files(now)}
    report["versions"] = versions(report["daemon"])
    if not report["ssh"]["ok"] or (report["zuul"] and not report["zuul"]["ok"]):
        report["network"] = network.checks()
    open_changes = None
    if report["ssh"]["ok"]:
        try:
            open_changes = open_patch_sets()
        except (subprocess.SubprocessError, OSError, ValueError) as error:
            report["open_changes_error"] = gerrit.error_detail(error)
    found = prunable(now, open_changes)
    if args.prune:
        prune(found)
        report["pruned"] = found
    else:
        report["prunable"] = found
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["ssh"]["ok"] and report["http"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
