# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
"""What BifluxVR needs, and how each distribution provides it.

plan() turns the system into a list of Items: per component, whether it is
missing, outdated or current, and how it would be installed. System
packages go through the distro's package manager (as one pkexec batch, see
root_helper.py); the rest is per-user: Flatpak, downloads, BifluxVR itself."""

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from bifluxvr import installer, openvr, steam
from bifluxvr.util import DATA_HOME, HOME

FLATHUB = "https://dl.flathub.org/repo/flathub.flatpakrepo"
SOURCE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(DATA_HOME, "bifluxvr", "app")


def _which(*names):
    return lambda: any(shutil.which(n) for n in names)


def _flatpak_has(app):
    return any(os.path.isdir(os.path.join(d, "app", app))
               for d in (os.path.join(DATA_HOME, "flatpak"), "/var/lib/flatpak"))


# key, title, why, detect, {manager: [alternative package lists]}, fallback
COMPONENTS = [
    ("gtk", "GTK 4 & libadwaita", "The toolkit BifluxVR's window is built with.",
     lambda: True, {
         "pacman": [["python-gobject", "gtk4", "libadwaita"]],
         "dnf": [["python3-gobject", "gtk4", "libadwaita"]],
         "apt": [["python3-gi", "gir1.2-gtk-4.0", "gir1.2-adw-1"]],
         "zypper": [["python3-gobject", "typelib-1_0-Gtk-4_0", "typelib-1_0-Adw-1"]],
         "emerge": [["dev-python/pygobject", "gui-libs/gtk", "gui-libs/libadwaita"]],
     }, None),
    ("wivrn", "WiVRn", "Streams games to your Quest or Pico and is the OpenXR runtime they run on.",
     lambda: bool(shutil.which("wivrn-server")) or _flatpak_has("io.github.wivrn.wivrn"), {
         "pacman": [["wivrn-server", "wivrn-dashboard"], ["wivrn"]],
         "dnf": [["wivrn"]],
         "apt": [["wivrn"]],
         "zypper": [["wivrn"]],
         "emerge": [["media-libs/wivrn"]],
     }, "flatpak:io.github.wivrn.wivrn"),
    ("steam", "Steam", "Brings Proton, the layer that runs Windows games, and your Steam library.",
     steam.installed, {
         "pacman": [["steam"]],
         "dnf": [["steam"]],
         "apt": [["steam-installer"], ["steam-launcher"], ["steam"]],
         "zypper": [["steam"]],
         "emerge": [["games-util/steam-launcher"]],
     }, "flatpak:com.valvesoftware.Steam"),
    ("umu", "umu-launcher", "Runs Windows games that aren't on Steam through Proton.",
     _which("umu-run"), {
         "pacman": [["umu-launcher"]],
         "dnf": [["umu-launcher"]],
         "apt": [["umu-launcher"]],
         "zypper": [["umu-launcher"]],
         "emerge": [["games-util/umu-launcher"]],
     }, "zipapp"),
    ("adb", "Android platform tools (adb)", "Reads your headset's battery over USB or Wi-Fi.",
     _which("adb"), {
         "pacman": [["android-tools"]],
         "dnf": [["android-tools"]],
         "apt": [["adb"]],
         "zypper": [["android-tools"]],
         "emerge": [["dev-util/android-tools"]],
     }, None),
    ("rar", "unrar", "Opens .rar games and mods dropped into the Drop Folder.",
     lambda: installer.rar_backend() is not None, {
         "pacman": [["unrar"], ["libarchive"]],
         "dnf": [["unrar"], ["bsdtar"]],
         "apt": [["unrar"], ["libarchive-tools"]],
         "zypper": [["unrar"], ["bsdtar"]],
         "emerge": [["app-arch/unrar"], ["app-arch/libarchive"]],
     }, None),
    ("pactl", "PipeWire/PulseAudio tools (pactl)", "Moves your sound into the headset while you play.",
     _which("pactl"), {
         "pacman": [["libpulse"]],
         "dnf": [["pulseaudio-utils"]],
         "apt": [["pulseaudio-utils"]],
         "zypper": [["pulseaudio-utils"]],
         "emerge": [["media-libs/libpulse"]],
     }, None),
]
FLATPAK_PKG = {"pacman": "flatpak", "dnf": "flatpak", "apt": "flatpak", "zypper": "flatpak", "emerge": "sys-apps/flatpak"}


