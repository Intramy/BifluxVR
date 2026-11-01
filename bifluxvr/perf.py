# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
"""Roughly how many frames per second a VR game will get here.

Steam lists the GPU each game needs. VR minimum specs were written for the
original Vive/Rift (90 Hz, about 1512×1680 per eye after supersampling), so
a game's minimum GPU is taken to manage 90 fps there. From that, scaled by
how fast this GPU is, how many pixels this headset renders, and Proton's
overhead, comes an estimate, capped by the refresh rate and by what the
stream encoder kept up with in the Test stream.

After a game has been played, the GPU load recorded during its sessions
corrects the estimate: a GPU with headroom means the game ran at the
refresh rate (or was held back by the CPU), not below it."""

import glob
import json
import os
import re
import subprocess
import time

from . import net, optimize, sessions, wivrn
from .util import CACHE_DIR, atomic_write
from .voice import t

# Relative raster performance, GTX 1060 6 GB = 100. Approximate, from
# public benchmark averages; good to maybe ±15%.
GPUS = {
    # NVIDIA desktop
    "gtx 750 ti": 35, "gtx 950": 50, "gtx 960": 58, "gtx 970": 88, "gtx 980": 100, "gtx 980 ti": 128,
    "gtx 1050": 40, "gtx 1050 ti": 50, "gtx 1060 3gb": 93, "gtx 1060": 100, "gtx 1070": 133,
    "gtx 1070 ti": 150, "gtx 1080": 165, "gtx 1080 ti": 215, "gtx titan x": 130,
    "gtx 1650": 63, "gtx 1650 super": 83, "gtx 1660": 92, "gtx 1660 super": 107, "gtx 1660 ti": 108,
    "rtx 2060": 133, "rtx 2060 super": 152, "rtx 2070": 162, "rtx 2070 super": 182, "rtx 2080": 196,
    "rtx 2080 super": 205, "rtx 2080 ti": 245,
    "rtx 3050": 110, "rtx 3060": 150, "rtx 3060 ti": 192, "rtx 3070": 220, "rtx 3070 ti": 232,
    "rtx 3080": 270, "rtx 3080 ti": 300, "rtx 3090": 310, "rtx 3090 ti": 335,
    "rtx 4060": 175, "rtx 4060 ti": 205, "rtx 4070": 265, "rtx 4070 super": 300, "rtx 4070 ti": 320,
    "rtx 4070 ti super": 345, "rtx 4080": 390, "rtx 4080 super": 400, "rtx 4090": 500,
    "rtx 5060": 200, "rtx 5060 ti": 235, "rtx 5070": 310, "rtx 5070 ti": 390, "rtx 5080": 440,
    "rtx 5090": 620,
    # NVIDIA laptop (nvidia-smi adds "Laptop GPU"; the 2050 and MX only exist in laptops)
    "mx 450": 28, "mx 550": 30, "rtx 2050": 75, "gtx 1650 laptop": 55, "gtx 1650 ti laptop": 62,
    "gtx 1660 ti laptop": 95, "rtx 2060 laptop": 115, "rtx 2070 laptop": 135, "rtx 2080 laptop": 160,
    "rtx 3050 laptop": 85, "rtx 3050 ti laptop": 95, "rtx 3060 laptop": 145, "rtx 3070 laptop": 175,
    "rtx 3070 ti laptop": 185, "rtx 3080 laptop": 200, "rtx 3080 ti laptop": 225,
    "rtx 4050 laptop": 140, "rtx 4060 laptop": 165, "rtx 4070 laptop": 185, "rtx 4080 laptop": 270,
    "rtx 4090 laptop": 320, "rtx 5060 laptop": 190, "rtx 5070 laptop": 215, "rtx 5070 ti laptop": 260,
    "rtx 5080 laptop": 320, "rtx 5090 laptop": 370,
    # AMD
    "r9 290": 85, "r9 290x": 92, "r9 390": 90, "r9 390x": 97, "r9 fury": 115, "r9 fury x": 125,
    "rx 470": 85, "rx 480": 95, "rx 570": 88, "rx 580": 100, "rx 590": 108, "vega 56": 135, "vega 64": 150,
    "radeon vii": 175, "rx 5500 xt": 85, "rx 5600 xt": 135, "rx 5700": 155, "rx 5700 xt": 170,
    "rx 6500 xt": 70, "rx 6600": 140, "rx 6600 xt": 160, "rx 6650 xt": 168, "rx 6700 xt": 200,
    "rx 6750 xt": 210, "rx 6800": 255, "rx 6800 xt": 290, "rx 6900 xt": 310, "rx 6950 xt": 330,
    "rx 7600": 165, "rx 7600 xt": 172, "rx 7700 xt": 245, "rx 7800 xt": 290, "rx 7900 gre": 320,
    "rx 7900 xt": 380, "rx 7900 xtx": 430, "rx 9060 xt": 230, "rx 9070": 360, "rx 9070 xt": 410,
    "780m": 55, "680m": 45, "890m": 70, "vega 8": 20,
    # Intel
    "arc a380": 55, "arc a580": 120, "arc a750": 140, "arc a770": 150, "arc b570": 170, "arc b580": 190,
}
REF_FPS = 90
REF_PIXELS = 1512 * 1680  # per eye, Vive/Rift with the usual 1.4× supersampling
PROTON = 0.9  # DXVK/VKD3D and xrizer together cost roughly this much
HEADROOM = 85  # GPU busy below this (%) during play = not the bottleneck

