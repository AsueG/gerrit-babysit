#!/usr/bin/env python3
"""Block until one of my open Gerrit changes gets a new actionable event, print it as JSON, exit."""
import argparse
import concurrent.futures
import json
import os
import pathlib
import subprocess
import sys
import time
import traceback
import urllib.parse

import ci
import events
import gerrit
import known_failures
import network
import procs
import repo
import snooze
from config import CACHE, INBOX, SESSION, SKILL_DIR, STALE_AFTER_S, STATUS, SWIFTBAR_PLUGIN, atomic_write, read_json
from config import DAEMON_POLL, VERSION, launched_skill_dir
from i18n import t
from memo import PollMemo

QUERY = "owner:self status:open"
REVIEW_QUERY = "reviewer:self status:open -owner:self"
ATTENTION_QUERY = "attention:self status:open"
DRAFTS_QUERY = "has:draft status:open"
STATE = CACHE / "seen.json"
DAEMON_STATE = CACHE / "daemon-seen.json"
FLAKY = CACHE / "flaky.json"
DIAGNOSES = CACHE / "diagnoses.json"
EXCERPT_CHARS = 300
FLAKY_RETENTION_S = 90 * 86400
# Longer than the daemon's interval, so the session sees at least one newer poll before reporting.
SETTLE_S = 90
# Only these come in bursts: a reply, its vote and zuul's verdict, or a push followed by its replies.
CLUSTERED = frozenset({"message", "review_reply", "review_new_patch_set"})
MAX_BACKOFF_S = 600
# Consecutive failed polls before a lasting outage is reported, once, instead of only retried.
UNREACHABLE_AFTER = 3
SEEN_RETENTION_S = 30 * 86400


def fetch_changes():
    return gerrit.query(QUERY, "--current-patch-set", "--comments", "--dependencies", "--all-reviewers")


def fetch_reviews():
    return gerrit.query(REVIEW_QUERY, "--current-patch-set", "--all-approvals", "--all-reviewers", "--comments")


def fetch_attention():
    return sorted(row["number"] for row in gerrit.query(ATTENTION_QUERY))


def my_attention_sets():
    """{change number: {"holders", "removed"}} for my changes, None when REST fails. SSH queries carry no attention
    set; without it, waiting_for_review falls back to counting days."""
    try:
        rows = gerrit.rest_get(f"/changes/?q={urllib.parse.quote(QUERY)}&o=DETAILED_ACCOUNTS")
    except (OSError, ValueError, KeyError):
        return None
    return {row["_number"]: {"holders": events.attention_entries(row.get("attention_set")),
                             "removed": events.attention_entries(row.get("removed_from_attention_set"))}
            for row in rows}


_threads_memo = PollMemo()


def open_threads(changes):
    """{change number: threads awaiting me}; the REST call only reruns when the change moves."""
    keys = {c["number"]: (c["number"], c.get("lastUpdated")) for c in changes}
    with _threads_memo.poll() as memo:
        threads = memo.get_all(keys.values(),
                               lambda key: events.awaiting_threads(gerrit.rest_get(f"/changes/{key[0]}/comments")))
    return {number: threads[key] for number, key in keys.items()}


_requirements_memo = PollMemo()


def submit_blockers(changes):
    """{change number: unsatisfied submit requirements} for the changes the votes call ready, None when REST fails.
    SSH queries carry no requirement, and the code-owners plugin only shows up there; asked again when the change
    moves."""
    keys = {c["number"]: (c["number"], c.get("lastUpdated")) for c in changes
            if events.is_ready_to_submit(c.get("currentPatchSet", {}))}
    def fetch(key):
        return events.unsatisfied_requirements(gerrit.rest_get(f"/changes/{key[0]}?o=SUBMIT_REQUIREMENTS"))

    try:
        with _requirements_memo.poll() as memo:
            requirements = memo.get_all(keys.values(), fetch)
    except (OSError, ValueError, KeyError):
        return None
    return {number: requirements[key] for number, key in keys.items()}


