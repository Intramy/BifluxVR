import os
import shutil
from dataclasses import dataclass

from . import mods, uninstall
from .util import HOME

_SKIP_FS = {"vfat", "msdos", "exfat", "ntfs", "ntfs3", "fuseblk", "iso9660", "udf", "squashfs", "overlay"}
_SYSTEM = ("/boot", "/efi", "/proc", "/sys", "/dev", "/run", "/var", "/usr", "/tmp", "/opt", "/etc", "/srv",
           "/nix", "/snap", "/gnu")
_SUBDIRS = {"install_dir_vr": "VR", "install_dir_simple": "Simple", "install_dir_setup": "Installed"}
_CHUNK = 8 << 20
_active = set()


class MoveError(Exception):
    pass


@dataclass
class Drive:
    mount: str
    dev: int
    rotational: bool
    model: str
    base: str
    disk: str = ""
    part: str = ""
    size: int = 0
    label: str = ""

    @property
    def free(self):
        try:
            return shutil.disk_usage(self.mount).free
        except OSError:
            return 0

    def folder(self, key):
        return os.path.join(self.base, _SUBDIRS[key])


def _unescape(path):
    return path.replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")


def _disk_info(source):
    name = os.path.basename(os.path.realpath(source))
    node = os.path.realpath(os.path.join("/sys/class/block", name))
    disk = os.path.dirname(node) if os.path.exists(os.path.join(node, "partition")) else node

    def read(*parts):
        try:
            with open(os.path.join(disk, *parts)) as f:
                return f.read().strip()
        except OSError:
            return ""
    size = read("size")
    return (read("queue", "rotational") == "1", read("device", "model"), os.path.basename(disk), name,
            int(size) * 512 if size.isdigit() else 0)


def _allowed(mount):
    if mount == "/" or HOME == mount or HOME.startswith(mount + os.sep):
        return True
    if mount.startswith(("/run/media/", "/media/", "/mnt/")) or mount in ("/media", "/mnt"):
        return True
    return not any(mount == s or mount.startswith(s + os.sep) for s in _SYSTEM)


def drives():
    try:
        with open("/proc/self/mountinfo") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    home_dev = os.stat(HOME).st_dev
    seen, out = set(), []
    for line in lines:
        head, _, tail = line.partition(" - ")
        fields, rest = head.split(), tail.split()
        if len(fields) < 5 or len(rest) < 2:
            continue
        mount, fstype, source = _unescape(fields[4]), rest[0], rest[1]
        if fstype in _SKIP_FS or not source.startswith("/dev/") or not _allowed(mount):
            continue
        try:
            dev = os.stat(mount).st_dev
        except OSError:
            continue
        if dev in seen:
            continue
        if dev == home_dev:
            base = os.path.join(HOME, "Games", "BifluxVR")
        else:
            base = os.path.join(mount, "Games", "BifluxVR")
            probe = next((p for p in (base, os.path.dirname(base), mount) if os.path.isdir(p)), mount)
            if not os.access(probe, os.W_OK):
                continue
        seen.add(dev)
        rotational, model, disk, part, size = _disk_info(source)
        out.append(Drive(mount, dev, rotational, model, base, disk, part, size))
    root_dev = os.stat("/").st_dev
    out.sort(key=lambda d: (d.dev != root_dev, d.dev != home_dev, d.rotational, -d.size, d.mount))
    kinds = [("HDD" if d.rotational else "SSD") for d in out]
    disks = [d.disk for d in out]
    numbers, counts = {}, {}
    for d, kind in zip(out, kinds):
        if kinds.count(kind) == 1:
            d.label = kind
            continue
        if d.disk not in numbers:
            counts[kind] = numbers[d.disk] = counts.get(kind, 0) + 1
        d.label = f"{kind}{numbers[d.disk]} ({d.part if disks.count(d.disk) > 1 else d.disk})"
    return out


def drive_of(path, among=None):
    among = drives() if among is None else among
    p = os.path.abspath(path)
    while not os.path.exists(p) and p != os.sep:
        p = os.path.dirname(p)
    try:
        dev = os.stat(p).st_dev
    except OSError:
        return None
    return next((d for d in among if d.dev == dev), None)


def size_of(path):
    total = 0
    for dirpath, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000:
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def _entry(game, cfg):
    gid = game.appid.split(":", 1)[1]
    return next((c for c in cfg["custom_games"] if c["id"] == gid), None)


