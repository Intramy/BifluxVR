# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
import copy
import json
import os
import threading

from .util import APP_CONFIG_DIR, CONFIG_HOME, HOME, atomic_write

PATH = os.path.join(APP_CONFIG_DIR, "config.json")
# Settings from before the app was renamed from Lumen.
LEGACY_PATH = os.path.join(CONFIG_HOME, "lumenvr", "config.json")
_lock = threading.Lock()

DEFAULTS = {
    "custom_games": [],  # [{"id", "name", "exe", "mode": "vr"|"simple", "prefix"?, "store_id"?}]
    "proton": "",  # path to the Proton dir used for non-Steam games
    "auto_start_server": True,
    "appearance": "system",  # system | light | dark
    "serious_mode": False,  # plain wording instead of the jokes
    "install_dir_vr": os.path.join(HOME, "Games", "BifluxVR", "VR"),
    "install_dir_simple": os.path.join(HOME, "Games", "BifluxVR", "Simple"),
    "install_mode": "vr",
    "online": True,  # ProtonDB ratings, store art and Steam matching
    "notify": True,  # WiVRn/xrizer/Proton update notices
    "vr_audio": True,  # move the sound output to the headset during VR games
    "audio_after": "",  # sink to return to after playing; "" = headphones first
    "welcomed": False,  # the first-start welcome was shown
    "library_sort": "name",  # name | recent | played
    "ratings": {},  # library key → "works" | "issues" | "broken"
    "meta_games": {},  # Meta app id → {"dir", "complete", "version_code"?, "custom_id"?, "exe"?}
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