def parent_statuses(changes):
    """{change number: (parent number, parent status, parent's current patch set)} — `dependsOn` lists merged
    parents too."""
    parent_of = {c["number"]: c["dependsOn"][0]["number"] for c in changes if c.get("dependsOn")}
    parents = {c["number"]: c for c in changes}
    others = sorted(set(parent_of.values()) - parents.keys())
    if others:
        query = " OR ".join(f"change:{n}" for n in others)
        parents |= {row["number"]: row for row in gerrit.query(query, "--current-patch-set")}
    return {number: (parent, parents.get(parent, {}).get("status"), parents.get(parent, {}).get("currentPatchSet", {}))
            for number, parent in parent_of.items()}


def outdated_parents(changes, statuses):
    """{change number: {parent, parent_patch_set, parent_ref, old_parent_sha, new_parent_sha}} when the open parent
    got a patch set the change is not based on yet."""
    outdated = {}
    for change in changes:
        parent, status, parent_patch_set = statuses.get(change["number"], (None, None, {}))
        base = repo.first_parent(change)
        new = (parent_patch_set or {}).get("revision")
        if status == "NEW" and base and new and base != new:
            outdated[change["number"]] = {"parent": parent, "parent_patch_set": parent_patch_set.get("number"),
                                          "parent_ref": parent_patch_set.get("ref"), "old_parent_sha": base,
                                          "new_parent_sha": new}
    return outdated


def fetch_drafts():
    """{change number: row} of the changes holding drafts I never published; {} when Gerrit is unreachable."""
    try:
        return {row["number"]: row for row in gerrit.query(DRAFTS_QUERY)}
    except (subprocess.SubprocessError, OSError, ValueError):
        return {}


def unfinished_events(changes, drafts):
    """Work an earlier session left halfway (a crash between the fix and its approval): local commits or edits Gerrit
    has not seen, and drafts I never published (see fetch_drafts), on my changes or on the ones I review."""
    local = repo.unfinished_work(changes)
    mine = {c["number"]: c for c in changes}
    for number in sorted(local.keys() | drafts.keys()):
        yield f"{number}:unfinished", {
            **events.event_base(mine.get(number) or drafts[number]), "kind": "unfinished",
            **local.get(number, {"worktree": None, "unpushed": False, "busy": False}), "drafts": number in drafts}


def threads_or_error(changes):
    """REST relies on the HTTP password, SSH does not: an expired token must not blind the whole watcher."""
    try:
        return open_threads(changes), None
    except (OSError, ValueError, KeyError) as error:
        return {}, gerrit.error_detail(error)


def base_health(changes):
    """{branch: periodic build health} for the branches my changes target, zuul unreachable included: the red base
    is watched on every poll, not only once a change of mine fails. Each branch is its own zuul round trip, so poll()
    does not pay N x their latency."""
    if not (ci.ZUUL_API and ci.PERIODIC_BUILD):
        return {}
    branches = sorted({c["branch"] for c in changes})
    if len(branches) < 2:
        return {branch: ci.base_health(branch) for branch in branches}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(branches)) as pool:
        return dict(zip(branches, pool.map(ci.base_health, branches)))


def poll():
    # Independent round trips run side by side: the SSH queries share one multiplexed connection, and the REST
    # calls overlap the local git fetch.
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
        reviews = pool.submit(fetch_reviews)
        attention = pool.submit(fetch_attention)
        attention_sets = pool.submit(my_attention_sets)
        changes = fetch_changes()
        threads_job = pool.submit(threads_or_error, changes)
        blockers = pool.submit(submit_blockers, changes)
        base_job = pool.submit(base_health, changes)
        statuses_job = pool.submit(parent_statuses, changes)
        conflicts = repo.merge_conflicts(changes)
        statuses = statuses_job.result()
        stale = repo.stale_parents(changes, statuses)
        threads, threads_error = threads_job.result()
        return {
            "changes": changes,
            "conflicts": conflicts,
            "parents": {n: parent for n, (parent, status, _) in statuses.items() if status == "NEW"},
            "stale_parents": stale,
            "outdated_parents": outdated_parents(changes, statuses),
            "threads": threads,
            "threads_error": threads_error,
            "reviews": reviews.result(),
            "attention": attention.result(),
            "attention_sets": attention_sets.result(),
            "submit_blockers": blockers.result(),
            "base_health": base_job.result(),
        }


