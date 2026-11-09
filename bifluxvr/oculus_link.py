import os
import sys


def main(argv):
    if len(argv) != 3:
        return 2
    inbox, address = argv[1], argv[2]
    if not address.lower().startswith(("oculus://", "oculus-client://")) or len(address) > 3 * 1024 * 1024:
        return 2
    tmp = f"{inbox}.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(address)
    os.replace(tmp, inbox)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
