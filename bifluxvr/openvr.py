# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
"""OpenVR → OpenXR translation: manages ~/.config/openvr/openvrpaths.vrpath
and the xrizer / OpenComposite installations it points at.

Proton's vrclient bridge loads whatever Linux OpenVR runtime this file
names, so pointing it at xrizer gives Windows OpenVR games a direct path to
the OpenXR runtime with no SteamVR in between."""

import glob
import json
import os
import shutil
import stat
import tempfile
import urllib.request
import zipfile
import tarfile
from dataclasses import dataclass

from . import net, steam
from .voice import t
from .util import CONFIG_HOME, DATA_HOME, atomic_write

VRPATH = os.path.join(CONFIG_HOME, "openvr", "openvrpaths.vrpath")
XRIZER_REPO = "Supreeeme/xrizer"
LOCAL_XRIZER = os.path.join(DATA_HOME, "bifluxvr", "xrizer")


@dataclass
class Layer:
    kind: str  # xrizer | opencomposite | steamvr
    title: str
    path: str
    version: str = ""


def _valid(path):
    return os.path.isfile(os.path.join(path, "bin", "linux64", "vrclient.so"))


def _version(path):
    for p in (os.path.join(path, "version.txt"), os.path.join(path, "bin", "version.txt")):
        try:
            with open(p) as f:
                return f.read().strip().splitlines()[0][:40]
        except (OSError, IndexError):
            pass
    return ""


def discover():
    cands = {
        "xrizer": [
            "/opt/xrizer", "/usr/lib/xrizer", "/usr/lib64/xrizer", "/usr/share/xrizer",
            os.path.join(DATA_HOME, "xrizer"),
            *sorted(glob.glob(os.path.join(LOCAL_XRIZER, "*")), reverse=True),
            *glob.glob(os.path.join(DATA_HOME, "envision", "prefixes", "*", "xrizer")),
        ],
        "opencomposite": [
            "/opt/opencomposite", "/opt/OpenComposite", "/usr/lib/opencomposite",
            "/usr/lib/OpenComposite", "/usr/local/lib/OpenComposite", "/usr/lib64/opencomposite",
            "/usr/share/opencomposite", os.path.join(DATA_HOME, "OpenComposite"),
            *glob.glob(os.path.join(DATA_HOME, "envision", "prefixes", "*", "OpenComposite")),
        ],
        "steamvr": [
            os.path.join(lib, "steamapps", "common", "SteamVR") for lib in steam.libraries()
        ],
    }
    titles = {"xrizer": "xrizer", "opencomposite": "OpenComposite", "steamvr": "SteamVR"}
    seen, out = set(), []
    for kind, paths in cands.items():
        for p in paths:
            if _valid(p) and os.path.realpath(p) not in seen:
                seen.add(os.path.realpath(p))
                out.append(Layer(kind, titles[kind], p, _version(p)))
    return out