_reqs = net.Cache("requirements.json", 14 * 86400)
TEST_PATH = os.path.join(CACHE_DIR, "stream-test.json")


def _norm(text):
    text = re.sub(r"™|®|\((r|tm)\)", "", text.lower())
    text = re.sub(r"\b(nvidia|geforce|amd|radeon|intel|graphics|series)\b", " ", text)
    text = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", text)  # gtx1060 → gtx 1060
    text = re.sub(r"\b(\d+) (gb)\b", r"\1\2", text)
    return " " + re.sub(r"[^a-z0-9]+", " ", text).strip() + " "


# Table names in the same normal form as the text they're matched against.
_NORM = {_norm(k).strip(): k for k in GPUS}
_KEYS = sorted(_NORM, key=len, reverse=True)


def gpu_score(name):
    """Score for a GPU name as nvidia-smi or a store page writes it, or 0."""
    text = _norm(name)
    laptop = " laptop " in text or " mobile " in text or re.search(r" \d{4} m ", text)
    for key in _KEYS:
        if f" {key} " in text:
            if laptop and f"{key} laptop" in _NORM:
                return GPUS[_NORM[f"{key} laptop"]]
            return GPUS[_NORM[key]]
    return 0


def _mentions(text):
    """Scores of every GPU named in a requirements line, longest match first."""
    text, found = _norm(text), []
    for key in _KEYS:
        if f" {key} " in text:
            found.append((_NORM[key], GPUS[_NORM[key]]))
            text = text.replace(f" {key} ", " ")
    return found


def _strip(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def requirements(appid):
    """{"gpu": [(name, score)], "vram": GB, "ram": GB, "text": str} from the
    Steam store's minimum spec (recommended if there is no minimum), or {}."""
    key = str(appid)
    hit = _reqs.get(key)
    if hit is not None:
        return hit
    data = net.get_json(f"https://store.steampowered.com/api/appdetails?appids={key}&cc=us&l=en", timeout=10)
    app = (data.get(key) or {}).get("data") or {}
    pc = app.get("pc_requirements") or {}
    if isinstance(pc, list):  # Steam sends [] when there are none
        pc = {}
    text = _strip(pc.get("minimum")) or _strip(pc.get("recommended"))
    graphics = re.search(r"Graphics:\s*(.+?)(?:\s+(?:DirectX|Storage|Sound|VR Support|Network|Additional|"
                         r"Hard Drive|OS)\b|$)", text)
    line = graphics.group(1) if graphics else ""
    vram = re.search(r"(\d+)\s*GB\s*(?:of\s*)?(?:VRAM|video)", text, re.I)
    ram = re.search(r"Memory:\s*(\d+)\s*GB", text)
    return _reqs.put(key, {"gpu": _mentions(line), "vram": int(vram.group(1)) if vram else 0,
                           "ram": int(ram.group(1)) if ram else 0, "text": line})


# ------------------------------------------------------------ measuring

def sample():
    """(GPU busy %, VRAM used MiB, VRAM total MiB) right now, or None."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
        busy, used, total = (int(float(v)) for v in out.splitlines()[0].split(","))
        return busy, used, total
    except (OSError, subprocess.TimeoutExpired, IndexError, ValueError):
        pass
    for dev in glob.glob("/sys/class/drm/card[0-9]/device"):
        try:
            with open(f"{dev}/gpu_busy_percent") as f:
                busy = int(f.read())
            with open(f"{dev}/mem_info_vram_used") as f:
                used = int(f.read()) >> 20
            with open(f"{dev}/mem_info_vram_total") as f:
                total = int(f.read()) >> 20
            return busy, used, total
        except (OSError, ValueError):
            continue
    return None


def measured(game_key):
    """GPU load over this game's sessions: {"busy": typical %, "vram": peak
    MiB, "vram_total": MiB, "minutes": n} or None."""
    busy, vram, total, minutes = [], 0, 0, 0
    for s in sessions.history():
        if s["game"] != game_key or not s.get("gpu"):
            continue
        busy += s["gpu"]
        vram = max(vram, s.get("vram_peak", 0))
        total = s.get("vram_total", total)
        minutes += (s["end"] - s["start"]) // 60
    if len(busy) < 8:  # two minutes of samples, or loading screens dominate
        return None
    busy.sort()
    return {"busy": busy[len(busy) * 3 // 4], "vram": vram, "vram_total": total, "minutes": minutes}


def save_stream_test(results):
    atomic_write(TEST_PATH, json.dumps({"time": time.time(), "results": results}))


def _stream_limit():
    """Frames per second the configured encoder managed in the last Test
    stream (both eyes), or None if it wasn't tested."""
    try:
        with open(TEST_PATH) as f:
            results = json.load(f)["results"]
    except (OSError, ValueError, KeyError):
        return None
    enc = wivrn.configured_encoder()
    for r in results:
        if (r.get("wivrn_encoder"), r.get("codec")) == enc and "fps" in r:
            return r["fps"] / 2
    return None


