import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .util import CACHE_DIR, atomic_write

UA = {"User-Agent": "BifluxVR"}
_lock = threading.Lock()


def get_json(url, timeout=15, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def post_json(url, body, timeout=15):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={**UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, {}


def github(path, timeout=20):
    return get_json(f"https://api.github.com/repos/{path}", timeout,
                    {"Accept": "application/vnd.github+json"})


def _github_web_latest(repo):
    req = urllib.request.Request(f"https://github.com/{repo}/releases/latest", headers=UA, method="HEAD")
    with urllib.request.urlopen(req, timeout=20) as r:
        final = r.url
    if "/releases/tag/" not in final:
        return None
    tag = urllib.parse.unquote(final.rsplit("/releases/tag/", 1)[1])
    req = urllib.request.Request(f"https://github.com/{repo}/releases/expanded_assets/{quote(tag)}", headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        page = r.read().decode("utf-8", "replace")
    assets = []
    for href in dict.fromkeys(re.findall(r'href="(/[^"]+/releases/download/[^"]+)"', page)):
        assets.append({"name": urllib.parse.unquote(href.rsplit("/", 1)[1]),
                       "browser_download_url": "https://github.com" + href})
    return {"tag_name": tag, "html_url": final, "body": "", "assets": assets,
            "tarball_url": f"https://github.com/{repo}/archive/refs/tags/{quote(tag)}.tar.gz"}


_latest = {}


def github_latest(repo):
    hit = _latest.get(repo)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        rel = github(f"{repo}/releases/latest")
    except urllib.error.HTTPError as e:
        if e.code not in (403, 429):
            raise
        rel = _github_web_latest(repo)
        if rel is None:
            raise
    _latest[repo] = (time.time(), rel)
    return rel


def github_releases(repo, count=5):
    return github(f"{repo}/releases?per_page={count}")


def download(url, dest, progress=None, timeout=60):
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
