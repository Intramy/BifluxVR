# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
"""The Headset and Stats pages."""

import time
from html import escape

from gi.repository import Adw, Gtk

from . import mods, optimize, perf, runtimes, sessions, wivrn
from .voice import t
from .widgets import ago, button, clear, dot, duration, label, run_async, scrolled

CODEC_NAMES = {"h264": "H.264", "h265": "H.265", "av1": "AV1", "raw": "Raw"}
ENCODER_NAMES = {"nvenc": "NVENC", "vaapi": "VA-API", "vulkan": "Vulkan Video", "x264": "x264 (CPU)"}


def grade(name):
    return {
        "excellent": (t("Excellent", "Chef's kiss"), "ok", "network-wireless-signal-excellent-symbolic"),
        "good": (t("Good", "Solid"), "ok", "network-wireless-signal-good-symbolic"),
        "fair": (t("Fair", "Meh"), "warn", "network-wireless-signal-ok-symbolic"),
        "poor": (t("Poor", "Rough"), "fail", "network-wireless-signal-weak-symbolic"),
    }[name]


def _pick(row, state, icon=None):
    """Swap the status dot (or icon) at the start of a row."""
    old = getattr(row, "_lead", None)
    if old:
        row.remove(old)
    lead = Gtk.Image(icon_name=icon) if icon else dot(state)
    if icon:
        lead.add_css_class({"ok": "success", "warn": "warning", "fail": "error"}.get(state, "dim-label"))
    row.add_prefix(lead)
    row._lead = lead


def _status_row(title):
    row = Adw.ActionRow(title=title, subtitle="…")
    row.set_subtitle_lines(3)
    _pick(row, "info")
    return row


