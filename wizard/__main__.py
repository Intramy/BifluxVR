# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
import platform
import sys


def unsupported(name):
    msg = f"Unsupported OS: BifluxVR runs on Linux only, and this is {name}. Nothing was installed."
    if name == "Windows":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, "BifluxVR Setup", 0x10)
            return 1
        except (ImportError, AttributeError, OSError):
            pass
    print(msg, file=sys.stderr)
    return 1


# Checked before anything Linux-only (GTK, /proc) is imported.
if platform.system() in ("Windows", "Darwin") or platform.system().startswith(("CYGWIN", "MSYS", "MINGW")):
    sys.exit(unsupported({"Darwin": "macOS"}.get(platform.system(), "Windows")))

from .app import main  # noqa: E402

sys.exit(main())
