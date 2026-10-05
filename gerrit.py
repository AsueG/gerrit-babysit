"""Gerrit access: SSH queries (multiplexed over one connection) and the authenticated REST API."""
import base64
import hashlib
import json
import netrc
import pathlib
import shlex
import subprocess
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


def query(text, *options):
    out = ssh("gerrit", "query", "--format=JSON", *options, shlex.quote(text), check=True).stdout
    rows = [json.loads(line) for line in out.splitlines() if line.strip()]
    return [r for r in rows if r.get("type") != "stats"]


def error_detail(error):
    """ssh's own stderr ('Could not resolve hostname…') says far more than 'exit status 255'."""
    stderr = getattr(error, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    last_line = stderr.strip().splitlines()[-1] if stderr and stderr.strip() else ""
    return last_line or str(error)


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
