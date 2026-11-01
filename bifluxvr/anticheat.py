# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
"""Whether a game's anti-cheat lets it run under Proton, from the community
list at areweanticheatyet.com, with a scan of the game folder as fallback."""

import os

from . import net
from .util import slug
from .voice import t

SOURCE = "https://raw.githubusercontent.com/AreWeAntiCheatYet/AreWeAntiCheatYet/master/games.json"
SITE = "https://areweanticheatyet.com"
# Works on Linux everywhere, so not worth a mention.
HARMLESS = {"VAC"}
BLOCKED = {"Broken", "Denied"}

# File or folder name prefixes → anti-cheat, for games the list doesn't know.
_MARKERS = (
    (("easyanticheat",), "Easy Anti-Cheat"),
    (("battleye", "beservice"), "BattlEye"),
    (("gameguard",), "nProtect GameGuard"),
    (("xigncode", "x3.xem"), "XIGNCODE3"),
    (("anticheatexpert", "ace-base"), "Anti-Cheat Expert"),
    (("equ8",), "EQU8"),
    (("randgrid",), "RICOCHET"),
    (("mhyprot",), "mhyprot"),
    (("pnkbstr", "pbsvc"), "PunkBuster"),
)

_cache = net.Cache("anticheat.json", 3 * 86400)
_db = None


def database(online=True):
    """{"steam": {appid: entry}, "name": {slug: entry}}. Without `online`, or
    when the list can't be fetched, whatever was cached (maybe nothing)."""
    global _db
    if _db is not None:
        return _db
    games = _cache.get("games")
    if games is None and online:
        try:
            games = _cache.put("games", [
                {"name": g["name"], "status": g.get("status", ""), "slug": g.get("slug", ""),
                 "anticheats": [a for a in g.get("anticheats", []) if a not in HARMLESS],
                 "steam": str((g.get("storeIds") or {}).get("steam") or "")}
                for g in net.get_json(SOURCE, timeout=30)])
        except (OSError, ValueError):
            games = None
    if games is None:
        return {"steam": {}, "name": {}}
    _db = {"steam": {g["steam"]: g for g in games if g["steam"]},
           "name": {slug(g["name"]): g for g in games}}
    return _db


def lookup(game, online=True):
    db = database(online)
    return db["steam"].get(game.store_id or ("" if game.custom else game.appid)) or \
        db["name"].get(slug(game.name))


def scan(root, max_depth=3):
    """The anti-cheat shipped in a game folder, or ""."""
    if not root or not os.path.isdir(root):
        return ""
    base = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, files in os.walk(root):
        if dirpath.count(os.sep) - base >= max_depth:
            dirnames[:] = []
        for name in dirnames + files:
            low = name.lower()
            for prefixes, title in _MARKERS:
                if low.startswith(prefixes):
                    return title
    return ""


def blocked(game):
    """True if the list says this game won't run. Uses only data already
    loaded, so it's cheap enough for library cards."""
    if _db is None:
        return False
    hit = lookup(game, online=False)
    return bool(hit and hit["status"] in BLOCKED and hit["anticheats"])


def verdict(game, online=True):
    """(state, detail, entry) with state "ok", "warn", "fail" or "" when there's
    no anti-cheat to talk about."""
    hit = lookup(game, online)
    if hit and hit["anticheats"]:
        ac = " + ".join(hit["anticheats"])
        status = hit["status"]
        if status == "Supported":
            return "ok", t(f"{ac}, enabled for Proton by the developer",
                           f"{ac}, and the devs switched on Linux support. Nice."), hit
        if status == "Running":
            return "ok", t(f"{ac}. Runs under Proton, though not officially supported",
                           f"{ac}. Runs on Linux, just not officially"), hit
        if status == "Planned":
            return "warn", t(f"{ac}. The developer has said Linux support is coming",
                             f"{ac}. The devs promised Linux support. Any day now."), hit
        if status == "Denied":
            return "fail", t(f"{ac}. The developer blocks Linux on purpose, so online play won't work",
                             f"{ac}, and the devs block Linux on purpose. Online play is a no."), hit
        if status == "Broken":
            return "fail", t(f"{ac} isn't enabled for Proton, so it won't get past the anti-cheat",
                             f"{ac} isn't switched on for Linux. It'll get kicked at the door."), hit
    if hit:
        return "", "", hit
    found = scan(game.install_dir if not game.custom else os.path.dirname(game.exe))
    if found:
        return "warn", t(f"Uses {found}. It runs only if the developer allowed Proton.",
                         f"Has {found}. Works only if the devs flipped the Proton switch."), None
    return "", "", None


def request_text(game, hit):
    ac = " + ".join(hit["anticheats"]) if hit else "the anti-cheat"
    return (f"Please enable Linux/Proton support for {ac} in {game.name}\n\n"
            f"I'd like to play {game.name} on Linux through Proton, but {ac} blocks it. "
            f"Both Easy Anti-Cheat and BattlEye support Proton officially; it's mostly a switch on the "
            f"developer's side. Elden Ring, VRChat and Halo: The "
            f"Master Chief Collection already did it. It would also make the game work on the Steam Deck.\n\n"
            f"Thanks for considering it!")
