# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
"""Carries out a plan from packages.plan(). System packages first, in one
pkexec (or sudo) batch; then the per-user steps. Progress goes to `emit` as
(event, key, payload) from a worker thread."""

import json
import os
import select
import shutil
import subprocess
import sys
import tarfile
import time

from bifluxvr import net, openvr
from bifluxvr.util import LOG_DIR

from . import packages
from .packages import FLATHUB, FLATPAK_PKG, LOCAL_BIN, SOURCE, UMU_DIR

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "root_helper.py")
LOG = os.path.join(LOG_DIR, "installer.log")


class StepError(RuntimeError):
    pass


def _root_steps(items, manager):
    mgr = packages.Manager(manager)
    steps, apt_updated = [], False
    for it in items:
        cmds = []
        if it.needs_flatpak:
            cmds += mgr.commands([FLATPAK_PKG[manager]], False)
        if it.method == "pkg" and it.action in ("install", "update"):
            cmds += mgr.commands(it.packages, it.action == "update")
        if not cmds:
            continue
        if manager == "apt" and not apt_updated:
            cmds.insert(0, ["apt-get", "update"])
            apt_updated = True
        steps.append({"id": it.key, "commands": cmds})
    return steps


def _helper(argv, steps, emit, prefix=""):
    """Feed `steps` to the root helper started by `argv` and relay its
    progress. Returns (failed, started keys, stderr, exit code)."""
    # A session of its own: with no terminal, pkexec can't fall back to
    # asking for the password on whatever terminal started the installer.
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors="replace", start_new_session=True)
    proc.stdin.write(prefix + json.dumps({"steps": steps}) + "\n")
    proc.stdin.close()
    failed, seen = {}, set()
    for line in proc.stdout:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        key = msg.get("id")
        if msg.get("event") == "start":
            seen.add(key)
            emit("start", key, None)
        elif msg.get("event") == "done":
            if not msg["ok"]:
                failed[key] = msg["reason"]
            emit("root-done", key, msg["reason"])
        elif "log" in msg:
            emit("log", key, msg["log"])
    err = proc.stderr.read().strip()
    return failed, seen, err, proc.wait()


