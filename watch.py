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
import procs
import repo
import snooze
from config import CACHE, SESSION, STALE_AFTER_S, STATUS, SWIFTBAR_PLUGIN, atomic_write, read_json
from memo import PollMemo

QUERY = "owner:self status:open"
REVIEW_QUERY = "reviewer:self status:open -owner:self"
ATTENTION_QUERY = "attention:self status:open"
DRAFTS_QUERY = "has:draft status:open"
STATE = CACHE / "seen.json"
DAEMON_STATE = CACHE / "daemon-seen.json"
DAEMON_POLL = CACHE / "daemon-poll.json"
FLAKY = CACHE / "flaky.json"
FLAKY_RETENTION_S = 90 * 86400
# Longer than the daemon's interval, so the session sees at least one newer poll before reporting.
SETTLE_S = 90
MAX_BACKOFF_S = 600
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
        missing = [key for key in keys.values() if key not in memo]
        fetched = {}
        if missing:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                fetched = dict(zip(missing, pool.map(
                    lambda key: events.awaiting_threads(gerrit.rest_get(f"/changes/{key[0]}/comments")), missing)))
        return {number: memo.get(key, fetched.get, key) for number, key in keys.items()}


_requirements_memo = PollMemo()


def submit_blockers(changes):
    """{change number: unsatisfied submit requirements} for the changes the votes call ready, None when REST fails.
    SSH queries carry no requirement, and the code-owners plugin only shows up there; asked again when the change
    moves."""
    keys = {c["number"]: (c["number"], c.get("lastUpdated")) for c in changes
            if events.is_ready_to_submit(c.get("currentPatchSet", {}))}
    def fetch(number):
        return events.unsatisfied_requirements(gerrit.rest_get(f"/changes/{number}?o=SUBMIT_REQUIREMENTS"))

    try:
        with _requirements_memo.poll() as memo:
            return {number: memo.get(key, fetch, number) for number, key in keys.items()}
    except (OSError, ValueError, KeyError):
        return None


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
        base = change.get("currentPatchSet", {}).get("parents", [None])[0]
        new = (parent_patch_set or {}).get("revision")
        if status == "NEW" and base and new and base != new:
            outdated[change["number"]] = {"parent": parent, "parent_patch_set": parent_patch_set.get("number"),
                                          "parent_ref": parent_patch_set.get("ref"), "old_parent_sha": base,
                                          "new_parent_sha": new}
    return outdated


