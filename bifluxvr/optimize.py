import os
import re
import shutil
import subprocess
import time

from . import health
from .voice import t

HEADSETS = [
    ("quest 3s", 1832, 1920),
    ("quest 3", 2064, 2208),
    ("quest pro", 1800, 1920),
    ("quest 2", 1832, 1920),
    ("pico 4", 2160, 2160),
    ("pico neo 3", 1832, 1920),
]
DEFAULT_EYE = (1832, 1920)

FFMPEG_CODECS = {
    "nvenc": {"h264": "h264_nvenc", "h265": "hevc_nvenc", "av1": "av1_nvenc"},
    "vaapi": {"h264": "h264_vaapi", "h265": "hevc_vaapi", "av1": "av1_vaapi"},
    "vulkan": {"h264": "h264_vulkan", "h265": "hevc_vulkan"},
    "x264": {"h264": "libx264"},
}

_wivrn_cache = {}


def gpu():
    cards = health.gpus()
    vendor = cards[0][0] if cards else "Unknown"
    info = {"vendor": vendor, "name": f"{vendor} GPU", "vram": 0}
    if vendor == "NVIDIA" and shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=5).stdout.splitlines()
            name, vram = out[0].rsplit(",", 1)
            info.update(name=name.strip(), vram=int(vram))
        except (OSError, subprocess.TimeoutExpired, IndexError, ValueError):
            pass
        return info
    for path in sorted(os.listdir("/sys/class/drm")):
        try:
            with open(f"/sys/class/drm/{path}/device/mem_info_vram_total") as f:
                info["vram"] = int(f.read()) // (1 << 20)
            break
        except (OSError, ValueError):
            continue
    if shutil.which("lspci"):
        try:
            out = subprocess.run(["lspci", "-mm"], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                if re.search(r'"(VGA|3D|Display)', line):
                    fields = re.findall(r'"([^"]*)"', line)
                    if len(fields) >= 3:
                        info["name"] = re.sub(r"\s*\[[^]]*]$", "", fields[2]) or info["name"]
                        break
        except (OSError, subprocess.TimeoutExpired):
            pass
    return info


def wivrn_encoders():
    exe = shutil.which("wivrn-server")
    if not exe:
        return []
    key = (exe, os.stat(exe).st_mtime)
    if key not in _wivrn_cache:
        with open(exe, "rb") as f:
            blob = f.read()
        found = []
        for name in ("nvenc", "vaapi"):
            if name.encode() in blob and f"{name} support not enabled".encode() not in blob:
                found.append(name)
        if b"video_encoder_vulkan" in blob:
            found.append("vulkan")
        if b"video_encoder_x264" in blob:
            found.append("x264")
        _wivrn_cache.clear()
        _wivrn_cache[key] = found
    return _wivrn_cache[key]


def eye_size(headset_name):
    name = (headset_name or "").lower()
    for key, w, h in HEADSETS:
        if key in name:
            return w, h
    return DEFAULT_EYE


def _av1_capable(info):
    name = info["name"].lower()
    if info["vendor"] == "NVIDIA":
        return bool(re.search(r"rtx\s*[45]0\d\d|rtx\s*(pro|ada)|\bl4\b|\bl40", name))
    if info["vendor"] == "AMD":
        return bool(re.search(r"rx\s*[79]\d\d\d", name))
    return "arc" in name


def tier(info):
    vram = info["vram"]
    if not vram:
        return "mid"
    return "low" if vram < 6000 else "mid" if vram < 10000 else "high"


def recommend(info, available, headset=None):
    headset = headset or {}
    codecs = headset.get("codecs") or ["h264", "h265", "av1"]
    level = tier(info)
    hw = "nvenc" if info["vendor"] == "NVIDIA" else "vaapi"
    if hw in available:
        encoder = hw
    elif "vulkan" in available:
        encoder = "vulkan"
    else:
        encoder = "x264"

    if encoder in ("nvenc", "vaapi") and "av1" in codecs and _av1_capable(info):
        codec = "av1"
    elif encoder != "x264" and "h265" in codecs:
        codec = "h265"
    else:
        codec = "h264"
    deep = codec in ("h265", "av1") and encoder in ("nvenc", "vaapi") and level != "low"

    bitrate = {"low": 60, "mid": 100, "high": 150}[level]
    scale = {"low": 0.8, "mid": 1.0, "high": 1.2}[level]
    refresh = {"low": 72, "mid": 90, "high": 90}[level]
    if encoder == "x264":
        bitrate, scale, refresh = 30, 0.7, 72
    rates = headset.get("refresh_rates") or []
    if rates:
        refresh = min(rates, key=lambda r: abs(r - refresh))

    notes = []
    if hw not in available and info["vendor"] in ("NVIDIA", "AMD", "Intel"):
        notes.append(_rebuild_hint(hw))
    if encoder == "x264":
        notes.append(t("Software encoding uses a lot of CPU and adds latency. Keep the resolution modest.",
                       "Your CPU is doing the video encoding. Keep the settings low."))
    return {
        "encoder": encoder, "codec": codec, "bit_depth": 10 if deep else 8,
        "bitrate": bitrate, "scale": scale, "refresh": round(refresh), "tier": level, "notes": notes,
    }


def _rebuild_hint(hw):
    name = "NVENC" if hw == "nvenc" else "VA-API"
    if os.path.isdir("/var/db/pkg/media-libs") and any(
            p.startswith("wivrn-") for p in os.listdir("/var/db/pkg/media-libs")):
        how = t(f"enable the “{hw}” USE flag for media-libs/wivrn and re-emerge it",
                f"add the “{hw}” USE flag to media-libs/wivrn and re-emerge")
    else:
        how = t(f"install a WiVRn package built with {name}", f"grab a WiVRn build that has {name} in it")
    return t(f"This WiVRn build has no {name} support, so your GPU's best encoder sits unused. To fix it, {how}.",
             f"Your WiVRn was built without {name}, so your GPU's best encoder goes unused. "
             f"To fix it, {how}.")


def test_encode(encoder, codec, width, height, fps, seconds=3):
    if not shutil.which("ffmpeg"):
        raise RuntimeError(t("ffmpeg is needed for the stream test", "Install ffmpeg and I'll run the test"))
    name = FFMPEG_CODECS.get(encoder, {}).get(codec)
    if not name:
        raise RuntimeError(t(f"{encoder} can't encode {codec}", f"{encoder} doesn't do {codec}. Pick another."))
    w, h = width // 2 * 2, height // 2 * 2
    frames = max(60, int(fps * 2 * seconds))
    argv = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error"]
    if encoder == "vaapi":
        argv += ["-vaapi_device", "/dev/dri/renderD128"]
    elif encoder == "vulkan":
        argv += ["-init_hw_device", "vulkan=vk", "-filter_hw_device", "vk"]
    argv += ["-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r={fps}", "-frames:v", str(frames)]
    if encoder in ("vaapi", "vulkan"):
        argv += ["-vf", "format=nv12,hwupload"]
    else:
        argv += ["-pix_fmt", "yuv420p"]
    argv += ["-c:v", name]
    if encoder == "x264":
        argv += ["-preset", "ultrafast", "-tune", "zerolatency"]
    argv += ["-b:v", "60M", "-f", "null", "-"]

    start = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError(t("The encoder test took too long", "The encoder stalled during the test")) from None
    elapsed = time.monotonic() - start
    if proc.returncode != 0:
        why = next((re.sub(r"^\[[^]]*]\s*", "", line) for line in proc.stderr.splitlines() if line.strip()),
                   "unknown error")
        raise RuntimeError(t(f"{name} isn't usable on this GPU ({why})",
                             f"{name} failed ({why}). Your GPU probably can't do that one."))
    achieved = frames / elapsed
    needed = fps * 2
    return {"encoder": name, "size": f"{w}×{h}", "fps": achieved, "needed": needed,
            "ok": achieved >= needed * 1.15}


def candidates(rec, available):
    order = [(rec["encoder"], rec["codec"])]
    for enc in ("nvenc", "vaapi", "vulkan", "x264"):
        if enc not in available:
            continue
        for codec in ("h265", "h264"):
            if codec in FFMPEG_CODECS[enc] and (enc, codec) not in order:
                order.append((enc, codec))
    return order


def find_working(rec, available, width, height, limit=4):
    results = []
    for enc, codec in candidates(rec, available)[:limit]:
        try:
            res = test_encode(enc, codec, width, height, rec["refresh"])
        except RuntimeError as e:
            res = {"encoder": FFMPEG_CODECS[enc].get(codec, enc), "error": str(e), "ok": False}
        res.update(wivrn_encoder=enc, codec=codec)
        results.append(res)
        if res["ok"]:
            break
    return results
