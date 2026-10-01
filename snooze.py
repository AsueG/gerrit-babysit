#!/usr/bin/env python3
"""Snoozed changes: their events are held back, not dropped, until a date, their next patch set or a green base."""
import argparse
import datetime
import json
import sys
import time

from config import CACHE, CONFIG, atomic_write, read_json

SNOOZE = CACHE / "snooze.json"


def load():
    return {int(k): v for k, v in read_json(SNOOZE).items()}


def save(entries):
    atomic_write(SNOOZE, {str(n): e for n, e in sorted(entries.items())})


def is_active(entry, patch_set, base_red, now):
    if entry.get("base_green"):
        return base_red
    if entry.get("patch_set") is not None:
        return patch_set is not None and int(patch_set) <= int(entry["patch_set"])
    return now < entry.get("until", 0)


def active(patch_sets, now=None, entries=None, base_red=frozenset()):
    """{change number: entry} for the snoozes still holding, given {change number: current patch set} and the
    changes whose base is red (see red_base).

    A change missing from `patch_sets` (merged, abandoned) is not snoozed anymore."""
    now = now or time.time()
    entries = load() if entries is None else entries
    return {n: e for n, e in entries.items() if n in patch_sets and is_active(e, patch_sets[n], n in base_red, now)}


def red_base(changes, base_health):
    """Numbers of the changes whose target branch is red. An unreadable build (zuul down) counts as red: a snooze
    must not end on a network error."""
    def holds(health):
        return bool(health) and (health.get("result") == "FAILURE" or "error" in health)
    return {c["number"] for c in changes if holds(base_health.get(c.get("branch")))}


def snapshot_active(status, now=None):
    """The snoozes holding for the rows of status.json."""
    changes = status.get("changes", [])
    return active({c["number"]: c.get("patch_set") for c in changes}, now,
                  base_red=red_base(changes, status.get("base_health", {})))


def morning(day):
    """Start of the working hours on `day`, pushed to Monday on a weekend."""
    if day.weekday() >= 5:
        day += datetime.timedelta(days=7 - day.weekday())
    return time.mktime(datetime.datetime.combine(day, datetime.time(CONFIG["work_hours"][0])).timetuple())


def in_days(days, now=None):
    return morning(datetime.date.fromtimestamp(now or time.time()) + datetime.timedelta(days=days))


def parse_date(text):
    return morning(datetime.date.fromisoformat(text.strip()))


def set_snooze(number, until=None, patch_set=None, base_green=False, now=None):
    """Expired date snoozes are pruned on the way; patch-set and base ones need the snapshot, so they wait for a
    wake."""
    now = now or time.time()
    entries = {n: e for n, e in load().items()
               if e.get("patch_set") is not None or e.get("base_green") or now < e.get("until", 0)}
    if base_green:
        entries[int(number)] = {"base_green": True}
    else:
        entries[int(number)] = {"patch_set": int(patch_set)} if patch_set is not None else {"until": until}
    save(entries)


def forget(numbers):
    """Drops the snoozes of these changes, e.g. closed ones: `active` already ignores them."""
    entries = load()
    kept = {n: e for n, e in entries.items() if n not in numbers}
    if kept != entries:
        save(kept)


def wake(number):
    entries = load()
    if entries.pop(int(number), None) is not None:
        save(entries)


def main():
    parser = argparse.ArgumentParser(description="Snooze a Gerrit change: no notification nor wake-up for it.")
    parser.add_argument("change", type=int, nargs="?", help="change number; none lists the snoozes")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--until", metavar="YYYY-MM-DD", help="until that day's working hours start")
    group.add_argument("--days", type=int, help="until the start of the working day, that many days from now")
    group.add_argument("--patch-set", type=int, metavar="N", help="while the current patch set is still N")
    group.add_argument("--base-green", action="store_true",
                       help="while the target branch's periodic build is red (needs periodic_build)")
    group.add_argument("--clear", action="store_true", help="wake the change up now")
    args = parser.parse_args()
    if args.change is None:
        print(json.dumps({str(n): e for n, e in load().items()}, indent=1))
        return 0
    if args.clear:
        wake(args.change)
    elif args.base_green:
        if not (CONFIG["periodic_build"] and CONFIG["zuul_api"]):
            parser.error("--base-green needs periodic_build and zuul_api in config.json")
        set_snooze(args.change, base_green=True)
    elif args.patch_set is not None:
        set_snooze(args.change, patch_set=args.patch_set)
    elif args.until or args.days is not None:
        set_snooze(args.change, until=parse_date(args.until) if args.until else in_days(args.days))
    else:
        parser.error("one of --until, --days, --patch-set, --base-green or --clear is required")
    return 0


if __name__ == "__main__":
    sys.exit(main())
