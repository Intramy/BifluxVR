import glob
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field, asdict

from . import vdf
from .voice import t
from .util import CACHE_DIR, atomic_write, process_running

HOME = os.path.expanduser("~")

_ROOT_CANDIDATES = [
    "~/.local/share/Steam",
    "~/.steam/steam",
    "~/.steam/root",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",
]

_TOOL_NAME = re.compile(
    r"^(Proton\b|Steam Linux Runtime|Steamworks Common|SteamVR\b|Steam Audio)", re.I
)

_MARKERS = {
    "openvr_api.dll": "openvr",
    "xrsdkopenvr.dll": "openvr",
    "openxr_loader.dll": "openxr",
    "unityopenxr.dll": "openxr",
    "ovrplugin.dll": "oculus",
    "libovrplatform64_1.dll": "oculus",
    "libovrrt64_1.dll": "oculus",
}
_ENGINE_BUNDLED = re.compile(r"[\\/]engine[\\/](binaries[\\/]thirdparty|plugins)[\\/]", re.I)

SCAN_VERSION = 2


@dataclass
class Game:
    appid: str
    name: str
    install_dir: str
    library: str = ""
    buildid: str = ""
    vr_apis: list = field(default_factory=list)
    vr_engine: list = field(default_factory=list)
    custom: bool = False
    exe: str = ""
    mode: str = "vr"
    store_id: str = ""
    prefix: str = ""
    playtime: int = 0
    last_played: int = 0
    kind: str = "game"
    root: str = ""

    @property
    def is_vr(self):
        return bool(self.vr_apis)

    @property
    def primary_api(self):
        apis = self.vr_apis or self.vr_engine
        for api in ("openxr", "openvr", "oculus"):
            if api in apis:
                return api
        return ""


def roots():
    seen, out = set(), []
    for cand in _ROOT_CANDIDATES:
        p = os.path.expanduser(cand)
        if os.path.isdir(os.path.join(p, "steamapps")):
            real = os.path.realpath(p)
            if real not in seen:
                seen.add(real)
                out.append(real)
    return out


def installed():
    return bool(shutil.which("steam") or roots())


def running():
    return process_running("steam", "steamwebhelper")