def with_int_keys(result):
    """JSON turns the change-number keys into strings."""
    keys = ("conflicts", "parents", "stale_parents", "outdated_parents", "threads", "attention_sets", "submit_blockers")
    return {**result, **{key: {int(k): v for k, v in result[key].items()} for key in keys if result.get(key) is not None}}


class DaemonError(Exception):
    pass


def daemon_poll():
    """The daemon's latest poll when it is alive, so a session never hits Gerrit or the fetch refs a second time."""
    heartbeat = read_json(DAEMON_POLL)
    if time.time() - heartbeat.get("attempt", 0) > STALE_AFTER_S:
        return None
    if heartbeat["error"]:
        raise DaemonError(heartbeat["error"])
    return with_int_keys(heartbeat["poll"])


def daemon_alive():
    """True once the daemon's heartbeat looks fresh again. A daemon stuck in fetch retries can outlast
    STALE_AFTER_S yet still be working: re-checked right before a session's own poll persists, so a daemon that
    finishes and publishes in the meantime is not clobbered by the session's now-stale fallback."""
    return time.time() - read_json(DAEMON_POLL).get("attempt", 0) <= STALE_AFTER_S


def write_daemon_poll(result=None, error=None):
    previous = read_json(DAEMON_POLL)
    atomic_write(DAEMON_POLL, {"attempt": time.time(), "error": error, "version": VERSION, "skill_dir": str(SKILL_DIR),
                               "poll": result if error is None else previous.get("poll")})


def load_flaky():
    return read_json(FLAKY)


def record_flaky(result, now=None):
    """Keeps the flakes seen in the polled changes: their messages leave the query once the change merges."""
    now = now or time.time()
    runs = dict(run for c in result["changes"] + result.get("reviews", []) for run in ci.flaky_runs(c))
    stored = load_flaky()
    kept = {k: r for k, r in {**stored, **runs}.items() if now - r["passed_at"] < FLAKY_RETENTION_S}
    if kept.keys() != stored.keys():
        atomic_write(FLAKY, dict(sorted(kept.items())))


def ci_verdict(event):
    if event.get("ci_verdict"):
        return event["ci_verdict"]
    return event.get("message") if event.get("author_username") == gerrit.CI_USER else None


def diagnose_failures(failures, base):
    """Side by side in one bounded pool: a morning flush of red changes downloads its logs at once without flooding
    zuul. `base` is the poll's base_health: the periodic build is not asked again."""
    histories = {}
    known = known_failures.load()
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        diagnoses = [ci.submit_diagnosis(pool, {"change": event["change"], "message": verdict}, histories,
                                         event.get("known_flaky"), known) for event, verdict in failures]
        for (event, _), futures in zip(failures, diagnoses):
            event["ci_diagnosis"] = [future.result() for future in futures]
            if ci.PERIODIC_BUILD:
                event["base_build"] = base.get(event["branch"])


def job_summary(entry):
    """What the menu and the Investigate prompt show of one failed job's diagnosis."""
    summary = {key: entry[key] for key in ("job", "category", "log_url") if entry.get(key)}
    excerpt = (entry.get("failure") or entry.get("log_tail") or "").strip()
    if excerpt:
        summary["excerpt"] = excerpt[:EXCERPT_CHARS]
    if entry.get("resembles"):
        summary["resembles"] = [{key: r.get(key) for key in ("change", "cause", "fix")} for r in entry["resembles"]]
    return summary


