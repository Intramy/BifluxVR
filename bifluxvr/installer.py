# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
"""Drop Folder: unpack a game's .zip or .rar into a chosen folder and find
the executable that actually starts the game."""

import functools
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass, field

from . import steam
from .voice import t

ARCHIVE_EXTS = (".zip", ".rar")
# Multi-part RARs: game.part1.rar, game.part02.rar, …
_VOLUME = re.compile(r"\.part0*(\d+)\.rar$", re.I)

# Executables that ship alongside games but never start them.
_NOT_GAME = re.compile(
    r"(unins|setup|install|redist|vc_?redist|dxsetup|directx|dotnet|prereq|crash|report|"
    r"helper|updater|update|uninstall|easyanticheat|eac_|battleye|cleanup|benchmark|"
    r"config|settings|launcherpatcher|cefprocess|webhelper|7z|unitycrashhandler|ue4prereq)",
    re.I,
)


class InstallError(RuntimeError):
    pass


@dataclass
class ArchiveInfo:
    path: str
    name: str  # suggested game name
    size: int  # uncompressed bytes; 0 when the backend can't tell
    files: int
    exes: list = field(default_factory=list)  # archive paths of .exe files


def supported(path):
    return path.lower().endswith(ARCHIVE_EXTS)


def volume_number(path):
    """The part number of a multi-part RAR, or None for a single archive."""
    m = _VOLUME.search(path)
    return int(m.group(1)) if m else None


def first_volume(path):
    """For game.part3.rar, the path of the part that opens the set, or None."""
    d, base = os.path.split(path)
    stem = _VOLUME.sub("", base)
    for cand in sorted(os.listdir(d or ".")):
        if cand.startswith(stem) and volume_number(cand) == 1:
            return os.path.join(d, cand)
    return None


