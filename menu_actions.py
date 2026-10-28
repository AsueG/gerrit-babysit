"""What a click in the SwiftBar menu does (macos/gerrit.30s.py draws it): confirm, then act on Gerrit, git or launchd.

Each public action first checks the snapshot still shows what the menu offered: stale rows could hide a -1 or a push."""

import datetime
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import time

import ci
import snapshot
import snooze
from config import CONFIG, REPO, STATUS, SWIFTBAR_PLUGIN, read_json
from i18n import t
from snapshot import GATE_LABEL, HASHTAGS

ORCA = shutil.which("orca") or "/opt/homebrew/bin/orca"
CLAUDE = shutil.which("claude") or str(pathlib.Path.home() / ".local/bin/claude")
DOMAIN = f"gui/{os.getuid()}"
SERVICE = f"{DOMAIN}/{CONFIG['launchd_label']}"
PLIST = pathlib.Path.home() / "Library/LaunchAgents" / f"{CONFIG['launchd_label']}.plist"


def osascript(script, *argv):
    return subprocess.run(["osascript", "-e", script, *argv], capture_output=True, text=True)


def notify(title, body):
    osascript("on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run", title, body)


def snapshot_row(number, status=None):
    changes = (read_json(STATUS) if status is None else status).get("changes", [])
    return next((c for c in changes if str(c["number"]) == number), {})


def confirmed(question, detail, button):
    """Cancel is the default button: a stray Enter never publishes anything."""
    return (
        osascript(
            "on run argv\n"
            "display dialog (item 1 of argv & return & return & item 2 of argv) "
            'buttons {item 3 of argv, item 4 of argv} default button 1 cancel button 1 with title "Gerrit" with icon caution\n'
            "end run",
            question,
            detail,
            t("bar_cancel"),
            button,
        ).returncode
        == 0
    )


def alert(title, detail):
    osascript("on run argv\ndisplay alert (item 1 of argv) message (item 2 of argv) as critical\nend run", title, detail)


def refresh():
    subprocess.run(["open", "-g", f"swiftbar://refreshplugin?name={SWIFTBAR_PLUGIN}"], capture_output=True)


def review(number, patch_set, subject, done, failed, params=None, **action):
    """On the confirmed patch set: Gerrit refuses if a newer one was pushed since the snapshot."""
    # Imported on use: resolving the SSH user can run git, and the menu redraws every 30 s.
    import gerrit

    try:
        gerrit.review(number, patch_set, **action)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        alert(t(failed, n=number, **(params or {})), gerrit.error_detail(error))
    else:
        notify(t(done, n=number, **(params or {})), subject)
    refresh()


def fresh_snapshot():
    """The rows behind a public action, or None when they may hide a -1 or a push that arrived since."""
    status = read_json(STATUS)
    return None if status.get("last_error") or snapshot.is_stopped(status, time.time()) else status


def fresh_row(number):
    status = fresh_snapshot()
    return snapshot_row(number, status) if status else {}


def acted_row(number, patch_set):
    """The row behind a public action on `patch_set`: {} when the snapshot is stale or shows another patch set."""
    row = fresh_row(number)
    return row if str(row.get("patch_set")) == patch_set else {}


def submit(number, patch_set):
    row = acted_row(number, patch_set)
    if not row or row.get("gate") is not None or snapshot.state(row) != "ready":
        return
    subject = row.get("subject", "")
    if confirmed(t("bar_submit_confirm", n=number, ps=patch_set), subject, t("bar_submit_button")):
        review(number, patch_set, subject, "bar_submitted", "bar_submit_failed", submit=True)


def vote_gate(number, patch_set):
    row = acted_row(number, patch_set)
    if row.get("gate") != "vote":
        return
    subject = row.get("subject", "")
    if confirmed(t("bar_gate_confirm", label=GATE_LABEL, n=number, ps=patch_set), subject, t("bar_gate_button")):
        review(number, patch_set, subject, "bar_gate_voted", "bar_gate_failed", {"label": GATE_LABEL}, labels={GATE_LABEL: 1})


