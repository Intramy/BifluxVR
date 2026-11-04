import hashlib
import json
import os
import shutil

from . import mods
from .voice import t

CONTROLLERS = ("oculus_touch", "knuckles")
_TOUCH = "/user/hand/{}/input/{}"


def _pair(hand, a, b):
    return _TOUCH.format(hand, a), _TOUCH.format(hand, b)


def _both(a, b):
    return [_pair("left", a, b), _pair("right", a, b)]


SWAPS = {
    "ab": ("Swap A and B", "A ↔ B",
           {"oculus_touch": [_pair("right", "a", "b")], "knuckles": [_pair("right", "a", "b")]}),
    "xy": ("Swap X and Y", "X ↔ Y",
           {"oculus_touch": [_pair("left", "x", "y")], "knuckles": [_pair("left", "a", "b")]}),
    "grip": ("Swap trigger and grip", "Trigger ↔ grip",
             {"oculus_touch": _both("trigger", "grip"), "knuckles": _both("trigger", "grip")}),
    "sticks": ("Swap left and right stick", "Left stick ↔ right stick",
               {"oculus_touch": [(_TOUCH.format("left", "joystick"), _TOUCH.format("right", "joystick"))],
                "knuckles": [(_TOUCH.format("left", "thumbstick"), _TOUCH.format("right", "thumbstick"))]}),
}


def find_manifest(root, max_depth=6):
    base = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, files in os.walk(root):
        if dirpath.count(os.sep) - base >= max_depth:
            dirnames[:] = []
        for f in files:
            if f.lower() != "actions.json":
                continue
            path = os.path.join(dirpath, f)
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("default_bindings"):
                return path
    return ""


def binding_files(manifest):
    with open(manifest, encoding="utf-8-sig") as f:
        data = json.load(f)
    out = {}
    for entry in data.get("default_bindings", []):
        kind = entry.get("controller_type")
        url = entry.get("binding_url", "")
        if kind in CONTROLLERS and url:
            path = os.path.normpath(os.path.join(os.path.dirname(manifest), url))
            if os.path.isfile(path):
                out[kind] = path
    return out


def _swap_paths(node, mapping):
    if isinstance(node, dict):
        path = node.get("path")
        if isinstance(path, str) and path in mapping and "inputs" in node:
            node["path"] = mapping[path]
        for value in node.values():
            _swap_paths(value, mapping)
    elif isinstance(node, list):
        for value in node:
            _swap_paths(value, mapping)


def remap(data, kind, swaps):
    mapping = {}
    for swap in swaps:
        for a, b in SWAPS[swap][2].get(kind, []):
            mapping[a], mapping[b] = b, a
    _swap_paths(data.get("bindings", {}), mapping)
    return data


def _digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def active(game):
    return mods.load_state(game).get("bindings", {}).get("swaps", [])


def apply(game, manifest, swaps):
    state = mods.load_state(game)
    saved = state.get("bindings", {"files": {}})
    keep_dir = os.path.join(mods.MODS_DIR, mods.game_key(game), "bindings")
    files = binding_files(manifest)
    if not files:
        raise mods.ModError(t("This game has no bindings for Touch or Index controllers",
                              "This game didn't ship bindings for Touch or Index controllers. Nothing to swap."))
    for kind, path in files.items():
        entry = saved["files"].get(path)
        if not entry or _digest(path) != entry.get("written"):
            os.makedirs(keep_dir, exist_ok=True)
            backup = os.path.join(keep_dir, f"{kind}.json")
            shutil.copy2(path, backup)
            entry = {"backup": backup, "written": ""}
        if swaps:
            with open(entry["backup"], encoding="utf-8-sig") as f:
                data = remap(json.load(f), kind, swaps)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        else:
            shutil.copy2(entry["backup"], path)
        entry["written"] = _digest(path)
        saved["files"][path] = entry
    if swaps:
        saved["swaps"] = list(swaps)
        state["bindings"] = saved
    else:
        state.pop("bindings", None)
    mods.save_state(game, state)
