import glob
import json
import os
import shutil
import signal
import time
from dataclasses import dataclass

from . import steam
from .util import CONFIG_HOME, DATA_HOME, LOG_DIR, pids_of, spawn_detached, terminate

ACTIVE_LINK = os.path.join(CONFIG_HOME, "openxr", "1", "active_runtime.json")


@dataclass
class XRRuntime:
    name: str
    manifest: str
    library: str
    kind: str
    valid: bool


def _manifest_dirs():
    cfg = (os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":")
    data = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    dirs = [os.path.join(d, "openxr", "1") for d in cfg]
    dirs.append("/etc/openxr/1")
    dirs += [os.path.join(d, "openxr", "1") for d in data]
    dirs.append(os.path.join(DATA_HOME, "openxr", "1"))
    dirs += glob.glob(os.path.join(DATA_HOME, "envision", "prefixes", "*", "share", "openxr", "1"))
    return dirs


def _kind(name, path):
    s = f"{name} {path}".lower()
    for k in ("wivrn", "monado", "steamvr"):
        if k in s:
            return k
    if "steamxr" in s:
        return "steamvr"
    return "other"


def _load(manifest):
    try:
        with open(manifest) as f:
            rt = json.load(f).get("runtime", {})
    except (OSError, ValueError, AttributeError):
        return None
    lib = rt.get("library_path", "")
    if not lib:
        return None
    if os.sep in lib and not os.path.isabs(lib):
        lib = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(manifest)), lib))
    valid = os.path.isfile(lib) if os.sep in lib else True
    name = rt.get("name") or os.path.splitext(os.path.basename(manifest))[0].replace("openxr_", "")
    return XRRuntime(name, manifest, lib, _kind(name, os.path.realpath(manifest)), valid)


def discover():
    seen, out = set(), []
    cands = []
    for d in _manifest_dirs():
        cands += sorted(glob.glob(os.path.join(d, "*.json")))
    for root in steam.roots():
        for lib in [root] + steam.libraries():
            cands.append(os.path.join(lib, "steamapps", "common", "SteamVR", "steamxr_linux64.json"))
    for m in cands:
        if os.path.basename(m) == "active_runtime.json" or not os.path.isfile(m):
            continue
        real = os.path.realpath(m)
        if real in seen:
            continue
        seen.add(real)
        rt = _load(m)
        if rt:
            out.append(rt)
    order = {"wivrn": 0, "monado": 1, "other": 2, "steamvr": 3}
    out.sort(key=lambda r: (order[r.kind], r.name.lower()))
    return out


def active():
    for p in [ACTIVE_LINK] + [
        os.path.join(d, "openxr", "1", "active_runtime.json")
        for d in (os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":")
    ] + ["/etc/openxr/1/active_runtime.json"]:
        if os.path.exists(p):
            rt = _load(p)
            if rt:
                rt.manifest = os.path.realpath(p)
            return rt
    return None


def set_active(manifest):
    os.makedirs(os.path.dirname(ACTIVE_LINK), exist_ok=True)
    tmp = ACTIVE_LINK + ".bifluxvr-tmp"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(os.path.realpath(manifest), tmp)
    os.replace(tmp, ACTIVE_LINK)


@dataclass
class Server:
    kind: str
    title: str
    binary: str
    process: str
    dashboard: str = ""
    args: tuple = ()
    manages_runtime: bool = False
    flatpak: str = ""

    @property
    def from_flatpak(self):
        return bool(self.flatpak and not shutil.which(self.binary) and shutil.which("flatpak") and any(
            os.path.isdir(os.path.join(d, "app", self.flatpak))
            for d in (os.path.join(DATA_HOME, "flatpak"), "/var/lib/flatpak")))

    @property
    def installed(self):
        return bool(shutil.which(self.binary)) or self.from_flatpak

    @property
    def running(self):
        return bool(pids_of(self.process))

    def start(self):
        if self.running:
            return
        argv = [self.binary, *self.args]
        if self.from_flatpak:
            argv = ["flatpak", "run", f"--command={self.binary}", self.flatpak, *self.args]
        spawn_detached(argv, os.path.join(LOG_DIR, f"{self.binary}.log"))

    def stop(self, timeout=10.0):
        terminate(pids_of(self.process))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not pids_of(self.process):
                return
            time.sleep(0.2)
        terminate(pids_of(self.process), signal.SIGKILL)

    def open_dashboard(self):
        if self.dashboard and shutil.which(self.dashboard):
            argv = [self.dashboard]
        elif self.dashboard and self.from_flatpak:
            argv = ["flatpak", "run", self.flatpak]
        else:
            return False
        spawn_detached(argv, os.path.join(LOG_DIR, f"{self.dashboard}.log"))
        return True

    @property
    def ipc_path(self):
        return {
            "wivrn": "$XDG_RUNTIME_DIR/wivrn/comp_ipc",
            "monado": "$XDG_RUNTIME_DIR/monado_comp_ipc",
        }.get(self.kind, "")


SERVERS = [
    Server("wivrn", "WiVRn", "wivrn-server", "wivrn-server", "wivrn-dashboard",
           ("--early-active-runtime",), True, "io.github.wivrn.wivrn"),
    Server("monado", "Monado", "monado-service", "monado-service"),
]


def server_for(kind):
    for s in SERVERS:
        if s.kind == kind:
            return s
    return None


def preferred_server():
    rt = active()
    if rt:
        s = server_for(rt.kind)
        if s and s.installed:
            return s
    for s in SERVERS:
        if s.installed:
            return s
    return None


ENV_KEYS = ("PRESSURE_VESSEL_IMPORT_OPENXR_1_RUNTIMES", "PRESSURE_VESSEL_FILESYSTEMS_RW")


def vr_env(kind=None):
    env = {"PRESSURE_VESSEL_IMPORT_OPENXR_1_RUNTIMES": "1"}
    if kind is None:
        rt = active()
        srv = None if rt else preferred_server()
        kind = rt.kind if rt else (srv.kind if srv else None)
    s = server_for(kind) if kind else None
    if s and s.ipc_path:
        env["PRESSURE_VESSEL_FILESYSTEMS_RW"] = s.ipc_path
    return env
