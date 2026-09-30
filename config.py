"""Settings shared by the watcher, the status line and the SwiftBar plugin."""
import getpass
import json
import os
import pathlib
import subprocess
import sys
import tempfile

SKILL_DIR = pathlib.Path(__file__).resolve().parent
# Survives plugin updates, unlike SKILL_DIR: config.json and LOCAL.md for plugin installs.
USER_DIR = pathlib.Path.home() / ".config" / "gerrit-babysit"
CACHE = pathlib.Path(os.environ.get("GERRIT_BABYSIT_CACHE") or pathlib.Path.home() / ".cache" / "gerrit-babysit")
STATUS = CACHE / "status.json"
# Read by macos/open-babysit.sh and the Stop hook: the Claude session the watcher runs in.
SESSION = CACHE / "session.json"
# A daemon loop can take interval + fetch timeout + ssh timeout before it writes again.
STALE_AFTER_S = 360
CI_STUCK_S = 2 * 3600
SWIFTBAR_PLUGIN = "gerrit"

DEFAULTS = {
    "gerrit_host": None,
    "ssh_port": 29418,
    "gerrit_user": None,
    "repo": None,
    "gerrit_mcp_config": None,
    "zuul_api": None,
    "zuul_status_url": None,
    "ci_user": "zuul",
    "bot_users": [],
    "ci_labels": ["Verified"],
    "protected_branches": ["main", "master"],
    "periodic_build": None,
    "screenshot_regression_marker": None,
    "review_dashboard_url": None,
    "max_reviewers": 10,
    "work_hours": [9, 19],
    "launchd_label": "local.gerrit-babysit",
    "language": "auto",
    "recheck_comment": "recheck",
}


def atomic_write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp name: two writers (daemon and session) must not clobber each other's half-written file.
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", delete=False) as tmp:
        try:
            tmp.write(json.dumps(payload, ensure_ascii=False))
            tmp.flush()
            # Without it, a power loss can leave the renamed file empty.
            os.fsync(tmp.fileno())
        except BaseException:
            pathlib.Path(tmp.name).unlink()
            raise
    pathlib.Path(tmp.name).replace(path)


def read_json(path):
    """A corrupt file reads as missing: otherwise every poll, menu redraw and status line would crash on it."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except ValueError as error:
        print(f"gerrit-babysit: ignoring corrupt {path}: {error}", file=sys.stderr)
        return {}


def config_path():
    candidates = [os.environ.get("GERRIT_BABYSIT_CONFIG"), SKILL_DIR / "config.json", USER_DIR / "config.json"]
    return next((pathlib.Path(p).expanduser() for p in candidates if p and pathlib.Path(p).expanduser().is_file()),
                None)


def load():
    path = config_path()
    return {**DEFAULTS, **(json.loads(path.read_text()) if path else {})}


def inside_repo():
    """Cloned as <repo>/.claude/skills/gerrit-babysit, the layout that needs no `repo` setting."""
    return SKILL_DIR.parent.name == "skills" and SKILL_DIR.parents[1].name == ".claude"


def default_repo():
    if inside_repo():
        return SKILL_DIR.parents[2]
    # Plugin or user-level install: the Claude Code project the session runs in.
    return pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


# Plugin skills are namespaced by the plugin name; cloned or symlinked skills are not.
PLUGIN = pathlib.Path.home() / ".claude" / "plugins" in SKILL_DIR.parents
COMMAND = "/gerrit-babysit:gerrit-babysit" if PLUGIN else "/gerrit-babysit"

CONFIG = load()
REPO = pathlib.Path(CONFIG["repo"]).expanduser() if CONFIG["repo"] else default_repo()


def gerrit_user():
    return (CONFIG["gerrit_user"] or os.environ.get("GERRIT_USER")
            or subprocess.run(["git", "-C", str(REPO), "config", "--get", "gitreview.username"],
                              capture_output=True, text=True).stdout.strip()
            or getpass.getuser())
