#!/usr/bin/env python3
# <xbar.title>Gerrit CLs</xbar.title>
# <xbar.desc>My open Gerrit CLs, read from the gerrit-babysit watcher snapshot.</xbar.desc>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>
import json
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
import snooze  # noqa: E402
from config import (CI_STUCK_S, COMMAND, CONFIG, REPO, SKILL_DIR, STALE_AFTER_S, STATUS,  # noqa: E402
                    SWIFTBAR_PLUGIN, gerrit_user, t)

HOST = CONFIG["gerrit_host"]
DASHBOARD = CONFIG["review_dashboard_url"] or f"https://{HOST}/dashboard/self"
ICON = "sfimage=arrow.triangle.pull"
OPEN_CLAUDE = SKILL_DIR / "GerritBabysit.app"
SSH = ["ssh", "-p", str(CONFIG["ssh_port"]), "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
       f"{gerrit_user()}@{HOST}"]
ORCA = shutil.which("orca") or "/opt/homebrew/bin/orca"
CLAUDE = shutil.which("claude") or str(pathlib.Path.home() / ".local/bin/claude")
# Worst first: the menu lists problems before anything else.
ORDER = {"red": 0, "green": 1, "orange": 2, None: 3, "gray": 4}


def scope(subject):
    match = re.match(r"^(?:\[WIP\]\s*)?\w+\(([^)]+)\)", subject)
    return match.group(1) if match else subject[:30]