def _headset():
    info = wivrn.status() or {}
    if info.get("connected"):
        return info["name"], info.get("refresh") or 0
    last = next((s["headset"] for s in sessions.history() if s.get("headset")), "")
    return last, 0


# ------------------------------------------------------------ the estimate

def estimate(game, appid):
    """{"fps", "low", "high", "refresh", "state": ok|warn|fail, "lines": [(title, text)],
    "measured": bool} or None when Steam names no GPU we know."""
    req = requirements(appid)
    if not req or not req["gpu"]:
        return None
    gpu = optimize.gpu()
    mine = gpu_score(gpu["name"])
    if not mine:
        return {"unknown_gpu": gpu["name"]}
    need = sum(s for _, s in req["gpu"]) / len(req["gpu"])
    names = " / ".join(n.upper().replace("LAPTOP", "Laptop") for n, _ in req["gpu"])

    headset, refresh = _headset()
    w, h = optimize.eye_size(headset)
    rec = optimize.recommend(gpu, optimize.wivrn_encoders(), {"refresh_rates": [refresh]} if refresh else None)
    refresh = refresh or rec["refresh"]
    scale = rec["scale"]
    pixels = w * h * scale * scale
    fps = REF_FPS * (mine / need) * (REF_PIXELS / pixels) ** 0.8 * PROTON
    lines = [
        (t("Your GPU vs. what it asks for", "Your GPU vs. the minimum"),
         t(f"{gpu['name']} is about {mine / need:.1f}× the {names} it needs",
           f"Your {gpu['name']} is roughly {mine / need:.1f}× the {names} it wants")),
        (t("Pixels per frame", "Pixels it has to push"),
         t(f"{int(w * scale)}×{int(h * scale)} per eye on {headset or 'a Quest 2-class headset'}, "
           f"{pixels / REF_PIXELS:.1f}× what the minimum spec was written for",
           f"{int(w * scale)}×{int(h * scale)} per eye on {headset or 'a Quest 2-ish headset'}, "
           f"{pixels / REF_PIXELS:.1f}× what the spec assumed")),
    ]
    vram_gb = gpu["vram"] / 1024
    if req["vram"] and vram_gb and vram_gb < req["vram"]:
        fps *= 0.7
        lines.append((t("Video memory", "VRAM"),
                      t(f"It wants {req['vram']} GB and you have {vram_gb:.0f} GB; expect stutter when it fills up",
                        f"Wants {req['vram']} GB, you've got {vram_gb:.0f}. Expect hiccups when it fills up")))

    limit = _stream_limit()
    if limit and limit < min(fps, refresh):
        lines.append((t("Stream encoder", "Video encoder"),
                      t(f"The Test stream managed {limit:.0f} fps, which caps the headset at that",
                        f"Your encoder topped out at {limit:.0f} fps in the Test stream, so that's the ceiling")))
        fps = limit

    real = measured(game.appid)
    if real:
        vram_note = ""
        if real["vram_total"] and real["vram"] > real["vram_total"] * 0.95:
            vram_note = t(" · video memory was full", " · VRAM was maxed out")
        if real["busy"] < HEADROOM:
            fps = max(fps, min(refresh, limit or refresh))
            lines.append((t("Measured while you played", "What actually happened"),
                          t(f"GPU {real['busy']}% busy over {real['minutes']} min: it had headroom, so frames "
                            f"were limited by the refresh rate or the CPU{vram_note}",
                            f"GPU only {real['busy']}% busy over {real['minutes']} min. It was chilling, so the "
                            f"refresh rate (or your CPU) is the limit{vram_note}")))
        else:
            lines.append((t("Measured while you played", "What actually happened"),
                          t(f"GPU {real['busy']}% busy over {real['minutes']} min: the GPU is the limit{vram_note}",
                            f"GPU sweating at {real['busy']}% over {real['minutes']} min. It's the bottleneck"
                            f"{vram_note}")))

    fps = min(fps, refresh)
    spread = 0.1 if real else 0.25
    state = "ok" if fps >= refresh * 0.95 else "warn" if fps >= refresh * 0.7 else "fail"
    return {"fps": round(fps), "low": round(fps * (1 - spread)), "high": round(min(refresh, fps * (1 + spread))),
            "refresh": round(refresh), "state": state, "lines": lines, "measured": bool(real)}
