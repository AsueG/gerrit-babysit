"""Settings and UI strings shared by the watcher, the status line and the SwiftBar plugin."""
import getpass
import json
import os
import pathlib
import re
import subprocess

SKILL_DIR = pathlib.Path(__file__).resolve().parent
CACHE = pathlib.Path.home() / ".cache" / "gerrit-babysit"

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
}


def config_path():
    candidates = [os.environ.get("GERRIT_BABYSIT_CONFIG"), SKILL_DIR / "config.json",
                  pathlib.Path.home() / ".config" / "gerrit-babysit" / "config.json"]
    return next((pathlib.Path(p).expanduser() for p in candidates if p and pathlib.Path(p).expanduser().is_file()),
                None)


def load():
    path = config_path()
    return {**DEFAULTS, **(json.loads(path.read_text()) if path else {})}


CONFIG = load()
# Default layout: <repo>/.claude/skills/gerrit-babysit.
REPO = pathlib.Path(CONFIG["repo"]).expanduser() if CONFIG["repo"] else SKILL_DIR.parents[2]


def gerrit_user():
    return (CONFIG["gerrit_user"] or os.environ.get("GERRIT_USER")
            or subprocess.run(["git", "-C", str(REPO), "config", "--get", "gitreview.username"],
                              capture_output=True, text=True).stdout.strip()
            or getpass.getuser())


def system_language():
    """macOS UI language (`AppleLanguages`), else $LANG."""
    try:
        out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    match = re.search(r"[a-z]{2}", out) or re.match(r"[a-z]{2}", os.environ.get("LANG", ""))
    return match.group(0) if match else "en"


MESSAGES = {
    "en": {
        "conflict": "{n} has conflicts",
        "ready": "{n} ready to submit",
        "ready_for": " for {days} d",
        "ci_stuck": "{n} · CI stuck?",
        "ci_stuck_body": "nothing from zuul for {hours} h · {subject}",
        "rebase": "{n} needs a rebase",
        "rebase_body": "parent {parent} merged · {subject}",
        "unreviewed": "{n} unreviewed for {days} working days",
        "review_requested": "Review requested · {owner}",
        "new_patch_set": "{n} · new patch set {ps}",
        "replied": "{n} · {author} replied",
        "digest": "{count} Gerrit events",
        "threads_unknown": "threads ?",
        "bar_conflict": "merge conflict",
        "bar_stale_base": "rebase needed (Merge Failed)",
        "bar_ci_failed": "CI failed ({labels})",
        "bar_ci_stuck": "CI stuck? (nothing from zuul for 2 h)",
        "bar_ready_parent": "ready, but parent {parent} is open",
        "bar_ready": "ready to submit",
        "bar_ci_running": "CR {cr} · CI running",
        "bar_ci_passed": "CR {cr} · CI ✓",
        "bar_threads": " · {count} thread(s) to handle",
        "bar_open_gerrit": "Open in Gerrit",
        "bar_open_worktree": "Open the worktree in a terminal",
        "bar_copy": "Copy the change number",
        "bar_parent_open": "Parent {parent} still open",
        "bar_submit": "Submit…",
        "bar_submit_confirm": "Submit change {n} (patch set {ps})?",
        "bar_cancel": "Cancel",
        "bar_submit_button": "Submit",
        "bar_submitted": "{n} submitted",
        "bar_submit_failed": "Submit of {n} failed",
        "bar_copied": "Copied",
        "bar_no_snapshot": "No snapshot: run /gerrit-babysit in Claude Code",
        "bar_no_changes": "No open changes",
        "bar_stopped": "Watcher stopped — data from {age} ago",
        "bar_unreachable": "Gerrit unreachable (VPN?) — data from {age} ago",
        "bar_active": "Watcher running",
        "bar_open_claude": "Open Claude (/gerrit-babysit)",
        "bar_dashboard": "Changes to review",
        "bar_refresh": "Refresh",
    },
    "fr": {
        "conflict": "{n} en conflit",
        "ready": "{n} prête à soumettre",
        "ready_for": " depuis {days} j",
        "ci_stuck": "{n} · CI bloquée ?",
        "ci_stuck_body": "rien de zuul depuis {hours} h · {subject}",
        "rebase": "{n} à rebaser",
        "rebase_body": "parente {parent} mergée · {subject}",
        "unreviewed": "{n} sans review depuis {days} j ouvrés",
        "review_requested": "Review demandée · {owner}",
        "new_patch_set": "{n} · nouveau patch set {ps}",
        "replied": "{n} · {author} a répondu",
        "digest": "{count} événements Gerrit",
        "threads_unknown": "fils ?",
        "bar_conflict": "conflit",
        "bar_stale_base": "rebase requis (Merge Failed)",
        "bar_ci_failed": "CI en échec ({labels})",
        "bar_ci_stuck": "CI bloquée ? (rien de zuul depuis 2 h)",
        "bar_ready_parent": "prête, mais parente {parent} ouverte",
        "bar_ready": "prête à soumettre",
        "bar_ci_running": "CR {cr} · CI en cours",
        "bar_ci_passed": "CR {cr} · CI ✓",
        "bar_threads": " · {count} fil(s) à traiter",
        "bar_open_gerrit": "Ouvrir sur Gerrit",
        "bar_open_worktree": "Ouvrir le worktree dans un terminal",
        "bar_copy": "Copier le n° de CL",
        "bar_parent_open": "Parente {parent} encore ouverte",
        "bar_submit": "Soumettre…",
        "bar_submit_confirm": "Soumettre la CL {n} (patch set {ps}) ?",
        "bar_cancel": "Annuler",
        "bar_submit_button": "Soumettre",
        "bar_submitted": "{n} soumise",
        "bar_submit_failed": "Échec du submit de {n}",
        "bar_copied": "Copié",
        "bar_no_snapshot": "Aucun snapshot : lance /gerrit-babysit dans Claude Code",
        "bar_no_changes": "Aucune CL ouverte",
        "bar_stopped": "Watcher arrêté — données d'il y a {age}",
        "bar_unreachable": "Gerrit injoignable (VPN ?) — données d'il y a {age}",
        "bar_active": "Watcher actif",
        "bar_open_claude": "Ouvrir Claude (/gerrit-babysit)",
        "bar_dashboard": "CLs à reviewer",
        "bar_refresh": "Rafraîchir",
    },
}

LANGUAGE = CONFIG["language"] if CONFIG["language"] != "auto" else system_language()


def t(key, **values):
    return MESSAGES.get(LANGUAGE, MESSAGES["en"])[key].format(**values)
