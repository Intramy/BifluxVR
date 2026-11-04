import glob
import json
import os
import re
import shutil
import subprocess
import time

from gi.repository import Gio, GLib

from .openvr import WIVRN_CONFIG
from .util import LOG_DIR, atomic_write, pids_of

BUS_NAME = "io.github.wivrn.Server"
OBJECT_PATH = "/io/github/wivrn/Server"
DEFAULT_PORT = 9757


def status():
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(BUS_NAME, OBJECT_PATH, "org.freedesktop.DBus.Properties", "GetAll",
                              GLib.Variant("(s)", (BUS_NAME,)), GLib.VariantType("(a{sv})"),
                              Gio.DBusCallFlags.NONE, 2000, None)
    except GLib.Error:
        return None
    props = reply.unpack()[0]
    return {
        "connected": props.get("HeadsetConnected", False),
        "session": props.get("SessionRunning", False),
        "name": props.get("SystemName", ""),
        "bitrate": props.get("Bitrate", 0),
        "refresh": props.get("PreferredRefreshRate", 0.0),
        "refresh_rates": list(props.get("AvailableRefreshRates", [])),
        "codecs": list(props.get("SupportedCodecs", [])),
        "hand_tracking": props.get("HandTracking", False),
        "eye_tracking": props.get("EyeGaze", False),
        "face_tracking": props.get("FaceTracking", False),
        "pairing": props.get("PairingEnabled", False),
    }


_LOG_BATTERY = re.compile(r"Battery: HMD: ([\d.]+)")
_LOG_CHARGING = re.compile(r"Charging: HMD: (\w+)")


def _adb_battery():
    if not shutil.which("adb"):
        return None
    try:
        out = subprocess.run(["adb", "shell", "dumpsys", "battery"], capture_output=True,
                             text=True, timeout=4).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    level = re.search(r"^\s*level: (\d+)", out, re.M)
    if not level:
        return None
    state = re.search(r"^\s*status: (\d+)", out, re.M)
    return int(level.group(1)), bool(state and state.group(1) in ("2", "5")), "adb"


def _log_battery():
    try:
        with open(os.path.join(LOG_DIR, "wivrn-server.log"), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    levels = _LOG_BATTERY.findall(tail)
    if not levels:
        return None
    charging = _LOG_CHARGING.findall(tail)
    level = float(levels[-1])
    return round(level * 100 if level <= 1 else level), bool(charging) and charging[-1] == "true", "log"


def battery():
    return _adb_battery() or _log_battery()


def _port():
    try:
        with open(WIVRN_CONFIG) as f:
            return int(json.load(f).get("port", DEFAULT_PORT))
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_PORT


def _hex_ip(text):
    raw = bytes.fromhex(text)
    if len(raw) == 4:
        return ".".join(str(b) for b in reversed(raw))
    words = [raw[i:i + 4][::-1] for i in range(0, 16, 4)]
    addr = b"".join(words)
    if addr[:12] == b"\0" * 10 + b"\xff\xff":
        return ".".join(str(b) for b in addr[12:])
    return ":".join(addr[i:i + 2].hex() for i in range(0, 16, 2))


def headset_address():
    pids = pids_of("wivrn-server")
    if not pids:
        return ""
    inodes = set()
    for fd in glob.glob(f"/proc/{pids[0]}/fd/*"):
        try:
            link = os.readlink(fd)
        except OSError:
            continue
        if link.startswith("socket:["):
            inodes.add(link[8:-1])
    port = f"{_port():04X}"
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table) as f:
                rows = f.readlines()[1:]
        except OSError:
            continue
        for row in rows:
            cols = row.split()
            local, remote, state, inode = cols[1], cols[2], cols[3], cols[9]
            if state == "01" and inode in inodes and local.endswith(":" + port):
                return _hex_ip(remote.split(":")[0])
    return ""


def link_quality(address, count=6):
    if not address or not shutil.which("ping"):
        return None
    try:
        out = subprocess.run(["ping", "-n", "-q", "-c", str(count), "-i", "0.2", "-W", "1", address],
                             capture_output=True, text=True, timeout=count + 4).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    loss = re.search(r"([\d.]+)% packet loss", out)
    rtt = re.search(r"= [\d.]+/([\d.]+)/[\d.]+/([\d.]+) ms", out)
    if not loss:
        return None
    loss = float(loss.group(1))
    avg, jitter = (float(rtt.group(1)), float(rtt.group(2))) if rtt else (0.0, 0.0)
    if loss >= 50 or not rtt:
        grade = "poor"
    elif loss == 0 and avg < 6 and jitter < 3:
        grade = "excellent"
    elif loss < 5 and avg < 15:
        grade = "good"
    elif loss < 15 and avg < 40:
        grade = "fair"
    else:
        grade = "poor"
    return {"avg": avg, "jitter": jitter, "loss": loss, "grade": grade}


def read_config():
    try:
        with open(WIVRN_CONFIG) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_encoder(encoder, codec, bit_depth=8):
    cfg = read_config()
    if os.path.exists(WIVRN_CONFIG):
        shutil.copy2(WIVRN_CONFIG, f"{WIVRN_CONFIG}.bifluxvr-{time.strftime('%Y%m%d-%H%M%S')}.bak")
    cfg["encoder"] = {"encoder": encoder, "codec": codec}
    if bit_depth == 8:
        cfg.pop("bit-depth", None)
    else:
        cfg["bit-depth"] = bit_depth
    atomic_write(WIVRN_CONFIG, json.dumps(cfg, indent=2) + "\n")


def configured_encoder():
    enc = read_config().get("encoder")
    if isinstance(enc, list):
        enc = enc[0] if enc else None
    if isinstance(enc, str):
        return enc, ""
    if isinstance(enc, dict):
        return enc.get("encoder", ""), enc.get("codec", "")
    return "", ""