class HeadsetPage(Adw.PreferencesPage):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.polling = False
        self.info = None
        self.rec = None
        self.results = []

        head = Adw.PreferencesGroup()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(label(t("Headset", "Face Computer"), "large-title"))
        box.append(label(t("Live status of your standalone headset and streaming settings tuned to this PC.",
                           "How your headset's doing, and how hard this PC can push pixels at it."),
                         "page-subtitle", wrap=True))
        head.add(box)
        self.add(head)

        g = Adw.PreferencesGroup(title=t("Status", "Vital signs"))
        self.conn = _status_row(t("Connection", "Connection"))
        self.server_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self.server_btn.connect("clicked", lambda *_: self._start_server())
        self.conn.add_suffix(self.server_btn)
        self.battery = _status_row(t("Battery", "Juice"))
        self.link = _status_row(t("Connection quality", "Wi-Fi vibes"))
        self.stream = _status_row(t("Stream", "The stream"))
        self.tracking = _status_row(t("Tracking", "Tracking"))
        for row in (self.conn, self.battery, self.link, self.stream, self.tracking):
            g.add(row)
        self.add(g)

        self.opt = Adw.PreferencesGroup(title=t("Auto-Optimize", "Auto-optimize"),
                                        description=t("Detected hardware and the settings that suit it.",
                                                      "I looked at your hardware. Here's what it can handle."))
        self.opt_rows = []
        self.add(self.opt)
        run_async(self._detect, self._show_optimize)

    # -- live status

    def poll(self):
        if self.polling:
            return
        self.polling = True

        def work():
            info = wivrn.status()
            batt = wivrn.battery() if info and info["connected"] else None
            addr = wivrn.headset_address() if info and info["connected"] else ""
            return info, batt, addr, wivrn.link_quality(addr) if addr else None

        def fail(e):
            self.polling = False
        run_async(work, self._show_status, fail)

    def _show_status(self, res):
        self.polling = False
        info, batt, addr, link = res
        self.info = info
        server = runtimes.server_for("wivrn")
        self.server_btn.set_visible(False)
        if not server or not server.installed:
            self.conn.set_subtitle(t("WiVRn isn't installed. This page is for Quest and Pico headsets.",
                                     "No WiVRn here. This page is for Quest/Pico folks."))
            _pick(self.conn, "info")
        elif not info:
            self.conn.set_subtitle(t("WiVRn isn't running", "WiVRn is asleep"))
            self.server_btn.set_label(t("Start", "Wake It Up"))
            self.server_btn.set_visible(True)
            _pick(self.conn, "warn")
        elif not info["connected"]:
            self.conn.set_subtitle(t("Waiting for the headset. Open WiVRn on it and connect.",
                                     "Waiting for your headset. Open WiVRn on it and say hi."))
            _pick(self.conn, "warn")
        else:
            name = info["name"] or t("Headset", "Your headset")
            self.conn.set_subtitle(escape(name + (t(" · in a VR session", " · in VR right now") if info["session"] else
                                                  t(" · connected", " · connected, chilling"))))
            _pick(self.conn, "ok")

        live = bool(info and info["connected"])
        if batt:
            pct, charging, source = batt
            state = "ok" if pct > 40 or charging else "warn" if pct > 15 else "fail"
            level = min(100, max(0, round(pct / 10) * 10))
            plug = ("-charged" if level == 100 else "-charging") if charging else ""
            icon = f"battery-level-{level}{plug}-symbolic"
            via = t(" (adb)", " (via adb)") if source == "adb" else ""
            self.battery.set_subtitle(f"{pct}%" + (t(" · charging", " · charging up") if charging else "") + via)
            _pick(self.battery, state, icon)
        else:
            self.battery.set_subtitle(
                t("Connect the headset over USB with developer mode and adb installed to see the battery.",
                  "Plug it in over USB with developer mode and adb, and I'll read the battery.")
                if live else t("Not connected", "No headset, no battery"))
            _pick(self.battery, "info", "battery-missing-symbolic")

        if link:
            text, state, icon = grade(link["grade"])
            self.link.set_subtitle(t(f"{text} · {link['avg']:.1f} ms ping, ±{link['jitter']:.1f} ms, "
                                     f"{link['loss']:.0f}% loss · {addr}",
                                     f"{text} · {link['avg']:.1f} ms ping, ±{link['jitter']:.1f} ms wobble, "
                                     f"{link['loss']:.0f}% lost · {addr}"))
            _pick(self.link, state, icon)
        else:
            self.link.set_subtitle(t("Measured once the headset connects", "I'll measure once your headset shows up"))
            _pick(self.link, "info", "network-wireless-offline-symbolic")

        if live:
            parts = []
            if info["bitrate"]:
                parts.append(f"{info['bitrate'] / 1e6:.0f} Mbit/s")
            if info["refresh"]:
                parts.append(f"{info['refresh']:.0f} Hz")
            if info["codecs"]:
                parts.append(t("decodes ", "speaks ") + ", ".join(CODEC_NAMES.get(c, c) for c in info["codecs"]))
            self.stream.set_subtitle(" · ".join(parts) or t("No details yet", "Nothing to report yet"))
            _pick(self.stream, "ok")
            feats = [n for k, n in (("hand_tracking", t("hands", "hands")), ("eye_tracking", t("eyes", "eyeballs")),
                                    ("face_tracking", t("face", "face"))) if info[k]]
            self.tracking.set_subtitle(t("Head and controllers", "Head and controllers") +
                                       (" · " + ", ".join(feats) if feats else ""))
            _pick(self.tracking, "ok")
        else:
            for row in (self.stream, self.tracking):
                row.set_subtitle(t("Not connected", "Nobody home"))
                _pick(row, "info")
        if self.rec is None or (live and self.rec.get("for") != info["name"]):
            run_async(self._detect, self._show_optimize)

    def _start_server(self):
        server = runtimes.server_for("wivrn")
        if server:
            self.win.toggle_server(server, True)

    # -- auto-optimize

    def _detect(self):
        gpu = optimize.gpu()
        available = optimize.wivrn_encoders()
        info = self.info if self.info and self.info.get("connected") else None
        rec = optimize.recommend(gpu, available, info)
        rec["for"] = info["name"] if info else None
        return gpu, available, rec, wivrn.configured_encoder()

    def _row(self, title, subtitle, icon=None):
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        row.set_subtitle_lines(4)
        if icon:
            row.add_prefix(Gtk.Image(icon_name=icon))
        self.opt.add(row)
        self.opt_rows.append(row)
        return row

    def _show_optimize(self, res):
        gpu, available, rec, (cur_enc, cur_codec) = res
        self.gpu, self.available, self.rec = gpu, available, rec
        self.configured = (cur_enc, cur_codec)
        for row in self.opt_rows:
            self.opt.remove(row)
        self.opt_rows = []
        vram = f" · {gpu['vram'] / 1024:.0f} GB" if gpu["vram"] else ""
        self._row(t("Graphics card", "Graphics card"), escape(gpu["name"] + vram), "video-display-symbolic")
        if not available:
            self._row(t("WiVRn", "WiVRn"), t("Not installed", "Not installed"), "dialog-information-symbolic")
            return
        built = ", ".join(ENCODER_NAMES[e] for e in available)
        now = f"{ENCODER_NAMES.get(cur_enc, cur_enc)} {CODEC_NAMES.get(cur_codec, cur_codec)}".strip() if cur_enc \
            else t("automatic", "automatic (WiVRn decides)")
        self._row(t("Encoders in this WiVRn", "Encoders WiVRn has"), t(f"{built} · configured: {now}",
                                                                      f"{built} · set to: {now}"),
                  "applications-multimedia-symbolic")

        enc = f"{ENCODER_NAMES[rec['encoder']]} · {CODEC_NAMES[rec['codec']]}"
        if rec["bit_depth"] == 10:
            enc += t(" · 10-bit", " · 10-bit")
        row = self._row(t("Recommended encoder", "Best encoder for you"), enc, "emblem-system-symbolic")
        if (rec["encoder"], rec["codec"]) == self.configured:
            row.add_suffix(self._in_use())
        else:
            row.add_suffix(self._suffix(t("Apply", "Use It"), "suggested-action",
                                        cb=lambda: self.apply_encoder(rec["encoder"], rec["codec"], rec["bit_depth"])))
        headset = rec["for"] or t("Quest 2-class headset", "a Quest 2-ish headset")
        self._row(t("Set on the headset", "Set these on the headset"),
                  t(f"Bitrate {rec['bitrate']} Mbit/s · resolution scale {rec['scale']:.0%} · {rec['refresh']} Hz "
                    f"(for {headset}). WiVRn keeps these in the headset app's settings.",
                    f"Bitrate {rec['bitrate']} Mbit/s · resolution {rec['scale']:.0%} · {rec['refresh']} Hz "
                    f"(for {headset}). These live in the WiVRn app on the headset."),
                  "preferences-other-symbolic")
        for note in rec["notes"]:
            self._row(t("Heads-up", "Heads-up"), escape(note), "dialog-warning-symbolic")

        test = self._row(t("Test stream", "Test stream"),
                         t("Encodes test video at your headset's resolution and checks it keeps up.",
                           "Pushes test video through the encoder at headset resolution to see if it keeps up."),
                         "media-playback-start-symbolic")
        self.test_btn = self._suffix(t("Run Test", "Go"), cb=self.run_test)
        test.add_suffix(self.test_btn)
        self.result_rows = []
        if self.results:
            self._show_results(self.results)

    def _suffix(self, text, *classes, cb):
        b = button(text, *classes, cb=cb)
        b.set_valign(Gtk.Align.CENTER)
        return b

    def _in_use(self):
        w = label(t("In use", "In use"), "dim-label")
        w.set_valign(Gtk.Align.CENTER)
        return w

    def apply_encoder(self, encoder, codec, depth):
        try:
            wivrn.write_encoder(encoder, codec, depth)
        except OSError as e:
            self.win.toast(t(f"Couldn't write the WiVRn config: {e}", f"WiVRn's config said no: {e}"))
            return
        server = runtimes.server_for("wivrn")
        msg = t(f"WiVRn will use {ENCODER_NAMES[encoder]} {CODEC_NAMES[codec]}",
                f"WiVRn now uses {ENCODER_NAMES[encoder]} {CODEC_NAMES[codec]}")
        if server and server.running:
            msg += t(" after a restart. Restart the server from the Runtime page.",
                     " once it restarts. Bounce it from the Engine Room.")
        self.win.toast(msg, timeout=6)
        run_async(self._detect, self._show_optimize)

    def run_test(self):
        self.test_btn.set_sensitive(False)
        self.test_btn.set_child(Adw.Spinner())
        w, h = optimize.eye_size(self.rec["for"])
        rec, available = self.rec, self.available
        w, h = int(w * rec["scale"]), int(h * rec["scale"])
        run_async(lambda: optimize.find_working(rec, available, w, h), self._show_results,
                  lambda e: self.win.toast(t(f"Test failed: {e}", f"The test fell over: {e}")))

    def _show_results(self, results):
        if results is not self.results:
            perf.save_stream_test(results)
        self.results = results
        self.test_btn.set_sensitive(True)
        self.test_btn.set_label(t("Run Again", "Again"))
        for row in getattr(self, "result_rows", []):
            if row in self.opt_rows:
                self.opt.remove(row)
                self.opt_rows.remove(row)
        self.result_rows = []
        for res in results:
            name = f"{ENCODER_NAMES[res['wivrn_encoder']]} · {CODEC_NAMES[res['codec']]}"
            if "error" in res:
                row = self._row(name, escape(res["error"]), None)
                _pick(row, "fail")
            else:
                verdict = t("keeps up", "keeps up, nice") if res["ok"] else t("too slow", "can't keep up")
                row = self._row(name, t(f"{res['fps']:.0f} fps at {res['size']} per eye; needs {res['needed']:.0f} "
                                        f"for both eyes · {verdict}",
                                        f"{res['fps']:.0f} fps at {res['size']} per eye, needs {res['needed']:.0f} "
                                        f"for both eyes · {verdict}"), None)
                _pick(row, "ok" if res["ok"] else "warn")
            self.result_rows.append(row)
            if (res["wivrn_encoder"], res["codec"]) == self.configured:
                row.add_suffix(self._in_use())
        best = next((i for i, r in enumerate(results) if r["ok"]), None)
        if best is not None and (results[best]["wivrn_encoder"], results[best]["codec"]) != self.configured:
            res = results[best]
            self.result_rows[best].add_suffix(self._suffix(
                t("Use This", "Use This One"), "suggested-action",
                cb=lambda: self.apply_encoder(res["wivrn_encoder"], res["codec"], 8)))
        elif not best and results:
            row = self._row(t("Nothing kept up", "Nothing kept up"),
                            t("Lower the resolution scale or refresh rate on the headset.",
                              "Turn down the resolution or refresh rate on the headset and try again."),
                            "dialog-warning-symbolic")
            self.result_rows.append(row)