def read_vrpath():
    try:
        with open(VRPATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def current_runtime():
    rts = read_vrpath().get("runtime") or []
    return rts[0] if rts else ""


def classify(path):
    if not path:
        return ""
    for layer in discover():
        if os.path.realpath(layer.path) == os.path.realpath(path):
            return layer.kind
    low = path.lower()
    if "xrizer" in low:
        return "xrizer"
    if "opencomposite" in low:
        return "opencomposite"
    if "steamvr" in low:
        return "steamvr"
    return "other"


def is_locked():
    try:
        return not (os.stat(VRPATH).st_mode & stat.S_IWUSR)
    except OSError:
        return False


def set_runtime(path, lock=None):
    """Point OpenVR at `path`, preserving the rest of the vrpath file."""
    was_locked = is_locked()
    data = read_vrpath()
    data.setdefault("config", [os.path.join(steam.roots()[0], "config")] if steam.roots() else [])
    data.setdefault("external_drivers", None)
    data.setdefault("jsonid", "vrpathreg")
    data.setdefault("log", [os.path.join(steam.roots()[0], "logs")] if steam.roots() else [])
    data["runtime"] = [path]
    data["version"] = 1
    lock = was_locked if lock is None else lock
    atomic_write(VRPATH, json.dumps(data, indent=1) + "\n", mode=0o444 if lock else 0o644)
    _sync_wivrn(path)


WIVRN_CONFIG = os.path.join(CONFIG_HOME, "wivrn", "config.json")


def _sync_wivrn(path):
    """WiVRn rewrites the vrpath file with its own `openvr-compat-path` each
    time it starts; keep that setting in step with the user's choice."""
    if not shutil.which("wivrn-server") or classify(path) == "steamvr":
        return
    try:
        with open(WIVRN_CONFIG) as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            return  # don't clobber a config we don't understand
    except FileNotFoundError:
        cfg = {}
    except (OSError, ValueError):
        return
    if cfg.get("openvr-compat-path") != path:
        cfg["openvr-compat-path"] = path
        atomic_write(WIVRN_CONFIG, json.dumps(cfg, indent=2) + "\n")


def set_locked(lock):
    if not os.path.exists(VRPATH):
        return
    mode = os.stat(VRPATH).st_mode
    os.chmod(VRPATH, (mode & ~0o222) if lock else (mode | stat.S_IWUSR))


def latest_xrizer_version():
    return net.github_latest(XRIZER_REPO).get("tag_name", "")


def install_latest_xrizer(progress=None):
    """Download the latest xrizer release into ~/.local/share/bifluxvr/xrizer
    and return the runtime directory. Runs on a worker thread."""
    rel = net.github_latest(XRIZER_REPO)
    tag = rel.get("tag_name", "latest")
    assets = [a for a in rel.get("assets", [])
              if a["name"].lower().startswith("xrizer") and a["name"].endswith((".zip", ".tar.xz", ".tar.gz"))]
    if not assets:
        raise RuntimeError(t(f"xrizer {tag} has no downloadable build", f"xrizer {tag} forgot to upload a build. Try later?"))
    asset = assets[0]
    dest = os.path.join(LOCAL_XRIZER, tag)
    if _valid(dest):
        return dest
    os.makedirs(LOCAL_XRIZER, exist_ok=True)
    work = tempfile.mkdtemp(dir=LOCAL_XRIZER, prefix=".dl-")
    try:
        archive = os.path.join(work, asset["name"])
        req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "BifluxVR"})
        with urllib.request.urlopen(req, timeout=60) as r, open(archive, "wb") as f:
            total = int(r.headers.get("Content-Length") or asset.get("size") or 0)
            done = 0
            while chunk := r.read(1 << 16):
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
        out = os.path.join(work, "x")
        os.makedirs(out)
        if archive.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                for member in z.namelist():
                    target = os.path.realpath(os.path.join(out, member))
                    if not target.startswith(os.path.realpath(out) + os.sep):
                        raise RuntimeError(t("archive contains unsafe paths", "that download tried to put files where it shouldn't. Blocked."))
                z.extractall(out)
        else:
            with tarfile.open(archive) as tar:
                tar.extractall(out, filter="data")
        found = None
        for dirpath, _, files in os.walk(out):
            if dirpath.endswith(os.path.join("bin", "linux64")) and "vrclient.so" in files:
                found = os.path.dirname(os.path.dirname(dirpath))
                break
        if not found:
            raise RuntimeError(t("downloaded archive does not contain bin/linux64/vrclient.so",
                               "the download is missing its vrclient.so. That's the whole point of it."))
        so = os.path.join(found, "bin", "linux64", "vrclient.so")
        os.chmod(so, os.stat(so).st_mode | 0o755)
        if not os.path.exists(os.path.join(found, "version.txt")):
            with open(os.path.join(found, "version.txt"), "w") as f:
                f.write(tag + "\n")
        shutil.move(found, dest)
        return dest
    finally:
        shutil.rmtree(work, ignore_errors=True)
