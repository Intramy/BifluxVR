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


if platform.system() in ("Windows", "Darwin") or platform.system().startswith(("CYGWIN", "MSYS", "MINGW")):
    sys.exit(unsupported({"Darwin": "macOS"}.get(platform.system(), "Windows")))

from .app import main

sys.exit(main())
