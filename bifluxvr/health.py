import glob
import os
import re
import shutil
from dataclasses import dataclass

from . import anticheat, installer, launcher, mods, openvr, runtimes, steam
from .util import HOME, LOG_DIR, process_running_any_user, slug
from .voice import t

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"

_VENDORS = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}


@dataclass
class Check:
    id: str
    title: str
    state: str
    detail: str
    fix: str = ""


def gpus():
    out = []
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        try:
            with open(os.path.join(card, "device", "vendor")) as f:
                vendor = _VENDORS.get(f.read().strip(), "Unknown")
        except OSError:
            continue
        driver = os.path.basename(os.path.realpath(os.path.join(card, "device", "driver")))
        out.append((vendor, driver))
    return out


def nvidia_version():
    try:
        with open("/proc/driver/nvidia/version") as f:
            for tok in f.readline().split():
                if tok[:1].isdigit() and "." in tok:
                    return tok
    except OSError:
        pass
    return ""


def server_managed(usable):
    for rt in usable:
        s = runtimes.server_for(rt.kind)
        if s and s.installed and s.manages_runtime:
            return rt
    return None


def run_all():
    checks = []

    g = gpus()
    icds = glob.glob("/usr/share/vulkan/icd.d/*.json") + glob.glob("/etc/vulkan/icd.d/*.json")
    if not icds:
        checks.append(Check("vulkan", t("Vulkan driver", "Graphics driver (Vulkan)"), FAIL,
                            t("No Vulkan ICD found. DXVK, VKD3D-Proton and VR compositors need Vulkan.",
                              "No Vulkan driver found. Without it, nothing here works.")))
    else:
        names = ", ".join(sorted({v for v, _ in g})) or "GPU"
        extra = f" · driver {nvidia_version()}" if any(v == "NVIDIA" for v, _ in g) and nvidia_version() else ""
        checks.append(Check("vulkan", t("Vulkan driver", "Graphics driver (Vulkan)"), OK, f"{names}{extra}"))

    if steam.installed():
        checks.append(Check("steam", "Steam", OK, t(f"{len(steam.libraries())} library folder(s)", f"{len(steam.libraries())} library folder(s) full of games")))
    else:
        checks.append(Check("steam", "Steam", FAIL, t("Install Steam to play Steam VR titles.", "Install Steam to play Steam games.")))

    protons = steam.proton_builds()
    if protons:
        checks.append(Check("proton", "Proton", OK,
                            t(f"{protons[0][0]}: Win32 via Wine, D3D9-11 via DXVK, D3D12 via VKD3D-Proton",
                              f"{protons[0][0]}, ready to pretend to be Windows")))
    else:
        checks.append(Check("proton", "Proton", FAIL,
                            t("Install Proton Experimental from Steam (Library → Tools).",
                              "Get Proton Experimental in Steam (Library → Tools). It's free.")))

    if shutil.which("umu-run"):
        checks.append(Check("umu", "umu-launcher", OK, t("Runs non-Steam Windows games with Proton", "Handles your off-Steam games")))
    else:
        checks.append(Check("umu", "umu-launcher", INFO,
                            t("Optional. Improves non-Steam games. Proton is used directly otherwise.",
                              "Optional. Makes off-Steam games happier. Without it we just use Proton directly.")))

    backend = installer.rar_backend()
    if backend == "unrar":
        checks.append(Check("rar", t("RAR support", "RAR support"), OK,
                            t("unrar · every .rar works, including multi-part", "unrar · opens every .rar you throw at it")))
    elif backend:
        checks.append(Check("rar", t("RAR support", "RAR support"), INFO,
                            t("bsdtar · basic .rar only. Install unrar for solid and multi-part archives",
                              "bsdtar · handles simple .rar files. Get unrar for the fancy ones")))
    else:
        checks.append(Check("rar", t("RAR support", "RAR support"), INFO,
                            t("Install unrar to open .rar files in the Drop Folder",
                              "Install unrar and the Yeet Zone will take .rar files too")))

    xr_all = runtimes.discover()
    rt = runtimes.active()
    usable = [r for r in xr_all if r.valid and r.kind != "steamvr"]
    if rt and rt.valid:
        state = WARN if rt.kind == "steamvr" else OK
        detail = rt.name if state == OK else \
            t(f"{rt.name}. WiVRn/Monado give lower latency without SteamVR",
              f"{rt.name}. It works, but WiVRn/Monado are snappier")
        checks.append(Check("openxr", t("OpenXR runtime", "OpenXR runtime"), state, detail,
                            t(f"Use {usable[0].name}", f"Switch to {usable[0].name}") if state == WARN and usable else ""))
    elif server_managed(usable):
        checks.append(Check("openxr", t("OpenXR runtime", "OpenXR runtime"), OK,
                            t(f"{server_managed(usable).name}, activated by its server when it starts",
                              f"{server_managed(usable).name}, clocks in when its server starts")))
    elif usable:
        checks.append(Check("openxr", t("OpenXR runtime", "OpenXR runtime"), FAIL,
                            t("No runtime is active", "None active"),
                            t(f"Use {usable[0].name}", f"Use {usable[0].name}")))
    else:
        checks.append(Check("openxr", t("OpenXR runtime", "OpenXR runtime"), FAIL,
                            t("Install WiVRn (Quest/Pico, wireless) or Monado (wired PC headsets).",
                              "Install WiVRn (wireless Quest/Pico) or Monado (headsets with cables).")))

    cur = openvr.current_runtime()
    kind = openvr.classify(cur)
    layers = openvr.discover()
    xrizer = [l for l in layers if l.kind == "xrizer"]
    if kind in ("xrizer", "opencomposite"):
        if not os.path.isfile(os.path.join(cur, "bin", "linux64", "vrclient.so")):
            checks.append(Check("openvr", t("OpenVR translation", "OpenVR translator"), FAIL,
                                t(f"{cur} is missing vrclient.so", f"{cur} is missing its vrclient.so."),
                                t("Repair", "Fix It") if layers else ""))
        else:
            name = "xrizer" if kind == "xrizer" else "OpenComposite"
            checks.append(Check("openvr", t("OpenVR translation", "OpenVR translator"), OK,
                                t(f"{name}: OpenVR games talk straight to OpenXR", f"{name} is translating OpenVR")))
    elif kind == "steamvr":
        checks.append(Check("openvr", t("OpenVR translation", "OpenVR translator"), WARN,
                            t("OpenVR is routed through SteamVR", "OpenVR is going through SteamVR"),
                            t("Use xrizer", "Switch to xrizer") if xrizer else t("Get xrizer", "Get xrizer")))
    else:
        checks.append(Check("openvr", t("OpenVR translation", "OpenVR translator"), FAIL,
                            t("OpenVR games have no runtime", "OpenVR games have nowhere to go"),
                            t("Use xrizer", "Switch to xrizer") if xrizer else t("Get xrizer", "Get xrizer")))

    server = runtimes.preferred_server()
    if server:
        if server.running:
            checks.append(Check("server", server.title, OK, t("Running. Put on your headset", "Running. Put your headset on")))
        else:
            checks.append(Check("server", server.title, WARN, t("Not running", "Snoozing"), t("Start", "Wake It Up")))
        if server.kind == "wivrn":
            if process_running_any_user("avahi-daemon"):
                checks.append(Check("avahi", t("Headset discovery", "Headset finder"), OK, t("Avahi is running", "Avahi is on the lookout")))
            else:
                checks.append(Check("avahi", t("Headset discovery", "Headset finder"), INFO,
                                    t("Avahi isn't running. Connect from the headset by IP address",
                                      "Avahi's off, so connect from the headset by IP address")))
    return checks