def red_verdicts(changes):
    """{"<change>:<patch set>:<verdict time>": (change, verdict)} of my changes whose current patch set failed: a
    recheck's new verdict is diagnosed again."""
    red = {}
    for change in changes:
        patch_set, _, _, verdict = events.ci_reading(change)
        if verdict:
            red[f"{change['number']}:{patch_set.get('number')}:{verdict['timestamp']}"] = (change, verdict)
    return red


def diagnose_red(result):
    """{change number: [job summary]} of my red changes, each verdict diagnosed once and kept while the change is
    open: the snapshot then tells why a change is red, not only that it is."""
    if not ci.ZUUL_API:
        return {}
    stored = read_json(DIAGNOSES)
    red = red_verdicts(result["changes"])
    missing = [(key, {**events.event_base(change), "kind": "ci_red", "ci_verdict": verdict["message"]})
               for key, (change, verdict) in red.items() if key not in stored]
    enrich([event for _, event in missing], result.get("base_health"))
    found = {key: {"diagnosed_at": time.time(), "jobs": [job_summary(d) for d in event.get("ci_diagnosis", [])]}
             for key, event in missing}
    open_changes = {str(c["number"]) for c in result["changes"]}
    kept = {key: entry for key, entry in {**stored, **found}.items() if key.split(":", 1)[0] in open_changes}
    if kept != stored:
        atomic_write(DIAGNOSES, dict(sorted(kept.items())))
    return {int(key.split(":", 1)[0]): kept[key]["jobs"] for key in red}


def rebase(event):
    try:
        return {"rebase": repo.prepare_rebase(event)}
    except (subprocess.SubprocessError, OSError) as error:
        return {"rebase": {"status": "error", "detail": gerrit.error_detail(error)}}


def interdiff(event):
    return {"interdiff": repo.interdiff(event)} if event.get("since_ref") and event.get("current_ref") else {}


def zuul_queue(event):
    return {"zuul_queue": ci.zuul_queue(event["change"], event["patch_set"])} if ci.ZUUL_API else {}


def prereview(event):
    if event.get("participated"):
        return {}
    path = CACHE / "prereview" / f"{event['change']}-{event['patch_set']}.md"
    return {"prereview": {"path": str(path), "done": path.exists()}}


# Run one event after the other: they work on the local repo, where concurrent git commands fight over locks.
EXTRAS = {"parent_merged": rebase, "parent_updated": rebase, "review_new_patch_set": interdiff,
          "ci_stuck": zuul_queue, "review_requested": prereview}


def enrich(reported, base=None):
    """Network-bound extras, only for events about to be reported, so the idle loop stays free."""
    failures = [(event, verdict) for event in reported if (verdict := ci_verdict(event))]
    flakes = load_flaky()
    for event, verdict in failures:
        flaky = ci.flaky_counts(flakes, [j["job"] for j in ci.failed_jobs(verdict)])
        if flaky:
            event["known_flaky"] = flaky
    if failures and ci.ZUUL_API:
        diagnose_failures(failures, base or {})
    for event in reported:
        if event["kind"] in EXTRAS:
            event.update(EXTRAS[event["kind"]](event))
    return reported


def write_status(result=None, error=None, listing=None):
    """Snapshot read by the SwiftBar plugin and the Claude Code status line, so they never hit Gerrit themselves.

    On error the last good rows are kept and `updated` stays at the last success. `listing` is `git worktree list`
    when the caller already ran it."""
    now = time.time()
    if error is not None:
        previous = read_json(STATUS)
        atomic_write(STATUS, {**previous, "last_attempt": now, "last_error": error})
        return
    try:
        diagnoses = diagnose_red(result)
    # The why of a red change is a bonus: a bug there must not leave the menu without a snapshot.
    except Exception as error:  # noqa: BLE001
        print(f"gerrit-babysit: CI diagnosis skipped: {type(error).__name__}: {error}", file=sys.stderr)
        diagnoses = {}
    rows = events.status_rows(result, load_flaky(), repo.worktrees_by_change_id(listing), now, diagnoses)
    atomic_write(STATUS, {"updated": now, "last_attempt": now, "last_error": None,
                          "threads_error": result.get("threads_error"), "changes": rows,
                          "base_health": result.get("base_health", {})})