def state_of(change):
    """(label, sf symbol, color) — worst problem first."""
    if change["wip"]:
        return "WIP", "pencil", "gray"
    if change["conflict"]:
        return t("bar_conflict"), "exclamationmark.triangle.fill", "red"
    if change["ci"] == "stale_base":
        return t("bar_stale_base"), "arrow.triangle.branch", "red"
    if change["ci"] == "failed":
        return t("bar_ci_failed", labels=", ".join(change["ci_failed"])), "xmark.octagon.fill", "red"
    if change["code_review"] < 0:
        return f"CR {change['code_review']}", "hand.thumbsdown.fill", "red"
    if change.get("outdated_parent"):
        return t("bar_parent_updated", parent=change["outdated_parent"]), "square.stack.3d.up", "orange"
    if change.get("ci_stuck"):
        return t("bar_ci_stuck", hours=CI_STUCK_S // 3600), "exclamationmark.arrow.triangle.2.circlepath", "orange"
    if change["ready"] and change.get("open_parent"):
        return t("bar_ready_parent", parent=change["open_parent"]), "link", "orange"
    if change["ready"]:
        return t("bar_ready"), "checkmark.seal.fill", "green"
    code_review = f"+{change['code_review']}" if change["code_review"] > 0 else "0"
    if change["ci"] == "running":
        return t("bar_ci_running", cr=code_review), "hourglass", "orange"
    return t("bar_ci_passed", cr=code_review), "clock", None


def action(name, *params):
    """Only plain tokens (numbers, action names) go through: SwiftBar params have no escaping for a `"`."""
    args = " ".join(f'param{i}="{p}"' for i, p in enumerate((name, *params), start=1))
    return f'bash="{SELF}" {args} terminal=false'


def osascript(script, *argv):
    return subprocess.run(["osascript", "-e", script, *argv], capture_output=True, text=True)


def notify(title, body):
    osascript("on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run", title, body)


def snapshot_row(number):
    changes = json.loads(STATUS.read_text()).get("changes", [])
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


def submit(number, patch_set):
    subject = snapshot_row(number).get("subject", "")
    if not confirmed(t("bar_submit_confirm", n=number, ps=patch_set), subject, t("bar_submit_button")):
        return
    # Pinning the patch set makes Gerrit refuse if a newer one was pushed since the snapshot.
    result = subprocess.run([*SSH, "gerrit", "review", "--submit", f"{number},{patch_set}"],
                            capture_output=True, text=True, timeout=120)
    if result.returncode == 0:
        notify(t("bar_submitted", n=number), subject)
    else:
        alert(t("bar_submit_failed", n=number), result.stderr.strip() or result.stdout.strip())
    refresh()


RECHECK = re.compile(r"recheck(?:-[\w-]+)?")


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
    if not RECHECK.fullmatch(comment) or str(row.get("patch_set")) != patch_set:
        return
    if not confirmed(t("bar_recheck_confirm", comment=comment, n=number, ps=patch_set), row.get("subject", ""),
                     t("bar_recheck_button")):
        return
    result = subprocess.run([*SSH, "gerrit", "review", "--message", comment, f"{number},{patch_set}"],
                            capture_output=True, text=True, timeout=120)
    if result.returncode == 0:
        notify(t("bar_rechecked", n=number), row.get("subject", ""))
    else:
        alert(t("bar_recheck_failed", n=number), result.stderr.strip() or result.stdout.strip())
    refresh()


def ask_date(number):
    tomorrow = time.strftime("%Y-%m-%d", time.localtime(snooze.in_days(1)))
    answer = osascript(
        'on run argv\n'
        'text returned of (display dialog (item 1 of argv) default answer (item 2 of argv) '
        'buttons {item 3 of argv, item 4 of argv} default button 2 cancel button 1 with title "Gerrit")\n'
        'end run',
        t("bar_snooze_prompt", n=number), tomorrow, t("bar_cancel"), t("bar_snooze_button"))
    if answer.returncode != 0:
        return None
    try:
        return snooze.parse_date(answer.stdout)
    except ValueError:
        alert(t("bar_snooze_invalid", value=answer.stdout.strip()), "")
        return None


def snooze_change(number, mode, value=None):
    if mode == "ps":
        snooze.set_snooze(number, patch_set=int(value))
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
    return not change["wip"] and (state_of(change)[2] == "red" or bool(change.get("threads")))


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


def handle(args):
    name, *params = args
    if name == "submit":
        submit(*params)
    elif name == "worktree":
        open_worktree(*params)
    elif name == "investigate":
        investigate(*params)
    elif name == "copy":
        copy(*params)
    elif name == "recheck":
        recheck(*params)
    elif name == "snooze":
        snooze_change(*params)
    elif name == "wake":
        wake(*params)


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
    print(f"----{t('bar_snooze_tomorrow')} | {action('snooze', number, 'days', 1)}")
    print(f"----{t('bar_snooze_week')} | {action('snooze', number, 'days', 7)}")
    print(f"----{t('bar_snooze_date')} | {action('snooze', number, 'date')}")
    # Stale data could hide a -1 or a push that arrived meanwhile: no public shortcut then.
    recheck_reason = recheck_detail(change) if fresh and change.get("patch_set") else None
    if recheck_reason:
        print("-----")
        print(f"--{t('bar_recheck', detail=recheck_reason)} | {action('recheck', number, change['patch_set'])} "
              "sfimage=arrow.clockwise")
    if fresh and change["ready"] and not change.get("open_parent") and change.get("patch_set"):
        print("-----")
        print(f"--{t('bar_submit')} | {action('submit', number, change['patch_set'])} sfimage=paperplane.fill")


def print_snoozed(change, entry):
    until = (t("bar_snoozed_ps") if entry.get("patch_set") is not None
             else t("bar_snoozed_until", date=time.strftime("%Y-%m-%d", time.localtime(entry["until"]))))
    print(f"{change['number']}  {scope(change['subject'])} — {until} | href={change['url']} sfimage=moon.zzz sfcolor=gray")
    print(f"--{t('bar_open_gerrit')} | href={change['url']} sfimage=safari")
    print(f"--{t('bar_wake')} | {action('wake', change['number'])} sfimage=bell")


def main():
    if not STATUS.exists():
        print(f"– | {ICON}")
        print("---")
        print(t("bar_no_snapshot", command=COMMAND))
        return
    snapshot = json.loads(STATUS.read_text())
    now = time.time()
    last_attempt = snapshot.get("last_attempt", snapshot["updated"])
    error = snapshot.get("last_error")
    stopped = now - last_attempt > STALE_AFTER_S

    changes = snapshot.get("changes", [])
    # Read here rather than from the snapshot: a snooze shows at once, not at the daemon's next poll.
    snoozed = snooze.active({c["number"]: c.get("patch_set") for c in changes}, now)
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
    data_age = age(now - snapshot["updated"]) if snapshot.get("updated") else "?"
    if stopped:
        print(f"{t('bar_stopped', age=data_age)} | color=orange sfimage=pause.circle")
    elif error:
        print(f"{t('bar_unreachable', age=data_age)} | color=orange sfimage=wifi.exclamationmark")
        print(f"--{error.replace('|', '¦')} | disabled=true")
    else:
        print(f"{t('bar_active')} | sfimage=dot.radiowaves.left.and.right")
    if OPEN_CLAUDE.exists():
        print(f"{t('bar_open_claude', command=COMMAND)} | href={OPEN_CLAUDE.as_uri()} sfimage=sparkles")
    print(f"{t('bar_dashboard')} | href={DASHBOARD} sfimage=eye")
    print(f"{t('bar_refresh')} | refresh=true")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        handle(sys.argv[1:])
    else:
        main()
