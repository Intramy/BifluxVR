# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
"""Small widget helpers shared by every page."""

import concurrent.futures
import threading
import time
import traceback

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from . import health  # noqa: E402
from .voice import t  # noqa: E402

_images = concurrent.futures.ThreadPoolExecutor(max_workers=3)

def run_async(fn, done=None, error=None):
    """Run `fn` on a worker thread and deliver the result on the main loop."""
    def deliver(cb, value):
        cb(value)
        return False

    def work():
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001 — surfaced to the user
            traceback.print_exc()
            if error:
                GLib.idle_add(deliver, error, e)
            return
        if done:
            GLib.idle_add(deliver, done, result)

    threading.Thread(target=work, daemon=True).start()


def label(text="", *classes, xalign=0.0, wrap=False, ellipsize=False, lines=0):
    w = Gtk.Label(label=text, xalign=xalign)
    for c in classes:
        w.add_css_class(c)
    if wrap:
        w.set_wrap(True)
        w.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if ellipsize:
        w.set_ellipsize(Pango.EllipsizeMode.END)
    if lines:
        w.set_lines(lines)
    return w


def dot(state):
    w = Gtk.Box(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
    w.add_css_class("dot")
    w.add_css_class(state)
    return w


def button(text, *classes, icon=None, cb=None, tooltip=None):
    b = Gtk.Button()
    if icon and text:
        b.set_child(Adw.ButtonContent(label=text, icon_name=icon))
    elif icon:
        b.set_icon_name(icon)
    else:
        b.set_label(text)
    for c in classes:
        b.add_css_class(c)
    if tooltip:
        b.set_tooltip_text(tooltip)
    if cb:
        b.connect("clicked", lambda *_: cb())
    return b


def clear(box):
    while child := box.get_first_child():
        box.remove(child)


def load_texture(path, widget):
    """Decode an image off the main thread, then show it in a Gtk.Picture."""
    def work():
        try:
            tex = Gdk.Texture.new_from_filename(path)
        except GLib.Error:
            return
        GLib.idle_add(lambda: (widget.set_paintable(tex), False)[1])
    _images.submit(work)


def scrolled(child, clamp=1080, follow_focus=True):
    c = Adw.Clamp(maximum_size=clamp, tightening_threshold=clamp - 200, child=child)
    view = Gtk.Viewport(child=c, scroll_to_focus=follow_focus)
    return Gtk.ScrolledWindow(child=view, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)


STATE_ICONS = {
    health.OK: "object-select-symbolic",
    health.WARN: "dialog-warning-symbolic",
    health.FAIL: "dialog-error-symbolic",
    health.INFO: "dialog-information-symbolic",
}


class DynamicPrefs(Adw.PreferencesPage):
    """A PreferencesPage whose groups are rebuilt from a State snapshot."""

    TITLE = ("", "")  # (serious, funny)
    SUBTITLE = ("", "")

    def __init__(self, win, **kw):
        super().__init__(**kw)
        self.win = win
        self.groups = []
        self.updating = False
        head = Adw.PreferencesGroup()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(label(t(*self.TITLE), "large-title"))
        box.append(label(t(*self.SUBTITLE), "page-subtitle", wrap=True))
        head.add(box)
        self.add(head)

    def reset(self):
        for g in self.groups:
            self.remove(g)
        self.groups = []

    def group(self, **kw):
        g = Adw.PreferencesGroup(**kw)
        self.add(g)
        self.groups.append(g)
        return g


def duration(seconds):
    minutes = int(seconds) // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes and hours < 10 else f"{hours} h"


def ago(stamp):
    days = int((time.time() - stamp) // 86400)
    if days <= 0:
        return t("today", "today")
    if days == 1:
        return t("yesterday", "yesterday")
    if days < 30:
        return t(f"{days} days ago", f"{days} days ago")
    return time.strftime("%-d %b %Y", time.localtime(stamp))
