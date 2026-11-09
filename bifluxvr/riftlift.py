import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import sys
import threading

from . import net
from .util import APP_DATA_DIR, CONFIG_HOME, DATA_HOME
from .voice import t

VERSION = "1.0.1.1"
ARCHIVE = f"RiftLift-{VERSION}-linux-x86_64.tar.gz"
URL = f"https://github.com/Villagers654/RiftLift/releases/download/v{VERSION}/{ARCHIVE}"
SHA256 = "1fb8886759ef300f4a1750b6c72d6906d3354f4848d0690cbe21dc21a2f027c0"
HOME_PAGE = "https://github.com/Villagers654/RiftLift"
ROOT = os.path.join(APP_DATA_DIR, "tools", "riftlift")
CLI = os.path.join(ROOT, VERSION, "riftlift-cli")
MAX_ACCOUNTS = 4
SCHEME = "x-scheme-handler/oculus"
LINK_ENTRY = "io.github.intramy.BifluxVR.MetaSignIn.desktop"
LINK_INBOX = os.path.join(APP_DATA_DIR, "meta-sign-in-link")
LINK_PREVIOUS = os.path.join(APP_DATA_DIR, "meta-sign-in-link.previous")
_PCT = re.compile(r"\s(\d{1,3})%\s")
_ADDRESS = re.compile(r"oculus://[^\s\"'“”‘’<>]+", re.I)


class RiftLiftError(Exception):
    pass


def installed():
    return os.access(CLI, os.X_OK)


def _safe_extract(archive, dest):
    with tarfile.open(archive) as tar:
        for m in tar.getmembers():
            parts = m.name.split("/")[1:]
            if not parts or not parts[0]:
                continue
            rel = os.path.normpath(os.path.join(*parts))
            if rel.startswith("..") or os.path.isabs(rel) or not (m.isfile() or m.isdir() or m.issym()):
                raise RiftLiftError(t(f"Unexpected file in the RiftLift download: {m.name}",
                                      f"The RiftLift download has a weird file in it: {m.name}"))
            if m.issym() and (os.path.isabs(m.linkname) or
                              os.path.normpath(os.path.join(os.path.dirname(rel), m.linkname)).startswith("..")):
                raise RiftLiftError(t("The RiftLift download links outside its folder",
                                      "The RiftLift download points outside its own folder"))
            m.name = rel
            tar.extract(m, dest, filter="tar")


def install(progress=None):
    staging = os.path.join(ROOT, f".{VERSION}.staging")
    archive = os.path.join(ROOT, ARCHIVE)
    shutil.rmtree(staging, ignore_errors=True)
    try:
        net.download(URL, archive, progress, timeout=120)
        digest = hashlib.sha256()
        with open(archive, "rb") as f:
            while chunk := f.read(1 << 20):
                digest.update(chunk)
        if digest.hexdigest() != SHA256:
            raise RiftLiftError(t("The RiftLift download didn't match its checksum, so it wasn't installed",
                                  "The RiftLift download failed its checksum. Not touching it"))
        _safe_extract(archive, staging)
        if not os.path.isfile(os.path.join(staging, "riftlift-cli")):
            raise RiftLiftError(t("The RiftLift download has no riftlift-cli", "No riftlift-cli in the download"))
        target = os.path.join(ROOT, VERSION)
        shutil.rmtree(target, ignore_errors=True)
        os.replace(staging, target)
        for old in os.listdir(ROOT):
            if old not in (VERSION, ARCHIVE) and not old.startswith("."):
                shutil.rmtree(os.path.join(ROOT, old), ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if os.path.exists(archive):
            os.unlink(archive)
    return CLI


def _run(*args, timeout=120, json_out=True):
    if not installed():
        raise RiftLiftError(t("RiftLift isn't installed", "RiftLift isn't installed yet"))
    argv = [CLI] + (["--json"] if json_out else []) + [str(a) for a in args]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RiftLiftError(t("RiftLift took too long to answer", "RiftLift went quiet on us"))
    if r.returncode != 0:
        lines = [ln for ln in (r.stderr or r.stdout).strip().splitlines() if ln.strip()]
        raise RiftLiftError(lines[-1] if lines else f"riftlift-cli exited with {r.returncode}")
    if not json_out:
        return r.stdout
    try:
        return json.loads(r.stdout) if r.stdout.strip() else None
    except ValueError:
        raise RiftLiftError(t("RiftLift answered with something unreadable", "RiftLift said something garbled"))


def status():
    return _run("status")


def library():
    return _run("library") or []


def accounts():
    return _run("accounts") or []


def builds(game):
    return _run("builds", game) or []


def account_action(action, key):
    _run("accounts", action, key, *(["--yes"] if action == "remove" else []), json_out=False)


def uninstall(game):
    _run("uninstall", game, "--yes", json_out=False)


def add_exe(path, name=""):
    _run("add", path, *(["--name", name] if name else []), json_out=False)


def add_link(link):
    _run("add", link, json_out=False, timeout=60)


def add_steam():
    _run("add", "--steam", "--all", json_out=False, timeout=60)


def logs(game, lines=200):
    return _run("logs", game, "--lines", lines, json_out=False)


def report(game):
    return _run("report", game, json_out=False, timeout=60)


def play_argv(game):
    return [CLI, "play", str(game)]


class Job:
    def __init__(self):
        self.proc = None
        self.cancelled = False

    def cancel(self, game):
        self.cancelled = True
        try:
            _run("downloads", "cancel", game, json_out=False, timeout=20)
        except RiftLiftError:
            pass
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def download(game, folder=None, build=None, progress=None, job=None):
    argv = [CLI, "install", str(game)]
    if folder:
        argv += ["--to", folder]
    if build:
        argv += ["--build", build]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, bufsize=1)
    if job:
        job.proc = proc
    tail = []
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            tail = (tail + [line])[-5:]
        m = _PCT.search(f" {line} ")
        if m and progress:
            progress(min(int(m.group(1)), 100) / 100)
    code = proc.wait()
    if job and job.cancelled:
        raise RiftLiftError("cancelled")
    if code != 0:
        raise RiftLiftError(tail[-1] if tail else f"riftlift-cli exited with {code}")


