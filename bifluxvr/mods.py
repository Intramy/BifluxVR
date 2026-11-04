import glob
import json
import os
import shutil
import tempfile
import time
import uuid

from . import installer, net, steam
from .util import APP_DATA_DIR, LOG_DIR, atomic_write, slug, spawn_detached
from .voice import t

MODS_DIR = os.path.join(APP_DATA_DIR, "mods")
TOOLS_DIR = os.path.join(APP_DATA_DIR, "tools")

REFRAMEWORK = {
    "883710": "RE2.zip",
    "952060": "RE3.zip",
    "418370": "RE7.zip",
    "1196590": "RE8.zip",
    "2050650": "RE4.zip",
}


class ModError(RuntimeError):
    pass


def game_key(game):
    return slug(game.appid)


def _dir(game):
    return os.path.join(MODS_DIR, game_key(game))


def load_state(game):
    try:
        with open(os.path.join(_dir(game), "state.json")) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"mods": []}


def save_state(game, state):
    atomic_write(os.path.join(_dir(game), "state.json"), json.dumps(state, indent=2))


def installed(game):
    return load_state(game)["mods"]


def game_dir(game):
    if game.custom:
        return os.path.dirname(game.exe)
    exes = installer.find_executables(game.install_dir, game.name)
    shipping = [e for e in exes if e.lower().endswith("-win64-shipping.exe")]
    return os.path.dirname((shipping or exes or [os.path.join(game.install_dir, "x")])[0])


def is_unreal(game):
    root = game.install_dir
    return bool(glob.glob(os.path.join(root, "*", "Binaries", "Win64", "*-Win64-Shipping.exe"))
                or glob.glob(os.path.join(root, "Engine", "Binaries")))


def dll_overrides(game):
    seen = []
    for mod in installed(game):
        for part in (mod.get("dll") or "").split(";"):
            if part and part not in seen:
                seen.append(part)
    return ";".join(seen)


def launch_env(game):
    dll = dll_overrides(game)
    if not dll:
        return {}
    return {"WINEDLLOVERRIDES": f'"{dll}"' if ";" in dll else dll}


def sync_launch_options(game):
    env = launch_env(game)
    if env:
        steam.set_launch_options(game.appid, lambda o: steam.merge_env(o, env))
    else:
        steam.set_launch_options(game.appid, lambda o: steam.strip_env(o, ["WINEDLLOVERRIDES"]))


def _files(root):
    for dirpath, _, files in os.walk(root):
        for f in files:
            full = os.path.join(dirpath, f)
            yield os.path.relpath(full, root)


def install_folder(game, src, name, target=None, dll="", source=""):
    target = target or game_dir(game)
    if not os.path.isdir(target):
        raise ModError(t(f"{target} doesn't exist", f"{target} isn't there anymore."))
    state = load_state(game)
    owned = {os.path.join(m["target"], rel): m["name"] for m in state["mods"] for rel in m["added"] + m["replaced"]}
    files = sorted(_files(src))
    if not files:
        raise ModError(t("The mod contains no files", "That mod is empty."))
    clash = next((owned[os.path.join(target, rel)] for rel in files if os.path.join(target, rel) in owned), None)
    if clash:
        raise ModError(t(f"It overwrites files from {clash}. Remove that mod first.",
                         f"It would overwrite {clash}'s files. Remove that one first."))

    mod_id = uuid.uuid4().hex[:10]
    backup = os.path.join(_dir(game), mod_id)
    record = {"id": mod_id, "name": name, "source": source, "target": target, "dll": dll,
              "installed": int(time.time()), "added": [], "replaced": [], "dirs": []}
    try:
        for rel in files:
            dest = os.path.join(target, rel)
            parent = os.path.dirname(dest)
            missing = []
            while not os.path.isdir(parent):
                missing.append(parent)
                parent = os.path.dirname(parent)
            for d in reversed(missing):
                os.mkdir(d)
                record["dirs"].append(os.path.relpath(d, target))
            if os.path.lexists(dest):
                keep = os.path.join(backup, rel)
                os.makedirs(os.path.dirname(keep), exist_ok=True)
                shutil.copy2(dest, keep, follow_symlinks=False)
                record["replaced"].append(rel)
            else:
                record["added"].append(rel)
            shutil.copy2(os.path.join(src, rel), dest)
    except BaseException:
        _undo(record, backup)
        raise
    state["mods"].append(record)
    save_state(game, state)
    return record


def install_archive(game, archive, name=None, dll="", source=""):
    name = name or installer.suggest_name(archive)
    with tempfile.TemporaryDirectory(dir=_ensure(MODS_DIR), prefix=".unpack-") as work:
        installer.extract(archive, work)
        entries = os.listdir(work)
        root = work
        if len(entries) == 1 and os.path.isdir(os.path.join(work, entries[0])) and \
                slug(entries[0]) == slug(os.path.splitext(os.path.basename(archive))[0]):
            root = os.path.join(work, entries[0])
        return install_folder(game, root, name, dll=dll, source=source or os.path.basename(archive))


