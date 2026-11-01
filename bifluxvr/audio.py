# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
"""Where the sound goes: into the headset while a game runs, back to the
headphones or speakers when it stops. Talks to PipeWire/PulseAudio through
pactl. WiVRn only adds its sink once a headset is connected."""

import json
import subprocess
import threading
import time

from .voice import t

_lock = threading.Lock()
_switched = False  # the output was moved to the headset by us
_wanted = False  # a game is on and hasn't been given the headset yet


def _pactl(*args):
    return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5)


def _kind(sink):
    text = f"{sink['name']} {sink.get('description', '')}".lower()
    if "wivrn" in text:
        return "vr"
    props = sink.get("properties", {})
    ports = sink.get("ports", [])
    port = next((p for p in ports if p["name"] == sink.get("active_port")), {})
    if props.get("device.form_factor") in ("headset", "headphone") or props.get("device.bus") == "bluetooth" \
            or (port.get("type") == "Headphones" and "not available" not in port.get("availability", "")):
        return "headphones"
    if props.get("device.form_factor") == "speaker" or port.get("type") == "Speaker":
        return "speakers"
    return "other"


def sinks():
    """[{name, title, kind}] with kind one of vr, headphones, speakers, other."""
    try:
        res = _pactl("-f", "json", "list", "sinks")
        data = json.loads(res.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    return [{"name": s["name"], "title": s.get("description") or s["name"], "kind": _kind(s)}
            for s in data if not s["name"].endswith(".monitor")]


def default():
    try:
        return _pactl("get-default-sink").stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def set_default(name):
    try:
        return _pactl("set-default-sink", name).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def back_target(preferred=""):
    """The sink to return to after playing: the one picked in Settings if it
    still exists, else headphones, then speakers, then anything not VR."""
    found = [s for s in sinks() if s["kind"] != "vr"]
    for s in found:
        if s["name"] == preferred:
            return s
    rank = {"headphones": 0, "speakers": 1, "other": 2}
    return min(found, key=lambda s: rank[s["kind"]], default=None)


def to_headset(wait=120):
    """Move the default output to WiVRn, waiting for the headset to connect.
    Runs on a worker thread; returns the sink's title or ""."""
    global _switched, _wanted
    _wanted = True
    deadline = time.monotonic() + wait
    while True:
        vr = next((s for s in sinks() if s["kind"] == "vr"), None)
        if vr:
            with _lock:
                if _wanted and set_default(vr["name"]):
                    _switched = True
                    return vr["title"]
            return ""
        if time.monotonic() > deadline:
            return ""
        time.sleep(2)


def restore(preferred=""):
    """Undo to_headset(). Returns the title switched to, or "" if the output
    wasn't ours to move."""
    global _switched, _wanted
    with _lock:
        _wanted = False
        if not _switched:
            return ""
        _switched = False
        target = back_target(preferred)
        if target and set_default(target["name"]):
            return target["title"]
    return ""


def in_game():
    return _switched or _wanted


def label(kind):
    return {"vr": t("VR headset", "Face computer"), "headphones": t("Headphones", "Headphones"),
            "speakers": t("Speakers", "Speakers"), "other": t("Other", "Other")}[kind]