def address_in(text):
    m = _ADDRESS.search(text or "")
    return m.group(0) if m else ""


def _mime(*args):
    try:
        r = subprocess.run(["xdg-mime", *args], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _exec_arg(arg):
    out = "".join("\\\\" + c if c in '"`$\\' else "%%" if c == "%" else c for c in arg)
    return f'"{out}"'


def take_link():
    if os.environ.get("FLATPAK_ID"):
        return False
    before = _mime("query", "default", SCHEME)
    if before is None:
        return False
    try:
        os.unlink(LINK_INBOX)
    except OSError:
        pass
    if before != LINK_ENTRY or not os.path.exists(LINK_PREVIOUS):
        os.makedirs(APP_DATA_DIR, exist_ok=True)
        with open(LINK_PREVIOUS, "w") as f:
            f.write("" if before == LINK_ENTRY else before)
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oculus_link.py")
    apps = os.path.join(DATA_HOME, "applications")
    os.makedirs(apps, exist_ok=True)
    with open(os.path.join(apps, LINK_ENTRY), "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=BifluxVR Meta sign-in\nNoDisplay=true\n"
                f"Exec={_exec_arg(sys.executable)} -I {_exec_arg(helper)} {_exec_arg(LINK_INBOX)} %u\n"
                f"MimeType={SCHEME};\n")
    return _mime("default", LINK_ENTRY, SCHEME) is not None


def take_address():
    try:
        with open(LINK_INBOX) as f:
            address = f.read().strip()
        os.unlink(LINK_INBOX)
        return address
    except OSError:
        return ""


def give_back_link():
    try:
        with open(LINK_PREVIOUS) as f:
            before = f.read().strip()
    except OSError:
        return
    if before:
        _mime("default", before, SCHEME)
    else:
        lists = os.path.join(CONFIG_HOME, "mimeapps.list")
        try:
            with open(lists) as f:
                lines = f.read().splitlines()
            kept = [ln for ln in lines if ln.rstrip(";") != f"{SCHEME}={LINK_ENTRY}"]
            if kept != lines:
                with open(lists + ".tmp", "w") as f:
                    f.write("\n".join(kept) + "\n")
                os.replace(lists + ".tmp", lists)
        except OSError:
            pass
    for path in (os.path.join(DATA_HOME, "applications", LINK_ENTRY), LINK_PREVIOUS, LINK_INBOX):
        try:
            os.unlink(path)
        except OSError:
            pass


class SignIn:
    def __init__(self):
        self.proc = subprocess.Popen([CLI, "accounts", "sign-in", "--manual"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.url = None
        self.lines = []
        self.ready = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                self.lines.append(line)
            if line.startswith("https://") and not self.url:
                self.url = line
                self.ready.set()
        self.ready.set()

    def link(self, timeout=60):
        self.ready.wait(timeout)
        if not self.url:
            self.cancel()
            raise RiftLiftError(self.lines[-1] if self.lines else t("Meta didn't give a sign-in link",
                                                                     "Meta didn't hand over a sign-in link"))
        return self.url

    def finish(self, address, timeout=120):
        seen = len(self.lines)
        try:
            self.proc.stdin.write((address_in(address) or address.strip()) + "\n")
            self.proc.stdin.close()
        except (BrokenPipeError, ValueError):
            pass
        try:
            code = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.cancel()
            raise RiftLiftError(t("Sign-in timed out", "Sign-in took forever, gave up"))
        self.reader.join(2)
        if code != 0:
            said = [ln for ln in self.lines[seen:] if ln != "Sign-in did not finish."]
            raise RiftLiftError(said[0] if said else self.lines[-1] if self.lines else
                                t("Sign-in didn't finish", "Sign-in didn't finish"))

    def cancel(self):
        if self.proc.poll() is None:
            self.proc.terminate()