def snoozed_changes(result, now):
    patch_sets = {c["number"]: c.get("currentPatchSet", {}).get("number")
                  for c in result["changes"] + result.get("reviews", [])}
    return snooze.active(patch_sets, now, base_red=snooze.red_base(result["changes"], result.get("base_health", {})))


def awake(pairs, snoozed):
    """Events of snoozed changes are left out, hence left unseen: they come back once the snooze ends. A red base
    only names the changes still awake, and waits while all of them sleep."""
    kept = {}
    for key, event in pairs:
        if event["kind"] == "base_red":
            event = {**event, "changes": [n for n in event["changes"] if n not in snoozed]}
            if not event["changes"]:
                continue
        elif event.get("change") in snoozed:
            continue
        kept[key] = event
    return kept


def remember(seen, keys, result, current, now):
    """Marks `keys` seen. Keys of changes gone from both queries (merged, abandoned) are kept for a while, so a
    change restored soon after does not replay its history, then dropped so the seen files stop growing."""
    alive = {str(c["number"]) for c in result["changes"] + result.get("reviews", [])}
    live = lambda key: key in current or key.split(":", 1)[0] in alive
    return {key: now if live(key) else stamp
            for key, stamp in {**seen, **dict.fromkeys(keys, now)}.items()
            if live(key) or now - stamp < SEEN_RETENTION_S}


def claude_pid():
    """The Claude session running this watcher, found by walking up the process tree."""
    table = procs.processes("comm")
    return next((pid for pid in procs.ancestors(os.getppid(), table)
                 if pathlib.Path(table[pid][1].strip()).name == "claude"), None)


def write_session_lock():
    """Read by macos/open-babysit.sh: a notification click focuses this session instead of opening a second one."""
    pid = claude_pid()
    if pid:
        atomic_write(SESSION, {"claude_pid": pid, "orca_terminal": os.environ.get("ORCA_TERMINAL_HANDLE", "")})


def load_seen(state=None):
    """{event key: last time its change was alive}."""
    return read_json(state or STATE)


def save_seen(seen, state=None):
    atomic_write(state or STATE, dict(sorted(seen.items())))