def add_hashtag(number, kind):
    tag = HASHTAGS.get(kind)
    row = fresh_row(number)
    if not tag or not row or tag in row.get("hashtags", []):
        return
    subject = row.get("subject", "")
    if not confirmed(t(f"bar_{kind}_confirm", tag=tag, n=number), subject, t("bar_hashtag_button")):
        return
    # Imported on use: the menu redraws every 30 s and only the actions need it.
    import gerrit

    try:
        gerrit.rest_post(f"/changes/{number}/hashtags", {"add": [tag]})
    except (OSError, ValueError, KeyError) as error:
        alert(t("bar_hashtag_failed", tag=tag, n=number), gerrit.error_detail(error))
    else:
        notify(t("bar_hashtag_added", tag=tag, n=number), subject)
    refresh()


def recheck(number, patch_set):
    comment = CONFIG["recheck_comment"]
    row = acted_row(number, patch_set)
    # Only a comment zuul takes for a recheck: anything else would be published for nothing.
    if not ci.RECHECK.fullmatch(comment) or not row:
        return
    subject = row.get("subject", "")
    if confirmed(t("bar_recheck_confirm", comment=comment, n=number, ps=patch_set), subject, t("bar_recheck_button")):
        review(number, patch_set, subject, "bar_rechecked", "bar_recheck_failed", message=comment)


def push_rebase(number, patch_set):
    """Pushes the rebase the watcher prepared, after a confirmation: only while the snapshot still shows the patch set
    it was made from and the private ref still holds it."""
    # Imported on use: the menu redraws every 30 s and only this action needs git.
    import repo

    row = acted_row(number, patch_set)
    prepared = row.get("rebase_ready") or {}
    if str(prepared.get("patch_set")) != patch_set:
        return
    sha = repo.git("rev-parse", "--verify", "--quiet", f"{repo.REBASED}/{number}").strip()
    if not sha or sha != prepared.get("sha"):
        return
    detail = row.get("subject", "")
    if row.get("worktree"):
        detail += "\n\n" + t("bar_push_rebase_worktree", path=row["worktree"], ps=patch_set, n=number)
    if not confirmed(
        t("bar_push_rebase_confirm", n=number, ps=patch_set, onto=snapshot.rebase_onto(row)), detail, t("bar_push_button")
    ):
        return
    result = repo.push_for_review(sha, row["branch"])
    if result.returncode == 0:
        notify(t("bar_pushed", n=number), row.get("subject", ""))
    else:
        alert(t("bar_push_failed", n=number), result.stderr.strip() or result.stdout.strip())
    refresh()


# A plain text field: on macOS 26 osascript gets SIGKILLed as soon as it builds an AppKit NSDatePicker.
DATE_PROMPT = (
    "on run argv\n"
    "text returned of (display dialog (item 1 of argv) default answer (item 4 of argv) "
    'buttons {item 2 of argv, item 3 of argv} default button 2 cancel button 1 with title "Gerrit")\n'
    "end run"
)


def ask_date(number):
    today = datetime.date.today()
    answer = osascript(
        DATE_PROMPT,
        t("bar_snooze_prompt", n=number),
        t("bar_cancel"),
        t("bar_snooze_button"),
        (today + datetime.timedelta(days=1)).isoformat(),
    )
    if answer.returncode != 0:
        return None
    text = answer.stdout.strip()
    try:
        day = datetime.date.fromisoformat(text)
    except ValueError:
        day = None
    if day is None or day <= today:
        alert(t("bar_snooze_bad_date"), text)
        return None
    return snooze.morning(day)


def snooze_change(number, mode, value=None):
    if mode == "ps":
        snooze.set_snooze(number, patch_set=int(value))
    elif mode == "base":
        snooze.set_snooze(number, base_green=True)
    else:
        until = snooze.in_days(int(value)) if mode == "days" else ask_date(number)
        if until is None:
            return
        snooze.set_snooze(number, until=until)
    notify(t("bar_snoozed", n=number), snapshot_row(number).get("subject", ""))
    refresh()


def wake(number):
    snooze.wake(number)
    refresh()


def open_terminal(path, title, command=None):
    if pathlib.Path(ORCA).is_file():
        subprocess.run(["open", "-a", "Orca"], capture_output=True)
        extra = ["--command", command] if command else []
        created = subprocess.run(
            [ORCA, "terminal", "create", "--worktree", f"path:{path}", "--title", title, *extra, "--focus"],
            capture_output=True,
            timeout=30,
        )
        if created.returncode == 0:
            return
    if not command:
        subprocess.run(["open", "-a", "Terminal", path], capture_output=True)
        return
    # Path and command go in as argv, never spliced into the script.
    osascript(
        "on run argv\n"
        'tell application "Terminal" to do script "cd " & quoted form of item 1 of argv & " && " & item 2 of argv\n'
        'tell application "Terminal" to activate\n'
        "end run",
        str(path),
        command,
    )