def _undo(record, backup):
    target = record["target"]
    for rel in record["added"]:
        try:
            os.unlink(os.path.join(target, rel))
        except FileNotFoundError:
            pass
    for rel in record["replaced"]:
        keep = os.path.join(backup, rel)
        if os.path.lexists(keep):
            dest = os.path.join(target, rel)
            if os.path.lexists(dest):
                os.unlink(dest)
            shutil.copy2(keep, dest, follow_symlinks=False)
    for rel in sorted(record["dirs"], key=lambda d: -d.count(os.sep)):
        try:
            os.rmdir(os.path.join(target, rel))
        except OSError:
            pass
    shutil.rmtree(backup, ignore_errors=True)


def rollback(game, mod_id):
    state = load_state(game)
    record = next((m for m in state["mods"] if m["id"] == mod_id), None)
    if not record:
        raise ModError(t("That mod isn't installed", "That mod is already gone"))
    _undo(record, os.path.join(_dir(game), mod_id))
    state["mods"] = [m for m in state["mods"] if m["id"] != mod_id]
    save_state(game, state)
    return record


def _ensure(path):
    os.makedirs(path, exist_ok=True)
    return path


def reframework_asset(game):
    return REFRAMEWORK.get(game.store_id or ("" if game.custom else game.appid), "")


def install_reframework(game, progress=None):
    asset = reframework_asset(game)
    rel = net.github_latest("praydog/REFramework")
    url = next((a["browser_download_url"] for a in rel.get("assets", []) if a["name"] == asset), "")
    if not url:
        raise ModError(t(f"REFramework {rel.get('tag_name', '')} has no {asset}",
                         f"REFramework {rel.get('tag_name', '')} has no {asset} to download"))
    tag = rel.get("tag_name", "")
    archive = net.download(url, os.path.join(_ensure(os.path.join(TOOLS_DIR, "reframework", tag)), asset), progress)
    return install_archive(game, archive, f"REFramework {tag}", dll="dinput8=n,b", source=f"REFramework {tag}")


def uevr_path():
    hits = sorted(glob.glob(os.path.join(TOOLS_DIR, "uevr", "*", "UEVRInjector.exe")), key=os.path.getmtime)
    return hits[-1] if hits else ""


def install_uevr(progress=None):
    rel = net.github_latest("praydog/UEVR")
    tag = rel.get("tag_name", "latest")
    asset = next((a for a in rel.get("assets", []) if a["name"] == "UEVR.zip"), None)
    if not asset:
        raise ModError(t(f"UEVR {tag} has no download", f"UEVR {tag} has nothing to download."))
    dest = os.path.join(TOOLS_DIR, "uevr", tag)
    if os.path.isfile(os.path.join(dest, "UEVRInjector.exe")):
        return dest
    archive = net.download(asset["browser_download_url"], os.path.join(_ensure(TOOLS_DIR), "UEVR.zip"), progress)
    staging = tempfile.mkdtemp(dir=TOOLS_DIR, prefix=".uevr-")
    try:
        installer.extract(archive, staging)
        found = glob.glob(os.path.join(staging, "**", "UEVRInjector.exe"), recursive=True)
        if not found:
            raise ModError(t("The UEVR download has no UEVRInjector.exe",
                             "The UEVR download is missing its injector."))
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(os.path.dirname(found[0]), dest)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        os.unlink(archive)
    return dest


def remove_uevr():
    shutil.rmtree(os.path.join(TOOLS_DIR, "uevr"), ignore_errors=True)


def run_in_prefix(game, exe, proton_dir):
    env = os.environ.copy()
    log = os.path.join(LOG_DIR, f"{slug(game.name)}-{slug(os.path.basename(exe))}.log")
    if game.custom:
        pfx = game.prefix
        os.makedirs(pfx, exist_ok=True)
        if shutil.which("umu-run"):
            env.update(WINEPREFIX=pfx, GAMEID=env.get("GAMEID", "umu-default"))
            if proton_dir:
                env["PROTONPATH"] = proton_dir
            spawn_detached(["umu-run", exe], log, env=env, cwd=os.path.dirname(exe))
            return
        compat = pfx
    else:
        pfx = steam.compat_prefix(game.appid)
        if not pfx:
            raise ModError(t("Start the game once from Steam first so it gets a Wine prefix",
                             "Run the game once through Steam first. It needs a Wine prefix to live in."))
        proton_dir = steam.proton_for_prefix(pfx) or proton_dir
        compat = os.path.dirname(pfx)
    if not proton_dir or not os.path.isfile(os.path.join(proton_dir, "proton")):
        raise ModError(t("No matching Proton build found", "Couldn't find the Proton this game uses"))
    roots = steam.roots()
    env.pop("WINEPREFIX", None)
    env["STEAM_COMPAT_DATA_PATH"] = compat
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = roots[0] if roots else ""
    spawn_detached([os.path.join(proton_dir, "proton"), "run", exe], log, env=env, cwd=os.path.dirname(exe))
