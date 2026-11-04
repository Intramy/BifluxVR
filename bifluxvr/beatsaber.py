import hashlib
import os
import re
import shutil
import tempfile
import time

from . import mods, net, installer
from .mods import ModError
from .voice import t

API = "https://beatmods.com/api"
CDN = "https://beatmods.com/cdn/mod"
ROOTS = ("BSIPA", "SongCore")
SOURCE = "beatmods:"
LOOSE = ("Plugins/", "Libs/", "IPA/", "IPA.", "winhttp.dll", "Beat Saber_Data/Managed/IPA.")

_catalog = net.Cache("beatmods.json", 6 * 3600)


def game_root(game):
    root = mods.game_dir(game)
    return root if os.path.isfile(os.path.join(root, "Beat Saber.exe")) else ""


def game_version(root):
    try:
        with open(os.path.join(root, "Beat Saber_Data", "globalgamemanagers"), "rb") as f:
            m = re.search(rb"(\d+\.\d+\.\d+)_\d{4,}", f.read())
        return m.group(1).decode() if m else ""
    except OSError:
        return ""


def catalog(version):
    hit = _catalog.get(version)
    if hit is None:
        url = (f"{API}/mods?gameName=BeatSaber&gameVersion={net.quote(version)}"
               "&status=verified&platform=steampc")
        hit = _catalog.put(version, net.get_json(url, timeout=30).get("mods", []))
    return hit


def plan(version):
    entries = catalog(version)
    by_vid = {e["latest"]["id"]: e for e in entries}
    by_name = {e["mod"]["name"]: e for e in entries}
    if not all(n in by_name for n in ROOTS):
        return []
    out, seen = [], set()

    def visit(e):
        vid = e["latest"]["id"]
        if vid in seen:
            return
        seen.add(vid)
        for dep in e["latest"].get("dependencies", []):
            if dep in by_vid:
                visit(by_vid[dep])
        out.append({"name": e["mod"]["name"], "version": e["latest"]["modVersion"], "vid": vid,
                    "hash": e["latest"]["zipHash"],
                    "paths": [c["path"] for c in e["latest"].get("contentHashes", [])]})
    for n in ROOTS:
        visit(by_name[n])
    return out


def _dests(rel):
    out = [rel]
    if rel.startswith("IPA/Libs/"):
        out.append(rel[4:])
    elif rel.startswith("IPA/Data/"):
        out.append("Beat Saber_Data/" + rel[9:])
    elif rel == "IPA/winhttp.dll":
        out.append("winhttp.dll")
    return out


def ours(game):
    return [m for m in mods.installed(game) if m.get("source", "").startswith(SOURCE)]


def status(game):
    root = game_root(game)
    version = game_version(root)
    todo = plan(version) if version else []
    have = ours(game)
    owned = {os.path.join(m["target"], r) for m in mods.installed(game)
             if not m.get("source", "").startswith(SOURCE) for r in m["added"] + m["replaced"]}
    mine = {os.path.join(m["target"], r) for m in have for r in m["added"] + m["replaced"]}
    wanted = {os.path.join(root, d) for p in todo for rel in p["paths"] for d in _dests(rel)}
    stray = any(p in owned or (p not in mine and os.path.lexists(p)) for p in wanted)
    modded = have or os.path.lexists(os.path.join(root, "winhttp.dll")) or \
        os.path.lexists(os.path.join(root, "Plugins", "SongCore.dll"))
    if not todo:
        state = "unsupported"
    elif {m["source"] for m in have} == {f"{SOURCE}{p['vid']}" for p in todo} and not stray:
        state = "current"
    else:
        state = "update" if modded else "install"
    return {"version": version, "plan": todo, "installed": have, "state": state}


def _fetch(p):
    dest = os.path.join(mods.TOOLS_DIR, "beatmods", f"{p['hash']}.zip")
    if not os.path.isfile(dest):
        net.download(f"{CDN}/{p['hash']}.zip", dest)
    with open(dest, "rb") as f:
        ok = hashlib.md5(f.read()).hexdigest() == p["hash"]
    if not ok:
        os.unlink(dest)
        raise ModError(t(f"{p['name']} download is corrupt", f"{p['name']} arrived broken. Try again."))
    return dest


def _stage(archive, into):
    with tempfile.TemporaryDirectory(dir=mods._ensure(mods.TOOLS_DIR), prefix=".unpack-") as work:
        installer.extract(archive, work)
        for rel in mods._files(work):
            for d in _dests(rel.replace(os.sep, "/")):
                os.makedirs(os.path.dirname(os.path.join(into, d)) or into, exist_ok=True)
                shutil.copy2(os.path.join(work, rel), os.path.join(into, d))


def install(game):
    root = game_root(game)
    version = game_version(root)
    todo = plan(version) if version else []
    if not todo:
        raise ModError(t(f"BeatMods has no mods for Beat Saber {version or '(unknown version)'} yet",
                         f"BeatMods hasn't caught up with Beat Saber {version or '???'} yet. Give it a few days."))
    archives = [_fetch(p) for p in todo]

    wanted = {d for p in todo for rel in p["paths"] for d in _dests(rel)}
    for m in reversed(mods.installed(game)):
        files = {os.path.relpath(os.path.join(m["target"], r), root) for r in m["added"] + m["replaced"]}
        if m.get("source", "").startswith(SOURCE) or files & wanted:
            mods.rollback(game, m["id"])

    aside = os.path.join(mods._dir(game), f"loose-{int(time.time())}")
    for rel in sorted(wanted):
        path = os.path.join(root, rel)
        if rel.startswith(LOOSE) and os.path.isfile(path):
            os.makedirs(os.path.dirname(os.path.join(aside, rel)), exist_ok=True)
            shutil.move(path, os.path.join(aside, rel))

    done = []
    try:
        for p, archive in zip(todo, archives):
            with tempfile.TemporaryDirectory(dir=mods._ensure(mods.TOOLS_DIR), prefix=".stage-") as stage:
                _stage(archive, stage)
                done.append(mods.install_folder(game, stage, f"{p['name']} {p['version']}", target=root,
                                                dll="winhttp=n,b" if p["name"] == "BSIPA" else "",
                                                source=f"{SOURCE}{p['vid']}"))
    except BaseException:
        for rec in reversed(done):
            mods.rollback(game, rec["id"])
        raise
    return done


def remove(game):
    for m in reversed(ours(game)):
        mods.rollback(game, m["id"])