def _sudo_ok(password):
    """One password attempt, counted once. sudo retries on its own, and a
    closed stdin makes every retry another failure, which pam_faillock
    turns into a lockout after a single typo. So stdin stays open and sudo
    is stopped as soon as it asks again."""
    try:
        proc = subprocess.Popen(["sudo", "-S", "-k", "-p", "", "-v"], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
    except OSError:
        return False
    try:
        proc.stdin.write(password.encode() + b"\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 30
        while proc.poll() is None and time.monotonic() < deadline:
            # Output means a lecture (before the check) or a complaint
            # (after a failure); only a failure leaves sudo waiting for more.
            if select.select([proc.stderr], [], [], 0.2)[0] and proc.stderr.read1(4096):
                try:
                    return proc.wait(timeout=3) == 0
                except subprocess.TimeoutExpired:
                    return False
        return proc.poll() == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def _sudo(steps, emit, ask_password):
    """No polkit agent to ask for the password (common on Hyprland, Sway, i3):
    ask in the installer's own window and hand it to sudo. Never stored."""
    if not shutil.which("sudo"):
        return "couldn't get administrator rights (no polkit agent and no sudo)"
    # Two tries: pam_faillock locks sudo after three failures by default.
    for attempt in range(2):
        password = ask_password(attempt > 0)
        if password is None:
            return "the administrator password wasn't entered"
        if _sudo_ok(password):
            return _helper(["sudo", "-S", "-p", "", sys.executable, HELPER], steps, emit, password + "\n")
    return ("sudo didn't accept the password twice. If it was right, sudo may have locked your account "
            "for a few minutes after earlier wrong tries (check with: faillock)")


def _as_root(steps, emit, ask_password):
    """Run `steps` as root. Returns {key: failure reason} for the ones that failed."""
    keys = [s["id"] for s in steps]
    for k in keys:
        emit("log", k, "Asking for your password to install system packages…")
    if os.geteuid() == 0:
        failed, seen, err, rc = _helper([sys.executable, HELPER], steps, emit)
    elif shutil.which("pkexec"):
        failed, seen, err, rc = _helper(["pkexec", sys.executable, HELPER], steps, emit)
        if rc == 127 and not seen and "agent" in err.lower() and ask_password:
            for k in keys:
                emit("log", k, "No password dialog (polkit agent) is running, so asking here instead")
            via_sudo = _sudo(steps, emit, ask_password)
            if isinstance(via_sudo, str):
                return {k: via_sudo for k in keys}
            failed, seen, err, rc = via_sudo
    else:
        via_sudo = _sudo(steps, emit, ask_password) if ask_password else "couldn't get administrator rights"
        if isinstance(via_sudo, str):
            return {k: via_sudo for k in keys}
        failed, seen, err, rc = via_sudo
    if rc in (126, 127) and not seen:
        reason = ("the administrator password wasn't entered" if rc == 126
                  else f"couldn't get administrator rights ({err.splitlines()[-1] if err else 'pkexec ' + str(rc)})")
        return {k: reason for k in keys}
    for k in keys:
        if k not in seen:
            failed[k] = f"never ran ({err.splitlines()[-1] if err else 'helper stopped'})"
    return failed


def _stream(argv, key, emit):
    emit("log", key, "$ " + " ".join(argv))
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, errors="replace", env={**os.environ, "LC_ALL": "C"})
    tail = []
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            emit("log", key, line)
            tail = (tail + [line])[-3:]
    if proc.wait():
        raise StepError(f"{argv[0]} failed (exit {proc.returncode}): {tail[-1] if tail else 'no output'}")


def _flatpak(it, emit):
    app = it.packages[0]
    if it.action == "install":
        _stream(["flatpak", "remote-add", "--user", "--if-not-exists", "flathub", FLATHUB], it.key, emit)
        _stream(["flatpak", "install", "--user", "-y", "--noninteractive", "flathub", app], it.key, emit)
    else:
        _stream(["flatpak", "update", "-y", "--noninteractive", app], it.key, emit)


def _umu(it, emit):
    latest = packages._umu_latest()
    if not latest:
        raise StepError("couldn't find umu-launcher's latest release on GitHub")
    dest = os.path.join(UMU_DIR, latest["version"])
    archive = os.path.join(UMU_DIR, "umu.tar")
    emit("log", it.key, f"Downloading {latest['url']}")
    net.download(latest["url"], archive, lambda p: emit("fraction", it.key, p))
    try:
        shutil.rmtree(dest, ignore_errors=True)
        with tarfile.open(archive) as tar:
            tar.extractall(dest, filter="data")
    finally:
        os.unlink(archive)
    run = next((os.path.join(d, "umu-run") for d, _, files in os.walk(dest) if "umu-run" in files), None)
    if not run:
        raise StepError("the umu-launcher download has no umu-run inside")
    os.chmod(run, 0o755)
    os.makedirs(LOCAL_BIN, exist_ok=True)
    link = os.path.join(LOCAL_BIN, "umu-run")
    if os.path.lexists(link):
        os.unlink(link)
    os.symlink(run, link)
    emit("log", it.key, f"umu-run → {run}")


def _xrizer(it, emit):
    emit("log", it.key, "Downloading the latest xrizer from GitHub")
    path = openvr.install_latest_xrizer(lambda p: emit("fraction", it.key, p))
    if openvr.classify(openvr.current_runtime()) in ("", "other", "xrizer"):
        openvr.set_runtime(path)
        emit("log", it.key, f"OpenVR games now use {path}")


def _bifluxvr(it, emit):
    _stream(["sh", os.path.join(SOURCE, "install.sh"), "--files-only"], it.key, emit)


USER_STEPS = {"flatpak": _flatpak, "zipapp": _umu, "xrizer": _xrizer, "bifluxvr": _bifluxvr}


def run(items, manager, emit, ask_password=None):
    """Returns {key: failure reason}; empty when everything worked."""
    os.makedirs(LOG_DIR, exist_ok=True)
    log = open(LOG, "w")

    def logged(event, key, payload):
        if event == "log":
            log.write(f"[{key}] {payload}\n")
            log.flush()
        emit(event, key, payload)

    failed = {}
    try:
        for it in items:
            if it.action == "skip":
                logged("skip", it.key, it.detail)
            elif it.action == "fail":
                failed[it.key] = it.detail
                logged("log", it.key, it.detail)
                logged("done", it.key, it.detail)
        steps = _root_steps([it for it in items if it.action in ("install", "update")], manager) if manager else []
        if steps:
            failed.update(_as_root(steps, logged, ask_password))
        for it in items:
            if it.action not in ("install", "update"):
                continue
            if it.key in failed:
                logged("done", it.key, failed[it.key])
                continue
            step = USER_STEPS.get(it.method)
            if step:
                logged("start", it.key, None)
                try:
                    step(it, logged)
                except (StepError, OSError, RuntimeError, ValueError) as e:
                    failed[it.key] = str(e)
            logged("done", it.key, failed.get(it.key, ""))
    finally:
        log.close()
    return failed
