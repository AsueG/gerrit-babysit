"""The background loop behind `watch.py --daemon`: status snapshot + macOS notifications with no Claude session.

Started by launchd through macos/launch.py; clicking a notification opens a session."""

import os
import pathlib
import subprocess
import sys
import time
import traceback
import urllib.parse

import events
import gerrit
import repo
import watch
from config import CACHE, CONFIG, DAEMON_POLL, SKILL_DIR, SWIFTBAR_PLUGIN, VERSION, atomic_write, launched_skill_dir
from config import read_json
from extras import record_flaky
from i18n import t
from poll import poll
from status_writer import write_status

DAEMON_STATE = CACHE / "daemon-seen.json"
AUTO_SUBMIT_TAGGED = CACHE / "auto-submit-tagged.json"


def write_daemon_poll(result=None, error=None):
    previous = read_json(DAEMON_POLL)
    atomic_write(
        DAEMON_POLL,
        {
            "attempt": time.time(),
            "error": error,
            "version": VERSION,
            "skill_dir": str(SKILL_DIR),
            "poll": result if error is None else previous.get("poll"),
        },
    )


def swiftbar(action, **params):
    """Best effort: a hung SwiftBar must not kill the daemon before it saves what it just notified."""
    try:
        subprocess.run(
            ["open", "-g", f"swiftbar://{action}?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"],
            capture_output=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        pass


def superseded():
    """True once the launcher (macos/launch.py) would run another install than this one: a plugin update."""
    launcher = os.environ.get("GERRIT_BABYSIT_LAUNCHER")
    if not launcher or not pathlib.Path(launcher).is_file():
        return False
    return launched_skill_dir(launcher) != SKILL_DIR


def notify_unreachable(detail, now):
    """Once per outage, in working hours: past them, the next working day's first failed poll tells."""
    seen = watch.load_seen(DAEMON_STATE)
    key = watch.outage_key()
    if events.is_quiet(now) or key in seen:
        return
    swiftbar("notify", plugin=SWIFTBAR_PLUGIN, title=t("unreachable", host=gerrit.HOST), body=t("unreachable_body", detail=detail))
    watch.save_seen({**seen, key: now}, DAEMON_STATE)


def log(line):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}", file=sys.stderr, flush=True)


def run(interval):
    first_run = not DAEMON_STATE.exists()
    last_error = None
    failures = 0

    def failed(error, expected):
        """Logged once per distinct error: a VPN left off overnight would otherwise add a line a minute."""
        nonlocal last_error
        detail = gerrit.error_detail(error) if expected else f"{type(error).__name__}: {error}"
        if detail != last_error:
            log(f"poll failed: {detail}" if expected else traceback.format_exc())
            last_error = detail
        write_status(error=detail)
        swiftbar("refreshplugin", name=SWIFTBAR_PLUGIN)
        return detail

    while True:
        started = time.monotonic()
        # The poll's own duration (a slow fetch) comes off the wait, so a round still starts every `interval`.
        pause = lambda every=interval, started=started: time.sleep(max(0, every - (time.monotonic() - started)))
        try:
            result = poll()
        # A bug on one odd change must not turn into a silent launchd crash loop.
        except Exception as error:  # noqa: BLE001
            expected = isinstance(error, (subprocess.SubprocessError, OSError, ValueError))
            detail = failed(error, expected)
            write_daemon_poll(error=detail)
            failures = failures + 1 if expected else 0
            # Not on a first install: a seen file saved now would turn the first good poll into a notification flood.
            if failures >= watch.UNREACHABLE_AFTER and not first_run:
                notify_unreachable(detail, time.time())
            pause(watch.session_cap_wait(detail, interval))
            continue
        failures = 0
        try:
            daemon_round(result, first_run)
            first_run = False
            if last_error is not None:
                log("recovered")
                last_error = None
        except Exception as error:  # noqa: BLE001
            failed(error, expected=False)
        if superseded():
            # launchd's KeepAlive starts it again through the launcher, on the new version.
            log(f"a newer install replaced {SKILL_DIR}: restarting")
            return 0
        pause()


def daemon_round(result, first_run):
    # Published first: a bug further down must not starve the session of a poll that is fine.
    write_daemon_poll(result)
    write_status(result, listing=repo.worktree_listing())
    swiftbar("refreshplugin", name=SWIFTBAR_PLUGIN)

    now = time.time()
    current = watch.awake(events.events(result, now), watch.snoozed_changes(result, now))
    seen = watch.load_seen(DAEMON_STATE)
    quiet = events.is_quiet(now)
    if not first_run and not quiet:
        for title, body, href in events.notifications((current[key] for key in sorted(current.keys() - seen.keys())), now):
            swiftbar("notify", plugin=SWIFTBAR_PLUGIN, title=title, body=body, href=href)
    # Held back at night and on weekends: still unseen, they notify at the start of the next working day.
    marked = () if quiet and not first_run else current.keys()
    watch.save_seen(watch.remember(seen, marked, result, current, now), DAEMON_STATE)
    record_flaky(result, now)
    tag_auto_submit(result)


def tag_auto_submit(result):
    """Each ready change of mine gets the auto-submit hashtag once: removing it by hand keeps it off."""
    tag = CONFIG["auto_submit_hashtag"]
    if not (CONFIG["auto_submit_all"] and tag):
        return
    tagged = read_json(AUTO_SUBMIT_TAGGED)
    open_numbers = {str(c["number"]) for c in result["changes"]}
    kept = {n: True for n in tagged if n in open_numbers}
    for change in result["changes"]:
        number = str(change["number"])
        if change.get("wip") or number in kept:
            continue
        if tag not in change.get("hashtags", []):
            try:
                gerrit.rest_post(f"/changes/{number}/hashtags", {"add": [tag]})
            except (OSError, ValueError, KeyError) as error:
                log(f"could not add {tag} to {number}: {gerrit.error_detail(error)}")
                continue
            log(f"added {tag} to {number}")
        kept[number] = True
    if kept != tagged:
        atomic_write(AUTO_SUBMIT_TAGGED, dict(sorted(kept.items())))