def summary(checks):
    blocking = [c for c in checks if c.state == FAIL]
    warns = [c for c in checks if c.state == WARN]
    return blocking, warns


def _tail(path, size=1 << 20):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - size))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def _swapchain_problem(m):
    server = _tail(os.path.join(LOG_DIR, "wivrn-server.log")) or ""
    if "OUT_OF_DEVICE_MEMORY" in server:
        return t("the graphics card ran out of video memory for the headset view. Lower the resolution scale "
                 "in the WiVRn app on the headset (try 70%) and close other GPU-heavy apps",
                 "your graphics card ran out of VRAM for the headset view. Turn the resolution scale down "
                 "in the WiVRn app on the headset (70% is a good start) and close other GPU-hungry stuff")
    return t("the OpenXR runtime couldn't create the headset view. Try a lower resolution scale on the headset",
             "the runtime couldn't build the headset view. Try a lower resolution scale on the headset")


_LOG_PROBLEMS = [
    (re.compile(r"XR_ERROR_\w+ in xrCreateSwapchain"), _swapchain_problem),
    (re.compile(r"XR_ERROR_RUNTIME_UNAVAILABLE|xrCreateInstance failed"),
     lambda m: t("the game couldn't reach the OpenXR runtime", "the game couldn't find the OpenXR runtime")),
    (re.compile(r"XR_ERROR_RUNTIME_FAILURE in (xr\w+)"),
     lambda m: t(f"the OpenXR runtime failed in {m.group(1)}", f"the runtime choked on {m.group(1)}")),
    (re.compile(r"VRInitError_\w+|HmdNotFound"),
     lambda m: t("OpenVR found no headset", "OpenVR looked for a headset and found nothing")),
    (re.compile(r"err:module:import_dll (?:Library|Loading library) (\S+)"),
     lambda m: t(f"{m.group(1)} is missing", f"it's missing {m.group(1)}")),
    (re.compile(r"Unhandled (page fault|exception)"),
     lambda m: t("it crashed (unhandled exception)", "it crashed")),
]


