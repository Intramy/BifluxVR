import os
import shutil
import time
from dataclasses import dataclass, field

from . import launcher, mods, riftlift, steam
from .util import APP_DATA_DIR, HOME

_USER_DIRS = ("Desktop", "Documents", "Downloads", "Games", "Music", "Pictures", "Public", "Templates", "Videos",
              ".local", ".local/share", ".config", ".cache", ".steam", ".wine")


@dataclass
class Plan:
    steam: bool = False
    paths: list = field(default_factory=list)
    shared: str = ""
    riftlift: str = ""


def _entry(game, cfg):
    gid = game.appid.split(":", 1)[1]
    return next((c for c in cfg["custom_games"] if c["id"] == gid), None)


def _install_dirs(cfg):
    return [cfg[k] for k in ("install_dir_vr", "install_dir_simple", "install_dir_setup") if cfg.get(k)]


def _protected(cfg):
    out = {"/", HOME, APP_DATA_DIR, os.path.join(APP_DATA_DIR, "prefixes"), os.path.join(HOME, "Games", "BifluxVR")}
    out.update(os.path.join(HOME, d) for d in _USER_DIRS)
    out.update(_install_dirs(cfg))
    out.update(steam.roots())
    out.update(steam.libraries())
    from . import storage
    for d in storage.drives():
        out.update((d.mount, d.base, os.path.dirname(d.base)))
    return {os.path.realpath(p) for p in out}


def removable(path, cfg, game=None):
    real = os.path.realpath(path)
    if not os.path.isabs(path) or real.count(os.sep) < 2 or os.path.islink(path):
        return False
    for p in _protected(cfg):
        if real == p or p.startswith(real + os.sep):
            return False
    for c in cfg["custom_games"]:
        if game and game.appid == f"custom:{c['id']}":
            continue
        if os.path.realpath(c["exe"]).startswith(real + os.sep):
            return False
    return True


def game_root(entry, cfg):
    if entry.get("root"):
        return entry["root"]
    for rec in cfg.get("meta_games", {}).values():
        if rec.get("custom_id") == entry["id"]:
            return rec["dir"]
    exe = os.path.realpath(entry["exe"])
    for base in _install_dirs(cfg):
        base = os.path.realpath(base)
        if exe.startswith(base + os.sep):
            return os.path.join(base, os.path.relpath(exe, base).split(os.sep)[0])
    return os.path.dirname(entry["exe"])


def plan(game, cfg):
    if not game.custom:
        return Plan(steam=True)
    entry = _entry(game, cfg)
    if not entry:
        return Plan()
    if entry.get("engine") == "riftlift":
        return Plan(riftlift=entry["riftlift"])
    out = Plan()
    root = game_root(entry, cfg)
    if removable(root, cfg, game):
        out.paths.append(root)
    else:
        out.shared = root
    prefix = entry.get("prefix") or launcher.default_prefix(entry["name"])
    others = [c for c in cfg["custom_games"] if c["id"] != entry["id"]]
    in_use = any((c.get("prefix") or launcher.default_prefix(c["name"])) == prefix for c in others)
    inside = out.paths and prefix.startswith(out.paths[0] + os.sep)
    if os.path.isdir(prefix) and not inside and not in_use and removable(prefix, cfg, game):
        out.paths.append(prefix)
    return out


def _unlock(path):
    for dirpath, dirs, _ in os.walk(path):
        for d in dirs:
            full = os.path.join(dirpath, d)
            if not os.path.islink(full):
                try:
                    os.chmod(full, 0o755)
                except OSError:
                    pass


def wipe(path):
    if os.path.isdir(path) and not os.path.islink(path):
        _unlock(path)
        shutil.rmtree(path)


def _forget(game, cfg):
    cfg.get("game_folders", {}).pop(game.appid, None)
    cfg.get("ratings", {}).pop(game.appid, None)
    if not game.custom:
        return
    gid = game.appid.split(":", 1)[1]
    cfg["custom_games"] = [c for c in cfg["custom_games"] if c["id"] != gid]
    metas = cfg.get("meta_games", {})
    for key in [k for k, r in metas.items() if r.get("custom_id") == gid]:
        del metas[key]


def run(game, cfg, chosen):
    if chosen.riftlift:
        riftlift.uninstall(chosen.riftlift)
    for path in chosen.paths:
        wipe(path)
    wipe(os.path.join(mods.MODS_DIR, mods.game_key(game)))
    _forget(game, cfg)


def wait_for_steam(game, cfg, patience=600):
    manifest = os.path.join(game.library, "steamapps", f"appmanifest_{game.appid}.acf")
    deadline = time.time() + patience
    while os.path.exists(manifest):
        if time.time() > deadline:
            return False
        time.sleep(2)
    pfx = steam.compat_prefix(game.appid)
    if pfx and game.appid.isdigit():
        wipe(os.path.dirname(pfx))
    wipe(os.path.join(mods.MODS_DIR, mods.game_key(game)))
    _forget(game, cfg)
    return True
