"""Gerrit access: change queries over the authenticated REST API (SSH when no HTTP password is set) and SSH commands."""
import base64
import datetime
import hashlib
import json
import netrc
import pathlib
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from config import CACHE, CONFIG, gerrit_user

HOST = CONFIG["gerrit_host"]
PORT = str(CONFIG["ssh_port"])
USER = gerrit_user()
CI_USER = CONFIG["ci_user"]
# "" = Gerrit itself (auto-abandon notices carry no username).
BOT_USERS = frozenset({CI_USER, "", *CONFIG["bot_users"]})
NOT_ME = BOT_USERS | {USER}
REST = f"https://{HOST}/a"

# A short hashed name: macOS caps socket paths at 104 bytes and ssh appends a 17-char temp suffix while binding.
CONTROL_PATH = CACHE / f"ssh-{hashlib.sha1(f'{USER}@{HOST}:{PORT}'.encode()).hexdigest()[:10]}"
MULTIPLEX = ["-o", "ControlMaster=auto", "-o", f"ControlPath={str(CONTROL_PATH).replace('%', '%%')}",
             "-o", "ControlPersist=120",
             # A master stuck on a dead VPN must exit instead of hanging every query that reuses it.
             "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2"] if len(str(CONTROL_PATH)) <= 80 else []