def game_log(game):
    if game.custom:
        return os.path.join(LOG_DIR, f"{slug(game.name)}.log")
    return os.path.join(HOME, f"steam-{game.appid}.log")


def meta_check_failed(game):
    pfx = (game.prefix or launcher.default_prefix(game.name)) if game.custom else steam.compat_prefix(game.appid)
    if not pfx:
        return False
    for path in glob.glob(os.path.join(glob.escape(pfx), "drive_c/users/steamuser/AppData/LocalLow/*/*/Player.log")):
        try:
            with open(path, errors="replace") as f:
                if "Unable to load DLL 'LibOVRPlatform" in f.read(1 << 20):
                    return True
        except OSError:
            pass
    return False


def log_problem(path):
    text = _tail(path)
    if text is None:
        return None
    for pattern, describe in _LOG_PROBLEMS:
        m = pattern.search(text)
        if m:
            return describe(m)
    return ""


def check_game(game, cfg):
    checks = []
    vr = not game.custom or game.mode == "vr"

    if game.custom:
        if os.path.isfile(game.exe):
            checks.append(Check("game-files", t("Game files", "Game files"), OK, os.path.basename(game.exe)))
        else:
            checks.append(Check("game-files", t("Game files", "Game files"), FAIL,
                                t(f"{game.exe} is missing", f"{os.path.basename(game.exe)} is gone")))
            return checks
    elif not installer.find_executables(game.install_dir):
        checks.append(Check("game-files", t("Game files", "Game files"), FAIL,
                            t("No game executable in the install folder. The install looks incomplete.",
                              "There's no .exe in the install folder. Looks half-installed."),
                            t("Verify in Steam", "Verify in Steam")))
        return checks
    else:
        checks.append(Check("game-files", t("Game files", "Game files"), OK,
                            t("Installed", "All there")))

    apis = set(game.vr_apis)
    if not vr:
        checks.append(Check("game-vr", t("VR support", "VR support"), INFO,
                            t("Simple mode: launches without VR", "Simple mode. No headset involved.")))
    elif apis & {"openxr", "openvr"}:
        names = " + ".join(n for k, n in (("openxr", "OpenXR"), ("openvr", "OpenVR")) if k in apis)
        checks.append(Check("game-vr", t("VR support", "VR support"), OK, names))
    elif "oculus" in apis:
        checks.append(Check("game-vr", t("VR support", "VR support"), FAIL,
                            t("Oculus SDK only. There is no Linux runtime for it.",
                              "Oculus-only, and the Oculus runtime doesn't exist on Linux.")))
    else:
        unreal = mods.is_unreal(game)
        checks.append(Check("game-vr", t("VR support", "VR support"), INFO,
                            t("Flat game. UEVR can add VR (see Mods below)." if unreal else
                              "Flat game. It plays on a virtual screen in the headset.",
                              "Flat game, but it's Unreal, so UEVR can VR-ify it. Check Mods below." if unreal else
                              "Flat game. It'll be a big screen in your headset.")))

    state, detail, hit = anticheat.verdict(game, cfg.get("online", True))
    if state:
        ask = state == FAIL and (game.store_id or not game.custom)
        checks.append(Check("game-ac", t("Anti-cheat", "Anti-cheat"), state, detail,
                            t("Ask the Devs", "Ask the Devs") if ask else ""))

    if game.custom:
        protons = steam.proton_builds()
        if shutil.which("umu-run") or cfg.get("proton") or protons:
            checks.append(Check("game-proton", "Proton", OK,
                                "umu-launcher" if shutil.which("umu-run") else
                                os.path.basename(cfg.get("proton") or protons[0][1])))
        else:
            checks.append(Check("game-proton", "Proton", FAIL,
                                t("Install umu-launcher or Proton", "Install umu-launcher or Proton first")))
    else:
        pfx = steam.compat_prefix(game.appid)
        proton = steam.proton_for_prefix(pfx) if pfx else ""
        if pfx:
            checks.append(Check("game-proton", "Proton", OK,
                                t(f"Prefix made by {os.path.basename(proton)}", f"Last ran with {os.path.basename(proton)}")
                                if proton else t("Prefix ready", "Prefix ready")))
        else:
            checks.append(Check("game-proton", "Proton", INFO,
                                t("Not run through Proton yet", "Hasn't run under Proton yet")))

    if vr and not game.custom and game.vr_apis:
        opts = steam.get_launch_options(game.appid)
        if steam.has_env(opts, runtimes.vr_env()):
            checks.append(Check("game-options", t("Launch options", "Launch options"), OK,
                                t("VR variables are set", "Set")))
        else:
            checks.append(Check("game-options", t("Launch options", "Launch options"), WARN,
                                t("VR variables missing; the game may not find the runtime",
                                  "Missing, so the game might not find your headset"),
                                t("Apply", "Do It")))

    if vr and "openvr" in apis:
        kind = openvr.classify(openvr.current_runtime())
        if kind in ("xrizer", "opencomposite"):
            checks.append(Check("openvr", t("OpenVR route", "OpenVR route"), OK,
                                t(f"Through {kind}", f"Goes through {kind}")))
        else:
            checks.append(Check("openvr", t("OpenVR route", "OpenVR route"), WARN,
                                t("No OpenVR translation layer is active", "Nothing is translating OpenVR"),
                                t("Use xrizer", "Switch to xrizer")))

    dll = mods.dll_overrides(game)
    if dll:
        if game.custom:
            checks.append(Check("game-dll", t("Mod DLLs", "Mod DLLs"), OK,
                                t(f"Loaded at launch: {dll}", f"Loaded at launch: {dll}")))
        elif steam.has_env(steam.get_launch_options(game.appid), mods.launch_env(game)):
            checks.append(Check("game-dll", t("Mod DLLs", "Mod DLLs"), OK, dll))
        else:
            checks.append(Check("game-dll", t("Mod DLLs", "Mod DLLs"), WARN,
                                t(f"Installed mods need WINEDLLOVERRIDES={dll} in the launch options",
                                  f"Your mods need WINEDLLOVERRIDES={dll} in the launch options or they won't load"),
                                t("Apply", "Do It")))

    if meta_check_failed(game):
        checks.append(Check("game-meta", t("Meta ownership check", "Meta ownership check"), FAIL,
                            t("This copy is DRM protected. It asks the Meta app whether you own it, and that app "
                              "doesn't run on Linux, so the game quits. A Steam copy may work instead.",
                              "This copy is DRM protected. It asks the Meta app if you own it, the app doesn't do "
                              "Linux, and the game gives up. A Steam copy may work instead.")))

    log = game_log(game)
    problem = log_problem(log)
    if problem:
        checks.append(Check("game-log", t("Last launch", "Last launch"), WARN,
                            t(f"The log says {problem}", f"The log says {problem}"), t("Open Log", "Read It")))
    elif problem == "":
        checks.append(Check("game-log", t("Last launch", "Last launch"), OK,
                            t("No errors in the last log", "The last log looks clean")))
    return checks
