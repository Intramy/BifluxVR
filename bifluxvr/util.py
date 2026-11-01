# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
import os
import signal
import subprocess
import tempfile
import threading

HOME = os.path.expanduser("~")
CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
CACHE_HOME = os.environ.get("XDG_CACHE_HOME") or os.path.join(HOME, ".cache")
DATA_HOME = os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share")
RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"

CACHE_DIR = os.path.join(CACHE_HOME, "bifluxvr")
LOG_DIR = os.path.join(CACHE_DIR, "logs")
APP_CONFIG_DIR = os.path.join(CONFIG_HOME, "bifluxvr")
APP_DATA_DIR = os.path.join(DATA_HOME, "bifluxvr")


def atomic_write(path, text, mode=None):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".bifluxvr-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        elif os.path.exists(path):
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def pids_of(*names):
    """PIDs owned by this user whose comm matches one of `names`."""
    out = []
    uid = os.getuid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            if os.stat(f"/proc/{entry}").st_uid != uid:
                continue
            with open(f"/proc/{entry}/comm") as f:
                comm = f.read().strip()
            # A zombie has exited and is only waiting to be reaped.
            with open(f"/proc/{entry}/stat") as f:
                if f.read().rsplit(")", 1)[1].split()[0] in ("Z", "X"):
                    continue
        except (OSError, IndexError):
            continue
        # comm is truncated to 15 chars by the kernel
        if any(comm == n[:15] for n in names):
            out.append(int(entry))
    return out


def process_running(*names):
    return bool(pids_of(*names))


def process_running_any_user(*names):
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                with open(f"/proc/{entry}/comm") as f:
                    if f.read().strip() in [n[:15] for n in names]:
                        return True
            except OSError:
                pass
    return False


def spawn_detached(argv, log_path, **kw):
    """Start a long-running child and reap it in the background when it exits,
    so it never lingers as a zombie that looks like a live process."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True, **kw)
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc


def terminate(pids, sig=signal.SIGTERM):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def slug(text):
    s = "".join(c.lower() if c.isalnum() else "-" for c in text)
    return "-".join(p for p in s.split("-") if p) or "game"
