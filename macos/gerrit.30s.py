#!/usr/bin/env python3
# <xbar.title>Gerrit CLs</xbar.title>
# <xbar.desc>My open Gerrit CLs, read from the gerrit-babysit watcher snapshot.</xbar.desc>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>
import datetime
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import time

# Installed as a symlink into the SwiftBar plugin folder: resolve it to find the skill.
SELF = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(SELF.parents[1]))
import ci  # noqa: E402
import snapshot  # noqa: E402
import snooze  # noqa: E402
from config import (CI_STUCK_S, COMMAND, CONFIG, DAEMON_POLL, REPO, STALE_AFTER_S, STATUS, SWIFTBAR_PLUGIN,  # noqa: E402
                    USER_DIR, VERSION, read_json)
from i18n import t  # noqa: E402

DASHBOARD = CONFIG["review_dashboard_url"] or f"https://{CONFIG['gerrit_host']}/dashboard/self"
ICON = "sfimage=arrow.triangle.pull"
# Built by macos/install.sh outside the plugin folder, which each update replaces.
OPEN_CLAUDE = USER_DIR / "GerritBabysit.app"
ORCA = shutil.which("orca") or "/opt/homebrew/bin/orca"
CLAUDE = shutil.which("claude") or str(pathlib.Path.home() / ".local/bin/claude")
# Worst first: the menu lists problems before anything else.
ORDER = {"red": 0, "green": 1, "orange": 2, None: 3, "gray": 4}


def scope(subject):
    match = re.match(r"^(?:\[WIP\]\s*)?\w+\(([^)]+)\)", subject)
    # A `|` would end SwiftBar's title and turn the rest into parameters.
    return (match.group(1) if match else subject[:30]).replace("|", "¦")


# snapshot.state → (sf symbol, color); red is exactly snapshot.PROBLEMS.
STYLES = {
    "wip": ("pencil", "gray"),
    "conflict": ("exclamationmark.triangle.fill", "red"),
    "stale_base": ("arrow.triangle.branch", "red"),
    "ci_failed": ("xmark.octagon.fill", "red"),
    "rejected": ("hand.thumbsdown.fill", "red"),
    "parent_updated": ("square.stack.3d.up", "orange"),
    "ci_stuck": ("exclamationmark.arrow.triangle.2.circlepath", "orange"),
    "ready_parent": ("link", "orange"),
    "submit_blocked": ("lock.fill", "orange"),
    "ready":("checkmark.seal.fill", "green"),
    "ci_running": ("hourglass", "orange"),
    "ci_passed": ("clock", None),
}


def cr_label(change):
    return f"+{change['code_review']}" if change["code_review"] > 0 else "0"


