# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
"""Games bought on Meta's PC store, downloaded straight from Meta.

BifluxVR never sees a password. The user signs in on Meta's own site and
hands over the session token from its cookie; it stays in a private file
and is only ever sent back to Meta."""

import hashlib
import http.client
import io
import json
import os
import re
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor

from .net import UA
from .util import APP_CONFIG_DIR, CACHE_DIR, atomic_write
from .voice import t

LOGIN_URL = "https://secure.oculus.com/my/profile/"
GRAPH = "https://graph.oculus.com/graphql"
CDN = "https://securecdn.oculus.com/binaries"
OWNED_DOC = "4850747515044496"
TOKEN_PATH = os.path.join(APP_CONFIG_DIR, "meta-token")
MANIFESTS = os.path.join(CACHE_DIR, "meta")

# What a game's files say about whether it can run here.
OPENXR, DRM, OCULUS, UNKNOWN = "openxr", "drm", "oculus", "unknown"


class MetaError(RuntimeError):
    pass


class LoginNeeded(MetaError):
    pass


class Stopped(MetaError):
    pass


def token():
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            return f.read().replace("\ufeff", "").strip()
    except OSError:
        return ""


def save_token(raw):
    tok = raw.replace("\ufeff", "").strip().strip("\"'").strip()
    if not re.fullmatch(r"(OC|FRL)[A-Za-z0-9|_\-]{20,}", tok):
        raise MetaError(t("That doesn't look like a Meta token. It starts with OC.",
                          "That's not a Meta token. The right one starts with OC."))
    atomic_write(TOKEN_PATH, tok, mode=0o600)


def forget():
    try:
        os.unlink(TOKEN_PATH)
    except FileNotFoundError:
        pass


