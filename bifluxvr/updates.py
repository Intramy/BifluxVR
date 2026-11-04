import calendar
import json
import os
import re
import subprocess
import threading
import time

from . import net, openvr, selfupdate
from .util import CACHE_DIR, atomic_write
from .voice import t

PATH = os.path.join(CACHE_DIR, "notifications.json")
INTERVAL = 6 * 3600
KEEP = 30
PROTON_REPOS = (("ValveSoftware/Proton", "Proton"), ("GloriousEggroll/proton-ge-custom", "GE-Proton"))
RECENT = 45 * 86400
_lock = threading.Lock()


def _load():
    try:
        with open(PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {"items": data.get("items", []), "checked": data.get("checked", 0), "read": data.get("read", 0)}
    except (OSError, ValueError):
        pass
    return {"items": [], "checked": 0, "read": 0}


def _save(data):
    atomic_write(PATH, json.dumps(data, indent=1))


def items():
    with _lock:
        return _load()["items"]


def unread():
    with _lock:
        data = _load()
    return [i for i in data["items"] if i["time"] > data["read"]]


def mark_read():
    with _lock:
        data = _load()
        data["read"] = time.time()
        _save(data)


def _version(text):
    return tuple(int(n) for n in re.findall(r"\d+", text or ""))


def wivrn_version():
    try:
        out = subprocess.run(["wivrn-server", "--version"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"(\d+(\.\d+)+)", out)
    return m.group(1) if m else ""


def _check_app():
    rel = selfupdate.check()
    if not rel:
        return []
    return [{"id": f"app-{rel['version']}", "kind": "app", "version": rel["version"],
             "title": t(f"BifluxVR {rel['version']} is available", f"BifluxVR {rel['version']} is here"),
             "body": t("Install it from Settings.", "Get it in Settings & Stuff.")}]


def _check_wivrn():
    have = wivrn_version()
    if not have:
        return []
    rel = net.github_latest("WiVRn/WiVRn")
    tag = rel.get("tag_name", "")
    if _version(tag) <= _version(have):
        return []
    return [{"id": f"wivrn-{tag}", "kind": "wivrn", "url": rel.get("html_url", ""),
             "title": t(f"WiVRn {tag.lstrip('v')} is available", f"WiVRn {tag.lstrip('v')} just dropped"),
             "body": t(f"You have {have}. Update the server and the headset app together.",
                       f"You're on {have}. Update the server and the headset app together, they need to match.")}]


def _check_xrizer():
    mine = [layer for layer in openvr.discover() if layer.kind == "xrizer"]
    if not mine:
        return []
    tag = openvr.latest_xrizer_version()
    have = max((layer.version for layer in mine), key=_version, default="")
    if not tag or not have or _version(tag) <= _version(have):
        return []
    return [{"id": f"xrizer-{tag}", "kind": "xrizer", "url": f"https://github.com/{openvr.XRIZER_REPO}/releases",
             "title": t(f"xrizer {tag} is available", f"New xrizer: {tag}"),
             "body": t("Download it from the Translation page.", "Get it on the Babel Fish page.")}]


def _mentions(body, name):
    words = re.escape(name).replace(r"\ ", r"[\s:–-]+")
    pattern = re.compile(rf"(?<![\w]){words}(?![\w])", re.I)
    for line in body.splitlines():
        if pattern.search(line):
            return line.strip(" -*\t")
    return ""


def _release_name(label, tag):
    tag = re.sub(r"^proton-", "", tag, flags=re.I)
    return tag if label.lower() in tag.lower() else f"{label} {tag}"


def _check_proton(games):
    out = []
    names = [(key, name) for key, name in games if len(re.sub(r"\W", "", name)) >= 4]
    for repo, label in PROTON_REPOS:
        for rel in net.github_releases(repo, 6):
            published = rel.get("published_at", "")
            try:
                when = calendar.timegm(time.strptime(published, "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                continue
            if time.time() - when > RECENT:
                continue
            tag = rel.get("tag_name", "")
            release = _release_name(label, tag)
            body = rel.get("body") or ""
            for key, name in names:
                line = _mentions(body, name)
                if line:
                    out.append({"id": f"proton-{tag}-{key}", "kind": "proton", "url": rel.get("html_url", ""),
                                "title": t(f"{release} mentions {name}", f"{release} has fixes for {name}"),
                                "body": line[:200]})
    return out


def check(games, force=False):
    with _lock:
        data = _load()
    if not force and time.time() - data["checked"] < INTERVAL:
        return []
    found = []
    for source in (_check_app, _check_wivrn, _check_xrizer, lambda: _check_proton(games)):
        try:
            found += source()
        except (OSError, ValueError):
            continue
    with _lock:
        data = _load()
        known = {i["id"] for i in data["items"]}
        new = [dict(i, time=time.time()) for i in found if i["id"] not in known]
        data["items"] = (new + data["items"])[:KEEP]
        data["checked"] = time.time()
        _save(data)
    return new
