# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
"""BifluxVR updating itself from GitHub releases.

A release is any GitHub release whose tag is a version number (v1.2.0); its source tarball must have
install.sh at the top, like this folder does."""

import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error

from . import __version__, net
from .util import CACHE_DIR

REPO = "Intramy/BifluxVR"
STAGE = os.path.join(CACHE_DIR, "self-update")

# What this run has learned, shared by the Settings row and the bell.
found = None  # the newer release, from latest()
state = ""  # "" | "checked" | "installed"


def enabled():
    return bool(REPO)


def _version(text):
    return tuple(int(n) for n in re.findall(r"\d+", text or ""))


def newer(tag):
    return _version(tag) > _version(__version__)


def latest():
    """The newest release as {version, notes, url, tarball}, or None when
    this copy is already up to date."""
    if not REPO:
        return None
    try:
        rel = net.github_latest(REPO)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # nothing released yet
            return None
        raise
    tag = rel.get("tag_name", "")
    if not newer(tag) or not rel.get("tarball_url"):
        return None
    return {"version": tag.lstrip("v"), "notes": (rel.get("body") or "").strip(),
            "url": rel.get("html_url", ""), "tarball": rel["tarball_url"]}


def check():
    global found, state
    rel = latest()
    if state != "installed":
        found, state = rel, "checked"
    return rel


def _source_root(tmp):
    for root, dirs, files in os.walk(tmp):
        if "install.sh" in files and os.path.isfile(os.path.join(root, "bifluxvr", "__init__.py")):
            return root
        if root.count(os.sep) - tmp.count(os.sep) >= 2:
            dirs.clear()
    raise ValueError("the release has no install.sh")


def install(release, progress=None):
    """Download `release`, check it really is the version it claims, and run
    its install.sh. The running app keeps the old code until it restarts."""
    global state
    os.makedirs(STAGE, exist_ok=True)
    archive = net.download(release["tarball"], os.path.join(STAGE, "release.tar.gz"), progress)
    tmp = tempfile.mkdtemp(dir=STAGE)
    try:
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter="data")
        root = _source_root(tmp)
        with open(os.path.join(root, "bifluxvr", "__init__.py")) as f:
            got = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", f.read())
        if not got or _version(got.group(1)) != _version(release["version"]):
            raise ValueError(f"release {release['version']} contains version "
                             f"{got.group(1) if got else 'unknown'}")
        res = subprocess.run(["sh", "install.sh", "--files-only"], cwd=root, capture_output=True, text=True, timeout=120)
        if res.returncode:
            lines = (res.stderr or res.stdout).strip().splitlines()
            raise RuntimeError(lines[-1] if lines else "install.sh failed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            os.unlink(archive)
        except OSError:
            pass
    state = "installed"
    return release["version"]
