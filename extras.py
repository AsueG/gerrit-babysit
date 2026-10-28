"""Network-bound extras added to the events about to be reported: CI diagnosis, known flakes, local rebase, interdiff."""

import concurrent.futures
import subprocess
import time

import ci
import gerrit
import known_failures
import repo
from config import CACHE, atomic_write, read_json

FLAKY = CACHE / "flaky.json"
FLAKY_RETENTION_S = 90 * 86400


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
        diagnoses = [
            ci.submit_diagnosis(pool, {"change": event["change"], "message": verdict}, histories, event.get("known_flaky"), known)
            for event, verdict in failures
        ]
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
EXTRAS = {
    "parent_merged": rebase,
    "parent_updated": rebase,
    "review_new_patch_set": interdiff,
    "ci_stuck": zuul_queue,
    "review_requested": prereview,
}


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