@functools.lru_cache(maxsize=1)
def rar_backend():
    """'unrar' (RARLAB's, not unrar-free) if present, else 'bsdtar', else None."""
    if shutil.which("unrar"):
        try:
            banner = subprocess.run(["unrar"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            banner = ""
        if "Alexander Roshal" in banner:
            return "unrar"
    if shutil.which("bsdtar"):
        return "bsdtar"
    return None


def suggest_name(path):
    stem = os.path.basename(path)
    stem = _VOLUME.sub("", stem)
    stem = re.sub(r"\.(zip|rar)$", "", stem, flags=re.I)
    stem = re.sub(r"\s*[\[(].*?[\])]\s*", " ", stem)  # drop [tags] and (versions)
    stem = re.sub(r"[\s._-]+v?\d+([._]\d+)+.*$", "", stem, flags=re.I)  # trailing version
    stem = re.sub(r"[._]+", " ", stem)
    return " ".join(stem.split()) or "Game"


def _unsafe(name):
    n = name.replace("\\", "/")
    return n.startswith("/") or re.match(r"^[A-Za-z]:", n) or ".." in n.split("/")


def _unsafe_error(member):
    return InstallError(t(f"Unsafe path in archive: {member}",
                          f"This archive tried to sneak a file outside its folder ({member}). Blocked."))


def _password_error(path):
    name = os.path.basename(path)
    return InstallError(t(f"{name} is password-protected. Extract it manually, then add the game's .exe with +",
                          f"{name} is password-protected and I don't know the password. Unpack it yourself, "
                          "then add the .exe with +"))


def _list_rar(path):
    """[(name, size, is_dir)] for a RAR archive."""
    backend = rar_backend()
    if backend is None:
        raise InstallError(t("Install unrar (or bsdtar) to open .rar files",
                             "I need unrar (or bsdtar) installed to crack open .rar files"))
    if backend == "bsdtar":
        r = subprocess.run(["bsdtar", "-tf", path], capture_output=True, text=True)
        if r.returncode:
            raise _bsdtar_error(path, r.stderr)
        return [(n, 0, False) for n in r.stdout.splitlines() if n]
    # -p-: never prompt for a password
    r = subprocess.run(["unrar", "lt", "-p-", path], capture_output=True, text=True)
    out = r.stdout + r.stderr
    if r.returncode == 11 or "encrypted headers" in out:
        raise _password_error(path)
    if r.returncode:
        raise _read_error(path, out)
    entries, cur = [], None
    for line in r.stdout.splitlines():
        key, _, value = line.strip().partition(": ")
        if key == "Name":
            cur = {"name": value, "type": "", "size": 0, "encrypted": False}
            entries.append(cur)
        elif cur is None:
            continue
        elif key == "Type":
            cur["type"] = value
        elif key == "Size" and value.isdigit():
            cur["size"] = int(value)
        elif key == "Flags" and "encrypted" in value:
            cur["encrypted"] = True
    if any(e["encrypted"] for e in entries):
        raise _password_error(path)
    return [(e["name"], e["size"], e["type"] == "Directory")
            for e in entries if e["type"] in ("File", "Directory")]


def _reason(output):
    """The most telling line of a tool's error output."""
    lines = [l.strip() for l in output.strip().splitlines()
             if l.strip() and "Error exit delayed" not in l]
    return lines[0] if lines else "unknown error"


def _read_error(path, output):
    name = os.path.basename(path)
    reason = _reason(output)
    return InstallError(t(f"Couldn't read {name}: {reason}", f"{name} won't open: {reason}"))


def _bsdtar_error(path, output):
    """bsdtar only handles some RARs; point at unrar, which handles them all."""
    if re.search(r"encrypt|passphrase|password", output, re.I):
        return _password_error(path)
    name = os.path.basename(path)
    reason = _reason(output)
    return InstallError(t(f"Couldn't read {name} ({reason}). Install unrar for full .rar support",
                          f"{name} is too fancy for bsdtar ({reason}). Install unrar and try again"))


def inspect(path):
    """Read the archive's directory (fast — no decompression)."""
    name = os.path.basename(path)
    if path.lower().endswith(".rar"):
        entries = _list_rar(path)
    else:
        try:
            with zipfile.ZipFile(path) as z:
                entries = [(i.filename, i.file_size, i.is_dir()) for i in z.infolist()]
        except (zipfile.BadZipFile, OSError) as e:
            raise InstallError(t(f"{name} isn't a valid .zip: {e}", f"{name} isn't a real .zip. Nice try: {e}")) from e
    for member, _, _ in entries:
        if _unsafe(member):
            raise _unsafe_error(member)
    files = [(n, s) for n, s, is_dir in entries if not is_dir]
    exes = [n for n, _ in files if n.lower().endswith(".exe")]
    return ArchiveInfo(path, suggest_name(path), sum(s for _, s in files), len(files), exes)


def _score(path, root, hint, size):
    rel = os.path.relpath(path, root)
    base = os.path.splitext(os.path.basename(path))[0].lower()
    score = -10 * rel.count(os.sep)
    if _NOT_GAME.search(base):
        score -= 100
    words = [w for w in re.split(r"[^a-z0-9]+", hint.lower()) if len(w) > 2]
    compact = re.sub(r"[^a-z0-9]", "", base)
    score += 25 * sum(1 for w in words if w in compact)
    if base.endswith("-win64-shipping"):
        score += 5  # Unreal's real binary; the root stub is fine too
    return score + math.log2(max(size, 1)) / 2


def find_executables(root, hint=""):
    """All .exe files under `root`, most likely game binary first."""
    found = []
    for dirpath, _, files in os.walk(root):
        for f in files:
            if f.lower().endswith(".exe"):
                p = os.path.join(dirpath, f)
                try:
                    size = os.path.getsize(p)
                except OSError:
                    size = 0
                found.append((_score(p, root, hint, size), p))
    found.sort(key=lambda item: -item[0])
    return [p for _, p in found]


def _unique_dir(parent, name):
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(" .") or "Game"
    target, n = os.path.join(parent, safe), 2
    while os.path.exists(target):
        target, n = os.path.join(parent, f"{safe} ({n})"), n + 1
    return target


def _extract_zip(path, staging, total, progress):
    root_real = os.path.realpath(staging)
    done = 0
    with zipfile.ZipFile(path) as z:
        for member in z.infolist():
            out = os.path.realpath(os.path.join(staging, member.filename))
            if out != root_real and not out.startswith(root_real + os.sep):
                raise _unsafe_error(member.filename)
            if member.is_dir():
                os.makedirs(out, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with z.open(member) as src, open(out, "wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
                    done += len(chunk)
                    if progress and total:
                        progress(min(done / total, 1.0))


def _dir_size(path):
    size = 0
    for dirpath, _, files in os.walk(path):
        for f in files:
            try:
                size += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return size


def _extract_rar(path, staging, total, progress):
    backend = rar_backend()
    if backend == "unrar":
        # x: keep paths · -p-: never prompt · -o+: overwrite · -ol-: skip symlinks
        argv = ["unrar", "x", "-p-", "-o+", "-y", "-ol-", "-idq", path, staging + os.sep]
    else:
        argv = ["bsdtar", "-xf", path, "-C", staging]
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
        while proc.poll() is None:  # the tools report no usable progress; watch the disk
            time.sleep(0.4)
            if progress and total:
                progress(min(_dir_size(staging) / total, 0.99))
        err.seek(0)
        output = err.read().decode(errors="replace")
    if backend == "unrar":
        if proc.returncode not in (0, 1):  # unrar: 1 = non-fatal warning
            if proc.returncode == 11 or re.search(r"password|encrypt", output, re.I):
                raise _password_error(path)
            raise _read_error(path, output)
    elif proc.returncode:  # bsdtar: any non-zero exit is a failure
        raise _bsdtar_error(path, output)
    if progress:
        progress(1.0)


def _drop_escaping_links(root):
    """Delete symlinks that point outside `root` — a game never needs them,
    and they'd let later writes land anywhere on disk."""
    root_real = os.path.realpath(root)
    for dirpath, dirnames, files in os.walk(root):
        for f in files + dirnames:
            p = os.path.join(dirpath, f)
            if os.path.islink(p):
                target = os.path.realpath(p)
                if target != root_real and not target.startswith(root_real + os.sep):
                    os.unlink(p)


def extract(path, dest, progress=None):
    """Unpack a .zip or .rar into the existing folder `dest`, with the same
    safety checks as install()."""
    info = inspect(path)
    if path.lower().endswith(".rar"):
        _extract_rar(path, dest, info.size, progress)
    else:
        _extract_zip(path, dest, info.size, progress)
    _drop_escaping_links(dest)
    return info


def install(path, dest_parent, name, progress=None):
    """Extract the archive at `path` into a new folder under `dest_parent`.
    Returns (game_dir, exe, vr_apis). Runs on a worker thread;
    `progress(fraction)` is called as files are written."""
    info = inspect(path)
    os.makedirs(dest_parent, exist_ok=True)
    free = shutil.disk_usage(dest_parent).free
    if info.size > free:
        raise InstallError(t(f"Not enough space: needs {human(info.size)}, {human(free)} free",
                             f"Your disk is full-ish: needs {human(info.size)}, only {human(free)} free"))
    target = _unique_dir(dest_parent, name)
    staging = target + ".partial"
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    try:
        if path.lower().endswith(".rar"):
            _extract_rar(path, staging, info.size, progress)
        else:
            _extract_zip(path, staging, info.size, progress)
        _drop_escaping_links(staging)
        # Most game archives wrap everything in one top-level folder; unwrap it.
        entries = os.listdir(staging)
        inner = os.path.join(staging, entries[0]) if len(entries) == 1 else None
        if inner and os.path.isdir(inner) and not os.path.islink(inner):
            os.rename(inner, target)
            os.rmdir(staging)
        else:
            os.rename(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    exes = find_executables(target, name)
    if not exes:
        shutil.rmtree(target, ignore_errors=True)
        archive = os.path.basename(path)
        raise InstallError(t(f"{archive} contains no Windows .exe",
                             f"{archive} has no Windows .exe inside. What is this?"))
    apis, _ = steam.detect_vr(target)
    return target, exes[0], apis


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