def unfinished_events(changes):
    """Work an earlier session left halfway (a crash between the fix and its approval): local commits or edits Gerrit
    has not seen, and drafts I never published, on my changes or on the ones I review."""
    local = repo.unfinished_work(changes)
    try:
        drafts = {row["number"]: row for row in gerrit.query(DRAFTS_QUERY)}
    except (subprocess.SubprocessError, OSError, ValueError):
        drafts = {}
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
    is watched on every poll, not only once a change of mine fails."""
    if not (ci.ZUUL_API and ci.PERIODIC_BUILD):
        return {}
    return {branch: ci.base_health(branch) for branch in sorted({c["branch"] for c in changes})}


def poll():
    # Independent round trips run side by side: the SSH queries share one multiplexed connection, and the REST
    # calls overlap the local git fetch.
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        reviews = pool.submit(fetch_reviews)
        attention = pool.submit(fetch_attention)
        attention_sets = pool.submit(my_attention_sets)
        changes = fetch_changes()
        threads_job = pool.submit(threads_or_error, changes)
        blockers = pool.submit(submit_blockers, changes)
        base_job = pool.submit(base_health, changes)
        statuses = parent_statuses(changes)
        conflicts = repo.merge_conflicts(changes)
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


def write_daemon_poll(result=None, error=None):
    previous = read_json(DAEMON_POLL)
    atomic_write(DAEMON_POLL, {"attempt": time.time(), "error": error,
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


def write_status(result=None, error=None):
    """Snapshot read by the SwiftBar plugin and the Claude Code status line, so they never hit Gerrit themselves.

    On error the last good rows are kept and `updated` stays at the last success."""
    now = time.time()
    if error is not None:
        previous = read_json(STATUS)
        atomic_write(STATUS, {**previous, "last_attempt": now, "last_error": error})
        return
    rows = events.status_rows(result, load_flaky(), repo.worktrees_by_change_id(), now)
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


def daemon(interval):
    """Status snapshot + notifications with no Claude session; clicking a notification opens one."""
    first_run = not DAEMON_STATE.exists()
    last_error = None

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
        try:
            result = poll()
        # A bug on one odd change must not turn into a silent launchd crash loop.
        except Exception as error:  # noqa: BLE001
            expected = isinstance(error, (subprocess.SubprocessError, OSError, ValueError))
            write_daemon_poll(error=failed(error, expected))
            time.sleep(interval)
            continue
        try:
            daemon_round(result, first_run)
            first_run = False
            if last_error is not None:
                print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} recovered", file=sys.stderr, flush=True)
                last_error = None
        except Exception as error:  # noqa: BLE001
            failed(error, expected=False)
        time.sleep(interval)


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


def main():
    if not gerrit.HOST:
        sys.exit("gerrit-babysit: set gerrit_host in config.json (see config.example.json)")
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--pending", action="store_true",
                        help="print what needs me now, mark everything current as seen and exit")
    parser.add_argument("--daemon", action="store_true", help="run forever: status snapshot + macOS notifications")
    args = parser.parse_args()
    if args.daemon:
        return daemon(args.interval)

    write_session_lock()
    failures = 0
    settling = {}
    while True:
        try:
            result = daemon_poll()
            if result is None:
                result = poll()
                write_status(result)
                record_flaky(result)
            cleanup = dict(repo.cleanup_candidates(frozenset(c["id"] for c in result["changes"])))
            now = time.time()
            snoozed = snoozed_changes(result, now)
            current = awake(events.events(result, now), snoozed) | cleanup
            failures = 0
        except (subprocess.SubprocessError, OSError, ValueError, DaemonError) as error:
            if not isinstance(error, DaemonError):
                write_status(error=gerrit.error_detail(error))
            if args.pending:
                print(json.dumps({"status": "error", "detail": gerrit.error_detail(error)}))
                return 1
            # Never gives up: a VPN off over lunch or overnight must not end the babysitting.
            failures += 1
            time.sleep(min(args.interval * failures, MAX_BACKOFF_S))
            continue

        seen = load_seen()
        if args.pending:
            # Same poll for the report and the seen set: nothing can slip in between.
            report = enrich(list(awake(enumerate(events.pending_events(result, now)), snoozed).values())
                            + list(cleanup.values()), result.get("base_health"))
            report = events.by_urgency(list(awake(unfinished_events(result["changes"]), snoozed).values()) + report)
            save_seen(remember(seen, current.keys(), result, current, now))
            threads_error = result.get("threads_error")
            if threads_error is None and not result["changes"]:
                # No open change, no REST call in the poll: check the HTTP password before a change needs it.
                threads_error = gerrit.http_error()
            print(json.dumps({"status": "pending", "events": report, "nudges": events.nudges(report),
                              "threads_error": threads_error, "snoozed": snoozed}, ensure_ascii=False, indent=1))
            return 0

        fresh = {key: current[key] for key in current.keys() - seen.keys()}
        if fresh and not settling:
            # A reply, its vote and zuul's verdict often land a minute apart: wait once to wake for all of them.
            settling = fresh
            time.sleep(SETTLE_S)
            continue
        settling = {key: current.get(key, event) for key, event in settling.items()} | fresh
        if settling:
            # Enriched before being marked seen: a crash in the extras must not swallow the events.
            # A snooze ending or a morning catch-up can flush many at once: most urgent first.
            report = events.by_urgency(enrich(list(settling.values()), result.get("base_health")))
            save_seen(remember(seen, settling.keys(), result, current, now))
            print(json.dumps({"status": "events", "events": report, "nudges": events.nudges(report),
                              "threads_error": result.get("threads_error")}, ensure_ascii=False, indent=1))
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
