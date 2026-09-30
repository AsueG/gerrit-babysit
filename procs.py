"""The process tree, read with a single `ps`: finds the Claude session a watcher or a hook runs under."""
import subprocess


def processes(column="command"):
    """{pid: (ppid, column)}; `column` goes last so `ps` does not truncate it."""
    out = subprocess.run(["ps", "-axo", f"pid=,ppid=,{column}="], capture_output=True, text=True).stdout
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            table[int(parts[0])] = (int(parts[1]), parts[2])
    return table


def ancestors(pid, table):
    while pid > 1 and pid in table:
        yield pid
        pid = table[pid][0]


def descends_from(pid, ancestor, table):
    return ancestor in ancestors(pid, table)