# snapshot.state → snapshot row → label; the others read `bar_<state>` as is.
LABELS = {
    "ci_failed": lambda c: t("bar_ci_failed", labels=", ".join(c["ci_failed"])),
    "rejected": lambda c: f"CR {c['code_review']}",
    "parent_updated": lambda c: t("bar_parent_updated", parent=c["outdated_parent"]),
    "ci_stuck": lambda c: t("bar_ci_stuck", hours=CI_STUCK_S // 3600),
    "ready_parent": lambda c: t("bar_ready_parent", parent=c["open_parent"]),
    "submit_blocked": lambda c: t("bar_submit_blocked", requirements=", ".join(c["submit_blocked"])),
    "ci_running": lambda c: t("bar_ci_running", cr=cr_label(c)),
    "ci_passed": lambda c: t("bar_ci_passed", cr=cr_label(c)),
}


def submit_wait(state, change):
    """What the submit waits for, when the state's label tells the votes instead."""
    if state == "ci_running":
        return t("bar_submit_wait_ci")
    if state != "ci_passed":
        return None
    if change["code_review"] < 2:
        return t("bar_submit_wait_review")
    if change.get("threads") is None:
        return t("threads_unknown")
    if change["threads"]:
        return t("bar_submit_wait_threads", count=change["threads"])
    return t("bar_submit_wait_ready")


def label_of(state, change):
    if state == "wip":
        problem = snapshot.problem(change)
        return f"WIP · {label_of(problem, change)}" if problem else "WIP"
    render = LABELS.get(state)
    return render(change) if render else t(f"bar_{state}")


def state_of(change):
    """(label, sf symbol, color) — worst problem first."""
    state = snapshot.state(change)
    return (label_of(state, change), *STYLES[state])


def action(name, *params):
    """Only plain tokens (numbers, action names) go through: SwiftBar params have no escaping for a `"`."""
    args = " ".join(f'param{i}="{p}"' for i, p in enumerate((name, *params), start=1))
    return f'bash="{SELF}" {args} terminal=false'


def osascript(script, *argv):
    return subprocess.run(["osascript", "-e", script, *argv], capture_output=True, text=True)


def notify(title, body):
    osascript("on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run", title, body)


def snapshot_row(number):
    changes = read_json(STATUS).get("changes", [])
    return next((c for c in changes if str(c["number"]) == number), {})


def confirmed(question, detail, button):
    """Cancel is the default button: a stray Enter never publishes anything."""
    return osascript(
        'on run argv\n'
        'display dialog (item 1 of argv & return & return & item 2 of argv) '
        'buttons {item 3 of argv, item 4 of argv} default button 1 cancel button 1 with title "Gerrit" with icon caution\n'
        'end run',
        question, detail, t("bar_cancel"), button).returncode == 0


def alert(title, detail):
    osascript('on run argv\ndisplay alert (item 1 of argv) message (item 2 of argv) as critical\nend run', title, detail)


def refresh():
    subprocess.run(["open", "-g", f"swiftbar://refreshplugin?name={SWIFTBAR_PLUGIN}"], capture_output=True)


def review(number, patch_set, option, subject, done, failed):
    """`gerrit review` on the confirmed patch set: Gerrit refuses if a newer one was pushed since the snapshot."""
    # Imported on use: resolving the SSH user can run git, and the menu redraws every 30 s.
    import gerrit
    result = gerrit.ssh("gerrit", "review", *option, f"{number},{patch_set}", timeout=120)
    if result.returncode == 0:
        notify(t(done, n=number), subject)
    else:
        alert(t(failed, n=number), result.stderr.strip() or result.stdout.strip())
    refresh()


def submit(number, patch_set):
    subject = snapshot_row(number).get("subject", "")
    if confirmed(t("bar_submit_confirm", n=number, ps=patch_set), subject, t("bar_submit_button")):
        review(number, patch_set, ["--submit"], subject, "bar_submitted", "bar_submit_failed")


def recheck_detail(change):
    """Why a recheck is worth one click: every failed job already flaked here, and none was rechecked yet."""
    jobs = change.get("ci_failed_jobs") or []
    flaky = change.get("flaky") or {}
    if change["ci"] != "failed" or change.get("rechecks") or not jobs:
        return None
    if any(flaky.get(j["job"], {}).get("month", 0) < ci.FLAKY_MIN for j in jobs):
        return None
    return ", ".join(t("bar_flaky_week", job=j["job"], count=flaky[j["job"]]["week"]) if flaky[j["job"]]["week"]
                     else t("bar_flaky_month", job=j["job"], count=flaky[j["job"]]["month"]) for j in jobs)


def recheck(number, patch_set):
    comment = CONFIG["recheck_comment"]
    row = snapshot_row(number)
    # The comment goes through Gerrit's SSH command line: only a plain recheck variant.
    if not ci.RECHECK.fullmatch(comment) or str(row.get("patch_set")) != patch_set:
        return
    subject = row.get("subject", "")
    if confirmed(t("bar_recheck_confirm", comment=comment, n=number, ps=patch_set), subject, t("bar_recheck_button")):
        review(number, patch_set, ["--message", comment], subject, "bar_rechecked", "bar_recheck_failed")


# A plain text field: on macOS 26 osascript gets SIGKILLed as soon as it builds an AppKit NSDatePicker.
DATE_PROMPT = ('on run argv\n'
               'text returned of (display dialog (item 1 of argv) default answer (item 4 of argv) '
               'buttons {item 2 of argv, item 3 of argv} default button 2 cancel button 1 with title "Gerrit")\n'
               'end run')


def ask_date(number):
    today = datetime.date.today()
    answer = osascript(DATE_PROMPT, t("bar_snooze_prompt", n=number), t("bar_cancel"), t("bar_snooze_button"),
                       (today + datetime.timedelta(days=1)).isoformat())
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
        created = subprocess.run([ORCA, "terminal", "create", "--worktree", f"path:{path}", "--title", title,
                                  *extra, "--focus"], capture_output=True, timeout=30)
        if created.returncode == 0:
            return
    if not command:
        subprocess.run(["open", "-a", "Terminal", path], capture_output=True)
        return
    # Path and command go in as argv, never spliced into the script.
    osascript('on run argv\n'
              'tell application "Terminal" to do script "cd " & quoted form of item 1 of argv & " && " & item 2 of argv\n'
              'tell application "Terminal" to activate\n'
              'end run', str(path), command)


def open_worktree(number):
    path = snapshot_row(number).get("worktree")
    if path:
        open_terminal(path, f"CL {number}")


def needs_investigation(change):
    if change["wip"]:
        return snapshot.problem(change) in snapshot.NEEDS_REBASE
    return state_of(change)[2] == "red" or bool(change.get("threads"))


def investigate(number):
    change = snapshot_row(number)
    if not change:
        return
    label = state_of(change)[0]
    if change.get("threads"):
        label += t("bar_threads", count=change["threads"])
    prompt = t("bar_investigate_prompt", n=number, url=change["url"], subject=change["subject"], state=label)
    open_terminal(change.get("worktree") or REPO, f"CL {number}", shlex.join([CLAUDE, prompt]))


def copy(text):
    subprocess.run(["pbcopy"], input=text, text=True)
    notify(t("bar_copied"), text)


def daemon_version(now):
    """The running daemon's version when it differs from this menu's ("?" for a daemon too old to say), else None."""
    heartbeat = read_json(DAEMON_POLL)
    if not heartbeat or now - heartbeat.get("attempt", 0) > STALE_AFTER_S:
        return None
    version = heartbeat.get("version") or "?"
    return version if version != VERSION else None


def restart_daemon():
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{CONFIG['launchd_label']}"], capture_output=True)
    refresh()


ACTIONS = {"submit": submit, "worktree": open_worktree, "investigate": investigate, "copy": copy,
           "recheck": recheck, "snooze": snooze_change, "wake": wake, "restart_daemon": restart_daemon}


def handle(args):
    name, *params = args
    if name in ACTIONS:
        ACTIONS[name](*params)


def age(seconds):
    minutes = int(seconds // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"


def print_change(change, label, symbol, color, fresh):
    tint = f" sfcolor={color}" if color else ""
    url = change["url"]
    number = str(change["number"])
    threads = change.get("threads", 0)
    if threads:
        label += t("bar_threads", count=threads)
    print(f"{number}  {scope(change['subject'])} — {label} | href={url} sfimage={symbol}{tint}")
    print(f"--{change['subject'].replace('|', '¦')} | disabled=true")
    print("-----")
    print(f"--{t('bar_open_gerrit')} | href={url} sfimage=safari")
    if needs_investigation(change):
        print(f"--{t('bar_investigate')} | {action('investigate', number)} sfimage=sparkle.magnifyingglass")
    if change.get("worktree"):
        print(f"--{t('bar_open_worktree')} | {action('worktree', number)} sfimage=terminal")
    print(f"--{t('bar_copy')} | {action('copy', number)} sfimage=doc.on.doc")
    if change.get("open_parent"):
        parent_url = url.rsplit("/", 1)[0] + f"/{change['open_parent']}"
        print(f"--{t('bar_parent_open', parent=change['open_parent'])} | href={parent_url} sfimage=link")
    print(f"--{t('bar_snooze')} | sfimage=moon.zzz")
    if change.get("patch_set"):
        print(f"----{t('bar_snooze_ps')} | {action('snooze', number, 'ps', change['patch_set'])}")
    if change.get("base_red"):
        print(f"----{t('bar_snooze_base')} | {action('snooze', number, 'base')}")
    print(f"----{t('bar_snooze_tomorrow')} | {action('snooze', number, 'days', 1)}")
    print(f"----{t('bar_snooze_week')} | {action('snooze', number, 'days', 7)}")
    print(f"----{t('bar_snooze_date')} | {action('snooze', number, 'date')}")
    # Stale data could hide a -1 or a push that arrived meanwhile: no public shortcut then.
    recheck_reason = recheck_detail(change) if fresh and change.get("patch_set") else None
    if recheck_reason:
        print("-----")
        print(f"--{t('bar_recheck', detail=recheck_reason)} | {action('recheck', number, change['patch_set'])} "
              "sfimage=arrow.clockwise")
    if change.get("patch_set"):
        print("-----")
        state = snapshot.state(change)
        if fresh and state == "ready":
            print(f"--{t('bar_submit')} | {action('submit', number, change['patch_set'])} sfimage=paperplane.fill")
        else:
            # Always there, greyed out with what stands in the way, so a missing Submit never needs explaining.
            reason = (submit_wait(state, change) or label_of(state, change)) if fresh else t("bar_submit_stale")
            print(f"--{t('bar_submit_unavailable', reason=reason.replace('|', '¦'))} | disabled=true sfimage=paperplane")


def print_snoozed(change, entry):
    if entry.get("base_green"):
        until = t("bar_snoozed_base")
    elif entry.get("patch_set") is not None:
        until = t("bar_snoozed_ps")
    else:
        until = t("bar_snoozed_until", date=time.strftime("%Y-%m-%d", time.localtime(entry["until"])))
    print(f"{change['number']}  {scope(change['subject'])} — {until} | href={change['url']} sfimage=moon.zzz sfcolor=gray")
    print(f"--{t('bar_open_gerrit')} | href={change['url']} sfimage=safari")
    print(f"--{t('bar_wake')} | {action('wake', change['number'])} sfimage=bell")


def main():
    if not STATUS.exists():
        print(f"– | {ICON}")
        print("---")
        print(t("bar_no_snapshot", command=COMMAND))
        return
    status = read_json(STATUS)
    now = time.time()
    error = status.get("last_error")
    stopped = snapshot.is_stopped(status, now)

    changes = status.get("changes", [])
    # Read here rather than from the snapshot: a snooze shows at once, not at the daemon's next poll.
    snoozed = snooze.snapshot_active(status, now)
    rows = sorted(((c, state_of(c)) for c in changes if c["number"] not in snoozed),
                  key=lambda row: ORDER.get(row[1][2], 3))
    active = [row for row in rows if not row[0]["wip"]]
    problems = sum(1 for _, (_, _, color) in rows if color == "red")
    ready = sum(1 for _, (_, _, color) in rows if color == "green")
    title = str(len(active))
    if error or stopped:
        title += " · \033[33m?\033[0m"
    elif problems:
        title += f" · \033[31m{problems} ⚠\033[0m"
    elif ready:
        title += f" · \033[32m{ready} ✓\033[0m"
    print(f"{title} | {ICON} ansi=true")

    print("---")
    for branch, health in sorted(status.get("base_health", {}).items()):
        if ci.is_red(health):
            link = f"href={health['log_url']} " if health.get("log_url") else ""
            print(f"{t('bar_base_red', branch=branch, age=age(ci.red_for_s(health, now)))} | "
                  f"{link}sfimage=flame.fill sfcolor=red")
    for change, state in rows:
        print_change(change, *state, fresh=not (error or stopped))
    if not changes:
        print(t("bar_no_changes"))
    if snoozed:
        print("---")
        print(f"{t('bar_snoozed_section')} | disabled=true")
        for change in changes:
            if change["number"] in snoozed:
                print_snoozed(change, snoozed[change["number"]])
    print("---")
    data_age = age(now - status["updated"]) if status.get("updated") else "?"
    if stopped:
        print(f"{t('bar_stopped', age=data_age)} | color=orange sfimage=pause.circle")
    elif error:
        print(f"{t('bar_unreachable', age=data_age)} | color=orange sfimage=wifi.exclamationmark")
        print(f"--{error.replace('|', '¦')} | disabled=true")
    else:
        print(f"{t('bar_active')} | sfimage=dot.radiowaves.left.and.right")
    drifted = daemon_version(now)
    if drifted:
        print(f"{t('bar_version_mismatch', daemon=drifted, menu=VERSION or '?')} | color=orange "
              "sfimage=exclamationmark.arrow.circlepath")
        print(f"--{t('bar_restart_daemon')} | {action('restart_daemon')} sfimage=arrow.clockwise")
    if OPEN_CLAUDE.exists():
        print(f"{t('bar_open_claude', command=COMMAND)} | href={OPEN_CLAUDE.as_uri()} sfimage=sparkles")
    print(f"{t('bar_dashboard')} | href={DASHBOARD} sfimage=eye")
    print(f"{t('bar_refresh')} | refresh=true")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        handle(sys.argv[1:])
    else:
        main()
