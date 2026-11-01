# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
"""The few HTTP calls BifluxVR makes, with a small on-disk cache so pages
don't hit the network every time they open."""

import json
import os
import threading
import time
import urllib.parse
import urllib.request

from .util import CACHE_DIR, atomic_write

UA = {"User-Agent": "BifluxVR"}
_lock = threading.Lock()


def get_json(url, timeout=15, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def github(path, timeout=20):
    return get_json(f"https://api.github.com/repos/{path}", timeout,
                    {"Accept": "application/vnd.github+json"})


def github_latest(repo):
    return github(f"{repo}/releases/latest")


def github_releases(repo, count=5):
    return github(f"{repo}/releases?per_page={count}")


def download(url, dest, progress=None, timeout=60):
    """Fetch `url` into `dest` via a temporary file, so a failed download
    never leaves a half-written file behind."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while chunk := r.read(1 << 16):
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return dest


def quote(text):
    return urllib.parse.quote(text)


class Cache:
    """A JSON file of {key: [timestamp, value]} entries that expire."""

    def __init__(self, name, max_age):
        self.path = os.path.join(CACHE_DIR, name)
        self.max_age = max_age
        self._data = None

    def _load(self):
        if self._data is None:
            try:
                with open(self.path) as f:
                    self._data = json.load(f)
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def get(self, key):
        with _lock:
            hit = self._load().get(key)
        if hit and time.time() - hit[0] < self.max_age:
            return hit[1]
        return None

    def put(self, key, value):
        with _lock:
            self._load()[key] = [time.time(), value]
            try:
                atomic_write(self.path, json.dumps(self._data))
            except OSError:
                pass
        return value
