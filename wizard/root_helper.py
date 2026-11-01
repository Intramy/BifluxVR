# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
"""Runs the installer's package-manager commands as root, so the user types
their password once. Started through pkexec or sudo; reads the steps as a
JSON line on stdin and reports progress as JSON lines on stdout.

Only package managers may be run, whatever the steps say."""

import json
import os
import shutil
import subprocess
import sys

ALLOWED = {"pacman", "dnf", "apt-get", "zypper", "emerge"}


def say(**msg):
    print(json.dumps(msg), flush=True)


def run(step):
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C",
           "DEBIAN_FRONTEND": "noninteractive", "HOME": "/root", "TERM": "dumb"}
    for argv in step["commands"]:
        prog = shutil.which(argv[0], path=env["PATH"]) if argv and argv[0] in ALLOWED else None
        if not prog:
            return f"refused to run {argv[:1]}"
        say(id=step["id"], log="$ " + " ".join(argv))
        proc = subprocess.Popen([prog, *argv[1:]], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, text=True, errors="replace")
        tail = []
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                say(id=step["id"], log=line)
                tail = (tail + [line])[-3:]
        if proc.wait():
            return f"{argv[0]} failed (exit {proc.returncode}): {tail[-1] if tail else 'no output'}"
    return ""


def main():
    if os.geteuid() != 0:
        sys.exit("must run as root")
    # Through sudo, an unused password line may come first; the plan is the JSON line.
    plan = next(json.loads(line) for line in sys.stdin if line.startswith("{"))
    for step in plan["steps"]:
        say(id=step["id"], event="start")
        reason = run(step)
        say(id=step["id"], event="done", ok=not reason, reason=reason)


if __name__ == "__main__":
    main()
