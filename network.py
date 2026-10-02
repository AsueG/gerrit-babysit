"""What to look at first when Gerrit or zuul stops answering: the VPN (GlobalProtect) and DNS."""
import pathlib
import socket
import subprocess
import urllib.parse

import ci
import gerrit
import procs

VPN_PROCESSES = frozenset({"GlobalProtect", "PanGPA", "PanGPS"})


def resolves(host):
    try:
        socket.getaddrinfo(host, None)
        return True
    except (OSError, UnicodeError):
        return False


def vpn_tunnel(ifconfig=None):
    """A utun interface with an IPv4 address; None when ifconfig cannot run. macOS's own utuns only carry IPv6."""
    if ifconfig is None:
        try:
            ifconfig = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return None
    interface = ""
    for line in ifconfig.splitlines():
        if line[:1] and not line[:1].isspace():
            interface = line.split(":", 1)[0]
        elif interface.startswith("utun") and line.strip().startswith("inet "):
            return True
    return False


def globalprotect_running():
    return any(pathlib.Path(command.strip()).name in VPN_PROCESSES for _, command in procs.processes("comm").values())


def checks():
    hosts = [gerrit.HOST] + ([urllib.parse.urlparse(ci.ZUUL_API).hostname] if ci.ZUUL_API else [])
    return {"dns": {host: resolves(host) for host in dict.fromkeys(h for h in hosts if h)},
            "vpn_tunnel": vpn_tunnel(), "globalprotect_running": globalprotect_running()}
