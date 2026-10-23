#!/usr/bin/env python3
# <xbar.title>Gerrit CLs</xbar.title>
# <xbar.desc>My open Gerrit CLs, read from the gerrit-babysit watcher snapshot.</xbar.desc>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>
import pathlib
import re
import sys
import time

# Installed as a symlink into the SwiftBar plugin folder: resolve it to find the skill.
SELF = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(SELF.parents[1]))
import ci  # noqa: E402
import menu_actions  # noqa: E402
import snapshot  # noqa: E402
import snooze  # noqa: E402
from config import COMMAND, DAEMON_POLL, STALE_AFTER_S, STATUS, USER_DIR, VERSION, dashboard_url, read_json  # noqa: E402
from i18n import t  # noqa: E402
from snapshot import GATE_LABEL, HASHTAGS, cleanup_line, display, label_of, rebase_onto, submit_wait  # noqa: E402

DASHBOARD = dashboard_url()
ICON = "sfimage=arrow.triangle.pull"
# Built by macos/install.sh outside the plugin folder, which each update replaces.
OPEN_CLAUDE = USER_DIR / "GerritBabysit.app"
# Worst first: the menu lists problems before anything else.
ORDER = {"red": 0, "green": 1, "orange": 2, None: 3, "gray": 4}


def menu_text(text):
    # A `|` would end SwiftBar's title and turn the rest into parameters.
    return str(text).replace("|", "¦")


def scope(subject):
    match = re.match(r"^(?:\[WIP\]\s*)?\w+\(([^)]+)\)", subject)
    return menu_text(match.group(1) if match else subject[:30])


def action(name, *params):
    """Only plain tokens (numbers, action names) go through: SwiftBar params have no escaping for a `"`."""
    args = " ".join(f'param{i}="{p}"' for i, p in enumerate((name, *params), start=1))
    return f'bash="{SELF}" {args} terminal=false'


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


def needs_investigation(change):
    if change["wip"]:
        return snapshot.problem(change) in snapshot.NEEDS_REBASE
    return display(change)[2] == "red" or bool(change.get("threads"))


def daemon_version(now):
    """The running daemon's version when it differs from this menu's ("?" for a daemon too old to say), else None."""
    heartbeat = read_json(DAEMON_POLL)
    if not heartbeat or now - heartbeat.get("attempt", 0) > STALE_AFTER_S:
        return None
    version = heartbeat.get("version") or "?"
    return version if version != VERSION else None


