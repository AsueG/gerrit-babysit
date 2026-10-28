#!/usr/bin/env python3
"""Block until one of my open Gerrit changes gets a new actionable event, print it as JSON, exit."""

import argparse
import concurrent.futures
import itertools
import json
import os
import pathlib
import subprocess
import sys
import time

import events
import gerrit
import network
import procs
import repo
import snooze
from config import CACHE, DAEMON_POLL, INBOX, SESSION, STALE_AFTER_S, STATUS, atomic_write, read_json
from extras import enrich, record_flaky
from poll import fetch_drafts, poll, with_int_keys
from status_writer import fixed_failures, snapshot_cleanup, write_status

STATE = CACHE / "seen.json"
# Longer than the daemon's interval, so the session sees at least one newer poll before reporting.
SETTLE_S = 90
# Only these come in bursts: a reply, its vote and zuul's verdict, or a push followed by its replies.
CLUSTERED = frozenset({"message", "review_reply", "review_new_patch_set"})
MAX_BACKOFF_S = 600
SESSION_CAP_BACKOFF_S = 600
# Consecutive failed polls before a lasting outage is reported, once, instead of only retried.
UNREACHABLE_AFTER = 3
SEEN_RETENTION_S = 30 * 86400


def unfinished_events(changes, drafts):
    """Work an earlier session left halfway (a crash between the fix and its approval): local commits or edits Gerrit
    has not seen, and drafts I never published (see fetch_drafts), on my changes or on the ones I review."""
    local = repo.unfinished_work(changes)
    mine = {c["number"]: c for c in changes}
    for number in sorted(local.keys() | drafts.keys()):
        yield (
            f"{number}:unfinished",
            {
                **events.event_base(mine.get(number) or drafts[number]),
                "kind": "unfinished",
                **local.get(number, {"worktree": None, "unpushed": False, "busy": False}),
                "drafts": number in drafts,
            },
        )


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


def snoozed_changes(result, now):
    patch_sets = {c["number"]: c.get("currentPatchSet", {}).get("number") for c in result["changes"] + result.get("reviews", [])}
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
    return {
        key: now if live(key) else stamp
        for key, stamp in {**seen, **dict.fromkeys(keys, now)}.items()
        if live(key) or now - stamp < SEEN_RETENTION_S
    }


def claude_pid():
    """The Claude session running this watcher, found by walking up the process tree."""
    table = procs.processes("comm")
    return next((pid for pid in procs.ancestors(os.getppid(), table) if pathlib.Path(table[pid][1].strip()).name == "claude"), None)


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


def outage_key():
    """One per outage: while polls fail, the snapshot's `updated` stays at the last success."""
    return f"unreachable:{read_json(STATUS).get('updated') or 0}"


def unreachable_event(detail, failures):
    return {
        "kind": "unreachable",
        "detail": detail,
        "failures": failures,
        "since": read_json(STATUS).get("updated"),
        "network": network.checks(),
    }


def session_cap_wait(detail, wait):
    """Each retry only meets the cap again: the lingering sessions take minutes to time out, not seconds."""
    return max(wait, SESSION_CAP_BACKOFF_S) if gerrit.SESSION_CAP in detail else wait


def session_poll():
    """(result, now, snoozed, cleanup): the daemon's poll when it runs, else one of our own."""
    result = daemon_poll()
    own_poll = result is None
    if own_poll:
        result = poll()
    # A slow-but-alive daemon may have finished and published while our own poll ran; its write wins.
    if own_poll and not daemon_alive():
        write_status(result, listing=repo.worktree_listing())
        record_flaky(result)
    # Whoever wrote the snapshot ran the cleanup check: not paid a second time.
    now = time.time()
    return result, now, snoozed_changes(result, now), snapshot_cleanup()


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
    return {"status": "pending", "events": report, "nudges": events.nudges(report), "threads_error": threads_error, "snoozed": snoozed}


def events_report(fresh, result):
    # A snooze ending or a morning catch-up can flush many at once: most urgent first.
    report = events.by_urgency(enrich(list(fresh.values()), result.get("base_health")))
    return {"status": "events", "events": report, "nudges": events.nudges(report), "threads_error": result.get("threads_error")}


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


def settle(settling, fresh, current):
    """(events due now, events held back): due is None while a burst is still settling. A burst waits once, for the
    whole of it to wake the session together; the other kinds have nothing following them, so they go at once. Held
    events take their latest version from `current`."""
    if not settling and any(event["kind"] in CLUSTERED for event in fresh.values()):
        return None, fresh
    return {key: current.get(key, event) for key, event in settling.items()} | fresh, {}


def main():
    if not gerrit.HOST:
        sys.exit("gerrit-babysit: set gerrit_host in config.json (see config.example.json)")
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--pending", action="store_true", help="print what needs me now, mark everything current as seen and exit")
    parser.add_argument("--daemon", action="store_true", help="run forever: status snapshot + macOS notifications")
    parser.add_argument("--ack", help="id of the last report, handled: without it, that report is printed again")
    args = parser.parse_args()
    if args.daemon:
        # Imported on use: daemon.py imports this module.
        import daemon

        return daemon.run(args.interval)

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
            current = awake(itertools.chain(events.events(result, now), fixed_failures(result)), snoozed) | cleanup
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
                report = {"status": "events", "events": [unreachable_event(detail, failures)], "nudges": [], "threads_error": None}
                report = deliver(report, time.time())
                save_seen({**seen, outage_key(): time.time()})
                return emit(report)
            time.sleep(session_cap_wait(detail, min(args.interval * failures, MAX_BACKOFF_S)))
            continue

        seen = load_seen()
        if args.pending:
            # Same poll for the report and the seen set: nothing can slip in between.
            report = pending_report(result, now, snoozed, current)
            save_seen(remember(seen, current.keys(), result, current, now))
            return emit(report)

        due, settling = settle(settling, {key: current[key] for key in current.keys() - seen.keys()}, current)
        if due is None:
            time.sleep(SETTLE_S)
            continue
        if due:
            # Enriched before being marked seen: a crash in the extras must not swallow the events.
            report = deliver(events_report(due, result), now)
            save_seen(remember(seen, due.keys(), result, current, now))
            return emit(report)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
