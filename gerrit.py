"""Gerrit access: SSH queries (multiplexed over one connection) and the authenticated REST API."""
import base64
import hashlib
import json
import netrc
import pathlib
import subprocess
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
    out = ssh("gerrit", "query", "--format=JSON", *options, f"'{text}'", check=True).stdout
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


def rest_get(path, retry=True):
    global _rest_auth
    if _rest_auth is None:
        _rest_auth = base64.b64encode(":".join(http_credentials()).encode()).decode()
    request = urllib.request.Request(REST + path, headers={"Authorization": f"Basic {_rest_auth}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            # Gerrit prefixes every JSON body with )]}' against XSSI.
            return json.loads(response.read().decode().split("\n", 1)[1])
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise
        # The token may have been rotated since the daemon started: reread it once.
        _rest_auth = None
        if not retry:
            raise
        error.close()
        return rest_get(path, retry=False)