def age(seconds):
    minutes = int(seconds // 60)
    if minutes < 120:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return t("age_days", days=minutes // (24 * 60))


def print_change(change, label, symbol, color, fresh):
    tint = f" sfcolor={color}" if color else ""
    url = change["url"]
    number = str(change["number"])
    threads = change.get("threads", 0)
    if threads:
        label += t("bar_threads", count=threads)
    print(f"{number}  {scope(change['subject'])} — {menu_text(label)} | href={url} sfimage={symbol}{tint}")
    print(f"--{menu_text(change['subject'])} | disabled=true")
    if color == "red":
        for job in change.get("ci_diagnosis") or []:
            line = f"{job['job']}: {job.get('category', '?')}"
            resembles = job.get("resembles") or []
            if resembles:
                line += " · " + t("bar_resembles", change=resembles[0]["change"], fix=resembles[0].get("fix") or "?")
            link = f" href={job['log_url']}" if job.get("log_url") else " disabled=true"
            print(f"--{menu_text(line)[:120]} |{link} sfimage=doc.text.magnifyingglass")
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
    prepared = change.get("rebase_ready")
    if fresh and prepared and prepared.get("patch_set") == change.get("patch_set"):
        print("-----")
        print(f"--{t('bar_push_rebase', onto=rebase_onto(change))} | "
              f"{action('push_rebase', number, change['patch_set'])} sfimage=arrow.triangle.branch")
    if change.get("patch_set"):
        print("-----")
        print_hashtags(change, number, fresh)
        state = snapshot.state(change)
        gated = change.get("gate") is not None
        if fresh and state == "ready" and gated:
            print(f"--{t('bar_gate_vote', label=GATE_LABEL)} | {action('vote_gate', number, change['patch_set'])} "
                  "sfimage=paperplane.fill")
        elif fresh and state == "ready":
            print(f"--{t('bar_submit')} | {action('submit', number, change['patch_set'])} sfimage=paperplane.fill")
        else:
            # Always there, greyed out with what stands in the way, so a missing Submit never needs explaining.
            reason = (submit_wait(state, change) or label_of(state, change)) if fresh else t("bar_submit_stale")
            unavailable = t("bar_gate_unavailable", label=GATE_LABEL, reason=reason) if gated \
                else t("bar_submit_unavailable", reason=reason)
            print(f"--{menu_text(unavailable)} | disabled=true sfimage=paperplane")


def print_hashtags(change, number, fresh):
    """Gerrit's Auto-submit and Claude review buttons: greyed out once the hashtag is on the change."""
    for kind, tag in HASHTAGS.items():
        if not tag:
            continue
        if tag in change.get("hashtags", []):
            print(f"--{t(f'bar_{kind}_on')} | disabled=true sfimage=checkmark.circle")
        elif fresh:
            print(f"--{t(f'bar_{kind}')} | {action('hashtag', number, kind)} sfimage=number")


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


def print_review(review, now):
    """Reviewable now in plain text; one its owner will rework first greyed out with why."""
    waited = age(now - review["since"]) if review.get("since") else "?"
    owner = menu_text(review["owner"])
    if review["blocked_by"]:
        print(f"{review['number']}  {scope(review['subject'])} — {owner} · {t('bar_review_' + review['blocked_by'])} | "
              f"href={review['url']} sfimage=hourglass sfcolor=gray")
    else:
        print(f"{review['number']}  {scope(review['subject'])} — {owner} · {t('bar_review_waiting', age=waited)} | "
              f"href={review['url']} sfimage=eyeglasses")
    print(f"--{menu_text(review['subject'])} | disabled=true")
    print(f"--{t('bar_open_gerrit')} | href={review['url']} sfimage=safari")


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
    rows = sorted(((c, display(c)) for c in changes if c["number"] not in snoozed),
                  key=lambda row: ORDER.get(row[1][2], 3))
    active = [row for row in rows if not row[0]["wip"]]
    problems = sum(1 for _, (_, _, color) in rows if color == "red")
    ready = sum(1 for _, (_, _, color) in rows if color == "green")
    title = str(len(active))
    paused = menu_actions.daemon_paused()
    if paused:
        title += " · ⏸"
    elif error or stopped:
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
            print(f"{menu_text(t('bar_base_red', branch=branch, age=age(ci.red_for_s(health, now))))} | "
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
    reviews = [r for r in status.get("reviews", []) if r["number"] not in snoozed]
    if reviews:
        print("---")
        print(f"{t('bar_reviews_section', count=sum(not r['blocked_by'] for r in reviews))} | disabled=true")
        for review in reviews:
            print_review(review, now)
    print("---")
    stale_branches = status.get("cleanup") or []
    if stale_branches:
        # SwiftBar greys out a line without an action, even a submenu parent.
        print(f"{t('bar_cleanup', count=len(stale_branches))} | {action('cleanup')} sfimage=trash")
        for candidate in sorted(stale_branches, key=lambda c: c["branch"]):
            print(f"--{menu_text(cleanup_line(candidate).splitlines()[0])[:120]} | disabled=true")
        print("-----")
        print(f"--{t('bar_cleanup_all')} | {action('cleanup')} sfimage=trash")
    data_age = age(now - status["updated"]) if status.get("updated") else "?"
    if paused:
        print(f"{t('bar_paused', age=data_age)} | {action('resume_daemon')} color=orange sfimage=pause.circle")
    elif stopped:
        print(f"{t('bar_stopped', age=data_age)} | color=orange sfimage=pause.circle")
    elif error:
        print(f"{t('bar_unreachable', age=data_age)} | color=orange sfimage=wifi.exclamationmark")
        print(f"--{menu_text(error)} | disabled=true")
    else:
        pause = f"{action('pause_daemon')} tooltip=\"{t('bar_pause_hint')}\" " if paused is False else ""
        print(f"{t('bar_active')} | {pause}sfimage=dot.radiowaves.left.and.right")
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
        menu_actions.handle(sys.argv[1:])
    else:
        main()
