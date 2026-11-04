import os
import shutil
import signal
import time

from . import openvr, runtimes, sessions, steam
from .voice import t
from .util import APP_DATA_DIR, LOG_DIR, RUNTIME_DIR, slug, spawn_detached, terminate

_procs = {}


class LaunchError(RuntimeError):
    pass


def prepare(cfg):
    notes = []
    server = runtimes.preferred_server()
    if cfg.get("auto_start_server", True) and server and server.installed and not server.running:
        server.start()
        notes.append(t(f"Started {server.title}", f"woke up {server.title}"))
        if server.manages_runtime:
            for _ in range(30):
                if runtimes.active():
                    break
                time.sleep(0.1)
    rt = runtimes.active()
    if rt is None or not rt.valid:
        cands = [c for c in runtimes.discover() if c.valid and c.kind != "steamvr"]
        cands.sort(key=lambda c: c.kind != (server.kind if server else ""))
        if cands:
            runtimes.set_active(cands[0].manifest)
            notes.append(t(f"Activated {cands[0].name}", f"put {cands[0].name} in charge"))
    if openvr.classify(openvr.current_runtime()) in ("", "other"):
        layers = [l for l in openvr.discover() if l.kind == "xrizer"] or \
                 [l for l in openvr.discover() if l.kind == "opencomposite"]
        if layers:
            openvr.set_runtime(layers[0].path)
            notes.append(t(f"OpenVR now uses {layers[0].title}", f"OpenVR now goes through {layers[0].title}"))
    return notes


def _spawn(argv, env, log_name, cwd=None):
    try:
        return spawn_detached(argv, os.path.join(LOG_DIR, f"{log_name}.log"), env=env, cwd=cwd)
    except OSError as e:
        raise LaunchError(str(e)) from e


def launch_steam(appid):
    url = f"steam://rungameid/{appid}"
    if shutil.which("steam"):
        _spawn(["steam", url], os.environ.copy(), "steam")
    elif shutil.which("xdg-open"):
        _spawn(["xdg-open", url], os.environ.copy(), "steam")
    else:
        raise LaunchError(t("Steam is not installed", "Steam isn't installed"))


def _expand(env):
    return {k: v.replace("$XDG_RUNTIME_DIR", RUNTIME_DIR) for k, v in env.items()}


def default_prefix(name):
    return os.path.join(APP_DATA_DIR, "prefixes", slug(name))


def launch_custom(game, proton_dir, dll_overrides=""):
    exe = game["exe"]
    if not os.path.isfile(exe):
        raise LaunchError(t(f"{exe} no longer exists", f"{exe} is gone"))
    prefix = game.get("prefix") or default_prefix(game["name"])
    os.makedirs(prefix, exist_ok=True)
    env = os.environ.copy()
    if game.get("mode", "vr") == "vr":
        env.update(_expand(runtimes.vr_env()))
    if dll_overrides:
        env["WINEDLLOVERRIDES"] = ";".join(filter(None, (env.get("WINEDLLOVERRIDES"), dll_overrides)))
    argv = _wine_command(exe, prefix, proton_dir, env)
    return _spawn(argv, env, slug(game["name"]), cwd=os.path.dirname(exe))


def run_setup(path, prefix, proton_dir, name):
    env = os.environ.copy()
    env["WINEDLLOVERRIDES"] = ";".join(filter(None, (env.get("WINEDLLOVERRIDES"), "winemenubuilder.exe=d")))
    target = path
    if path.lower().endswith(".msi"):
        target = os.path.join(prefix, "run-setup.bat")
        windows = "Z:" + path.replace("/", "\\")
        with open(target, "w") as f:
            f.write(f'@echo off\r\nmsiexec /i "{windows}"\r\n')
    argv = _wine_command(target, prefix, proton_dir, env)
    return _spawn(argv, env, f"setup-{slug(name)}", cwd=os.path.dirname(path))


def _wine_command(exe, prefix, proton_dir, env):
    if shutil.which("umu-run"):
        env["WINEPREFIX"] = prefix
        env["GAMEID"] = env.get("GAMEID", "umu-default")
        if proton_dir:
            env["PROTONPATH"] = proton_dir
        argv = ["umu-run", exe]
    else:
        if not proton_dir or not os.path.isfile(os.path.join(proton_dir, "proton")):
            raise LaunchError(t("Install umu-launcher or choose a Proton version in Translation",
                                 "Install umu-launcher or pick a Proton version in Babel Fish"))
        roots = steam.roots()
        if not roots:
            raise LaunchError(t("Proton needs a Steam installation", "Proton needs Steam installed"))
        env.pop("WINEPREFIX", None)
        env["STEAM_COMPAT_DATA_PATH"] = prefix
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = roots[0]
        argv = [os.path.join(proton_dir, "proton"), "run", exe]
    return argv


def log_path(name):
    return os.path.join(LOG_DIR, f"{name}.log")


def track(game, proc):
    _procs[game.appid] = proc


def running(game):
    if game.custom:
        proc = _procs.get(game.appid)
        return proc is not None and proc.poll() is None
    return sessions.steam_game_running(game.appid)


def _parents():
    out = {}
    uid = os.getuid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            if os.stat(f"/proc/{entry}").st_uid != uid:
                continue
            with open(f"/proc/{entry}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            out[int(entry)] = int(fields[1])
        except (OSError, IndexError, ValueError):
            continue
    return out


def stop(game):
    if game.custom:
        proc = _procs.pop(game.appid, None)
        victims = {proc.pid} if proc else set()
    else:
        victims = set(sessions.steam_game_pids(game.appid))
    _kill(victims)


def kill_tree(pid):
    _kill({pid})


def _kill(victims):
    parents = _parents()
    grew = True
    while grew:
        more = {pid for pid, ppid in parents.items() if ppid in victims} - victims
        victims |= more
        grew = bool(more)
    victims.discard(os.getpid())
    terminate(victims, signal.SIGKILL)
