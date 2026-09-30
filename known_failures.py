#!/usr/bin/env python3
"""Memory of the CI failures diagnosed by hand: the next look-alike failure points at the change that fixed it."""
import argparse
import json
import sys
import time

import ci
from config import CACHE, atomic_write, read_json

KNOWN = CACHE / "known_failures.json"
RETENTION_S = 180 * 86400


def load():
    return read_json(KNOWN)


def snippet_of(log_url):
    """The same excerpt diagnose_job() matches on, so a record and its next occurrence compare like for like."""
    log = ci.http_get(f"{log_url.rstrip('/')}/job-output.txt")
    return ci.gradle_failure(log) or ci.log_tail(log)


def record(change, patch_set, job, log_url, cause, fix, now=None):
    now = now or time.time()
    lines = ci.fingerprint(snippet_of(log_url))
    if not lines:
        raise ValueError(f"nothing to fingerprint in {log_url}/job-output.txt")
    kept = {k: r for k, r in load().items() if now - r.get("recorded_at", 0) < RETENTION_S}
    kept[f"{change}:{patch_set}:{job}"] = {"change": change, "patch_set": patch_set, "job": job, "cause": cause,
                                           "fix": fix, "log_url": log_url, "recorded_at": now, "fingerprint": lines}
    atomic_write(KNOWN, dict(sorted(kept.items())))
    return kept[f"{change}:{patch_set}:{job}"]


def forget(change, patch_set, job):
    known = load()
    if known.pop(f"{change}:{patch_set}:{job}", None) is None:
        return False
    atomic_write(KNOWN, known)
    return True


def main():
    parser = argparse.ArgumentParser(description="Remember how a CI failure was fixed, to recognize it next time.")
    commands = parser.add_subparsers(dest="command")
    for name in ("record", "forget"):
        command = commands.add_parser(name)
        command.add_argument("change", type=int)
        command.add_argument("patch_set", type=int)
        command.add_argument("job")
    commands.choices["record"].add_argument("--log-url", required=True, help="the failed build's log_url")
    commands.choices["record"].add_argument("--cause", required=True, help="what actually broke, in one line")
    commands.choices["record"].add_argument("--fix", required=True, help="what fixed it, in one line")
    args = parser.parse_args()
    if args.command == "record":
        try:
            found = record(args.change, args.patch_set, args.job, args.log_url, args.cause, args.fix)
        except (OSError, ValueError) as error:
            print(f"known_failures: {error}", file=sys.stderr)
            return 1
        print(json.dumps({k: v for k, v in found.items() if k != "fingerprint"}, ensure_ascii=False))
    elif args.command == "forget":
        return 0 if forget(args.change, args.patch_set, args.job) else 1
    else:
        print(json.dumps({k: {f: v for f, v in r.items() if f != "fingerprint"} for k, r in load().items()},
                         indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
