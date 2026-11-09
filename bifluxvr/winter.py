import math
import random

from gi.repository import Adw, Gtk

SKINS = ("classic", "winter")


class Snowfall(Gtk.DrawingArea):
    def __init__(self, win, count=90):
        super().__init__(can_target=False, can_focus=False, hexpand=True, vexpand=True)
        self.win = win
        self.count = count
        self.flakes = []
        self.tick = None
        self.last = 0
        self.set_draw_func(self._draw)
        win.connect("notify::is-active", lambda *_: self._sync())
        self.set_visible(False)

    def set_running(self, on):
        self.set_visible(on)
        self._sync()

    def _sync(self):
        want = self.get_visible() and self.win.is_active()
        if want and self.tick is None:
            self.last = 0
            self.tick = self.add_tick_callback(self._step)
        elif not want and self.tick is not None:
            self.remove_tick_callback(self.tick)
            self.tick = None

    def _spawn(self, w, h, top=False):
        size = random.uniform(1.2, 3.6)
        return [random.uniform(0, w), -size if top else random.uniform(0, h), size,
                random.uniform(18, 46) * (0.6 + size / 4), random.uniform(0, math.tau), random.uniform(0.35, 0.9)]

    def _step(self, _widget, clock):
        now = clock.get_frame_time() / 1e6
        dt = min(now - self.last, 0.05) if self.last else 0
        self.last = now
        w, h = self.get_width(), self.get_height()
        if not w or not h:
            return True
        if len(self.flakes) != self.count:
            self.flakes = [self._spawn(w, h) for _ in range(self.count)]
        for f in self.flakes:
            f[4] += dt * 1.3
            f[1] += f[3] * dt
            f[0] += math.sin(f[4]) * 12 * dt
            if f[1] > h + 4:
                f[:] = self._spawn(w, h, top=True)
        self.queue_draw()
        return True

    def _draw(self, _area, cr, _w, _h):
        for x, y, size, _speed, _phase, alpha in self.flakes:
            cr.set_source_rgba(1, 1, 1, alpha)
            cr.arc(x, y, size, 0, math.tau)
            cr.fill()


def _dark(win):
    if Adw.StyleManager.get_default().get_dark():
        win.add_css_class("is-dark")
    else:
        win.remove_css_class("is-dark")


def apply(win, skin, gold=False):
    if not getattr(win, "_dark_hooked", False):
        win._dark_hooked = True
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: _dark(win))
    _dark(win)
    for s in SKINS + ("gold",):
        win.remove_css_class(f"skin-{s}")
    win.add_css_class(f"skin-{skin}")
    if gold:
        win.add_css_class("skin-gold")
    win.snow.set_running(skin == "winter")
