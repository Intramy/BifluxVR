# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
"""The optional install report, sent through the relay in relay/ (never to
Discord directly, so no secret lives in this open-source file).

Set RELAY_URL to the deployed worker to switch reports on."""

import json
import os
import urllib.request

from bifluxvr.util import APP_CONFIG_DIR, atomic_write

RELAY_URL = ""
# Our user number and the relay's proof of it, so reinstalls keep the number.
ID_PATH = os.path.join(APP_CONFIG_DIR, "install-id.json")


def enabled():
    return bool(RELAY_URL)


def message(info, count="#", status="INSTALLING"):
    """Exactly the line the relay posts."""
    return f"{info['os']}, {info['distro']}, {info['kernel']} - USER COUNT: {count} - {status}"


def _post(path, body):
    req = urllib.request.Request(RELAY_URL.rstrip("/") + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "BifluxVR-Installer"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _saved():
    try:
        with open(ID_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def start(info):
    """Returns the run handle for finish(), or None if the relay is down."""
    try:
        res = _post("/start", {"os": info["os"], "distro": info["distro"], "kernel": info["kernel"], **_saved()})
        atomic_write(ID_PATH, json.dumps({"count": res["count"], "token": res["token"]}))
        return res
    except (OSError, ValueError, KeyError):
        return None


def finish(run, ok, reason=""):
    if not run:
        return
    try:
        _post("/finish", {"run": run["run"], "ok": ok, "reason": reason[:300]})
    except (OSError, ValueError):
        pass