# -------------------------------------------------------------------- stats

class DailyChart(Gtk.DrawingArea):
    """Minutes played per day as thin bars, newest on the right."""

    HEIGHT = 140

    def __init__(self):
        super().__init__(content_height=self.HEIGHT, hexpand=True, has_tooltip=True)
        self.values = []
        self.set_draw_func(self._draw)
        self.connect("query-tooltip", self._tooltip)

    def set_values(self, values):
        self.values = values
        self.queue_draw()

    def _slot(self, width):
        return width / max(len(self.values), 1)

    def _draw(self, _area, cr, width, height):
        if not self.values:
            return
        rgba = self.get_color()
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        top = max(max(self.values), 1)
        slot = self._slot(width)
        bar = min(slot - 2, 22)  # 2px gap between bars
        base = height - 1
        cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, 0.15)
        cr.rectangle(0, base, width, 1)
        cr.fill()
        cr.set_source_rgba(accent.red, accent.green, accent.blue, 1)
        for i, v in enumerate(self.values):
            if v <= 0:
                continue
            h = max(3, (height - 8) * v / top)
            x = i * slot + (slot - bar) / 2
            r = min(4, bar / 2, h)
            y = base - h
            cr.new_sub_path()
            cr.arc(x + r, y + r, r, 3.1416, 4.7124)
            cr.arc(x + bar - r, y + r, r, 4.7124, 6.2832)
            cr.line_to(x + bar, base)
            cr.line_to(x, base)
            cr.close_path()
            cr.fill()

    def _tooltip(self, _w, x, _y, _kb, tip):
        if not self.values:
            return False
        i = min(int(x // self._slot(self.get_width())), len(self.values) - 1)
        back = len(self.values) - 1 - i
        day = time.strftime("%a %-d %b", time.localtime(time.time() - back * 86400))
        tip.set_text(f"{day}: {duration(self.values[i] * 60)}")
        return True


class StatsPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                       margin_top=24, margin_bottom=36, margin_start=28, margin_end=28)
        body.append(label(t("Stats", "Nerd Stats"), "large-title"))
        body.append(label(t("Sessions started from BifluxVR. Steam games also keep their own play time in Steam.",
                            "Every session you launched from here, counted. Steam keeps its own tally too."),
                          "page-subtitle", wrap=True))
        self.tiles = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                 min_children_per_line=2, max_children_per_line=5,
                                 column_spacing=14, row_spacing=14, margin_top=10)
        body.append(self.tiles)

        self.chart_title = label(t("Minutes in VR, last 14 days", "Minutes in VR, last two weeks"), "section-title")
        body.append(self.chart_title)
        self.chart = DailyChart()
        self.chart_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.chart_frame.add_css_class("tile")
        self.chart_frame.append(self.chart)
        body.append(self.chart_frame)

        body.append(label(t("Most played", "Your favorites"), "section-title"))
        self.games = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.games.add_css_class("boxed-list")
        body.append(self.games)

        body.append(label(t("Recent sessions", "Recent sessions"), "section-title"))
        self.recent = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.recent.add_css_class("boxed-list")
        body.append(self.recent)
        self.append(scrolled(body))

    def _tile(self, value, caption):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("tile")
        box.append(label(value, "stat-value"))
        box.append(label(caption, "caption-text", wrap=True))
        child = Gtk.FlowBoxChild(child=box, focusable=False)
        child.add_css_class("tile-child")
        return child

    def refresh(self):
        run_async(sessions.summary, self._show)

    def _show(self, s):
        self.tiles.remove_all()
        for value, caption in (
            (duration(s["total"]), t("Total in VR", "Total face time")),
            (duration(s["week"]), t("This week", "This week")),
            (str(s["count"]), t("Sessions", "Sessions")),
            (duration(s["longest"]), t("Longest session", "Longest marathon")),
            (f"{s['drain']:.0f}%/h" if s["drain"] is not None else "–",
             t("Battery use per hour", "Battery drain per hour")),
        ):
            self.tiles.append(self._tile(value, caption))
        self.chart.set_values(s["daily"])
        for w in (self.chart_title, self.chart_frame):
            w.set_visible(any(s["daily"]))

        clear(self.games)
        top = s["games"][:8]
        most = max((g["seconds"] for g in top), default=1) or 1
        for g in top:
            row = Adw.ActionRow(title=escape(g["name"]),
                                subtitle=t(f"{g['sessions']} session{'s' if g['sessions'] != 1 else ''}",
                                           f"{g['sessions']} session{'s' if g['sessions'] != 1 else ''}"))
            bar = Gtk.LevelBar(min_value=0, max_value=most, value=g["seconds"], valign=Gtk.Align.CENTER)
            bar.set_size_request(140, -1)
            row.add_suffix(bar)
            row.add_suffix(label(duration(g["seconds"]), "caption-text"))
            self.games.append(row)
        if not top:
            self.games.append(Adw.ActionRow(title=t("Nothing yet", "Nothing yet"),
                                            subtitle=t("Play something from your library and it shows up here.",
                                                       "Go play something. I'll be counting.")))

        clear(self.recent)
        for r in s["recent"]:
            bits = [ago(r["start"]) + time.strftime(" %H:%M", time.localtime(r["start"])), duration(r["end"] - r["start"])]
            if r.get("headset"):
                bits.append(r["headset"])
            if r.get("bitrate"):
                bits.append(f"{r['bitrate'] / 1e6:.0f} Mbit/s")
            if r.get("battery_start") is not None and r.get("battery_end") is not None:
                bits.append(f"🔋 {r['battery_start']}% → {r['battery_end']}%")
            self.recent.append(Adw.ActionRow(title=escape(r["name"]), subtitle=escape(" · ".join(bits))))
        if not s["recent"]:
            self.recent.append(Adw.ActionRow(title=t("No sessions yet", "No sessions yet")))


def data_paths():
    return [(t("Session log", "Session log"), sessions.PATH),
            (t("Mod backups", "Mod backups"), mods.MODS_DIR)]