def swiftbar(action, **params):
    """Best effort: a hung SwiftBar must not kill the daemon before it saves what it just notified."""
    try:
        subprocess.run(["open", "-g", f"swiftbar://{action}?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"],
                       capture_output=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        pass


def superseded():
    """True once the launcher (macos/launch.py) would run another install than this one: a plugin update."""
    launcher = os.environ.get("GERRIT_BABYSIT_LAUNCHER")
    if not launcher or not pathlib.Path(launcher).is_file():
        return False
    return launched_skill_dir(launcher) != SKILL_DIR


def outage_key():
    """One per outage: while polls fail, the snapshot's `updated` stays at the last success."""
    return f"unreachable:{read_json(STATUS).get('updated') or 0}"


def unreachable_event(detail, failures):
    return {"kind": "unreachable", "detail": detail, "failures": failures, "since": read_json(STATUS).get("updated"),
            "network": network.checks()}


def notify_unreachable(detail, now):
    """Once per outage, in working hours: past them, the next working day's first failed poll tells."""
    seen = load_seen(DAEMON_STATE)
    key = outage_key()
    if events.is_quiet(now) or key in seen:
        return
    swiftbar("notify", plugin=SWIFTBAR_PLUGIN, title=t("unreachable", host=gerrit.HOST),
             body=t("unreachable_body", detail=detail))
    save_seen({**seen, key: now}, DAEMON_STATE)


def daemon(interval):
    """Status snapshot + notifications with no Claude session; clicking a notification opens one."""
    first_run = not DAEMON_STATE.exists()
    last_error = None
    failures = 0

    def failed(error, expected):
        """Logged once per distinct error: a VPN left off overnight would otherwise add a line a minute."""
        nonlocal last_error
        detail = gerrit.error_detail(error) if expected else f"{type(error).__name__}: {error}"
        if detail != last_error:
            log = f"poll failed: {detail}" if expected else traceback.format_exc()
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {log}", file=sys.stderr, flush=True)
            last_error = detail
        write_status(error=detail)
        swiftbar("refreshplugin", name=SWIFTBAR_PLUGIN)
        return detail

    while True:
        started = time.monotonic()
        # The poll's own duration (a slow fetch) comes off the wait, so a round still starts every `interval`.
        pause = lambda started=started: time.sleep(max(0, interval - (time.monotonic() - started)))
        try:
            result = poll()
        # A bug on one odd change must not turn into a silent launchd crash loop.
        except Exception as error:  # noqa: BLE001
            expected = isinstance(error, (subprocess.SubprocessError, OSError, ValueError))
            detail = failed(error, expected)
            write_daemon_poll(error=detail)
            failures = failures + 1 if expected else 0
            # Not on a first install: a seen file saved now would turn the first good poll into a notification flood.
            if failures >= UNREACHABLE_AFTER and not first_run:
                notify_unreachable(detail, time.time())
            pause()
            continue
        failures = 0
        try:
            daemon_round(result, first_run)
            first_run = False
            if last_error is not None:
                print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} recovered", file=sys.stderr, flush=True)
                last_error = None
        except Exception as error:  # noqa: BLE001
            failed(error, expected=False)
        if superseded():
            # launchd's KeepAlive starts it again through the launcher, on the new version.
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} a newer install replaced {SKILL_DIR}: restarting",
                  file=sys.stderr, flush=True)
            return 0
        pause()


def daemon_round(result, first_run):
    # Published first: a bug further down must not starve the session of a poll that is fine.
    write_daemon_poll(result)
    write_status(result)
    swiftbar("refreshplugin", name=SWIFTBAR_PLUGIN)

    now = time.time()
    current = awake(events.events(result, now), snoozed_changes(result, now))
    seen = load_seen(DAEMON_STATE)
    quiet = events.is_quiet(now)
    if not first_run and not quiet:
        for title, body, href in events.notifications((current[key] for key in sorted(current.keys() - seen.keys())), now):
            swiftbar("notify", plugin=SWIFTBAR_PLUGIN, title=title, body=body, href=href)
    # Held back at night and on weekends: still unseen, they notify at the start of the next working day.
    marked = () if quiet and not first_run else current.keys()
    save_seen(remember(seen, marked, result, current, now), DAEMON_STATE)
    record_flaky(result, now)


def session_poll():
    """(result, now, snoozed, cleanup): the daemon's poll when it runs, else one of our own."""
    result = daemon_poll()
    own_poll = result is None
    if own_poll:
        result = poll()
    # One listing for the snapshot and the cleanup check.
    listing = repo.worktree_listing()
    # A slow-but-alive daemon may have finished and published while our own poll ran; its write wins.
    if own_poll and not daemon_alive():
        write_status(result, listing=listing)
        record_flaky(result)
    cleanup = dict(repo.cleanup_candidates(frozenset(c["id"] for c in result["changes"]), listing))
    now = time.time()
    return result, now, snoozed_changes(result, now), cleanup