def open_worktree(number):
    path = snapshot_row(number).get("worktree")
    if path:
        open_terminal(path, f"CL {number}")


def diagnosis_text(job):
    parts = [f"{job['job']} ({job.get('category', '?')})"]
    if job.get("log_url"):
        parts.append(job["log_url"])
    if job.get("excerpt"):
        parts.append(json.dumps(job["excerpt"], ensure_ascii=False))
    parts += [t("bar_resembles", change=r["change"], fix=r.get("fix") or "?") for r in job.get("resembles", [])]
    return " ".join(parts)


def investigate(number):
    change = snapshot_row(number)
    if not change:
        return
    label = snapshot.display(change)[0]
    if change.get("threads"):
        label += t("bar_threads", count=change["threads"])
    prompt = t("bar_investigate_prompt", n=number, url=change["url"], subject=change["subject"], state=label)
    jobs = change.get("ci_diagnosis") or []
    if jobs and change["ci"] == "failed":
        prompt += " " + t("bar_investigate_diagnosis", jobs="; ".join(diagnosis_text(j) for j in jobs))
    open_terminal(change.get("worktree") or REPO, f"CL {number}", shlex.join([CLAUDE, prompt]))


def copy(text):
    subprocess.run(["pbcopy"], input=text, text=True)
    notify(t("bar_copied"), text)


def launchctl(*args):
    subprocess.run(["launchctl", *args], capture_output=True)


def restart_daemon():
    launchctl("kickstart", "-k", SERVICE)
    refresh()


def daemon_paused():
    """None without a LaunchAgent to toggle. Paused means disabled in launchd, so it stays off across logins."""
    if not PLIST.exists():
        return None
    listed = subprocess.run(["launchctl", "print-disabled", DOMAIN], capture_output=True, text=True).stdout
    return re.search(rf'"{re.escape(CONFIG["launchd_label"])}" => (disabled|true)', listed) is not None


def pause_daemon():
    launchctl("disable", SERVICE)
    launchctl("bootout", SERVICE)
    refresh()


def resume_daemon():
    launchctl("enable", SERVICE)
    launchctl("bootstrap", DOMAIN, str(PLIST))
    refresh()


def remove_candidate(candidate):
    """The worktree is never forced: one that got files meanwhile keeps them, and its branch with it."""
    import repo

    if candidate.get("worktree"):
        removed = repo.git_run("worktree", "remove", candidate["worktree"])
        if removed.returncode:
            return removed.stderr.strip() or removed.stdout.strip()
    deleted = repo.git_run("branch", "-D", candidate["branch"])
    return (deleted.stderr.strip() or deleted.stdout.strip()) if deleted.returncode else None


def cleanup():
    """Asked again of git and Gerrit on click: the snapshot may be a poll old, a branch may have moved since."""
    import gerrit
    import repo

    try:
        candidates = [event for _, event in repo.cleanup_candidates()]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        alert(t("bar_cleanup_failed"), gerrit.error_detail(error))
        return
    if not candidates:
        notify(t("bar_cleanup_none"), "")
        refresh()
        return
    detail = "\n".join(snapshot.cleanup_line(c) for c in sorted(candidates, key=lambda c: c["branch"]))
    if not confirmed(t("bar_cleanup_confirm", count=len(candidates)), detail, t("bar_cleanup_button")):
        return
    failures = {c["branch"]: error for c in candidates if (error := remove_candidate(c))}
    if failures:
        alert(t("bar_cleanup_failed"), "\n".join(f"{branch}: {error}" for branch, error in sorted(failures.items())))
    if len(failures) < len(candidates):
        notify(t("bar_cleaned", count=len(candidates) - len(failures)), "")
    refresh()


ACTIONS = {
    "submit": submit,
    "worktree": open_worktree,
    "investigate": investigate,
    "copy": copy,
    "recheck": recheck,
    "snooze": snooze_change,
    "wake": wake,
    "restart_daemon": restart_daemon,
    "pause_daemon": pause_daemon,
    "resume_daemon": resume_daemon,
    "cleanup": cleanup,
    "push_rebase": push_rebase,
    "vote_gate": vote_gate,
    "hashtag": add_hashtag,
}


def handle(args):
    name, *params = args
    if name in ACTIONS:
        ACTIONS[name](*params)
