import json
import os
import subprocess
import threading
import time
import uuid

from . import wivrn
from .util import APP_DATA_DIR, atomic_write

PATH = os.path.join(APP_DATA_DIR, "sessions.json")
BEAT = 15
MIN_LENGTH = 30
GPU_SAMPLES = 240
_lock = threading.Lock()
_watching = set()


def _load():
    try:
        with open(PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("sessions", [])
            data.setdefault("open", {})
            return data
    except (OSError, ValueError):
        pass
    return {"sessions": [], "open": {}}


def _update(fn):
    with _lock:
        data = _load()
        result = fn(data)
        atomic_write(PATH, json.dumps(data, indent=1))
        return result


def _close(data, sid):
    s = data["open"].pop(sid, None)
    if s and s["end"] - s["start"] >= MIN_LENGTH:
        data["sessions"].append(s)


def begin(game_key, name, vr=True):
    sid = uuid.uuid4().hex[:12]
    now = int(time.time())

    def add(data):
        data["open"][sid] = {"id": sid, "game": game_key, "name": name, "vr": vr,
                             "start": now, "end": now, "headset": "", "bitrate": 0,
                             "battery_start": None, "battery_end": None}
    _update(add)
    return sid


def checkpoint(sid):
    from . import perf
    info = wivrn.status() or {}
    batt = wivrn.battery()
    load = perf.sample()

    def beat(data):
        s = data["open"].get(sid)
        if not s:
            return
        s["end"] = int(time.time())
        if info.get("connected"):
            s["headset"] = info.get("name") or s["headset"]
            s["bitrate"] = max(s["bitrate"], int(info.get("bitrate") or 0))
        if load:
            s.setdefault("gpu", []).append(load[0])
            del s["gpu"][:-GPU_SAMPLES]
            s["vram_peak"] = max(s.get("vram_peak", 0), load[1])
            s["vram_total"] = load[2]
        if batt:
            if s["battery_start"] is None:
                s["battery_start"] = batt[0]
            s["battery_end"] = batt[0]
    _update(beat)


def finish(sid):
    def end(data):
        if sid in data["open"]:
            data["open"][sid]["end"] = int(time.time())
            _close(data, sid)
    _update(end)
    _watching.discard(sid)


def recover():
    def sweep(data):
        for sid in [k for k in data["open"] if k not in _watching]:
            _close(data, sid)
    _update(sweep)


def steam_game_pids(appid):
    token = f"AppId={appid}".encode()
    uid = os.getuid()
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            if os.stat(f"/proc/{entry}").st_uid != uid:
                continue
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                args = f.read().split(b"\0")
        except OSError:
            continue
        if token in args and b"SteamLaunch" in args:
            out.append(int(entry))
    return out


def steam_game_running(appid):
    return bool(steam_game_pids(appid))


def watch(game, proc=None, vr=True, on_end=None):
    def run():
        if proc is None:
            deadline = time.monotonic() + 180
            while not steam_game_running(game.appid):
                if time.monotonic() > deadline:
                    if on_end:
                        on_end()
                    return
                time.sleep(3)
        sid = begin(game.appid, game.name, vr)
        _watching.add(sid)
        try:
            while True:
                checkpoint(sid)
                if proc is not None:
                    try:
                        proc.wait(timeout=BEAT)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                time.sleep(BEAT)
                if not steam_game_running(game.appid):
                    break
        finally:
            finish(sid)
            if on_end:
                on_end()

    threading.Thread(target=run, daemon=True).start()


def history():
    with _lock:
        data = _load()
    return sorted(data["sessions"] + list(data["open"].values()), key=lambda s: s["start"], reverse=True)


def minutes_by_game():
    out = {}
    for s in history():
        out[s["game"]] = out.get(s["game"], 0) + (s["end"] - s["start"]) // 60
    return out


def last_played_by_game():
    out = {}
    for s in history():
        out[s["game"]] = max(out.get(s["game"], 0), s["end"])
    return out


def summary(days=14):
    sessions = history()
    now = time.time()
    week = [s for s in sessions if s["start"] > now - 7 * 86400]
    lengths = [s["end"] - s["start"] for s in sessions]
    per_game = {}
    for s in sessions:
        g = per_game.setdefault(s["game"], {"name": s["name"], "seconds": 0, "sessions": 0})
        g["seconds"] += s["end"] - s["start"]
        g["sessions"] += 1
    today = time.localtime(now)
    midnight = time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1))
    daily = [0] * days
    for s in sessions:
        day = int((midnight - s["start"]) // 86400) + 1 if s["start"] < midnight else 0
        if 0 <= day < days:
            daily[days - 1 - day] += (s["end"] - s["start"]) / 60
    drains = [(s["battery_start"] - s["battery_end"]) / max((s["end"] - s["start"]) / 3600, 1 / 60)
              for s in sessions if s.get("battery_start") is not None and s.get("battery_end") is not None
              and s["end"] - s["start"] >= 600]
    return {
        "total": sum(lengths),
        "count": len(sessions),
        "week": sum(s["end"] - s["start"] for s in week),
        "longest": max(lengths, default=0),
        "average": sum(lengths) / len(lengths) if lengths else 0,
        "games": sorted(per_game.values(), key=lambda g: -g["seconds"]),
        "daily": daily,
        "drain": sum(drains) / len(drains) if drains else None,
        "recent": sessions[:12],
    }
