"""Settings and UI strings shared by the watcher, the status line and the SwiftBar plugin."""
import getpass
import json
import os
import pathlib
import re
import subprocess
import tempfile

SKILL_DIR = pathlib.Path(__file__).resolve().parent
# Survives plugin updates, unlike SKILL_DIR: config.json and LOCAL.md for plugin installs.
USER_DIR = pathlib.Path.home() / ".config" / "gerrit-babysit"
CACHE = pathlib.Path(os.environ.get("GERRIT_BABYSIT_CACHE") or pathlib.Path.home() / ".cache" / "gerrit-babysit")
STATUS = CACHE / "status.json"
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
        tmp.write(json.dumps(payload, ensure_ascii=False))
    pathlib.Path(tmp.name).replace(path)


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
        "parent_updated": "{n} · parent {parent} has a new patch set",
        "unreviewed": "{n} unreviewed for {days} working days",
        "review_requested": "Review requested · {owner}",
        "new_patch_set": "{n} · new patch set {ps}",
        "replied": "{n} · {author} replied",
        "digest": "{count} Gerrit events",
        "threads_unknown": "threads ?",
        "bar_conflict": "merge conflict",
        "bar_stale_base": "rebase needed (Merge Failed)",
        "bar_ci_failed": "CI failed ({labels})",
        "bar_ci_stuck": "CI stuck? (nothing from zuul for {hours} h)",
        "bar_ready_parent": "ready, but parent {parent} is open",
        "bar_parent_updated": "to rebase on parent {parent}'s new patch set",
        "bar_ready": "ready to submit",
        "bar_ci_running": "CR {cr} · CI running",
        "bar_ci_passed": "CR {cr} · CI ✓",
        "bar_threads": " · {count} thread(s) to handle",
        "bar_open_gerrit": "Open in Gerrit",
        "bar_investigate": "Investigate with Claude",
        "bar_investigate_prompt": "Change {n} ({url}) — \"{subject}\" — is in state: {state}. Investigate why "
                                  "(CI logs, reviewer comments, conflict), explain the cause and propose a fix. "
                                  "Do not push or post anything on Gerrit without asking me.",
        "bar_open_worktree": "Open the worktree in a terminal",
        "bar_copy": "Copy the change number",
        "bar_parent_open": "Parent {parent} still open",
        "bar_submit": "Submit…",
        "bar_submit_confirm": "Submit change {n} (patch set {ps})?",
        "bar_cancel": "Cancel",
        "bar_submit_button": "Submit",
        "bar_submitted": "{n} submitted",
        "bar_submit_failed": "Submit of {n} failed",
        "bar_recheck": "Recheck ({detail})…",
        "bar_flaky_week": "{job} flaky {count}× this week",
        "bar_flaky_month": "{job} flaky {count}× in 30 days",
        "bar_recheck_confirm": "Post \"{comment}\" on change {n} (patch set {ps})?",
        "bar_recheck_button": "Recheck",
        "bar_rechecked": "Recheck posted on {n}",
        "bar_recheck_failed": "Recheck of {n} failed",
        "bar_snooze": "Snooze",
        "bar_snooze_ps": "Until the next patch set",
        "bar_snooze_tomorrow": "Until tomorrow morning",
        "bar_snooze_week": "For a week",
        "bar_snooze_date": "Until a date…",
        "bar_snooze_prompt": "Snooze change {n} until (YYYY-MM-DD):",
        "bar_snooze_invalid": "Not a date: {value}",
        "bar_snooze_button": "Snooze",
        "bar_snoozed": "{n} snoozed",
        "bar_snoozed_section": "Snoozed",
        "bar_snoozed_until": "until {date}",
        "bar_snoozed_ps": "until the next patch set",
        "bar_wake": "Wake up now",
        "bar_copied": "Copied",
        "bar_no_snapshot": "No snapshot: run {command} in Claude Code",
        "bar_no_changes": "No open changes",
        "bar_stopped": "Watcher stopped — data from {age} ago",
        "bar_unreachable": "Gerrit unreachable (VPN?) — data from {age} ago",
        "bar_active": "Watcher running",
        "bar_open_claude": "Open Claude ({command})",
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
        "parent_updated": "{n} · la parente {parent} a un nouveau patch set",
        "unreviewed": "{n} sans review depuis {days} j ouvrés",
        "review_requested": "Review demandée · {owner}",
        "new_patch_set": "{n} · nouveau patch set {ps}",
        "replied": "{n} · {author} a répondu",
        "digest": "{count} événements Gerrit",
        "threads_unknown": "fils ?",
        "bar_conflict": "conflit",
        "bar_stale_base": "rebase requis (Merge Failed)",
        "bar_ci_failed": "CI en échec ({labels})",
        "bar_ci_stuck": "CI bloquée ? (rien de zuul depuis {hours} h)",
        "bar_ready_parent": "prête, mais parente {parent} ouverte",
        "bar_parent_updated": "à rebaser sur le nouveau patch set de {parent}",
        "bar_ready": "prête à soumettre",
        "bar_ci_running": "CR {cr} · CI en cours",
        "bar_ci_passed": "CR {cr} · CI ✓",
        "bar_threads": " · {count} fil(s) à traiter",
        "bar_open_gerrit": "Ouvrir sur Gerrit",
        "bar_investigate": "Investiguer avec Claude",
        "bar_investigate_prompt": "La CL {n} ({url}) — « {subject} » — est en état : {state}. Investigue pourquoi "
                                  "(logs CI, commentaires des reviewers, conflit), explique la cause et propose un "
                                  "correctif. Ne pousse rien et ne poste rien sur Gerrit sans me demander.",
        "bar_open_worktree": "Ouvrir le worktree dans un terminal",
        "bar_copy": "Copier le n° de CL",
        "bar_parent_open": "Parente {parent} encore ouverte",
        "bar_submit": "Soumettre…",
        "bar_submit_confirm": "Soumettre la CL {n} (patch set {ps}) ?",
        "bar_cancel": "Annuler",
        "bar_submit_button": "Soumettre",
        "bar_submitted": "{n} soumise",
        "bar_submit_failed": "Échec du submit de {n}",
        "bar_recheck": "Recheck ({detail})…",
        "bar_flaky_week": "{job} flaky {count}× cette semaine",
        "bar_flaky_month": "{job} flaky {count}× en 30 jours",
        "bar_recheck_confirm": "Poster « {comment} » sur la CL {n} (patch set {ps}) ?",
        "bar_recheck_button": "Recheck",
        "bar_rechecked": "Recheck posté sur {n}",
        "bar_recheck_failed": "Échec du recheck de {n}",
        "bar_snooze": "Mettre en sourdine",
        "bar_snooze_ps": "Jusqu'au prochain patch set",
        "bar_snooze_tomorrow": "Jusqu'à demain matin",
        "bar_snooze_week": "Pendant une semaine",
        "bar_snooze_date": "Jusqu'à une date…",
        "bar_snooze_prompt": "Mettre la CL {n} en sourdine jusqu'au (AAAA-MM-JJ) :",
        "bar_snooze_invalid": "Date invalide : {value}",
        "bar_snooze_button": "Mettre en sourdine",
        "bar_snoozed": "{n} en sourdine",
        "bar_snoozed_section": "En sourdine",
        "bar_snoozed_until": "jusqu'au {date}",
        "bar_snoozed_ps": "jusqu'au prochain patch set",
        "bar_wake": "Réactiver maintenant",
        "bar_copied": "Copié",
        "bar_no_snapshot": "Aucun snapshot : lance {command} dans Claude Code",
        "bar_no_changes": "Aucune CL ouverte",
        "bar_stopped": "Watcher arrêté — données d'il y a {age}",
        "bar_unreachable": "Gerrit injoignable (VPN ?) — données d'il y a {age}",
        "bar_active": "Watcher actif",
        "bar_open_claude": "Ouvrir Claude ({command})",
        "bar_dashboard": "CLs à reviewer",
        "bar_refresh": "Rafraîchir",
    },
}

# Resolved on first use: the status line imports this module on every refresh and rarely needs a string.
_language = None


def language():
    global _language
    if _language is None:
        _language = CONFIG["language"] if CONFIG["language"] != "auto" else system_language()
    return _language


def t(key, **values):
    return MESSAGES.get(language(), MESSAGES["en"])[key].format(**values)
