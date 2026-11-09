#!/bin/sh
set -eu
SRC=$(cd "$(dirname "$0")" && pwd)
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
BIN=$HOME/.local/bin
APP=$DATA/bifluxvr/app
CONF=${XDG_CONFIG_HOME:-$HOME/.config}/bifluxvr
LOCKS="$DATA/bifluxvr/.nicetry $CONF/.nicetry"

locked() {
    for lock in $LOCKS; do
        [ -e "$lock" ] && return 0
    done
    return 1
}

case "$(uname -s)" in
    Linux) ;;
    *) echo "Unsupported OS: BifluxVR runs on Linux only. Nothing was installed." >&2; exit 1 ;;
esac
if grep -qi microsoft /proc/version 2>/dev/null; then
    echo "Unsupported OS: this is Windows (WSL). BifluxVR runs on Linux only. Nothing was installed." >&2
    exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
    rm -rf "$APP" "$BIN/bifluxvr" \
        "$DATA/applications/io.github.intramy.BifluxVR.desktop" \
        "$DATA/icons/hicolor/scalable/apps/io.github.intramy.BifluxVR.svg" \
        "$DATA/applications/io.github.bifluxvr.BifluxVR.desktop" \
        "$DATA/icons/hicolor/scalable/apps/io.github.bifluxvr.BifluxVR.svg"
    for lock in $LOCKS; do
        rm -f "$lock"
    done
    echo "BifluxVR removed. Settings in ~/.config/bifluxvr were kept."
    exit 0
fi

have_toolkit() {
    python3 - <<'PY' 2>/dev/null
import sys
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw
sys.exit((Adw.get_major_version(), Adw.get_minor_version()) < (1, 7))
PY
}

install_toolkit() {
    . /etc/os-release 2>/dev/null || true
    for id in ${ID:-} ${ID_LIKE:-}; do
        case $id in
            arch*) set -- pacman -S --needed --noconfirm python-gobject gtk4 libadwaita ;;
            fedora*|rhel*|centos*) set -- dnf install -y python3-gobject gtk4 libadwaita ;;
            debian*|ubuntu*) set -- apt-get install -y python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 ;;
            opensuse*|suse*) set -- zypper --non-interactive install python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 ;;
            gentoo*) set -- emerge --noreplace dev-python/pygobject gui-libs/gtk gui-libs/libadwaita ;;
            *) continue ;;
        esac
        echo "The installer needs GTK 4, libadwaita >= 1.7 and PyGObject. Install them with:"
        echo "    sudo $*"
        printf "Run that now? [Y/n] "
        read -r answer || answer=n
        case $answer in [nN]*) return 1 ;; esac
        sudo "$@"
        return
    done
    echo "Install PyGObject, GTK 4 and libadwaita >= 1.7 with your package manager, then run this again."
    return 1
}

if locked; then
    if [ "${1:-}" != "--files-only" ] && [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ] && have_toolkit; then
        cd "$SRC" && exec python3 -m wizard
    fi
    printf '\033[1;97;41m  Nice try!  \033[0m\n' >&2
    echo "This BifluxVR was tampered with. To use it again, remove it completely first: sh install.sh --uninstall" >&2
    exit 1
fi

if [ "${1:-}" != "--files-only" ]; then
    if [ -z "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]; then
        echo "No graphical session found, so only BifluxVR itself is installed."
    elif have_toolkit || { [ -t 0 ] && install_toolkit && have_toolkit; }; then
        cd "$SRC" && exec python3 -m wizard
    else
        echo "Missing dependency: PyGObject, GTK 4 or libadwaita >= 1.7." >&2
        exit 1
    fi
fi

if ! have_toolkit; then
    echo "Missing dependency: PyGObject, GTK 4 or libadwaita >= 1.7." >&2
    exit 1
fi

mkdir -p "$APP" "$BIN" "$DATA/applications" "$DATA/icons/hicolor/scalable/apps"
rm -rf "$APP/bifluxvr"
cp -r "$SRC/bifluxvr" "$APP/"
find "$APP" -name __pycache__ -prune -exec rm -rf {} +
rm -f "$APP/bifluxvr/.manifest" "$APP/bifluxvr/.manifest.tmp"
(cd "$APP/bifluxvr" && find . -name '*.py' | LC_ALL=C sort | sed 's|^\./||' | while IFS= read -r f; do
    sha256sum "$f"
done) > "$APP/bifluxvr/.manifest.tmp"
mv "$APP/bifluxvr/.manifest.tmp" "$APP/bifluxvr/.manifest"
cat > "$BIN/bifluxvr" <<SH
#!/bin/sh
exec python3 -c 'import sys; sys.path.insert(0, "$APP"); from bifluxvr.app import main; sys.exit(main())' "\$@"
SH
chmod +x "$BIN/bifluxvr"
rm -f "$DATA/applications/io.github.bifluxvr.BifluxVR.desktop" "$DATA/icons/hicolor/scalable/apps/io.github.bifluxvr.BifluxVR.svg"
cp "$SRC/data/io.github.intramy.BifluxVR.desktop" "$DATA/applications/"
cp "$SRC/data/io.github.intramy.BifluxVR.svg" "$DATA/icons/hicolor/scalable/apps/"
command -v update-desktop-database >/dev/null && update-desktop-database "$DATA/applications" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$DATA/icons/hicolor" || true
echo "BifluxVR installed. Launch it from your app menu or run: bifluxvr"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "Note: $BIN is not on your PATH." ;; esac
