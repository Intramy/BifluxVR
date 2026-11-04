import os
import re
import shutil
import time

from . import installer, launcher, steam
from .voice import t

_SCAN_SKIP = {"steamapps", "compatdata", "drive_c", "pfx", "prefixes", "node_modules", "__pycache__"}
_WINE_DIRS = {"common files", "internet explorer", "windows media player", "windows nt",
              "windows defender", "windows mail", "windows photo viewer", "windows sidebar",
              "windowspowershell", "microsoft.net", "msbuild", "reference assemblies", "steam", "temp",
              "package cache", "installshield installation information"}
_LNK_ANSI = re.compile(rb"[A-Za-z]:\\[^\x00\r\n]{1,400}?\.exe", re.I)
_LNK_WIDE = re.compile(rb"[A-Za-z]\x00:\x00\\\x00(?:[^\x00]\x00){1,400}?\.\x00[eE]\x00[xX]\x00[eE]\x00")
_UNINSTALLER = re.compile(r"unins|uninst|remove", re.I)


class Cancelled(Exception):
    pass


class Job:
    def __init__(self):
        self.cancel = False
        self.finish = False


def scan(folder, skip=(), depth=3):
    found = []
    top = folder.rstrip(os.sep).count(os.sep)
    skip = {os.path.realpath(p) for p in skip}
    for dirpath, dirs, files in os.walk(folder):
        keep = []
        for d in dirs:
            full = os.path.realpath(os.path.join(dirpath, d))
            if not d.startswith(".") and d.lower() not in _SCAN_SKIP and full not in skip:
                keep.append(d)
        dirs[:] = keep if dirpath.count(os.sep) - top < depth else []
        for f in files:
            path = os.path.join(dirpath, f)
            if installer.runnable(f):
                if installer.is_setup(path):
                    found.append(installer.inspect_setup(path))
            elif installer.supported(f) and (installer.volume_number(f) or 1) == 1:
                try:
                    info = installer.inspect(path)
                except (installer.InstallError, OSError):
                    continue
                if info.setup:
                    found.append(info)
    found.sort(key=lambda i: i.name.lower())
    return found


def drive_c(prefix):
    for cand in (os.path.join(prefix, "drive_c"), os.path.join(prefix, "pfx", "drive_c")):
        if os.path.isdir(cand):
            return cand
    return ""


def _wine_walk(c):
    for dirpath, dirs, files in os.walk(c):
        dirs[:] = [d for d in dirs if d.lower() not in _WINE_DIRS and not (dirpath == c and d.lower() == "windows")]
        yield dirpath, files


def _link_target(path, c):
    with open(path, "rb") as f:
        data = f.read(1 << 16)
    hits = [m.decode("utf-16-le", "ignore") for m in _LNK_WIDE.findall(data)]
    hits += [m.decode("latin-1") for m in _LNK_ANSI.findall(data)]
    for hit in hits:
        if hit[0] in "cC":
            full = os.path.join(c, hit[3:].replace("\\", "/"))
            if os.path.isfile(full):
                return full
    return ""


def find_installed(prefix, hint=""):
    c = drive_c(prefix)
    if not c:
        return []
    linked, loose = set(), set()
    for dirpath, files in _wine_walk(c):
        for f in files:
            full = os.path.join(dirpath, f)
            low = f.lower()
            if low.endswith(".lnk"):
                try:
                    target = _link_target(full, c)
                except OSError:
                    continue
                if target:
                    linked.add(target)
            elif low.endswith(".exe"):
                loose.add(full)

    def rank(paths):
        scored = []
        for p in paths:
            if _UNINSTALLER.search(os.path.basename(p)):
                continue
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            scored.append((installer._score(p, c, hint, size), p))
        return [p for _, p in sorted(scored, key=lambda s: -s[0])]

    first = rank(linked)
    return first + [p for p in rank(loose) if p not in first]


def install(info, parent, name, proton_dir, job, progress=None, started=None):
    os.makedirs(parent, exist_ok=True)
    free = shutil.disk_usage(parent).free
    if info.size > free:
        raise installer.InstallError(t(f"Not enough space: needs {installer.human(info.size)}, {installer.human(free)} free",
                                       f"Disk's too full: needs {installer.human(info.size)}, {installer.human(free)} free"))
    root = installer._unique_dir(parent, name)
    prefix = os.path.join(root, "prefix")
    staging = root + ".unpacked"
    os.makedirs(prefix)
    try:
        setup = info.path
        if not installer.runnable(info.path):
            os.makedirs(staging)
            installer.extract(info.path, staging, progress)
            setup = os.path.join(staging, info.setup)
        proc = launcher.run_setup(setup, prefix, proton_dir, name)
        if started:
            started()
        while proc.poll() is None:
            if job.cancel or job.finish:
                launcher.kill_tree(proc.pid)
                break
            time.sleep(0.4)
        if job.cancel:
            raise Cancelled()
        exes = find_installed(prefix, name)
        if not exes:
            raise installer.InstallError(t("The installer closed without installing anything",
                                           "The installer closed and nothing got installed"))
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    apis, _ = steam.detect_vr(os.path.dirname(exes[0]), 5)
    return root, exes[0], prefix, apis
