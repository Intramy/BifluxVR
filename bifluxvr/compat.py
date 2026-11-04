import os
import re
import urllib.error

from . import net, steam
from .util import CACHE_DIR

TIERS = ("platinum", "gold", "silver", "bronze", "borked")
CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/{file}"
ART_FILES = {
    "library_600x900": "library_600x900.jpg",
    "library_hero": "library_hero.jpg",
    "header": "header.jpg",
    "logo": "logo.png",
}

_protondb = net.Cache("protondb.json", 3 * 86400)
_matches = net.Cache("store-match.json", 30 * 86400)

_JUNK = re.compile(
    r"\[[^]]*]|\([^)]*\)|\bv?\d+(\.\d+){1,3}\w*\b|\bbuild\s*\d+\b|"
    r"\b\w*(rip|repack|-?gog|codex|plaza|skidrow|fitgirl|dodi|elamigos)\b|"
    r"\b(com|net|org|io|ru|to)\b", re.I)


def protondb(appid):
    key = str(appid)
    hit = _protondb.get(key)
    if hit is not None:
        return hit or None
    try:
        data = net.get_json(f"https://www.protondb.com/api/v1/reports/summaries/{key}.json", timeout=8)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return _protondb.put(key, {}) or None
        raise
    summary = {
        "tier": data.get("tier", ""),
        "confidence": data.get("confidence", ""),
        "total": data.get("total", 0),
        "trending": data.get("trendingTier", ""),
    }
    return _protondb.put(key, summary)


def clean_title(name):
    text = _JUNK.sub(" ", name.replace("_", " "))
    text = re.sub(r"[.]+|(?<=\w)-(?=\w)", " ", text)
    words = (w.strip("-") for w in _JUNK.sub(" ", text).split())
    return " ".join(w for w in words if w)


def store_match(name):
    title = clean_title(name)
    if not title:
        return ""
    hit = _matches.get(title.lower())
    if hit is not None:
        return hit
    data = net.get_json("https://store.steampowered.com/api/storesearch/"
                        f"?term={net.quote(title)}&cc=us&l=en", timeout=8)
    items = [i for i in data.get("items", []) if i.get("type") == "app"]
    want = _norm(title)
    exact = [i for i in items if _norm(i.get("name", "")) == want]
    pick = (exact or items[:1] or [{}])[0]
    return _matches.put(title.lower(), str(pick.get("id", "")))


def steam_twin(name):
    title = clean_title(name)
    if not title:
        return None
    key = "twin:" + title.lower()
    hit = _matches.get(key)
    if hit:
        return tuple(hit)
    bare = re.sub(r"\s*\bvr\b", "", title, flags=re.I).strip()
    want = _norm(bare)
    for term in dict.fromkeys((title, bare)):
        data = net.get_json(f"https://store.steampowered.com/api/storesearch/?term={net.quote(term)}&cc=us&l=en",
                            timeout=8)
        for i in data.get("items", []):
            if i.get("type") == "app" and _norm(re.sub(r"\bvr\b", "", i.get("name", ""), flags=re.I)) == want:
                return tuple(_matches.put(key, [str(i["id"]), i["name"]]))
    return None


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _art_path(appid, kind):
    return os.path.join(CACHE_DIR, "art", str(appid), ART_FILES[kind])


def fetch_art(appid, kinds=("library_600x900",)):
    for kind in kinds:
        path = _art_path(appid, kind)
        miss = path + ".missing"
        if os.path.exists(path) or os.path.exists(miss):
            continue
        try:
            net.download(CDN.format(appid=appid, file=ART_FILES[kind]), path, timeout=10)
        except urllib.error.HTTPError:
            os.makedirs(os.path.dirname(miss), exist_ok=True)
            open(miss, "w").close()


def artwork(game, kind="library_600x900"):
    if not game.custom:
        art = steam.artwork(game.appid, kind)
        if art:
            return art
    if game.store_id:
        path = _art_path(game.store_id, kind)
        if os.path.isfile(path):
            return path
    return None
