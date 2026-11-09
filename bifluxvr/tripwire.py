import hashlib
import json
import os
import time

from .util import APP_CONFIG_DIR, APP_DATA_DIR, atomic_write

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, ".manifest")
LOCKS = (os.path.join(APP_DATA_DIR, ".nicetry"), os.path.join(APP_CONFIG_DIR, ".nicetry"))
_handlers = []
_seen = {}


def locked():
    return any(os.path.exists(p) for p in LOCKS)


def on_trip(callback):
    _handlers.append(callback)


def _fire():
    for cb in list(_handlers):
        try:
            cb()
        except Exception:
            pass


def _scrub():
    path = os.path.join(APP_CONFIG_DIR, "config.json")
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(cfg, dict):
        return
    for key in ("perks_token", "perks_legacy", "donor_code", "donor_name"):
        cfg.pop(key, None)
    cfg["gold_look"] = False
    if cfg.get("skin") == "gold":
        cfg["skin"] = "classic"
    atomic_write(path, json.dumps(cfg, indent=2))


def trip(reason):
    note = json.dumps({"reason": reason, "at": int(time.time())})
    for path in LOCKS:
        try:
            atomic_write(path, note)
        except OSError:
            pass
    try:
        _scrub()
    except OSError:
        pass
    _fire()
    return False


def _files():
    try:
        with open(MANIFEST) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    want = {}
    for line in lines:
        digest, _, name = line.partition("  ")
        if digest and name:
            want[os.path.normpath(name)] = digest
    return want


def files_ok():
    want = _files()
    if want is None:
        return True
    have = set()
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".py"):
                have.add(os.path.normpath(os.path.relpath(os.path.join(root, f), HERE)))
    if have != set(want):
        return False
    for name, digest in want.items():
        path = os.path.join(HERE, name)
        try:
            st = os.stat(path)
        except OSError:
            return False
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        if _seen.get(path) == (stamp, digest):
            continue
        h = hashlib.sha256()
        with open(path, "rb") as f:
            h.update(f.read())
        if h.hexdigest() != digest:
            return False
        _seen[path] = (stamp, digest)
    return True


def check(cfg=None):
    if locked():
        _fire()
        return False
    if not files_ok():
        return trip("files")
    if cfg is not None:
        from . import perks
        if perks.forged(cfg):
            return trip("perks")
    return True
