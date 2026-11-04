import os
import platform
import shutil

from bifluxvr import health

FAMILIES = {
    "arch": "pacman", "fedora": "dnf", "rhel": "dnf", "centos": "dnf",
    "debian": "apt", "ubuntu": "apt", "opensuse": "zypper", "suse": "zypper", "gentoo": "emerge",
}


def os_release():
    out = {}
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(path) as f:
                for raw in f:
                    key, _, value = raw.strip().partition("=")
                    if key:
                        out[key] = value.strip("\"'")
            return out
        except OSError:
            continue
    return out


def is_wsl():
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def os_name():
    name = platform.system() or "Unknown"
    if name.startswith(("CYGWIN", "MSYS", "MINGW")):
        return "Windows"
    if name == "Darwin":
        return "macOS"
    return name


def supported():
    name = os_name()
    if name == "Windows":
        return "Windows"
    if name == "Linux" and is_wsl():
        return "Windows (WSL)"
    if name != "Linux":
        return name
    return None


def package_manager(rel):
    for ident in [rel.get("ID", "")] + rel.get("ID_LIKE", "").split():
        for prefix, manager in FAMILIES.items():
            if ident.startswith(prefix) and shutil.which(manager):
                return manager
    return ""


def _cpu():
    try:
        with open("/proc/cpuinfo") as f:
            for raw in f:
                if raw.startswith("model name"):
                    return raw.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "Unknown"


def _memory():
    try:
        with open("/proc/meminfo") as f:
            kb = int(f.readline().split()[1])
        return f"{kb / 1024 / 1024:.0f} GB"
    except (OSError, IndexError, ValueError):
        return "Unknown"


def _gpus():
    found = health.gpus()
    nv = health.nvidia_version() if any(v == "NVIDIA" for v, _ in found) else ""
    return ", ".join(f"{v} ({d}{' ' + nv if d == 'nvidia' and nv else ''})" for v, d in found) or "Unknown"


def _toolkit():
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk
    return (f"PyGObject {gi.__version__} · GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()} · "
            f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}")


def detect():
    rel = os_release()
    session = os.environ.get("XDG_SESSION_TYPE", "")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").replace(":", " / ")
    return {
        "os": os_name(),
        "distro": rel.get("PRETTY_NAME") or rel.get("NAME") or "Unknown Linux",
        "distro_id": rel.get("ID", ""),
        "kernel": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "desktop": " · ".join(filter(None, (desktop, session.capitalize()))) or "Unknown",
        "cpu": _cpu(),
        "memory": _memory(),
        "gpu": _gpus(),
        "manager": package_manager(rel),
        "python": platform.python_version(),
        "toolkit": _toolkit(),
    }