def _open(url, data=None, timeout=60):
    try:
        return urllib.request.urlopen(urllib.request.Request(url, data, UA), timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            raise LoginNeeded(t("Meta didn't accept the login. Sign in again.",
                                "Meta forgot who you are. Sign in again.")) from None
        raise


def owned():
    """PC games on the account: [{"id", "name", "canonical", "binary", "version_code"}]
    and how many Quest-only items were skipped."""
    tok = token()
    if not tok:
        raise LoginNeeded(t("Not signed in", "Not signed in"))
    body = urllib.parse.urlencode({"access_token": tok, "variables": "{}", "doc_id": OWNED_DOC}).encode()
    with _open(GRAPH, body) as r:
        data = json.load(r)
    if data.get("errors") and not data.get("data"):
        raise LoginNeeded(t("Meta didn't accept the login. Sign in again.",
                            "Meta forgot who you are. Sign in again."))
    nodes = (((data.get("data") or {}).get("viewer") or {}).get("user") or {}) \
        .get("active_entitlements", {}).get("nodes", [])
    games, skipped = [], 0
    for n in nodes:
        item = n.get("item") or {}
        binary = item.get("latest_supported_binary") or {}
        if item.get("platform") != "PC" or not binary.get("id"):
            skipped += 1
            continue
        games.append({"id": item["id"], "name": item.get("display_name") or item["id"],
                      "canonical": item.get("canonical_name") or item["id"],
                      "binary": binary["id"], "version_code": binary.get("version_code") or 0})
    games.sort(key=lambda g: g["name"].lower())
    return games, skipped


def manifest(game):
    """The build's file list. Cached, since a build never changes."""
    path = os.path.join(MANIFESTS, f"{game['binary']}.json")
    try:
        with open(path) as f:
            m = json.load(f)
        return _note_platform(game, m, path)
    except (OSError, ValueError):
        pass
    url = f"{CDN}/download/?" + urllib.parse.urlencode(
        {"id": game["binary"], "access_token": token(), "get_manifest": 1})
    with _open(url) as r:
        blob = r.read()
    try:
        m = json.loads(zipfile.ZipFile(io.BytesIO(blob)).read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, ValueError):
        raise MetaError(t("Meta sent an unreadable file list", "Meta sent gibberish instead of a file list")) from None
    atomic_write(path, json.dumps(m))
    return _note_platform(game, m, path)


def _base_names(m):
    return {n.replace("\\", "/").rsplit("/", 1)[-1].lower() for n in m.get("files", {})}


def _note_platform(game, m, path):
    """Whether the game's code uses Meta's platform library. The library itself
    ships with the Meta app, so the game's own files often don't include it, but
    a Unity build names its assemblies in one small file. A failed lookup isn't
    saved, so the next visit tries again."""
    if "platform" in m:
        return m
    used = "oculus.platform.dll" in _base_names(m)
    if not used:
        listed = [i for n, i in m.get("files", {}).items() if n.lower().endswith("scriptingassemblies.json")]
        try:
            used = any(b"Oculus.Platform.dll" in b"".join(_segment(game["binary"], s[1]) for s in i.get("segments", []))
                       for i in listed)
        except (OSError, MetaError, http.client.HTTPException, zlib.error):
            return m
    m["platform"] = used
    atomic_write(path, json.dumps(m))
    return m


def verdict(m):
    names = _base_names(m)
    if m.get("platform") or any(n.startswith("libovrplatform") for n in names):
        return DRM
    if names & {"openxr_loader.dll", "unityopenxr.dll"}:
        return OPENXR
    if any(n.startswith(("libovrrt", "ovrplugin")) for n in names):
        return OCULUS
    return UNKNOWN


def size(m):
    return sum(f.get("size", 0) for f in m.get("files", {}).values())


_local = threading.local()


def _segment(binary, sha):
    """One compressed chunk. Each download thread keeps its connection to
    the CDN open: a game is thousands of small files, and a fresh TLS
    handshake per chunk would cost more than the chunk itself."""
    path = "/binaries/segment/?" + urllib.parse.urlencode(
        {"access_token": token(), "binary_id": binary, "segment_sha256": sha})
    for attempt in range(3):
        conn = getattr(_local, "conn", None)
        if conn is None:
            conn = _local.conn = http.client.HTTPSConnection(urllib.parse.urlparse(CDN).netloc, timeout=120)
        try:
            conn.request("GET", path, headers=UA)
            r = conn.getresponse()
            body = r.read()
        except (OSError, http.client.HTTPException):
            conn.close()
            _local.conn = None
            if attempt == 2:
                raise
            continue
        if r.status in (400, 401, 403):
            raise LoginNeeded(t("Meta didn't accept the login. Sign in again.",
                                "Meta forgot who you are. Sign in again."))
        if r.status != 200:
            raise MetaError(f"Meta answered {r.status} {r.reason}")
        return zlib.decompress(body)


def _target(dest, name):
    parts = [p for p in name.replace("\\", "/").split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise MetaError(f"Refusing odd path in manifest: {name}")
    return os.path.join(dest, *parts)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def download(game, dest, progress=None, cancel=None, workers=12):
    """Fetch every file of the game's build into `dest`. Files already there
    and intact are kept, so a stopped download picks up where it left off.
    Returns the path of the game's .exe."""
    m = manifest(game)
    files = m.get("files", {})
    total = size(m)
    os.makedirs(dest, exist_ok=True)
    have = sum(os.path.getsize(_target(dest, n)) for n in files if os.path.isfile(_target(dest, n)))
    free = shutil.disk_usage(dest).free
    if total - have > free:
        raise MetaError(t(f"Not enough space: needs {(total - have) / 1e9:.1f} GB, {free / 1e9:.1f} GB free",
                          f"Your disk is full-ish: needs {(total - have) / 1e9:.1f} GB, only {free / 1e9:.1f} GB free"))
    cancel = cancel or threading.Event()
    lock = threading.Lock()
    done = [0]

    def advance(n):
        with lock:
            done[0] += n
            if progress:
                progress(done[0], total)

    def fetch(name, info):
        if cancel.is_set():
            raise Stopped(t("Download stopped", "Download stopped"))
        path = _target(dest, name)
        if os.path.isfile(path) and os.path.getsize(path) == info["size"] and _sha256(path) == info["sha256"]:
            advance(info["size"])
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        part, h, wrote = path + ".part", hashlib.sha256(), 0
        try:
            with open(part, "wb") as out:
                for _offset, seg_sha, _size in info.get("segments", []):
                    if cancel.is_set():
                        raise Stopped(t("Download stopped", "Download stopped"))
                    data = _segment(game["binary"], seg_sha)
                    out.write(data)
                    h.update(data)
                    wrote += len(data)
                    advance(len(data))
            if h.hexdigest() != info["sha256"]:
                raise MetaError(t(f"{name} arrived damaged", f"{name} arrived broken"))
            os.replace(part, path)
        except BaseException:
            advance(-wrote)
            if os.path.exists(part):
                os.unlink(part)
            raise

    with ThreadPoolExecutor(workers) as pool:
        jobs = [pool.submit(fetch, n, info) for n, info in files.items()]
        errors = []
        for job in jobs:
            try:
                job.result()
            except Exception as e:  # noqa: BLE001 — the first real cause is re-raised below
                cancel.set()
                errors.append(e)
    if errors:
        raise next((e for e in errors if not isinstance(e, Stopped)), errors[0])
    return _target(dest, m["launchFile"])