SSH = ["ssh", "-p", PORT, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", *MULTIPLEX, f"{USER}@{HOST}"]


def ssh(*args, timeout=60, check=False):
    CACHE.mkdir(parents=True, exist_ok=True)
    return subprocess.run([*SSH, *args], capture_output=True, text=True, timeout=timeout, check=check)


SESSION_CAP = "Too many concurrent connections"


def query(text, *options):
    """`gerrit query --format=JSON` rows, read over REST whenever an HTTP password is set.

    An SSH session cut by a VPN drop or a sleep lingers on the server, which never saw it close, and counts against
    the per-user connection cap until Gerrit times it out: polled every minute, those ghosts end up locking every
    SSH client out, git included. A REST call holds no session."""
    try:
        rest_auth()
    except (OSError, ValueError, KeyError):
        return ssh_query(text, *options)
    try:
        return rest_query(text, *options)
    except urllib.error.HTTPError as error:
        # An expired password must not blind the watcher: SSH still reads the changes.
        if error.code not in (401, 403):
            raise
        error.close()
        return ssh_query(text, *options)


def ssh_query(text, *options):
    out = ssh("gerrit", "query", "--format=JSON", *options, shlex.quote(text), check=True).stdout
    rows = [json.loads(line) for line in out.splitlines() if line.strip()]
    return [r for r in rows if r.get("type") != "stats"]


# What each `gerrit query` flag needs from REST; accounts always come detailed, as SSH gives them.
REST_OPTIONS = {
    "--current-patch-set": ("CURRENT_REVISION", "CURRENT_COMMIT", "DETAILED_LABELS"),
    "--patch-sets": ("ALL_REVISIONS",),
    "--all-approvals": ("ALL_REVISIONS", "CURRENT_REVISION", "CURRENT_COMMIT", "DETAILED_LABELS", "MESSAGES"),
    "--comments": ("MESSAGES",),
    "--dependencies": ("CURRENT_REVISION", "CURRENT_COMMIT"),
    "--all-reviewers": ("DETAILED_LABELS",),
    "--submit-records": (),
}


def rest_changes(text, rest_options):
    """One page, as the SSH query read: Gerrit's own result cap applies to both."""
    params = [("q", text), *(("o", option) for option in sorted({"DETAILED_ACCOUNTS", *rest_options}))]
    # The 60 s an SSH query had: a page of comments and revisions is slower than a single change.
    return rest_get(f"/changes/?{urllib.parse.urlencode(params)}", timeout=60)


def rest_query(text, *options):
    changes = rest_changes(text, [o for option in options for o in REST_OPTIONS[option]])
    rows = [ssh_row(change, options) for change in changes]
    if "--dependencies" in options:
        add_depends_on(rows)
    return rows


def epoch(timestamp):
    """REST times are UTC 'YYYY-MM-DD hh:mm:ss.nnnnnnnnn'; SSH gives epoch seconds."""
    moment = datetime.datetime.strptime(timestamp[:19], "%Y-%m-%d %H:%M:%S")
    return int(moment.replace(tzinfo=datetime.timezone.utc).timestamp())


def account(info):
    return {key: info[key] for key in ("name", "email", "username") if key in (info or {})}


VOTE = re.compile(r"(?<![\w-])([A-Za-z][\w-]*)([+-]\d+)(?![\w-])")


def current_approvals(change):
    """The votes on the current patch set, copied ones included, as SSH lists them: an unvoted reviewer is no vote."""
    return [{"type": label, "value": str(vote["value"]), "grantedOn": epoch(vote["date"]) if vote.get("date") else 0,
             "by": account(vote)}
            for label, info in change.get("labels", {}).items() for vote in info.get("all", []) if vote.get("value")]


def past_approvals(change):
    """{patch set: votes} of the older patch sets: REST keeps only the current votes, their messages tell the rest."""
    latest = {}
    for message in change.get("messages", []):
        number = message.get("_revision_number")
        header = message.get("message", "").split("\n", 1)[0]
        prefix = f"Patch Set {number}:"
        if not number or not header.startswith(prefix):
            continue
        by = account(message.get("author"))
        for label, value in VOTE.findall(header[len(prefix):]):
            latest[(number, by.get("username"), label)] = {"type": label, "value": str(int(value)),
                                                           "grantedOn": epoch(message["date"]), "by": by}
    approvals = {}
    for (number, _, _), approval in latest.items():
        if approval["value"] != "0":
            approvals.setdefault(number, []).append(approval)
    return approvals


def patch_set(sha, revision, approvals):
    entry = {"number": revision["_number"], "revision": sha, "ref": revision.get("ref"), "approvals": approvals,
             "createdOn": epoch(revision["created"]) if revision.get("created") else 0}
    if revision.get("kind"):
        entry["kind"] = revision["kind"]
    if revision.get("uploader"):
        entry["uploader"] = account(revision["uploader"])
    if "commit" in revision:
        entry["parents"] = [parent["commit"] for parent in revision["commit"].get("parents", [])]
        entry["author"] = account(revision["commit"].get("author"))
    return entry


def ssh_row(change, options):
    """A REST ChangeInfo shaped like the SSH row the same flags give, so callers read both alike."""
    number, project, status = change["_number"], change["project"], change["status"]
    row = {"project": project, "branch": change["branch"], "id": change["change_id"], "number": number,
           "subject": change.get("subject", ""), "owner": account(change.get("owner")),
           "url": f"https://{HOST}/c/{project}/+/{number}", "hashtags": change.get("hashtags", []),
           "createdOn": epoch(change["created"]), "lastUpdated": epoch(change["updated"]), "open": status == "NEW",
           "status": status}
    if change.get("topic"):
        row["topic"] = change["topic"]
    if change.get("work_in_progress"):
        row["wip"] = True
    revisions, current = change.get("revisions", {}), change.get("current_revision")
    approvals = current_approvals(change)
    if ("--current-patch-set" in options or "--all-approvals" in options) and current in revisions:
        row["currentPatchSet"] = patch_set(current, revisions[current], approvals)
    if "--patch-sets" in options or "--all-approvals" in options:
        past = past_approvals(change) if "--all-approvals" in options else {}
        row["patchSets"] = sorted((patch_set(sha, revision, approvals if sha == current else past.get(revision["_number"], []))
                                   for sha, revision in revisions.items()), key=lambda ps: ps["number"])
    if "--comments" in options:
        row["comments"] = [{"timestamp": epoch(m["date"]), "reviewer": account(m.get("author")),
                            "message": m.get("message", "")} for m in change.get("messages", [])]
    if "--all-reviewers" in options:
        people = [a for state in ("REVIEWER", "CC") for a in change.get("reviewers", {}).get(state, [])]
        row["allReviewers"] = list({a.get("_account_id"): account(a) for a in people}.values())
    if "--submit-records" in options:
        row["submitRecords"] = [{"status": r.get("status"),
                                 "labels": [{"label": l["label"], "status": l.get("status")} for l in r.get("labels", [])]}
                                for r in change.get("submit_records", [])]
    return row


def add_depends_on(rows):
    """SSH's `dependsOn`: the change, merged ones included, whose patch set is the current one's parent, same branch."""
    parent_of = {row["number"]: row["currentPatchSet"]["parents"][0] for row in rows
                 if row.get("currentPatchSet", {}).get("parents")}
    if not parent_of:
        return
    found = rest_changes(" OR ".join(f"commit:{sha}" for sha in sorted(set(parent_of.values()))), ["ALL_REVISIONS"])
    by_commit = {(c["project"], c["branch"], sha): {"id": c["change_id"], "number": c["_number"], "revision": sha,
                                                    "ref": revision.get("ref"),
                                                    "isCurrentPatchSet": sha == c.get("current_revision")}
                 for c in found for sha, revision in c.get("revisions", {}).items()}
    for row in rows:
        parent = by_commit.get((row["project"], row["branch"], parent_of.get(row["number"])))
        if parent:
            row["dependsOn"] = [parent]


def error_detail(error):
    """ssh's own stderr ('Could not resolve hostname…') says far more than 'exit status 255'."""
    stderr = getattr(error, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    lines = stderr.strip().splitlines() if stderr and stderr.strip() else []
    # Its last line is only "Disconnected from …": the reason sits on the one before.
    cap = next((line for line in lines if SESSION_CAP in line), None)
    if cap:
        return (f"Gerrit refuses new SSH sessions ({cap[cap.index(SESSION_CAP):]}): sessions cut by a network drop "
                "linger server-side until Gerrit times them out")
    return (lines[-1] if lines else "") or str(error)


_rest_auth = None
_rest_auth_lock = threading.Lock()


def http_credentials():
    """(user, HTTP password): from the Gerrit MCP server's config when set, so the token lives in one place."""
    if CONFIG["gerrit_mcp_config"]:
        hosts = json.loads(pathlib.Path(CONFIG["gerrit_mcp_config"]).expanduser().read_text())["gerrit_hosts"]
        entry = next((h for h in hosts if HOST in {urllib.parse.urlparse(h.get(key) or "").hostname
                                                   for key in ("external_url", "internal_url")}), None)
        if not entry:
            raise KeyError(f"no gerrit_hosts entry for {HOST} in {CONFIG['gerrit_mcp_config']}")
        auth = entry["authentication"]
        return auth["username"], auth["auth_token"]
    entry = netrc.netrc().authenticators(HOST)
    if not entry:
        raise KeyError(f"no ~/.netrc entry for {HOST}")
    return entry[0], entry[2]


_content_merge: dict[str, tuple[float, bool]] = {}
CONTENT_MERGE_TTL_S = 3600
# Short, so a slow Gerrit does not cost every poll a full REST timeout per project.
CONTENT_MERGE_RETRY_S = 600


def uses_content_merge(project, now=None):
    """Off, Gerrit calls a conflict any file both sides changed, however far apart the edits are.

    Asked again hourly, so a project setting changed meanwhile is seen without a daemon restart; unreadable (no HTTP
    password, network) counts as on, git's own behavior, and is asked again after a few minutes."""
    now = time.time() if now is None else now
    cached = _content_merge.get(project)
    if cached and cached[0] > now:
        return cached[1]
    try:
        config = rest_get(f"/projects/{urllib.parse.quote(project, safe='')}/config", timeout=10)
    except (OSError, ValueError, KeyError):
        _content_merge[project] = (now + CONTENT_MERGE_RETRY_S, True)
        return True
    _content_merge[project] = (now + CONTENT_MERGE_TTL_S, content_merge_of(config.get("use_content_merge")))
    return _content_merge[project][1]


def content_merge_of(setting):
    """Gerrit drops false booleans from its JSON, so a missing `value` or `inherited_value` means false."""
    if setting is None:
        return True
    configured = setting.get("configured_value", "INHERIT")
    return configured == "TRUE" if configured != "INHERIT" else bool(setting.get("inherited_value"))


def http_error():
    """None when the HTTP password works, else why not: SSH working says nothing about it."""
    try:
        rest_get("/accounts/self")
    except (OSError, ValueError, KeyError) as error:
        return error_detail(error)
    return None


def rest_auth(rejected=None):
    """The poll calls REST from several threads: one reads the credentials, the others wait for it. `rejected` is the
    header a 401 refused, dropped unless another thread already replaced it."""
    global _rest_auth
    with _rest_auth_lock:
        if rejected is not None and _rest_auth == rejected:
            _rest_auth = None
        if _rest_auth is None:
            _rest_auth = base64.b64encode(":".join(http_credentials()).encode()).decode()
        return _rest_auth


def rest_get(path, retry=True, data=None, timeout=30):
    auth = rest_auth()
    headers = {"Authorization": f"Basic {auth}"}
    if data is not None:
        headers["Content-Type"] = "application/json; charset=UTF-8"
    request = urllib.request.Request(REST + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # Gerrit prefixes every JSON body with )]}' against XSSI.
            return json.loads(response.read().decode().split("\n", 1)[1])
    except urllib.error.HTTPError as error:
        if error.code != 401 or not retry:
            raise
        # The token may have been rotated since the daemon started: reread it once.
        error.close()
        rest_auth(rejected=auth)
        return rest_get(path, retry=False, data=data, timeout=timeout)


def rest_post(path, payload):
    return rest_get(path, data=json.dumps(payload).encode())


if __name__ == "__main__" and sys.argv[1:] == ["credential", "get"]:
    # git credential helper for the HTTPS fetches: the HTTP password stays where http_credentials() reads it.
    username, password = http_credentials()
    print(f"username={username}\npassword={password}")