def pending_report(result, now, snoozed, current):
    """The start-up sweep over `current`, the poll's awake events and cleanup candidates. The drafts query overlaps
    the extras; the local git work stays one thing at a time."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        drafts = pool.submit(fetch_drafts)
        sweep = events.pending_events(result, now, current.values())
        report = enrich(list(awake(enumerate(sweep), snoozed).values()), result.get("base_health"))
        unfinished = awake(unfinished_events(result["changes"], drafts.result()), snoozed)
    report = events.by_urgency(list(unfinished.values()) + report)
    threads_error = result.get("threads_error")
    if threads_error is None and not result["changes"]:
        # No open change, no REST call in the poll: check the HTTP password before a change needs it.
        threads_error = gerrit.http_error()
    return {"status": "pending", "events": report, "nudges": events.nudges(report),
            "threads_error": threads_error, "snoozed": snoozed}


def events_report(fresh, result):
    # A snooze ending or a morning catch-up can flush many at once: most urgent first.
    report = events.by_urgency(enrich(list(fresh.values()), result.get("base_health")))
    return {"status": "events", "events": report, "nudges": events.nudges(report),
            "threads_error": result.get("threads_error")}


def emit(report):
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


def deliver(report, now):
    """Kept in the inbox before the events are marked seen: a session that relaunches without reading the output
    gets the report again instead of losing it."""
    report = {**report, "id": str(int(now * 1000))}
    atomic_write(INBOX, {"id": report["id"], "reported_at": now, "report": report})
    return report


def unacknowledged(ack):
    """The inbox's report unless `ack` names it; an acknowledged one is dropped."""
    inbox = read_json(INBOX)
    if not inbox:
        return None
    if inbox.get("id") == ack:
        INBOX.unlink(missing_ok=True)
        return None
    return {**inbox["report"], "replayed": True}


def main():
    if not gerrit.HOST:
        sys.exit("gerrit-babysit: set gerrit_host in config.json (see config.example.json)")
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--pending", action="store_true",
                        help="print what needs me now, mark everything current as seen and exit")
    parser.add_argument("--daemon", action="store_true", help="run forever: status snapshot + macOS notifications")
    parser.add_argument("--ack", help="id of the last report, handled: without it, that report is printed again")
    args = parser.parse_args()
    if args.daemon:
        return daemon(args.interval)

    write_session_lock()
    if not args.pending:
        replay = unacknowledged(args.ack)
        if replay:
            return emit(replay)
    failures = 0
    settling = {}
    while True:
        try:
            result, now, snoozed, cleanup = session_poll()
            current = awake(events.events(result, now), snoozed) | cleanup
            failures = 0
        except (subprocess.SubprocessError, OSError, ValueError, DaemonError) as error:
            detail = gerrit.error_detail(error)
            if not isinstance(error, DaemonError):
                write_status(error=detail)
            if args.pending:
                print(json.dumps({"status": "error", "detail": detail, "network": network.checks()}))
                return 1
            # Never gives up: a VPN off over lunch or overnight must not end the babysitting. A lasting outage
            # wakes the session once, so it can say so, then the relaunched watcher retries in silence.
            failures += 1
            if failures >= UNREACHABLE_AFTER and outage_key() not in (seen := load_seen()):
                report = {"status": "events", "events": [unreachable_event(detail, failures)], "nudges": [],
                          "threads_error": None}
                report = deliver(report, time.time())
                save_seen({**seen, outage_key(): time.time()})
                return emit(report)
            time.sleep(min(args.interval * failures, MAX_BACKOFF_S))
            continue

        seen = load_seen()
        if args.pending:
            # Same poll for the report and the seen set: nothing can slip in between.
            report = pending_report(result, now, snoozed, current)
            save_seen(remember(seen, current.keys(), result, current, now))
            return emit(report)

        fresh = {key: current[key] for key in current.keys() - seen.keys()}
        if not settling and any(event["kind"] in CLUSTERED for event in fresh.values()):
            # Wait once to wake for the whole burst; the other kinds have nothing following them, so they go now.
            settling = fresh
            time.sleep(SETTLE_S)
            continue
        settling = {key: current.get(key, event) for key, event in settling.items()} | fresh
        if settling:
            # Enriched before being marked seen: a crash in the extras must not swallow the events.
            report = deliver(events_report(settling, result), now)
            save_seen(remember(seen, settling.keys(), result, current, now))
            return emit(report)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
