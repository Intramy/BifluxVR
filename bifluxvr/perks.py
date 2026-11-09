import base64
import hashlib
import json
import os
import secrets

from . import net
from .util import APP_CONFIG_DIR, atomic_write

BASE = "https://intramy.xyz/donors/"
PUBLIC_KEY = "5GAD4WaZ67Ke9kEaEwRFP7QTSwoRD5ccEHz1fnfnHPU"

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _I % _P
    if (x * x - x2) % _P:
        return None
    return _P - x if (x & 1) != sign else x


def _add(a, b):
    p = _P
    aa = (a[1] - a[0]) * (b[1] - b[0]) % p
    bb = (a[1] + a[0]) * (b[1] + b[0]) % p
    cc = 2 * a[3] * b[3] * _D % p
    dd = 2 * a[2] * b[2] % p
    e, f, g, h = bb - aa, dd - cc, dd + cc, bb + aa
    return e * f % p, g * h % p, f * g % p, e * h % p


def _mul(s, pt):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _add(q, pt)
        pt = _add(pt, pt)
        s >>= 1
    return q


def _point(raw):
    y = int.from_bytes(raw, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


_GY = 4 * pow(5, _P - 2, _P) % _P
_G = (_x(_GY, 0), _GY, 1, _x(_GY, 0) * _GY % _P)


def verify(public, message, signature):
    if len(public) != 32 or len(signature) != 64:
        return False
    a, r = _point(public), _point(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a is None or r is None or s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % _L
    left, right = _mul(s, _G), _add(r, _mul(h, a))
    return (left[0] * right[2] - right[0] * left[2]) % _P == 0 and (left[1] * right[2] - right[1] * left[2]) % _P == 0


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def install_id():
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path) as f:
                machine = f.read().strip()
            if machine:
                return hashlib.sha256(f"bifluxvr-perks:{machine}".encode()).hexdigest()[:32]
        except OSError:
            continue
    path = os.path.join(APP_CONFIG_DIR, "install-id")
    try:
        with open(path) as f:
            own = f.read().strip()
        if len(own) == 32:
            return own
    except OSError:
        pass
    own = secrets.token_hex(16)
    atomic_write(path, own)
    return own


def read(token):
    try:
        body, sig = str(token).split(".")
        payload = _unb64(body)
        if not verify(_unb64(PUBLIC_KEY), payload, _unb64(sig)):
            return None
        data = json.loads(payload)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


def forged(cfg):
    token = cfg.get("perks_token")
    if token and read(token) is None:
        return True
    return bool((cfg.get("gold_look") or cfg.get("skin") == "gold") and not token)


def current(cfg):
    data = read(cfg.get("perks_token")) if cfg.get("perks_token") else None
    return data if data and data.get("i") == install_id() else None


def name(cfg):
    data = current(cfg)
    return str(data.get("n", ""))[:32] if data else ""


def gold(cfg):
    return bool(cfg.get("gold_look") and current(cfg))


def grant(cfg, token):
    cfg["perks_token"] = token
    cfg["gold_look"] = True
    cfg.pop("perks_legacy", None)


def drop(cfg):
    cfg.pop("perks_token", None)
    cfg.pop("perks_legacy", None)
    cfg["gold_look"] = False
    if cfg.get("skin") == "gold":
        cfg["skin"] = "classic"


def migrate(cfg):
    if cfg.get("perks_v") == 2:
        return cfg
    legacy = str(cfg.pop("donor_code", "") or "").strip()
    cfg.pop("donor_name", None)
    if cfg.get("skin") == "gold":
        cfg["skin"] = "classic"
    if legacy and not cfg.get("perks_token"):
        cfg["perks_legacy"] = legacy
    cfg["perks_v"] = 2
    return cfg


class PerksError(Exception):
    pass


def redeem(code):
    status, out = net.post_json(BASE + "redeem", {"code": code.strip().upper(), "install": install_id()})
    if status == 200 and read(out.get("token", "")):
        return out["token"]
    raise PerksError({404: "unknown", 409: "used", 429: "slow"}.get(status, "server"))


def check(cfg):
    token = cfg.get("perks_token")
    if not token:
        return None
    status, out = net.post_json(BASE + "check", {"install": install_id(), "token": token})
    if status != 200:
        return None
    if not out.get("ok"):
        return "revoked"
    fresh = out.get("token")
    return fresh if fresh and read(fresh) else "ok"
