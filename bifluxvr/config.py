import copy
import json
import os
import threading

from .util import APP_CONFIG_DIR, CONFIG_HOME, HOME, atomic_write

PATH = os.path.join(APP_CONFIG_DIR, "config.json")
LEGACY_PATH = os.path.join(CONFIG_HOME, "lumenvr", "config.json")
_lock = threading.Lock()

DEFAULTS = {
    "custom_games": [],
    "proton": "",
    "auto_start_server": True,
    "appearance": "system",
    "serious_mode": False,
    "install_dir_vr": os.path.join(HOME, "Games", "BifluxVR", "VR"),
    "install_dir_simple": os.path.join(HOME, "Games", "BifluxVR", "Simple"),
    "install_dir_setup": os.path.join(HOME, "Games", "BifluxVR", "Installed"),
    "install_mode": "vr",
    "scan_dir": os.path.join(HOME, "Downloads"),
    "folders": [],
    "game_folders": {},
    "online": True,
    "notify": True,
    "vr_audio": True,
    "audio_after": "",
    "welcomed": False,
    "library_sort": "name",
    "ratings": {},
    "meta_games": {},
}


def load():
    data = {}
    for path in (PATH, LEGACY_PATH):
        try:
            with open(path) as f:
                data = json.load(f)
            break
        except (OSError, ValueError):
            continue
    return {**copy.deepcopy(DEFAULTS), **(data if isinstance(data, dict) else {})}


def save(data):
    with _lock:
        atomic_write(PATH, json.dumps(data, indent=2))