@dataclass
class Item:
    key: str
    title: str
    why: str
    method: str  # pkg | flatpak | zipapp | xrizer | bifluxvr | none
    action: str  # install | update | skip | fail
    detail: str = ""
    packages: list = field(default_factory=list)
    needs_flatpak: bool = False


def _run(argv, timeout=180):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "LC_ALL": "C"})
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def _field(text, name):
    m = re.search(rf"^{name}\s*:\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else ""


class Manager:
    """Installed and candidate versions of a package, as the distro sees them."""

    def __init__(self, name):
        self.name = name

    def installed(self, pkg):
        n = self.name
        if n == "pacman":
            rc, out = _run(["pacman", "-Q", pkg])
            return out.split()[1] if rc == 0 and len(out.split()) > 1 else ""
        if n in ("dnf", "zypper"):
            rc, out = _run(["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}", pkg])
            return out.strip() if rc == 0 else ""
        if n == "apt":
            v = _field(_run(["apt-cache", "policy", pkg])[1], r"\s*Installed")
            return "" if v in ("", "(none)") else v
        if n == "emerge":
            return _run(["portageq", "best_version", "/", pkg])[1].strip().removeprefix(pkg + "-")
        return ""

    def candidate(self, pkg):
        n = self.name
        if n == "pacman":
            rc, out = _run(["pacman", "-Si", pkg])
            return _field(out, "Version") if rc == 0 else ""
        if n == "dnf":
            out = _run(["dnf", "-q", "repoquery", "--latest-limit=1", "--qf", "%{version}-%{release}\n", pkg])[1]
            return out.strip().splitlines()[-1] if out.strip() else ""
        if n == "apt":
            v = _field(_run(["apt-cache", "policy", pkg])[1], r"\s*Candidate")
            return "" if v in ("", "(none)") else v
        if n == "zypper":
            return _field(_run(["zypper", "--non-interactive", "--no-refresh", "info", pkg])[1], "Version")
        if n == "emerge":
            return _run(["portageq", "best_visible", "/", pkg])[1].strip().removeprefix(pkg + "-")
        return ""

    def outdated(self, pkg, have, best):
        if not have or not best or have == best:
            return False
        n = self.name
        if n == "pacman":
            return _run(["vercmp", best, have])[1].strip().startswith("1")
        if n == "dnf":
            return pkg in _run(["dnf", "-q", "list", "--upgrades", pkg])[1]
        if n == "zypper":
            return "out-of-date" in _field(_run(["zypper", "--non-interactive", "--no-refresh", "info", pkg])[1], "Status")
        if n == "emerge":
            out = _run(["emerge", "-pqu", "--color", "n", pkg])[1]
            return any(" U" in line.split("]")[0] for line in out.splitlines() if pkg in line)
        return True  # apt's candidate is only different when it is newer

    def commands(self, pkgs, update):
        n = self.name
        if n == "pacman":
            return [["pacman", "-S", "--needed", "--noconfirm", *pkgs]]
        if n == "dnf":
            return [["dnf", "upgrade" if update else "install", "-y", *pkgs]]
        if n == "apt":
            return [["apt-get", "install", "-y", *pkgs]]
        if n == "zypper":
            return [["zypper", "--non-interactive", "update" if update else "install",
                     "--auto-agree-with-licenses", *pkgs]]
        if n == "emerge":
            return [["emerge", "--update", "--color=n", "--quiet-build=y",
                     *(["--oneshot"] if update else ["--noreplace"]), *pkgs]]
        return []


def _flatpak_outdated(app):
    out = _run(["flatpak", "remote-ls", "--user", "--updates", "--columns=application"])[1]
    out += _run(["flatpak", "remote-ls", "--system", "--updates", "--columns=application"])[1]
    return app in out.split()


def _native(mgr, key, title, why, detect, alts, fallback):
    if mgr:
        for pkgs in alts.get(mgr.name, []):
            have = {p: mgr.installed(p) for p in pkgs}
            if all(have.values()):
                best = {p: mgr.candidate(p) for p in pkgs}
                old = [p for p in pkgs if mgr.outdated(p, have[p], best[p])]
                if old:
                    detail = ", ".join(f"{p} {have[p]} → {best[p]}" for p in old)
                    return Item(key, title, why, "pkg", "update", detail, old)
                return Item(key, title, why, "pkg", "skip",
                            "Up to date · " + ", ".join(f"{p} {have[p]}" for p in pkgs[:1]), pkgs)
    app = fallback[8:] if fallback and fallback.startswith("flatpak:") else ""
    if app and _flatpak_has(app):
        if _flatpak_outdated(app):
            return Item(key, title, why, "flatpak", "update", f"Flatpak {app}", [app])
        return Item(key, title, why, "flatpak", "skip", f"Up to date · Flatpak {app}", [app])
    if key == "umu" and os.path.realpath(shutil.which("umu-run") or "").startswith(UMU_DIR):
        latest = _umu_latest()
        have = _umu_installed()
        if latest and have and latest["version"] != have:
            return Item(key, title, why, "zipapp", "update", f"{have} → {latest['version']}")
        return Item(key, title, why, "zipapp", "skip", f"Up to date · {have}")
    if detect():
        return Item(key, title, why, "none", "skip", "Already installed (not through your package manager)")
    if mgr:
        for pkgs in alts.get(mgr.name, []):
            best = [mgr.candidate(p) for p in pkgs]
            if all(best):
                return Item(key, title, why, "pkg", "install", " ".join(f"{p} {v}" for p, v in zip(pkgs, best)), pkgs)
    if app:
        return Item(key, title, why, "flatpak", "install", f"From Flathub · {app}", [app],
                    needs_flatpak=not shutil.which("flatpak"))
    if fallback == "zipapp":
        return Item(key, title, why, "zipapp", "install", "Official release from GitHub")
    return Item(key, title, why, "none", "fail",
                f"No package for your distribution. Install {title} manually, then run the installer again.")


# ------------------------------------------------------------ user-level parts

UMU_REPO = "Open-Wine-Components/umu-launcher"
UMU_DIR = os.path.join(DATA_HOME, "bifluxvr", "umu")
LOCAL_BIN = os.path.join(HOME, ".local", "bin")


def _umu_latest():
    from bifluxvr import net
    try:
        rel = net.github_latest(UMU_REPO)
    except (OSError, ValueError):
        return None
    asset = next((a for a in rel.get("assets", []) if a["name"].endswith("zipapp.tar")), None)
    return {"version": rel.get("tag_name", ""), "url": asset["browser_download_url"]} if asset else None


def _umu_installed():
    target = os.path.realpath(shutil.which("umu-run") or "")
    rel = os.path.relpath(target, UMU_DIR) if target.startswith(UMU_DIR) else ""
    return rel.split(os.sep)[0] if rel else ""


def _version_of(path):
    try:
        with open(path) as f:
            m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", f.read())
            return m.group(1) if m else ""
    except OSError:
        return ""


def _xrizer_item():
    title, why = "xrizer", "Translates OpenVR games to OpenXR, so they run without SteamVR."
    mine = [layer for layer in openvr.discover() if layer.kind == "xrizer"]
    try:
        latest = openvr.latest_xrizer_version()
    except (OSError, ValueError):
        latest = ""
    if not mine:
        return Item("xrizer", title, why, "xrizer", "install", f"Latest release {latest}".strip())
    have = max((layer.version for layer in mine), default="")
    if latest and have and _ver(latest) > _ver(have):
        return Item("xrizer", title, why, "xrizer", "update", f"{have} → {latest}")
    return Item("xrizer", title, why, "xrizer", "skip", f"Up to date · {have}" if have else "Installed")


def _ver(text):
    return tuple(int(n) for n in re.findall(r"\d+", text or ""))


def _digest(root):
    base = os.path.join(root, "bifluxvr")
    paths = []
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        paths += [os.path.join(dirpath, n) for n in files]
    h = hashlib.sha256()
    for path in sorted(paths):
        with open(path, "rb") as f:
            h.update(os.path.relpath(path, base).encode() + f.read())
    return h.hexdigest()


def _bifluxvr_item():
    title, why = "BifluxVR", "The app itself, with a menu entry and the bifluxvr command."
    new = _version_of(os.path.join(SOURCE, "bifluxvr", "__init__.py"))
    have = _version_of(os.path.join(APP_DIR, "bifluxvr", "__init__.py"))
    if not have:
        return Item("bifluxvr", title, why, "bifluxvr", "install", f"Version {new}")
    if _digest(SOURCE) == _digest(APP_DIR):
        return Item("bifluxvr", title, why, "bifluxvr", "skip", f"Up to date · {have}")
    return Item("bifluxvr", title, why, "bifluxvr", "update", f"{have} → {new}" if have != new else f"Newer build of {new}")


def plan(manager):
    mgr = Manager(manager) if manager else None
    items = [_native(mgr, *c) for c in COMPONENTS]
    items.append(_xrizer_item())
    items.append(_bifluxvr_item())
    return items

