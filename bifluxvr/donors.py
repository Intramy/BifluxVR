import json
import os
import re

from . import net, tripwire
from .util import CACHE_DIR, atomic_write

BASE = "https://intramy.xyz/donors/"
CACHE = os.path.join(CACHE_DIR, "donors")
_FILE = re.compile(r"^avatars/[a-z0-9]{6,40}\.(png|jpg|webp|gif)$")


def _cached():
    try:
        with open(os.path.join(CACHE, "donors.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _clean(data):
    out = []
    for d in data.get("donors", []) if isinstance(data, dict) else []:
        try:
            name = str(d["name"]).strip()[:32]
            total = int(d["total"])
        except (KeyError, TypeError, ValueError):
            continue
        if not name or total <= 0:
            continue
        avatar = d.get("avatar") if isinstance(d.get("avatar"), str) and _FILE.match(d["avatar"]) else ""
        out.append({"id": str(d.get("id", ""))[:40], "name": name, "total": total,
                    "count": int(d.get("count", 1) or 1), "avatar": avatar})
    out.sort(key=lambda d: (-d["total"], d["name"].lower()))
    return {"donors": out}


def load(online=True):
    tripwire.check()
    data = _cached()
    if online:
        try:
            fresh = net.get_json(BASE + "donors.json", timeout=10)
            if isinstance(fresh, dict):
                data = fresh
                atomic_write(os.path.join(CACHE, "donors.json"), json.dumps(fresh))
        except (OSError, ValueError):
            pass
    return _clean(data)


def avatar(donor, online=True):
    rel = donor.get("avatar")
    if not rel:
        return ""
    path = os.path.join(CACHE, rel)
    if os.path.isfile(path):
        return path
    if not online:
        return ""
    try:
        return net.download(BASE + rel, path, timeout=15)
    except OSError:
        return ""


def euros(cents):
    return f"€{cents // 100}" if cents % 100 == 0 else f"€{cents / 100:.2f}"