def _read_vdf(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return vdf.loads(f.read())
    except (OSError, vdf.VDFError):
        return None


def libraries():
    seen, out = set(), []
    for root in roots():
        cands = [root]
        doc = _read_vdf(os.path.join(root, "steamapps", "libraryfolders.vdf"))
        folders = vdf.get(doc, "libraryfolders") if doc else None
        if isinstance(folders, list):
            for _, entry in folders:
                p = vdf.get(entry, "path") if isinstance(entry, list) else entry
                if isinstance(p, str):
                    cands.append(p)
        for c in cands:
            if os.path.isdir(os.path.join(c, "steamapps")):
                real = os.path.realpath(c)
                if real not in seen:
                    seen.add(real)
                    out.append(real)
    return out


def detect_vr(install_dir, max_depth=8):
    confident, engine = set(), set()
    base_depth = install_dir.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(install_dir):
        if dirpath.count(os.sep) - base_depth >= max_depth:
            dirnames[:] = []
        for fn in filenames:
            api = _MARKERS.get(fn.lower())
            if api:
                full = os.path.join(dirpath, fn)
                (engine if _ENGINE_BUNDLED.search(full) else confident).add(api)
    return sorted(confident), sorted(engine - confident)


def _cache_path():
    return os.path.join(CACHE_DIR, "vrscan.json")


def _load_cache():
    try:
        with open(_cache_path()) as f:
            data = json.load(f)
        if data.get("version") == SCAN_VERSION:
            return data.get("games", {})
    except (OSError, ValueError):
        pass
    return {}


def scan_games():
    cache = _load_cache()
    new_cache, games = {}, []
    for lib in libraries():
        for acf in glob.glob(os.path.join(lib, "steamapps", "appmanifest_*.acf")):
            state = vdf.get(_read_vdf(acf) or [], "AppState")
            if not isinstance(state, list):
                continue
            appid = vdf.get(state, "appid", "")
            name = vdf.get(state, "name", "") or f"App {appid}"
            installdir = vdf.get(state, "installdir", "")
            if not appid or not installdir or _TOOL_NAME.search(name):
                continue
            full = os.path.join(lib, "steamapps", "common", installdir)
            if not os.path.isdir(full):
                continue
            buildid = vdf.get(state, "buildid", "")
            key = f"{appid}:{buildid}:{full}"
            hit = cache.get(appid)
            if hit and hit.get("key") == key:
                apis, eng = hit["apis"], hit["engine"]
            else:
                apis, eng = detect_vr(full)
            new_cache[appid] = {"key": key, "apis": apis, "engine": eng}
            games.append(Game(appid, name, full, lib, buildid, apis, eng))
    try:
        atomic_write(_cache_path(), json.dumps({"version": SCAN_VERSION, "games": new_cache}))
    except OSError:
        pass
    games.sort(key=lambda g: g.name.lower())
    return games


def playtimes():
    out = {}
    for cfg in _localconfigs():
        apps = vdf.path(_read_vdf(cfg) or [], "UserLocalConfigStore", "Software", "Valve", "Steam", "apps")
        if not isinstance(apps, list):
            continue
        for appid, node in apps:
            if not isinstance(node, list):
                continue
            try:
                minutes = int(vdf.get(node, "Playtime", 0) or 0)
                last = int(vdf.get(node, "LastPlayed", 0) or 0)
            except ValueError:
                continue
            old = out.get(appid, (0, 0))
            out[appid] = (max(old[0], minutes), max(old[1], last))
    return out


def artwork(appid, kind="library_600x900"):
    exts = (".png",) if kind == "logo" else (".jpg", ".png")
    for root in roots():
        lc = os.path.join(root, "appcache", "librarycache")
        for ext in exts:
            for p in (os.path.join(lc, appid, kind + ext), os.path.join(lc, f"{appid}_{kind}{ext}")):
                if os.path.isfile(p):
                    return p
        d = os.path.join(lc, appid)
        if os.path.isdir(d):
            for sub in sorted(os.listdir(d)):
                for ext in exts:
                    p = os.path.join(d, sub, kind + ext)
                    if os.path.isfile(p):
                        return p
    return None


def proton_builds():
    found = {}
    dirs = []
    for lib in libraries():
        dirs += glob.glob(os.path.join(lib, "steamapps", "common", "*"))
    for root in roots():
        dirs += glob.glob(os.path.join(root, "compatibilitytools.d", "*"))
    dirs += glob.glob(os.path.expanduser("~/.steam/root/compatibilitytools.d/*"))
    dirs += glob.glob("/usr/share/steam/compatibilitytools.d/*")
    for d in dirs:
        script = os.path.join(d, "proton")
        if os.path.isfile(script):
            found.setdefault(os.path.realpath(d), (os.path.basename(d), d))

    def rank(item):
        name = item[0].lower()
        if "experimental" in name:
            return (0, name)
        if "ge" in name:
            return (1, name)
        return (2, name)

    return sorted(found.values(), key=rank)


def compat_prefix(appid):
    for lib in libraries():
        pfx = os.path.join(lib, "steamapps", "compatdata", str(appid), "pfx")
        if os.path.isdir(pfx):
            return pfx
    return ""


def _prefix_version(text):
    m = re.search(r'^CURRENT_PREFIX_VERSION="([^"]+)"', text, re.M)
    return m.group(1) if m else ""


def proton_for_prefix(pfx):
    try:
        with open(os.path.join(os.path.dirname(pfx), "version")) as f:
            want = f.read().strip()
    except OSError:
        return ""
    for _, d in proton_builds():
        try:
            with open(os.path.join(d, "proton"), encoding="utf-8", errors="replace") as f:
                if _prefix_version(f.read()) == want:
                    return d
        except OSError:
            continue
    return ""


def _localconfigs():
    out = []
    for root in roots():
        out += glob.glob(os.path.join(root, "userdata", "*", "config", "localconfig.vdf"))
    return out


def _app_node(doc, appid, create=False):
    steam = vdf.path(doc, "UserLocalConfigStore", "Software", "Valve", "Steam")
    if steam is None:
        if not create:
            return None
        node = vdf.ensure(doc, "UserLocalConfigStore")
        for k in ("Software", "Valve", "Steam"):
            node = vdf.ensure(node, k)
        steam = node
    apps = vdf.get(steam, "apps")
    if not isinstance(apps, list):
        if not create:
            return None
        apps = vdf.ensure(steam, "apps")
    app = vdf.get(apps, appid)
    if not isinstance(app, list):
        if not create:
            return None
        app = vdf.ensure(apps, appid)
    return app


def get_launch_options(appid):
    for cfg in _localconfigs():
        app = _app_node(_read_vdf(cfg) or [], appid)
        value = vdf.get(app, "LaunchOptions") if app else None
        if value:
            return value
    return ""


def set_launch_options(appid, transform):
    if running():
        raise RuntimeError(t("Quit Steam first. It overwrites its config on exit.",
                             "Close Steam first. It overwrites its config on exit."))
    changed = 0
    for cfg in _localconfigs():
        with open(cfg, encoding="utf-8") as f:
            original = f.read()
        doc = vdf.loads(original)
        app = _app_node(doc, appid, create=True)
        old = vdf.get(app, "LaunchOptions", "") or ""
        new = transform(old)
        if new == old:
            continue
        if new:
            vdf.put(app, "LaunchOptions", new)
        else:
            vdf.remove(app, "LaunchOptions")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup, n = f"{cfg}.bifluxvr-{stamp}.bak", 1
        while os.path.exists(backup):
            backup, n = f"{cfg}.bifluxvr-{stamp}-{n}.bak", n + 1
        shutil.copy2(cfg, backup)
        atomic_write(cfg, vdf.dumps(doc))
        changed += 1
    return changed


def merge_env(options, env, strip_keys=()):
    rest = strip_env(options, set(env) | set(strip_keys))
    prefix = " ".join(f"{k}={v}" for k, v in env.items())
    if not rest:
        return f"{prefix} %command%"
    if "%command%" in rest:
        return f"{prefix} {rest}"
    return f"{prefix} %command% {rest}"


def strip_env(options, keys):
    out = options or ""
    for k in keys:
        out = re.sub(rf"(^|\s){re.escape(k)}=\S*", " ", out)
    out = " ".join(out.split())
    return "" if out == "%command%" else out


def has_env(options, env):
    return all(f"{k}={v}" in (options or "").split() for k, v in env.items())


def to_dict(game):
    return asdict(game)