def game_root(game, cfg):
    entry = _entry(game, cfg) if game.custom else None
    if not entry or entry.get("engine"):
        return ""
    root = uninstall.game_root(entry, cfg)
    return root if uninstall.removable(root, cfg, game) else ""


def _key_for(root, entry, cfg):
    parent = os.path.realpath(os.path.dirname(root))
    for key in _SUBDIRS:
        if cfg.get(key) and os.path.realpath(cfg[key]) == parent:
            return key
    if entry.get("prefix", "").startswith(root + os.sep):
        return "install_dir_setup"
    return "install_dir_simple" if entry.get("mode") == "simple" else "install_dir_vr"


def target_dir(drive, key, cfg, among):
    current = cfg.get(key)
    if current and drive_of(current, among) == drive:
        return current
    return drive.folder(key)


def _copy_tree(src, dst, total, progress, cancelled):
    done = [0]

    def tick(n):
        done[0] += n
        if progress and total:
            progress(min(done[0] / total, 1.0))
        if cancelled and cancelled():
            raise MoveError("cancelled")

    os.makedirs(dst)
    for dirpath, dirs, files in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        out = os.path.normpath(os.path.join(dst, rel))
        for d in list(dirs):
            s, o = os.path.join(dirpath, d), os.path.join(out, d)
            if os.path.islink(s):
                os.symlink(os.readlink(s), o)
                dirs.remove(d)
            else:
                os.makedirs(o)
        for f in files:
            s, o = os.path.join(dirpath, f), os.path.join(out, f)
            if os.path.islink(s):
                os.symlink(os.readlink(s), o)
                continue
            with open(s, "rb") as a, open(o, "wb") as b:
                while chunk := a.read(_CHUNK):
                    b.write(chunk)
                    tick(len(chunk))
            shutil.copystat(s, o)
    for dirpath, dirs, _ in os.walk(src, topdown=False):
        for d in dirs:
            s = os.path.join(dirpath, d)
            if not os.path.islink(s):
                shutil.copystat(s, os.path.join(dst, os.path.relpath(s, src)))
    shutil.copystat(src, dst)


def _swap(path, old, new):
    if path == old or path.startswith(old + os.sep):
        return new + path[len(old):]
    return path


def move(game, cfg, drive, progress=None, cancelled=None):
    if game.appid in _active:
        raise MoveError("it's already being moved")
    _active.add(game.appid)
    try:
        return _move(game, cfg, drive, progress, cancelled)
    finally:
        _active.discard(game.appid)


def _move(game, cfg, drive, progress, cancelled):
    entry = _entry(game, cfg) if game.custom else None
    if not entry:
        raise MoveError("only games installed through BifluxVR can be moved")
    root = game_root(game, cfg)
    if not root or not os.path.isdir(root):
        raise MoveError("its folder is shared with other things, so it can't be moved on its own")
    among = drives()
    if drive_of(root, among) == drive:
        raise MoveError(f"it's already on the {drive.label}")
    parent = target_dir(drive, _key_for(root, entry, cfg), cfg, among)
    dest = os.path.join(parent, os.path.basename(root))
    if os.path.lexists(dest):
        raise MoveError(f"{dest} already exists")
    os.makedirs(parent, exist_ok=True)
    try:
        same = os.stat(parent).st_dev == os.stat(root).st_dev
    except OSError as e:
        raise MoveError(str(e))
    if same:
        os.rename(root, dest)
    else:
        total = size_of(root)
        if total + (512 << 20) > drive.free:
            raise MoveError(f"the {drive.label} needs {human(total)} free and has {human(drive.free)}")
        try:
            _copy_tree(root, dest, total, progress, cancelled)
        except BaseException:
            if os.path.isdir(dest):
                uninstall.wipe(dest)
            raise
        uninstall.wipe(root)
    for k in ("exe", "root", "prefix"):
        if entry.get(k):
            entry[k] = _swap(entry[k], root, dest)
    if not entry.get("root"):
        entry["root"] = dest
    for rec in cfg.get("meta_games", {}).values():
        if rec.get("custom_id") == entry["id"] and rec.get("dir"):
            rec["dir"] = _swap(rec["dir"], root, dest)
    state = mods.load_state(game)
    if state.get("mods"):
        for m in state["mods"]:
            if m.get("target"):
                m["target"] = _swap(m["target"], root, dest)
        mods.save_state(game, state)
    return root, dest
