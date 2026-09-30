#!/usr/bin/env python3
"""Snoozed changes: their events are held back, not dropped, until a date or their next patch set."""
import argparse
import datetime
import json
import sys
import time

from config import CACHE, CONFIG, atomic_write, read_json

SNOOZE = CACHE / "snooze.json"


def load():
    return {int(k): v for k, v in read_json(SNOOZE).items()}


def is_active(entry, patch_set, now):
    if entry.get("patch_set") is not None:
        return patch_set is not None and int(patch_set) <= int(entry["patch_set"])
    return now < entry.get("until", 0)


def active(patch_sets, now=None, entries=None):
    """{change number: entry} for the snoozes still holding, given {change number: current patch set}.

    A change missing from `patch_sets` (merged, abandoned) is not snoozed anymore."""
    now = now or time.time()
    entries = load() if entries is None else entries
    return {n: e for n, e in entries.items() if n in patch_sets and is_active(e, patch_sets[n], now)}


def morning(day):
    """Start of the working hours on `day`, pushed to Monday on a weekend."""
    if day.weekday() >= 5:
        day += datetime.timedelta(days=7 - day.weekday())
    return time.mktime(datetime.datetime.combine(day, datetime.time(CONFIG["work_hours"][0])).timetuple())


def in_days(days, now=None):
    return morning(datetime.date.fromtimestamp(now or time.time()) + datetime.timedelta(days=days))


def parse_date(text):
    return morning(datetime.date.fromisoformat(text.strip()))


def set_snooze(number, until=None, patch_set=None, now=None):
    """Expired date snoozes are pruned on the way; patch-set ones need the snapshot, so they wait for a wake."""
    now = now or time.time()
    entries = {n: e for n, e in load().items() if e.get("patch_set") is not None or now < e.get("until", 0)}
    entries[int(number)] = {"patch_set": int(patch_set)} if patch_set is not None else {"until": until}
    atomic_write(SNOOZE, {str(n): e for n, e in sorted(entries.items())})


def forget(numbers):
    """Drops the snoozes of these changes, e.g. closed ones: `active` already ignores them."""
    entries = load()
    kept = {n: e for n, e in entries.items() if n not in numbers}
    if kept != entries:
        atomic_write(SNOOZE, {str(n): e for n, e in sorted(kept.items())})


def wake(number):
    entries = load()
    if entries.pop(int(number), None) is not None:
        atomic_write(SNOOZE, {str(n): e for n, e in sorted(entries.items())})


def main():
    parser = argparse.ArgumentParser(description="Snooze a Gerrit change: no notification nor wake-up for it.")
    parser.add_argument("change", type=int, nargs="?", help="change number; none lists the snoozes")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--until", metavar="YYYY-MM-DD", help="until that day's working hours start")
    group.add_argument("--days", type=int, help="until the start of the working day, that many days from now")
    group.add_argument("--patch-set", type=int, metavar="N", help="while the current patch set is still N")
    group.add_argument("--clear", action="store_true", help="wake the change up now")
    args = parser.parse_args()
    if args.change is None:
        print(json.dumps({str(n): e for n, e in load().items()}, indent=1))
        return 0
    if args.clear:
        wake(args.change)
    elif args.patch_set is not None:
        set_snooze(args.change, patch_set=args.patch_set)
    elif args.until or args.days is not None:
        set_snooze(args.change, until=parse_date(args.until) if args.until else in_days(args.days))
    else:
        parser.error("one of --until, --days, --patch-set or --clear is required")
    return 0


if __name__ == "__main__":
    sys.exit(main())
