# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
import os
import shutil
import sys
import threading
import uuid
from html import escape

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from . import (__version__, anticheat, audio, compat, config, donate, health, installer, launcher, mods, openvr,  # noqa: E402
               runtimes, selfupdate, sessions, steam, updates)
from .metapage import MetaPage  # noqa: E402
from .pages import HeadsetPage, StatsPage, data_paths  # noqa: E402
from .panels import (ControlsGroup, HealthGroup, ModsGroup, UpdateGroup, perf_row, protondb_row,  # noqa: E402
                     rating_row, store_row)
from .util import LOG_DIR  # noqa: E402
from .voice import t  # noqa: E402
from . import voice  # noqa: E402
from .widgets import (STATE_ICONS, DynamicPrefs, ago, button, clear, dot, duration, label,  # noqa: E402
                      load_texture, run_async, scrolled)

APP_ID = "io.github.intramy.BifluxVR"
HERE = os.path.dirname(os.path.abspath(__file__))
DISCORD_URL = "https://discord.gg/U9XQ9eeGkx"


# -------------------------------------------------------------------- state

class State:
    """A snapshot of the whole VR stack, gathered off the main thread."""

    def __init__(self):
        self.checks = health.run_all()
        self.runtimes = runtimes.discover()
        self.active = runtimes.active()
        self.layers = openvr.discover()
        self.vr_current = openvr.current_runtime()
        self.vr_kind = openvr.classify(self.vr_current)
        self.vr_locked = openvr.is_locked()
        self.servers = [s for s in runtimes.SERVERS if s.installed]
        self.running = {s.kind: s.running for s in self.servers}
        self.server = runtimes.preferred_server()
        self.protons = steam.proton_builds()
        self.steam_running = steam.running()
        self.gpus = health.gpus()

    def signature(self):
        return repr((
            self.checks, self.runtimes, self.active, self.layers, self.vr_current,
            self.vr_locked, self.running, self.server and self.server.kind,
            self.protons, self.steam_running,
        ))

    def server_running(self):
        return bool(self.server and self.running.get(self.server.kind))


def perform_fix(check):
    """Carry out a health check's one-click fix. Runs on a worker thread."""
    if check.id == "openxr":
        usable = [r for r in runtimes.discover() if r.valid and r.kind != "steamvr"]
        if not usable:
            raise RuntimeError(t("No OpenXR runtime is installed", "There's no OpenXR runtime installed. At all."))
        runtimes.set_active(usable[0].manifest)
        return t(f"{usable[0].name} is now your OpenXR runtime", f"{usable[0].name} is in charge now")
    if check.id == "openvr":
        layers = openvr.discover()
        pick = [l for l in layers if l.kind == "xrizer"] or [l for l in layers if l.kind == "opencomposite"]
        path = pick[0].path if pick else openvr.install_latest_xrizer()
        openvr.set_runtime(path)
        return t(f"OpenVR games now use {openvr.classify(path) or 'xrizer'}",
                 f"OpenVR games now go through {openvr.classify(path) or 'xrizer'}")
    if check.id == "server":
        server = runtimes.preferred_server()
        server.start()
        return t(f"{server.title} started", f"{server.title} is up!")
    raise RuntimeError(t(f"Nothing to fix for {check.title}", f"{check.title} looks fine to me"))


def load_library(cfg):
    """Returns (games, {custom id: Steam app ID}) with the Steam matches found
    for non-Steam games, which the caller saves. Runs on a worker thread."""
    games = steam.scan_games()
    played = steam.playtimes()
    for g in games:
        g.store_id = g.appid
        g.playtime, g.last_played = played.get(g.appid, (0, 0))
    tracked = sessions.minutes_by_game()
    last = sessions.last_played_by_game()
    online = cfg.get("online", True)
    matches = {}
    for c in cfg.get("custom_games", []):
        d = os.path.dirname(c["exe"])
        apis, eng = steam.detect_vr(d, 5) if os.path.isdir(d) else ([], [])
        store_id = c.get("store_id", "")
        if online:
            try:
                if not store_id and not c.get("store_id_manual"):
                    store_id = matches[c["id"]] = compat.store_match(c["name"])
                if store_id:
                    compat.fetch_art(store_id, ("library_600x900", "library_hero", "logo"))
            except OSError:
                pass
        key = f"custom:{c['id']}"
        games.append(steam.Game(key, c["name"], d, "", "", apis, eng, True, c["exe"], c.get("mode", "vr"),
                                store_id=store_id, prefix=c.get("prefix") or launcher.default_prefix(c["name"]),
                                playtime=tracked.get(key, 0), last_played=last.get(key, 0)))
    games.sort(key=lambda g: g.name.lower())
    return games, {k: v for k, v in matches.items() if v}


# ------------------------------------------------------------- overview

class OverviewPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                       margin_top=24, margin_bottom=36, margin_start=28, margin_end=28)
        body.append(label(t("Overview", "Vibe Check"), "large-title"))
        body.append(label(t("Your Windows VR games, running natively on Linux.",
                            "Windows VR games on Linux. Yes, really. No, we don't fully get it either."),
                          "page-subtitle", wrap=True))

        self.hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=10)
        self.hero.add_css_class("hero-card")
        top = Gtk.Box(spacing=18)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        self.eyebrow = label("", "hero-eyebrow")
        self.hero_title = label(t("Checking your setup…", "Poking your setup with a stick…"), "hero-title", wrap=True)
        self.hero_body = label("", "hero-body", wrap=True)
        for w in (self.eyebrow, self.hero_title, self.hero_body):
            text.append(w)
        self.glyph = Gtk.Image(icon_name="input-gaming-symbolic", pixel_size=88, valign=Gtk.Align.CENTER)
        self.glyph.add_css_class("hero-glyph")
        top.append(text)
        top.append(self.glyph)
        self.hero.append(top)
        self.hero_buttons = Gtk.Box(spacing=10)
        self.hero.append(self.hero_buttons)
        body.append(self.hero)

        body.append(label(t("How it works", "How the magic trick works"), "section-title", xalign=0))
        self.tiles = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
            min_children_per_line=1, max_children_per_line=4,
            column_spacing=14, row_spacing=14,
        )
        body.append(self.tiles)

        self.attention_title = label(t("Needs attention", "Stuff that's on fire"), "section-title", xalign=0)
        self.attention_title.set_margin_top(10)
        self.attention = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.attention.add_css_class("boxed-list")
        body.append(self.attention_title)
        body.append(self.attention)
        self.append(scrolled(body))

    def _tile(self, step, icon, title, value, caption, state):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("tile")
        head = Gtk.Box(spacing=8)
        num = label(str(step), "step", xalign=0.5)
        head.append(num)
        head.append(Gtk.Box(hexpand=True))
        head.append(dot(state))
        box.append(head)
        img = Gtk.Image(icon_name=icon, pixel_size=30, halign=Gtk.Align.START, margin_top=6)
        img.add_css_class("tile-icon")
        box.append(img)
        box.append(label(title, "tile-title"))
        val = label(value, "tile-value", ellipsize=True)
        val.set_max_width_chars(14)
        box.append(val)
        cap = label(caption, "caption-text", wrap=True)
        cap.set_max_width_chars(18)
        cap.set_width_chars(14)
        box.append(cap)
        child = Gtk.FlowBoxChild(child=box, focusable=False)
        child.add_css_class("tile-child")
        return child

    def update(self, st):
        checks = {c.id: c for c in st.checks}
        blocking, warns = health.summary(st.checks)
        fixable = [c for c in blocking + warns if c.fix and c.id != "server"]
        server = st.server
        live = st.server_running()

        for cls in ("attention", "live"):
            self.hero.remove_css_class(cls)
        clear(self.hero_buttons)
        if blocking:
            self.hero.add_css_class("attention")
            self.eyebrow.set_label(t("SETUP", "UH OH"))
            self.hero_title.set_label(t("Almost there", "So close, yet so broken"))
            n = len(blocking)
            self.hero_body.set_label(t(
                f"{n} thing{'s' if n > 1 else ''} need{'' if n > 1 else 's'} attention before VR games can run.",
                f"{n} thing{'s are' if n > 1 else ' is'} being difficult. Let's sort that out before you "
                "strap a screen to your face."))
            self.glyph.set_from_icon_name("applications-engineering-symbolic")
            if fixable:
                self.hero_buttons.append(button(t("Fix Everything", "Make It Work™"), "pill", "light",
                                                icon="object-select-symbolic", cb=lambda: self.win.fix_all(fixable)))
            self.hero_buttons.append(button(t("Details", "Show Me the Damage"), "pill", "glass",
                                            cb=lambda: self.win.navigate("system")))
        elif live:
            self.hero.add_css_class("live")
            self.eyebrow.set_label(t(f"{server.title.upper()} IS LIVE", f"{server.title.upper()} IS ALIVE"))
            self.hero_title.set_label(t("Ready for VR", "Strap in, nerd"))
            self.hero_body.set_label(t("Put on your headset and pick a game. Steam and Proton take it from here.",
                                        "Headset on, pick a game, try not to punch the ceiling fan. "
                                        "Steam and Proton handle the rest."))
            self.glyph.set_from_icon_name("input-gaming-symbolic")
            self.hero_buttons.append(button(t("Open Library", "Show Me the Games"), "pill", "light", icon="view-grid-symbolic",
                                            cb=lambda: self.win.navigate("library")))
            self.hero_buttons.append(button(t(f"Stop {server.title}", f"Put {server.title} to Sleep"), "pill", "glass",
                                            cb=lambda: self.win.toggle_server(server, False)))
            if server.dashboard:
                self.hero_buttons.append(button(t("Dashboard", "Fiddly Settings"), "pill", "glass", cb=server.open_dashboard))
        else:
            self.eyebrow.set_label(t("READY", "LOCKED & LOADED"))
            if server:
                self.hero_title.set_label(t("Ready when you are", "Your move, champ"))
                self.hero_body.set_label(t(
                    f"Start {server.title}, then connect from the {server.title} app on your headset.",
                    f"Fire up {server.title}, then open the {server.title} app on your headset and say hi."))
                self.hero_buttons.append(button(t(f"Start {server.title}", f"Wake Up {server.title}"), "pill", "light",
                                                icon="media-playback-start-symbolic",
                                                cb=lambda: self.win.toggle_server(server, True)))
            else:
                self.hero_title.set_label(t("Ready for VR", "Strap in, nerd"))
                self.hero_body.set_label(t("Your OpenXR runtime is configured. Launch a game from your library.",
                                            "Your OpenXR runtime is set up and raring to go. Pick a game."))
            self.glyph.set_from_icon_name("input-gaming-symbolic")
            self.hero_buttons.append(button(t("Open Library", "Show Me the Games"), "pill", "glass",
                                            cb=lambda: self.win.navigate("library")))

        proton = checks.get("proton")
        ovr = checks.get("openvr")
        oxr = checks.get("openxr")
        srv = checks.get("server")
        self.tiles.remove_all()
        self.tiles.append(self._tile(
            1, "applications-games-symbolic", t("Windows game", "The Windows game"),
            st.protons[0][0] if st.protons else t("Proton missing", "Proton went AWOL"),
            t("Wine runs the game; DXVK and VKD3D-Proton turn Direct3D into Vulkan.",
              "Wine pretends to be Windows. DXVK and VKD3D-Proton turn DirectX into Vulkan. Everybody wins."),
            proton.state if proton else health.FAIL))
        vr_name = {"xrizer": "xrizer", "opencomposite": "OpenComposite", "steamvr": "SteamVR"}.get(st.vr_kind, t("Not set", "Nobody yet"))
        self.tiles.append(self._tile(
            2, "media-playlist-shuffle-symbolic", t("OpenVR bridge", "The OpenVR translator"), vr_name,
            t("OpenVR calls are translated to OpenXR. OpenXR games skip this step.",
              "Turns OpenVR-speak into OpenXR-speak. OpenXR games skip the queue."),
            ovr.state if ovr else health.FAIL))
        self.tiles.append(self._tile(
            3, "video-display-symbolic", t("OpenXR runtime", "The OpenXR runtime"),
            st.active.name if st.active else
            (t(f"{server.title}, on standby", f"{server.title}, on standby")
             if server and oxr and oxr.state == health.OK else t("None", "Nada")),
            t("Composites frames, handles tracking and reprojection.",
              "Draws the frames, tracks your head, hides the stutters."),
            oxr.state if oxr else health.FAIL))
        self.tiles.append(self._tile(
            4, "network-wireless-symbolic", t("Headset", "Your face computer"),
            ((t("Streaming", "Beaming pixels") if live else t("Server stopped", "Server's napping"))
             if server else t("Native", "Plugged in")),
            (t(f"{server.title} encodes and streams to your headset.",
               f"{server.title} squishes the video and yeets it at your headset over Wi-Fi.")
             if server else t("Direct-attached headset.", "Good old cable. Very retro.")),
            (health.OK if live else health.WARN) if srv else health.INFO))

        self.attention.remove_all()
        issues = [c for c in st.checks if c.state in (health.FAIL, health.WARN)]
        self.attention_title.set_visible(bool(issues))
        self.attention.set_visible(bool(issues))
        for c in issues:
            self.attention.append(self.win.check_row(c))


# -------------------------------------------------------------- library

class GameCard(Gtk.Button):
    WIDTH, HEIGHT = 156, 234

    def __init__(self, game, on_open):
        super().__init__(halign=Gtk.Align.CENTER, valign=Gtk.Align.START)
        self.game = game
        self.add_css_class("game-card")
        self.set_tooltip_text(game.name)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        frame = Gtk.Overlay(overflow=Gtk.Overflow.HIDDEN, halign=Gtk.Align.CENTER)
        frame.add_css_class("poster")
        frame.set_size_request(self.WIDTH, self.HEIGHT)
        art = compat.artwork(game)
        if art:
            pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
            pic.set_size_request(self.WIDTH, self.HEIGHT)
            load_texture(art, pic)
            frame.set_child(pic)
        else:
            fb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.FILL)
            fb.add_css_class("poster-fallback")
            fb.set_size_request(self.WIDTH, self.HEIGHT)
            initial = label(game.name[:1].upper(), "initial", xalign=0.5)
            initial.set_vexpand(True)
            fb.append(initial)
            name = label(game.name, "caption-text", xalign=0.5, wrap=True, lines=2, ellipsize=True)
            name.set_max_width_chars(12)
            name.set_margin_bottom(16)
            name.set_margin_start(10)
            name.set_margin_end(10)
            fb.append(name)
            frame.set_child(fb)
        api = game.primary_api if game.vr_apis else ""
        if api:
            badge = label({"openxr": "OpenXR", "openvr": "OpenVR", "oculus": "Oculus"}[api],
                          "badge", api, xalign=0.5)
            badge.set_halign(Gtk.Align.END)
            badge.set_valign(Gtk.Align.START)
            badge.set_margin_top(8)
            badge.set_margin_end(8)
            frame.add_overlay(badge)
        if anticheat.blocked(game):
            ac = label(t("No Linux", "Blocked"), "badge", "blocked", xalign=0.5)
            ac.set_tooltip_text(t("Its anti-cheat blocks Proton", "The anti-cheat won't let Linux in"))
            ac.set_halign(Gtk.Align.START)
            ac.set_valign(Gtk.Align.START)
            ac.set_margin_top(8)
            ac.set_margin_start(8)
            frame.add_overlay(ac)
        # Gtk.Picture reports the image's full size as natural width; cap it.
        box.append(Adw.Clamp(maximum_size=self.WIDTH, tightening_threshold=self.WIDTH, child=frame))
        title = label(game.name, "game-title", xalign=0.0, ellipsize=True)
        title.set_max_width_chars(1)
        title.set_hexpand(True)
        title.set_margin_start(4)
        box.append(title)
        if game.custom:
            sub = t("Windows · VR", "Windows · face mode") if game.mode == "vr" else t("Windows · Simple", "Windows · desk mode")
        else:
            sub = t("VR-ready", "VR-ready, baby") if game.is_vr else t("Steam", "Steam game")
        if game.playtime >= 60:
            sub += f" · {game.playtime // 60} h"
        elif game.playtime:
            sub += f" · {game.playtime} min"
        s = label(sub, "caption-text", ellipsize=True)
        s.set_margin_start(4)
        s.set_max_width_chars(1)
        box.append(s)
        box.set_size_request(self.WIDTH, -1)
        self.set_child(box)
        self.connect("clicked", lambda *_: on_open(game))


class LibraryPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.games = []
        self.nav = Adw.NavigationView()
        self.append(self.nav)

        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.filter = Adw.ToggleGroup()
        for name, text in (("all", t("All Games", "Everything")), ("vr", t("VR-Ready", "VR Only")), ("custom", t("Non-Steam", "Off-Steam"))):
            self.filter.add(Adw.Toggle(name=name, label=text))
        self.filter.set_active_name("all")
        self.filter.connect("notify::active-name", lambda *_: self._refilter())
        header.set_title_widget(self.filter)
        self.search_btn = Gtk.ToggleButton(icon_name="system-search-symbolic", tooltip_text=t("Search (Ctrl+F)", "Find a game (Ctrl+F)"))
        header.pack_end(self.search_btn)
        header.pack_end(button("", icon="list-add-symbolic", cb=self.add_game, tooltip=t("Add a Windows game", "Add a game Steam doesn't know about")))
        header.pack_start(button("", icon="view-refresh-symbolic", cb=self.reload, tooltip=t("Rescan library", "Look again, harder")))
        sorts = [("name", t("Name", "A to Z")), ("recent", t("Recently played", "Played lately")),
                 ("played", t("Most played", "Most played"))]
        self.sort_keys = [k for k, _ in sorts]
        self.sort = Gtk.DropDown.new_from_strings([text for _, text in sorts])
        self.sort.set_tooltip_text(t("Sort", "Sort the pile"))
        want = win.cfg.get("library_sort", "name")
        self.sort.set_selected(self.sort_keys.index(want) if want in self.sort_keys else 0)
        self.sort.connect("notify::selected", lambda *_: self._resort())
        header.pack_start(self.sort)
        tv.add_top_bar(header)

        self.search = Gtk.SearchEntry(placeholder_text=t("Search games", "What are we playing?"), hexpand=True)
        self.search.connect("search-changed", lambda *_: self._refilter())
        self.search_bar = Gtk.SearchBar(child=Adw.Clamp(maximum_size=420, child=self.search))
        self.search_bar.connect_entry(self.search)
        self.search_btn.bind_property("active", self.search_bar, "search-mode-enabled",
                                      GObject.BindingFlags.BIDIRECTIONAL)
        tv.add_top_bar(self.search_bar)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                          valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        loading.append(Adw.Spinner(width_request=40, height_request=40))
        loading.append(label(t("Looking for VR games…", "Rummaging through your hard drives…"), "page-subtitle", xalign=0.5))
        self.stack.add_named(loading, "loading")
        self.empty = Adw.StatusPage(icon_name="applications-games-symbolic", title=t("No Games", "It's empty in here"),
                                    description=t("Install games in Steam, or add a Windows game with +.",
                                                  "Install some games in Steam, or hit + to add one yourself. Go on."))
        self.stack.add_named(self.empty, "empty")

        grid_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                           margin_top=20, margin_bottom=32, margin_start=24, margin_end=24)
        self.heading = label(t("Library", "The Pile"), "large-title")
        self.count = label("", "page-subtitle")
        grid_box.append(self.heading)
        grid_box.append(self.count)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, halign=Gtk.Align.START,
                                valign=Gtk.Align.START, min_children_per_line=2, max_children_per_line=12,
                                column_spacing=10, row_spacing=18, margin_top=18)
        self.flow.set_filter_func(self._filter)
        self.flow.set_sort_func(self._compare)
        grid_box.append(self.flow)
        self.stack.add_named(scrolled(grid_box, clamp=1600), "grid")
        tv.set_content(self.stack)
        self.nav.add(Adw.NavigationPage(title=t("Library", "The Pile"), tag="root", child=tv))
        self.reload()

    def focus_search(self):
        self.nav.pop_to_tag("root")
        self.search_btn.set_active(True)
        self.search.grab_focus()

    def reload(self):
        self.stack.set_visible_child_name("loading")
        def work():
            result = load_library(self.win.cfg)
            anticheat.database(self.win.cfg.get("online", True))
            return result
        run_async(work, self._loaded,
                  lambda e: self.win.toast(t(f"Couldn't scan library: {e}",
                                              f"Tried to read your library and tripped: {e}")))

    def _loaded(self, result):
        games, matches = result
        if matches:
            for c in self.win.cfg["custom_games"]:
                if c["id"] in matches:
                    c["store_id"] = matches[c["id"]]
            config.save(self.win.cfg)
        self.games = games
        self.flow.remove_all()
        for g in games:
            child = Gtk.FlowBoxChild(child=GameCard(g, self.open_game), focusable=False)
            child.add_css_class("card-child")
            child.game = g
            self.flow.append(child)
        self._refilter()

    def _filter(self, child):
        g = child.game
        mode = self.filter.get_active_name()
        if mode == "vr" and not g.is_vr:
            return False
        if mode == "custom" and not g.custom:
            return False
        q = self.search.get_text().strip().lower()
        return not q or q in g.name.lower()

    def _compare(self, a, b):
        mode = self.sort_keys[self.sort.get_selected()]
        ga, gb = a.game, b.game
        if mode == "recent" and ga.last_played != gb.last_played:
            return -1 if ga.last_played > gb.last_played else 1
        if mode == "played" and ga.playtime != gb.playtime:
            return -1 if ga.playtime > gb.playtime else 1
        na, nb = ga.name.lower(), gb.name.lower()
        return (na > nb) - (na < nb)

    def _resort(self):
        self.win.set_cfg("library_sort", self.sort_keys[self.sort.get_selected()])
        self.flow.invalidate_sort()

    def _refilter(self):
        self.flow.invalidate_filter()
        visible = [g for g in self.games if self._filter(type("C", (), {"game": g}))]
        vr = sum(1 for g in self.games if g.is_vr)
        mode = self.filter.get_active_name()
        self.heading.set_label({"all": t("Library", "The Pile"), "vr": t("VR-Ready", "VR Only"), "custom": t("Non-Steam", "Off-Steam")}[mode])
        self.count.set_label(t(f"{len(self.games)} games · {vr} with VR support detected",
                               f"{len(self.games)} games · {vr} of them do the VR thing"))
        if visible:
            self.stack.set_visible_child_name("grid")
        else:
            if self.games and mode == "vr":
                self.empty.set_title(t("No VR Games Found", "Zero VR games. Zilch."))
                self.empty.set_description(t("None of your installed games ship OpenVR or OpenXR. "
                                             "Install a VR title in Steam and it will appear here.",
                                             "None of your games speak OpenVR or OpenXR. Go grab a VR game "
                                             "on Steam and it'll pop up here."))
            elif self.games and self.search.get_text():
                q = self.search.get_text()
                self.empty.set_title(t("No Results", "Nope, nothing"))
                self.empty.set_description(t(f"Nothing matches “{q}”.",
                                             f"Couldn't find anything called “{q}”. Typo? We won't judge."))
            elif mode == "custom":
                self.empty.set_title(t("No Non-Steam Games", "Nothing off-Steam yet"))
                self.empty.set_description(t("Add any Windows VR game's .exe with the + button.",
                                             "Hit + to add any Windows game's .exe, or throw a .zip "
                                             "or .rar at the Yeet Zone."))
            else:
                self.empty.set_title(t("No Games", "It's empty in here"))
                self.empty.set_description(t("Install games in Steam, or add a Windows game with +.",
                                             "Install some games in Steam, or hit + to add one yourself. Go on."))
            self.stack.set_visible_child_name("empty")

    def open_game(self, game):
        self.nav.push(GameDetail(self.win, game))

    # -- adding non-Steam games

    def add_game(self):
        dialog = Gtk.FileDialog(title=t("Choose a Windows Game", "Point me at a Windows game"))
        flt = Gtk.FileFilter(name=t("Windows executables", "Windows .exe files"))
        flt.add_suffix("exe")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        dialog.set_filters(store)
        dialog.open(self.win, None, self._picked)

    def _picked(self, dialog, result):
        try:
            f = dialog.open_finish(result)
        except GLib.Error:
            return
        exe = f.get_path()
        if not exe:
            return
        guess = os.path.basename(os.path.dirname(exe)) or os.path.splitext(os.path.basename(exe))[0]
        entry = Adw.EntryRow(title=t("Name", "What do we call it?"), text=guess)
        lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lst.add_css_class("boxed-list")
        lst.append(entry)
        alert = Adw.AlertDialog(heading=t("Add to Library", "New game, who dis?"), body=os.path.basename(exe), extra_child=lst)
        alert.add_response("cancel", t("Cancel", "Nevermind"))
        alert.add_response("add", t("Add", "Add It!"))
        alert.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
        alert.set_default_response("add")

        def on_response(_d, resp):
            if resp != "add":
                return
            name = entry.get_text().strip() or guess
            self.win.cfg["custom_games"].append({"id": uuid.uuid4().hex[:12], "name": name, "exe": exe})
            config.save(self.win.cfg)
            self.win.toast(t(f"Added {name}", f"{name} has joined the party"))
            self.filter.set_active_name("all")
            self.reload()

        alert.connect("response", on_response)
        alert.present(self.win)

    def remove_custom(self, game):
        gid = game.appid.split(":", 1)[1]
        self.win.cfg["custom_games"] = [c for c in self.win.cfg["custom_games"] if c["id"] != gid]
        config.save(self.win.cfg)
        self.nav.pop_to_tag("root")
        self.win.toast(t(f"Removed {game.name} from your library. Its files were kept",
                          f"{game.name} got kicked from the library. Its files are still on disk, chill."))
        self.reload()


class GameDetail(Adw.NavigationPage):
    def __init__(self, win, game):
        super().__init__(title=game.name)
        self.win, self.game = win, game
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22,
                       margin_top=12, margin_bottom=36, margin_start=24, margin_end=24)

        hero = Gtk.Overlay(overflow=Gtk.Overflow.HIDDEN)
        hero.add_css_class("hero-image")
        hero.set_size_request(-1, 300)
        art = compat.artwork(game, "library_hero") or compat.artwork(game, "header")
        if art:
            pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True, hexpand=True)
            load_texture(art, pic)
            hero.set_child(pic)
        else:
            hero.set_child(Gtk.Box(hexpand=True))
        scrim = Gtk.Box()
        scrim.add_css_class("hero-scrim")
        hero.add_overlay(scrim)
        logo = compat.artwork(game, "logo")
        if logo:
            lp = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True,
                             halign=Gtk.Align.START, valign=Gtk.Align.END,
                             margin_start=28, margin_bottom=24)
            lp.set_size_request(320, 110)
            load_texture(logo, lp)
            hero.add_overlay(lp)
        else:
            name = label(game.name, "detail-title", wrap=True)
            name.set_valign(Gtk.Align.END)
            name.set_margin_start(28)
            name.set_margin_bottom(24)
            name.set_margin_end(28)
            hero.add_overlay(name)
        body.append(hero)

        actions = Gtk.Box(spacing=10)
        self.play_btn = button("", "pill", "play-button", cb=self.play)
        self.running = None
        self.sync_running()
        actions.append(self.play_btn)
        if not game.custom:
            actions.append(button(t("Steam Page", "Gawk at It on Steam"), "pill", icon="web-browser-symbolic",
                                  cb=lambda: win.open_uri(f"steam://nav/games/details/{game.appid}")))
        actions.append(Gtk.Box(hexpand=True))
        actions.append(button("", "circular", "flat", icon="folder-open-symbolic",
                              cb=lambda: win.open_path(game.install_dir), tooltip=t("Show game files", "Show me the guts")))
        body.append(actions)

        self.health = HealthGroup(win, self)
        body.append(self.health)

        support = Adw.PreferencesGroup(title=t("VR Compatibility", "Will it VR?"))
        api_row = Adw.ActionRow(title=self._api_title(), subtitle=self._api_detail())
        api_row.set_subtitle_lines(3)
        api_row.add_prefix(Gtk.Image(icon_name=self._api_icon()))
        support.add(api_row)

        if not game.custom:
            self.opts_row = Adw.ActionRow(title=t("Steam launch options", "Steam launch options (the magic words)"))
            self.opts_row.set_subtitle_lines(3)
            self.opts_row.add_prefix(Gtk.Image(icon_name="utilities-terminal-symbolic"))
            self.copy_btn = button("", "flat", icon="edit-copy-symbolic", tooltip=t("Copy VR launch options", "Copy the magic words"),
                                   cb=self.copy_options)
            self.copy_btn.set_valign(Gtk.Align.CENTER)
            self.apply_btn = Gtk.Button(valign=Gtk.Align.CENTER)
            self.apply_btn.connect("clicked", lambda *_: self.toggle_options())
            self.opts_row.add_suffix(self.copy_btn)
            self.opts_row.add_suffix(self.apply_btn)
            support.add(self.opts_row)
        support.add(protondb_row(win, game))
        if not (game.custom and game.mode == "simple"):
            support.add(perf_row(win, game))
        support.add(rating_row(win, game))
        if game.custom:
            support.add(store_row(win, game, self))
        body.append(support)
        body.append(ModsGroup(win, self))
        body.append(ControlsGroup(win, self))

        info = Adw.PreferencesGroup(title=t("Details", "The boring bits"))
        if game.playtime:
            played = duration(game.playtime * 60)
            when = t(f" · last played {ago(game.last_played)}", f" · last seen {ago(game.last_played)}") \
                if game.last_played else ""
            info.add(Adw.ActionRow(title=t("Play time", "Time sunk"), subtitle=played + when))
        loc = Adw.ActionRow(title=t("Location", "Lives at"), subtitle=game.exe if game.custom else game.install_dir)
        loc.set_subtitle_selectable(True)
        info.add(loc)
        if game.custom:
            mode_row = Adw.ActionRow(title=t("Launch mode", "Launch mode"),
                                     subtitle=t("VR sets up your headset; Simple runs flat",
                                                "VR wakes up the whole headset circus. Simple just runs it on your monitor."))
            mode = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
            mode.add(Adw.Toggle(name="vr", label="VR"))
            mode.add(Adw.Toggle(name="simple", label="Simple"))
            mode.set_active_name(game.mode)
            mode.connect("notify::active-name", lambda g, _p: self.set_mode(g.get_active_name()))
            mode_row.add_suffix(mode)
            info.add(mode_row)
            self.exe_row = Adw.ActionRow(title=t("Executable", "The .exe that matters"), subtitle=escape(os.path.basename(game.exe)))
            change = Gtk.Button(label=t("Change…", "Swap It…"), valign=Gtk.Align.CENTER)
            change.connect("clicked", lambda *_: self.change_exe())
            self.exe_row.add_suffix(change)
            info.add(self.exe_row)
            info.add(Adw.ActionRow(title=t("Runs with", "Powered by"),
                                   subtitle="umu-launcher + Proton" if shutil.which("umu-run")
                                   else "Proton"))
            rm = Adw.ButtonRow(title=t("Remove from Library", "Kick It from the Library"))
            rm.add_css_class("destructive-action")
            rm.connect("activated", lambda *_: win.library.remove_custom(game))
            info.add(rm)
        else:
            info.add(Adw.ActionRow(title=t("App ID", "Steam's secret number"), subtitle=game.appid))
            if game.buildid:
                info.add(Adw.ActionRow(title=t("Build", "Build number"), subtitle=game.buildid))
        body.append(info)

        # Don't jump past the hero when the Play button takes focus on push.
        tv.set_content(scrolled(body, clamp=1000, follow_focus=False))
        self.set_child(tv)
        if not game.custom:
            self.update_options()

    def _api_title(self):
        g = self.game
        if "oculus" in g.vr_apis and not ({"openvr", "openxr"} & set(g.vr_apis)):
            return t("Oculus SDK only", "Oculus-only (yikes)")
        if g.vr_apis:
            names = {"openxr": "OpenXR", "openvr": "OpenVR"}
            return " + ".join(names[a] for a in ("openxr", "openvr") if a in g.vr_apis)
        if g.vr_engine:
            return t("Engine VR plugins", "Maybe VR? (engine leftovers)")
        return t("No VR runtime detected", "No VR in sight")

    def _api_detail(self):
        g = self.game
        apis = set(g.vr_apis)
        if apis & {"openxr", "openvr"}:
            parts = []
            if "openxr" in apis:
                parts.append(t("OpenXR goes straight to your runtime through Proton's wineopenxr.",
                                "Speaks OpenXR, so it goes straight to your runtime through Proton's "
                                "wineopenxr. Smooth."))
            if "openvr" in apis:
                parts.append(t("OpenVR is translated to OpenXR by xrizer — no SteamVR needed.",
                                "Speaks OpenVR, which xrizer translates on the fly. No SteamVR, no bloat."))
            return " ".join(parts)
        if "oculus" in apis:
            return t("This build only talks to the Oculus runtime, which has no Linux equivalent. "
                     "Look for an OpenVR/SteamVR version of the game.",
                     "This one only talks to the Oculus runtime, and that doesn't exist on Linux. "
                     "Hunt down the SteamVR version instead.")
        if g.vr_engine:
            return t("Unreal ships VR plugins with every game, so this may be a flat-screen title. "
                     "If it has a VR mode, launching it here will route it to your headset.",
                     "Unreal sticks VR plugins in every game, VR or not, so this might just be a regular "
                     "flat game. If it does have a VR mode, we'll send it to your headset.")
        return t("This game doesn't ship OpenVR or OpenXR. It will run on a virtual desktop "
                 "screen in your headset rather than in full VR.",
                 "No OpenVR or OpenXR in here. It'll show up on a big virtual screen in your headset "
                 "instead. Still cool, just not full VR.")

    def _api_icon(self):
        if self.game.vr_apis and self.game.vr_apis != ["oculus"]:
            return "object-select-symbolic"
        if "oculus" in self.game.vr_apis:
            return "dialog-warning-symbolic"
        return "dialog-information-symbolic"

    def update_options(self):
        def fetch():
            return steam.get_launch_options(self.game.appid), steam.running()

        def show(res):
            opts, steam_up = res
            env = runtimes.vr_env()
            ok = steam.has_env(opts, env)
            self.opts_row.set_subtitle(escape(opts) if opts else t("Not set", "Not set yet"))
            self.apply_btn.set_label(t("Remove", "Undo") if ok else t("Apply", "Do It"))
            for c in ("suggested-action", "destructive-action"):
                self.apply_btn.remove_css_class(c)
            if not ok:
                self.apply_btn.add_css_class("suggested-action")
            self.apply_btn.set_sensitive(not steam_up)
            self.apply_btn.set_tooltip_text(
                t("Quit Steam first — it overwrites launch options on exit",
                  "Close Steam first. It overwrites this stuff on exit, the rascal") if steam_up else
                (t("Remove the VR variables", "Take the magic words back out") if ok
                 else t("Add the VR variables for this game", "Add the magic words to this game")))
            self.options_ok = ok
        run_async(fetch, show)

    def copy_options(self):
        text = steam.merge_env(steam.get_launch_options(self.game.appid), runtimes.vr_env(), runtimes.ENV_KEYS)
        self.win.copy(text, t("Launch options copied — paste into Steam → Properties → Launch Options",
                             "Copied! Paste it into Steam → Properties → Launch Options"))

    def toggle_options(self):
        self.apply_options(not getattr(self, "options_ok", False))

    def apply_options(self, add):
        env = runtimes.vr_env()
        remove = not add

        def work():
            if remove:
                return steam.set_launch_options(self.game.appid,
                                                lambda o: steam.strip_env(o, runtimes.ENV_KEYS))
            return steam.set_launch_options(self.game.appid,
                                            lambda o: steam.merge_env(o, env, runtimes.ENV_KEYS))

        def done(n):
            self.win.toast(t("Launch options removed", "Magic words removed") if remove
                           else t("VR launch options applied", "Magic words applied. You're welcome."))
            if not self.game.custom:
                self.update_options()
            self.health.reload()

        run_async(work, done, lambda e: self.win.toast(str(e)))

    def play(self):
        if self.running:
            self.win.stop(self.game)
        else:
            self.win.play(self.game, self)

    def sync_running(self):
        running = launcher.running(self.game)
        if running == self.running:
            return
        self.running = running
        b = self.play_btn
        if running:
            text, icon = t("Stop", "Pull the Plug"), "media-playback-stop-symbolic"
            b.remove_css_class("suggested-action")
            b.add_css_class("destructive-action")
        else:
            text = t("Play", "Play It Flat") if self.game.custom and self.game.mode == "simple" \
                else t("Play in VR", "Enter the Matrix")
            icon = "media-playback-start-symbolic"
            b.remove_css_class("destructive-action")
            b.add_css_class("suggested-action")
        b.set_child(Adw.ButtonContent(label=text, icon_name=icon))

    def update_entry(self, **changes):
        gid = self.game.appid.split(":", 1)[1]
        for c in self.win.cfg["custom_games"]:
            if c["id"] == gid:
                c.update(changes)
        config.save(self.win.cfg)
        self.win.library.reload()

    def set_mode(self, mode):
        self.game.mode = mode
        self.running = None
        self.sync_running()
        self.update_entry(mode=mode)
        self.win.toast(t("VR mode on", "VR mode on. Mind the furniture.") if mode == "vr"
                       else t("Simple mode: launches without VR", "Simple mode. Flat and cozy, no headset needed."))

    def change_exe(self):
        dialog = Gtk.FileDialog(title=t("Choose the Game's Executable", "Which .exe is the real one?"))
        if os.path.isdir(self.game.install_dir):
            dialog.set_initial_folder(Gio.File.new_for_path(self.game.install_dir))
        flt = Gtk.FileFilter(name=t("Windows executables", "Windows .exe files"))
        flt.add_suffix("exe")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        dialog.set_filters(store)

        def picked(d, res):
            try:
                path = d.open_finish(res).get_path()
            except GLib.Error:
                return
            if path:
                self.game.exe = path
                self.exe_row.set_subtitle(escape(os.path.basename(path)))
                self.update_entry(exe=path)
                exe = os.path.basename(path)
                self.win.toast(t(f"{self.game.name} now starts {exe}", f"Got it, {self.game.name} now launches {exe}"))
        dialog.open(self.win, None, picked)


# ------------------------------------------------------- preference pages

class RuntimePage(DynamicPrefs):
    TITLE = ("Runtime", "The Engine Room")
    SUBTITLE = ("The OpenXR runtime and streaming server your games render to.",
                "Where the VR actually happens. Don't touch anything you don't understand. Or do, it's your PC.")

    def update(self, st):
        self.updating = True
        self.reset()
        for s in st.servers:
            g = self.group(title=t(f"{s.title} Server", f"{s.title} server"),
                           description=t("Streams your VR session to a standalone headset over Wi-Fi.",
                                         "Beams your games to a standalone headset over Wi-Fi. Wireless freedom, baby.")
                           if s.kind == "wivrn" else t("Drives headsets connected directly to this PC.",
                                                       "Runs headsets plugged straight into this PC."))
            row = Adw.SwitchRow(title=s.title,
                                subtitle=t("Running — connect from your headset", "Up and running. Go connect from your headset.")
                                if st.running[s.kind] else t("Stopped", "Taking a nap"),
                                active=st.running[s.kind])
            row.add_prefix(dot(health.OK if st.running[s.kind] else health.INFO))
            row.connect("notify::active", lambda r, _p, s=s: self.updating or self.win.toggle_server(s, r.get_active()))
            g.add(row)
            if s.dashboard:
                dash = Adw.ActionRow(title=t("Dashboard", "Fiddly Settings"), subtitle=t("Pair headsets, set bitrate and codecs",
                                              "Pair headsets, crank the bitrate, nerd out on codecs"),
                                     activatable=True)
                dash.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                dash.connect("activated", lambda *_a, s=s: s.open_dashboard())
                g.add(dash)
            logrow = Adw.ActionRow(title=t("Server log", "Server diary"), subtitle=os.path.join(LOG_DIR, f"{s.binary}.log"),
                                   activatable=True)
            logrow.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            logrow.connect("activated", lambda *_a, s=s: self.win.open_path(os.path.join(LOG_DIR, f"{s.binary}.log")))
            g.add(logrow)

        if st.servers:
            g = self.group(title=t("Behavior", "Manners"))
            auto = Adw.SwitchRow(title=t("Start automatically", "Auto-start"),
                                 subtitle=t("Launch the server when you press Play",
                                            "Wakes the server up when you hit Play, so you don't have to"),
                                 active=self.win.cfg.get("auto_start_server", True))
            auto.connect("notify::active", lambda r, _p: self.win.set_cfg("auto_start_server", r.get_active()))
            g.add(auto)

        g = self.group(title=t("OpenXR Runtime", "Pick your OpenXR runtime"),
                       description=t("Every OpenXR game — and every OpenVR game after translation — renders here.",
                                     "Every OpenXR game, and every OpenVR game after translation, ends up here."))
        if not st.runtimes:
            row = Adw.ActionRow(title=t("No runtimes installed", "No runtimes. Like, at all."),
                                subtitle=t("Install WiVRn for Quest/Pico or Monado for PC headsets.",
                                           "Grab WiVRn for Quest/Pico, or Monado for headsets with cables."))
            row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
            g.add(row)
        first = None
        active = os.path.realpath(st.active.manifest) if st.active else ""
        for rt in st.runtimes:
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            if first:
                check.set_group(first)
            else:
                first = check
            is_active = os.path.realpath(rt.manifest) == active
            check.set_active(is_active)
            sub = {"wivrn": t("Wireless streaming · recommended for Quest, Pico",
                              "Wireless streaming · the Quest/Pico crowd's favorite"),
                   "monado": t("Open-source runtime for wired headsets", "Open source, for headsets on a leash"),
                   "steamvr": t("Valve's runtime — heavier, adds latency",
                                "Valve's runtime. Chunky, and adds latency")}.get(rt.kind, rt.manifest)
            if not rt.valid:
                sub = t("Broken — runtime library is missing", "Busted. Its library went missing")
            row = Adw.ActionRow(title=rt.name, subtitle=sub, activatable_widget=check, sensitive=rt.valid)
            row.add_prefix(check)
            if is_active:
                row.add_suffix(label(t("Active", "In charge"), "caption-text"))
            check.connect("toggled", lambda c, rt=rt: (c.get_active() and not self.updating)
                          and self.win.set_xr_runtime(rt))
            g.add(row)
        self.updating = False


class TranslationPage(DynamicPrefs):
    TITLE = ("Translation", "Babel Fish")
    SUBTITLE = ("How Windows graphics and VR calls become native Linux ones.",
                "How Windows-isms get turned into Linux-isms without anyone noticing.")

    def __init__(self, win, **kw):
        super().__init__(win, **kw)
        self.downloading = False

    def update(self, st):
        self.updating = True
        self.reset()
        g = self.group(title=t("OpenVR Games", "OpenVR games"),
                       description=t("Most PC VR games use OpenVR. A translation layer turns those calls into "
                                     "OpenXR so they run on your runtime without SteamVR.",
                                     "Most PC VR games speak OpenVR. A translator turns that into OpenXR so they "
                                     "run on your runtime, no SteamVR required."))
        info = {
            "xrizer": t("Recommended · fast, modern OpenVR → OpenXR layer",
                        "The good one · fast, modern, OpenVR → OpenXR"),
            "opencomposite": t("Mature OpenVR → OpenXR layer", "The wise old one · OpenVR → OpenXR"),
            "steamvr": t("Native SteamVR — not recommended with WiVRn or Monado",
                         "Actual SteamVR. Doesn't play nice with WiVRn or Monado"),
        }
        first = None
        cur = os.path.realpath(st.vr_current) if st.vr_current else ""
        for layer in st.layers:
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            if first:
                check.set_group(first)
            else:
                first = check
            check.set_active(os.path.realpath(layer.path) == cur)
            row = Adw.ActionRow(title=layer.title, subtitle=f"{info[layer.kind]}\n{layer.path}",
                                activatable_widget=check)
            row.set_subtitle_lines(3)
            row.add_prefix(check)
            if layer.version:
                row.add_suffix(label(layer.version, "caption-text"))
            check.connect("toggled", lambda c, l=layer: (c.get_active() and not self.updating)
                          and self.win.set_vr_layer(l))
            g.add(row)
        if not st.layers:
            row = Adw.ActionRow(title=t("No translation layer installed", "No translator here"),
                                subtitle=t("Download xrizer below to run OpenVR games.",
                                           "Grab xrizer below and OpenVR games will work."))
            row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
            g.add(row)

        dl = Adw.ActionRow(title=t("Get the latest xrizer", "Get fresh xrizer"),
                           subtitle=t("Downloads the newest release from GitHub into your home folder",
                                      "Pulls the newest release off GitHub into your home folder"))
        self.dl_btn = Gtk.Button(label=t("Download", "Gimme"), valign=Gtk.Align.CENTER)
        self.dl_spinner = Adw.Spinner(visible=self.downloading)
        self.dl_btn.set_sensitive(not self.downloading)
        self.dl_btn.connect("clicked", lambda *_: self.download())
        dl.add_suffix(self.dl_spinner)
        dl.add_suffix(self.dl_btn)
        g.add(dl)

        lock = Adw.SwitchRow(title=t("Protect this choice", "Guard this setting"),
                             subtitle=t("Make the OpenVR config read-only so SteamVR can't take it over",
                                        "Locks the OpenVR config so SteamVR can't sneak in and change it"),
                             active=st.vr_locked, sensitive=bool(st.vr_current))
        lock.connect("notify::active", lambda r, _p: self.updating or self.win.set_vr_lock(r.get_active()))
        g.add(lock)

        g = self.group(title=t("Windows Compatibility", "Windows impersonation"),
                       description=t("Proton runs Windows games through Wine and translates graphics to Vulkan.",
                                     "Proton runs Windows games through Wine and turns DirectX into Vulkan. "
                                     "Witchcraft, basically."))
        if st.protons:
            names = [n for n, _ in st.protons]
            combo = Adw.ComboRow(title=t("Proton for non-Steam games", "Proton for off-Steam games"),
                                 model=Gtk.StringList.new(names),
                                 subtitle=t("Steam games use the version chosen in Steam",
                                            "Steam games use whatever you picked in Steam"))
            want = self.win.cfg.get("proton")
            paths = [p for _, p in st.protons]
            combo.set_selected(paths.index(want) if want in paths else 0)
            combo.connect("notify::selected",
                          lambda c, _p: self.updating or self.win.set_cfg("proton", paths[c.get_selected()]))
            g.add(combo)
        else:
            g.add(Adw.ActionRow(title=t("Proton not found", "Can't find Proton"),
                                subtitle=t("Install Proton Experimental in Steam → Library → Tools",
                                           "Install Proton Experimental in Steam → Library → Tools. Takes a minute.")))
        for icon, title, sub in (
            ("applications-system-symbolic", t("Win32 → Linux", "Windows → Linux, somehow"), "Wine"),
            ("video-display-symbolic", t("Direct3D 9 / 10 / 11 → Vulkan", "Old DirectX → Vulkan"), "DXVK"),
            ("video-display-symbolic", t("Direct3D 12 → Vulkan", "New DirectX → Vulkan"), "VKD3D-Proton"),
            ("input-gaming-symbolic", t("OpenXR → native runtime", "OpenXR → straight to your headset"),
             "wineopenxr (Proton)"),
            ("media-playlist-shuffle-symbolic", t("OpenVR → OpenXR", "OpenVR → OpenXR, lost in translation"),
             "vrclient bridge + xrizer"),
        ):
            row = Adw.ActionRow(title=title)
            row.add_prefix(Gtk.Image(icon_name=icon))
            row.add_suffix(label(sub, "caption-text"))
            g.add(row)
        self.updating = False

    def download(self):
        self.downloading = True
        self.dl_btn.set_sensitive(False)
        self.dl_spinner.set_visible(True)

        def work():
            path = openvr.install_latest_xrizer()
            openvr.set_runtime(path)
            return path

        def finish(msg):
            self.downloading = False
            self.win.toast(msg)
            self.win.refresh(force=True)

        run_async(work, lambda p: finish(t(f"xrizer installed and active · {os.path.basename(p)}",
                                           f"Fresh xrizer installed and working · {os.path.basename(p)}")),
                  lambda e: finish(t(f"Download failed: {e}", f"Download faceplanted: {e}")))


class SystemPage(DynamicPrefs):
    TITLE = ("Settings", "Settings & Stuff")
    SUBTITLE = ("Appearance, wording, and everything BifluxVR checks before a game starts.",
                "Make it pretty, make it serious, and see what's broken.")

    def __init__(self, win, **kw):
        super().__init__(win, **kw)
        g = Adw.PreferencesGroup(title=t("Appearance", "Looks"))
        row = Adw.ActionRow(title=t("Theme", "Theme"),
                            subtitle=t("Night and day mode", "Vampire mode or sunshine mode"))
        row.add_prefix(Gtk.Image(icon_name="preferences-desktop-appearance-symbolic"))
        self.theme = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        for name, text in (("system", "Auto"), ("light", "Day"), ("dark", "Night")):
            self.theme.add(Adw.Toggle(name=name, label=text))
        self.sync_appearance()
        self.theme.connect("notify::active-name",
                           lambda *_: self.updating or self.win.set_appearance(self.theme.get_active_name()))
        row.add_suffix(self.theme)
        g.add(row)
        serious = Adw.SwitchRow(title=t("Serious mode", "Serious mode"),
                                subtitle=t("Use plain, professional wording. Turn off for the fun version",
                                           "For when your boss, your mom or a Linux purist is watching"),
                                active=voice.is_serious())
        serious.add_prefix(Gtk.Image(icon_name="x-office-document-symbolic"))
        serious.connect("notify::active", lambda r, _p: self.win.set_serious(r.get_active()))
        g.add(serious)
        self.add(g)

        g = Adw.PreferencesGroup(title=t("Online", "Internet stuff"),
                                 description=t("BifluxVR only goes online for these. Nothing about you is sent.",
                                               "The only times BifluxVR talks to the internet. It never tells anyone "
                                               "about you."))
        online = Adw.SwitchRow(title=t("Ratings and artwork", "Ratings and artwork"),
                               subtitle=t("ProtonDB ratings, Steam store art and matching for non-Steam games",
                                          "ProtonDB ratings, cover art, and figuring out what your off-Steam games are"),
                               active=self.win.cfg.get("online", True))
        online.add_prefix(Gtk.Image(icon_name="emblem-default-symbolic"))
        online.connect("notify::active", lambda r, _p: self.win.set_cfg("online", r.get_active()))
        g.add(online)
        notify = Adw.SwitchRow(title=t("Update notifications", "Update notifications"),
                               subtitle=t("New BifluxVR, WiVRn and xrizer releases, and Proton releases that "
                                          "mention your games",
                                          "New BifluxVR, WiVRn and xrizer, and Proton releases that fix your games"),
                               active=self.win.cfg.get("notify", True))
        notify.add_prefix(Gtk.Image(icon_name="preferences-system-notifications-symbolic"))
        notify.connect("notify::active", lambda r, _p: self.win.set_cfg("notify", r.get_active()))
        g.add(notify)
        self.add(g)
        self.add(self._sound_group())
        if selfupdate.enabled():
            self.updates = UpdateGroup(win)
            self.add(self.updates)

    def _sound_group(self):
        g = Adw.PreferencesGroup(title=t("Sound", "Ears"))
        vr = Adw.SwitchRow(title=t("Play sound through the headset", "Sound goes in your face"),
                           subtitle=t("Switches the output to WiVRn while a VR game runs",
                                      "Moves the sound to WiVRn while you play, and back when you stop"),
                           active=self.win.cfg.get("vr_audio", True))
        vr.add_prefix(Gtk.Image(icon_name="audio-headphones-symbolic"))
        vr.connect("notify::active", lambda r, _p: self.win.set_cfg("vr_audio", r.get_active()))
        g.add(vr)

        self.sinks = [s for s in audio.sinks() if s["kind"] != "vr"]
        saved = self.win.cfg.get("audio_after", "")
        names = [t("Automatic (headphones first)", "Auto (headphones win)")] + \
                [f"{s['title']} · {audio.label(s['kind'])}" for s in self.sinks]
        after = Adw.ComboRow(title=t("Sound output after playing", "Where sound goes after"),
                             subtitle=t("Also switches the output now when no game is running",
                                        "Picks it right now too, if you're not mid-game"),
                             model=Gtk.StringList.new(names))
        after.add_prefix(Gtk.Image(icon_name="audio-speakers-symbolic"))
        after.set_selected(next((i + 1 for i, s in enumerate(self.sinks) if s["name"] == saved), 0))
        after.connect("notify::selected", lambda r, _p: self._pick_output(r.get_selected()))
        g.add(after)
        return g

    def _pick_output(self, index):
        name = self.sinks[index - 1]["name"] if index else ""
        self.win.set_cfg("audio_after", name)
        if audio.in_game():
            return

        def work():
            target = audio.back_target(name)
            return target["title"] if target and audio.set_default(target["name"]) else ""
        run_async(work, lambda title: title and self.win.toast(t(f"Sound output: {title}",
                                                                 f"Sound now comes out of {title}")))

    def sync_appearance(self):
        self.updating = True
        self.theme.set_active_name(self.win.cfg.get("appearance", "system"))
        self.updating = False

    def update(self, st):
        self.reset()
        g = self.group(title=t("Readiness", "Is everything OK?"))
        for c in st.checks:
            g.add(self.win.check_row(c))
        g = self.group(title=t("Graphics", "The pixel pushers"))
        for vendor, driver in st.gpus or [("Unknown", "")]:
            row = Adw.ActionRow(title=f"{vendor} GPU", subtitle=t(f"Kernel driver: {driver}", f"Driver: {driver}") if driver else "")
            row.add_prefix(Gtk.Image(icon_name="video-display-symbolic"))
            g.add(row)
        g = self.group(title=t("Files", "Files nerds care about"))
        for title, path in (
            (t("Active OpenXR runtime", "Current OpenXR runtime"), runtimes.ACTIVE_LINK),
            (t("OpenVR paths", "OpenVR paths"), openvr.VRPATH),
            (t("Logs", "Logs (for when things go boom)"), LOG_DIR),
            *data_paths(),
        ):
            row = Adw.ActionRow(title=title, subtitle=path, activatable=True)
            row.set_subtitle_selectable(True)
            row.add_suffix(Gtk.Image(icon_name="folder-open-symbolic"))
            row.connect("activated", lambda *_a, p=path: self.win.open_path(
                p if os.path.isdir(p) else os.path.dirname(p)))
            g.add(row)


# ------------------------------------------------------------ drop folder

class QueueItem:
    def __init__(self, info):
        self.info = info
        self.row = None
        self.status = None
        self.pct = -1
        self.state = "pending"  # pending | installing | failed


class DropPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.items = []
        self.busy = False
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                       margin_top=24, margin_bottom=36, margin_start=28, margin_end=28)
        body.append(label(t("Drop Folder", "Yeet Zone"), "large-title"))
        body.append(label(t("Drop a game's .zip or .rar. BifluxVR unpacks it, finds the executable and adds "
                            "it to your library.",
                            "Throw a game's .zip or .rar at me. I'll unpack it, sniff out the right .exe and "
                            "stick it in your library."), "page-subtitle", wrap=True))

        self.zone = Gtk.CenterBox(orientation=Gtk.Orientation.VERTICAL, margin_top=10)
        self.zone.add_css_class("dropzone")
        self.zone.set_size_request(-1, 240)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER)
        glyph = Gtk.Image(icon_name="folder-download-symbolic", pixel_size=56)
        glyph.add_css_class("drop-glyph")
        inner.append(glyph)
        inner.append(label(t("Drop .zip or .rar files here", "Yeet your .zip or .rar files here"), "drop-title", xalign=0.5))
        inner.append(label(t("Each game gets its own folder", "Every game gets its own little room"),
                           "caption-text", xalign=0.5))
        choose = button(t("Choose Files…", "Or Browse Like a Civilized Person…"), "pill", cb=self.choose_files)
        choose.set_halign(Gtk.Align.CENTER)
        choose.set_margin_top(8)
        inner.append(choose)
        self.zone.set_center_widget(inner)
        body.append(self.zone)

        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        target.connect("drop", self._on_drop)
        target.connect("enter", self._on_enter)
        target.connect("leave", lambda *_: self.zone.remove_css_class("hover"))
        self.add_controller(target)

        setup = Adw.PreferencesGroup(title=t("Setup", "How do you want it?"), margin_top=12)
        type_row = Adw.ActionRow(title=t("Game type", "What kind of game?"))
        self.type_row = type_row
        self.mode = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        self.mode.add(Adw.Toggle(name="vr", label="VR"))
        self.mode.add(Adw.Toggle(name="simple", label="Simple"))
        self.mode.set_active_name(win.cfg.get("install_mode", "vr"))
        self.mode.connect("notify::active-name", lambda *_: self._mode_changed())
        type_row.add_suffix(self.mode)
        setup.add(type_row)
        self.loc_row = Adw.ActionRow(title=t("Install to", "Put it in"))
        self.loc_row.set_subtitle_selectable(True)
        change = Gtk.Button(label=t("Change…", "Swap It…"), valign=Gtk.Align.CENTER)
        change.connect("clicked", lambda *_: self.choose_folder())
        self.loc_row.add_suffix(change)
        setup.add(self.loc_row)
        body.append(setup)

        self.queue = Adw.PreferencesGroup(title=t("Ready to Install", "Waiting in line"), margin_top=6)
        body.append(self.queue)
        self.install_btn = button(t("Install", "Yeet It In"), "pill", "suggested-action", cb=self.install_all)
        self.install_btn.set_halign(Gtk.Align.END)
        body.append(self.install_btn)

        self.done = Adw.PreferencesGroup(title=t("Installed", "Done and dusted"), margin_top=6, visible=False)
        body.append(self.done)
        self.append(scrolled(body))
        self._mode_changed(save=False)

    # -- settings

    def _mode_key(self):
        return f"install_dir_{self.mode.get_active_name()}"

    def _mode_changed(self, save=True):
        mode = self.mode.get_active_name()
        if save:
            self.win.set_cfg("install_mode", mode)
        self.type_row.set_subtitle(
            t("Starts your headset stack and routes the game to VR",
              "Fires up the whole headset circus and sends the game to VR") if mode == "vr"
            else t("Launches on your desktop, no VR setup", "Plain old monitor gaming. No headset, no fuss"))
        self.loc_row.set_subtitle(escape(self.win.cfg[self._mode_key()]))

    def choose_folder(self):
        dialog = Gtk.FileDialog(title=t("Choose Install Folder", "Where should games live?"))
        current = self.win.cfg[self._mode_key()]
        if os.path.isdir(current):
            dialog.set_initial_folder(Gio.File.new_for_path(current))

        def picked(d, res):
            try:
                folder = d.select_folder_finish(res)
            except GLib.Error:
                return
            if folder and folder.get_path():
                self.win.set_cfg(self._mode_key(), folder.get_path())
                self._mode_changed(save=False)
        dialog.select_folder(self.win, None, picked)

    # -- adding archives

    def _on_enter(self, *_):
        self.zone.add_css_class("hover")
        return Gdk.DragAction.COPY

    def _on_drop(self, _target, value, _x, _y):
        self.zone.remove_css_class("hover")
        self.add_paths([f.get_path() for f in value.get_files() if f.get_path()])
        return True

    def choose_files(self):
        dialog = Gtk.FileDialog(title=t("Choose Game Archives", "Pick some archives"))
        flt = Gtk.FileFilter(name=t("Game archives (.zip, .rar)", ".zip and .rar files"))
        for ext in installer.ARCHIVE_EXTS:
            flt.add_suffix(ext.lstrip("."))
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        dialog.set_filters(store)

        def picked(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            self.add_paths([files.get_item(i).get_path() for i in range(files.get_n_items())])
        dialog.open_multiple(self.win, None, picked)

    def add_paths(self, paths):
        archives = [p for p in paths if p and installer.supported(p)]
        if len(archives) < len(paths):
            self.win.toast(t("Only .zip and .rar files can be installed",
                             "Only .zip and .rar, please. I'm picky like that"))
        # A multi-part RAR is opened through part 1; the other parts come along.
        picked = []
        for p in archives:
            if (installer.volume_number(p) or 1) > 1:
                p = installer.first_volume(p)
                if not p:
                    self.win.toast(t("Part 1 of that multi-part archive is missing",
                                     "That's not part 1, and I can't find part 1. I need it to start"))
                    continue
            if p not in picked:
                picked.append(p)
        queued = {i.info.path for i in self.items}
        for p in picked:
            if p in queued:
                continue
            queued.add(p)
            run_async(lambda p=p: installer.inspect(p), self._add_item,
                      lambda e: self.win.toast(str(e)))

    def _add_item(self, info):
        item = QueueItem(info)
        row = Adw.EntryRow(title=self._row_title(info), text=info.name)
        row.add_prefix(Gtk.Image(icon_name="package-x-generic-symbolic"))
        item.status = Gtk.Box(valign=Gtk.Align.CENTER)
        row.add_suffix(item.status)
        item.row = row
        self._set_status(item, button("", "flat", "circular", icon="window-close-symbolic",
                                      tooltip=t("Remove", "Nope, remove it"), cb=lambda: self.remove(item)))
        self.items.append(item)
        self.queue.add(row)
        self._sync()

    def _row_title(self, info, note=""):
        n = len(info.exes)
        exes = (t("no .exe found", "no .exe in sight") if not n
                else t(f"{n} executable{'s' if n > 1 else ''}", f"{n} .exe file{'s' if n > 1 else ''}"))
        base = f"{os.path.basename(info.path)} · {installer.human(info.size)} · {exes}"
        return f"{base} · {note}" if note else base

    def _set_status(self, item, widget):
        clear(item.status)
        item.status.append(widget)

    def remove(self, item):
        if item.state == "installing":
            return
        self.queue.remove(item.row)
        self.items.remove(item)
        self._sync()

    def _sync(self):
        pending = [i for i in self.items if i.state in ("pending", "failed")]
        self.queue.set_visible(bool(self.items))
        self.install_btn.set_visible(bool(pending))
        self.install_btn.set_sensitive(not self.busy)
        n = len(pending)
        self.install_btn.set_label(t(f"Install {n} Game{'s' if n != 1 else ''}",
                                     f"Yeet {n} Game{'s' if n != 1 else ''} In"))

    # -- installing

    def install_all(self):
        if self.busy:
            return
        items = [i for i in self.items if i.state in ("pending", "failed")]
        if not items:
            return
        mode = self.mode.get_active_name()
        dest = self.win.cfg[self._mode_key()]
        jobs = []
        for item in items:
            name = item.row.get_text().strip() or item.info.name
            item.state = "installing"
            item.row.set_editable(False)
            spinner = Gtk.Box(spacing=8)
            spinner.append(Adw.Spinner())
            item.pct_label = label("0%", "caption-text")
            spinner.append(item.pct_label)
            self._set_status(item, spinner)
            jobs.append((item, name))
        self.busy = True
        self._sync()

        def progress(item, fraction):
            pct = int(fraction * 100)
            if pct != item.pct:
                item.pct = pct
                GLib.idle_add(lambda: (item.pct_label.set_label(f"{pct}%"), False)[1])

        def work():
            for item, name in jobs:
                try:
                    result = installer.install(item.info.path, dest, name,
                                               lambda f, item=item: progress(item, f))
                    GLib.idle_add(self._installed, item, name, mode, *result)
                except Exception as e:  # noqa: BLE001 — shown on the row
                    GLib.idle_add(self._failed, item, str(e))
            GLib.idle_add(self._finished)

        threading.Thread(target=work, daemon=True).start()

    def _installed(self, item, name, mode, game_dir, exe, apis):
        gid = uuid.uuid4().hex[:12]
        self.win.cfg["custom_games"].append({"id": gid, "name": name, "exe": exe, "mode": mode})
        config.save(self.win.cfg)
        self.queue.remove(item.row)
        self.items.remove(item)
        game = steam.Game(f"custom:{gid}", name, game_dir, "", "", list(apis), [], True, exe, mode)
        if mode == "vr" and not apis:
            note = t("No VR runtime found in the files. It may only run on a virtual screen",
                     "No VR stuff found inside. It'll probably show up on a virtual screen")
        elif mode == "simple" and apis:
            note = t("This game supports VR. Switch it to VR on its library page",
                     "Psst, this one does VR! Flip it to VR on its library page")
        else:
            note = os.path.relpath(exe, game_dir)
        row = Adw.ActionRow(title=escape(name), subtitle=escape(note))
        row.set_subtitle_lines(2)
        icon = Gtk.Image(icon_name="object-select-symbolic")
        icon.add_css_class("success")
        row.add_prefix(icon)
        show = button("", "flat", "circular", icon="folder-open-symbolic", tooltip=t("Show files", "Show me the files"),
                      cb=lambda: self.win.open_path(game_dir))
        show.set_valign(Gtk.Align.CENTER)
        play = button(t("Play", "Let's Go"), "pill", "suggested-action", cb=lambda: self.win.play(game))
        play.set_valign(Gtk.Align.CENTER)
        row.add_suffix(show)
        row.add_suffix(play)
        self.done.add(row)
        self.done.set_visible(True)
        self.win.toast(t(f"{name} installed", f"{name} is in. Have fun!"))
        return False

    def _failed(self, item, msg):
        item.state = "failed"
        item.row.set_editable(True)
        item.row.set_title(escape(f"⚠ {msg}"))
        item.row.add_css_class("error")
        self._set_status(item, button("", "flat", "circular", icon="window-close-symbolic",
                                      tooltip=t("Remove", "Nope, remove it"), cb=lambda: self.remove(item)))
        self.win.toast(msg)
        return False

    def _finished(self):
        self.busy = False
        self._sync()
        self.win.library.reload()
        return False


# --------------------------------------------------------------- donate

class DonatePage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                       margin_top=24, margin_bottom=36, margin_start=28, margin_end=28)
        body.append(label(t("Support BifluxVR", "Feed the Dev"), "large-title"))
        body.append(label(t("BifluxVR is free and made by intramy. If it made your VR nights better, "
                            "you can chip in.",
                            "BifluxVR is free, made by intramy with love and questionable sleep habits. "
                            "If it made your VR nights better, toss a coin."), "page-subtitle", wrap=True))
        body.append(self._goal_card())
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                           min_children_per_line=1, max_children_per_line=3,
                           column_spacing=16, row_spacing=16, margin_top=12)
        for tier in donate.TIERS:
            child = Gtk.FlowBoxChild(child=self._card(*tier), focusable=False)
            child.add_css_class("tile-child")
            flow.append(child)
        body.append(flow)
        body.append(self._custom_card(*donate.CUSTOM))
        note = label(t("Payments open in your browser and are handled by the payment provider. "
                       "BifluxVR never sees your card details.",
                       "Payment happens in your browser through the payment provider. BifluxVR never sees "
                       "your card. Couldn't even if it tried."), "caption-text", wrap=True, xalign=0.5)
        note.set_margin_top(10)
        body.append(note)
        self.append(scrolled(body))

    def _goal_card(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12)
        box.add_css_class("donate-card")
        text = label("", "donate-goal", wrap=True)
        bar = Gtk.ProgressBar()
        bar.add_css_class("donate-goal-bar")
        box.append(text)
        box.append(bar)

        def show(raised, goal):
            text.set_label(f"€{raised} / €{goal} — Monthly development expenses")
            bar.set_fraction(min(raised / goal, 1) if goal > 0 else 0)

        show(0, donate.GOAL)
        threading.Thread(target=lambda: GLib.idle_add(show, *donate.funding()), daemon=True).start()
        return box

    def _card(self, tid, emoji, title, blurb, lo, hi, default):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("donate-card")
        box.add_css_class(f"tier-{tid}")
        icon = label(emoji, "donate-emoji", xalign=0.5)
        icon.set_halign(Gtk.Align.START)
        box.append(icon)
        heading = label(title, "donate-title", wrap=True)
        heading.set_margin_top(8)
        box.append(heading)
        b = label(t(*blurb), "caption-text", wrap=True)
        b.set_max_width_chars(22)
        box.append(b)
        amount = label(f"€{default}", "donate-amount")
        amount.set_margin_top(10)
        box.append(amount)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, 1)
        scale.set_draw_value(False)
        scale.set_round_digits(0)
        scale.set_value(default)
        for v in range(lo, hi + 1):
            scale.add_mark(v, Gtk.PositionType.BOTTOM, f"€{v}" if v in (lo, hi) else None)
        box.append(scale)
        btn = button(t(f"Donate €{default}", f"Yes, Take My €{default}"), "pill", "donate")
        btn.set_margin_top(10)
        box.append(btn)

        def changed(s):
            v = round(s.get_value())
            if v != s.get_value():
                s.set_value(v)  # snap to whole euros
                return
            amount.set_label(f"€{v}")
            btn.set_label(t(f"Donate €{v}", f"Yes, Take My €{v}"))
        scale.connect("value-changed", changed)
        btn.connect("clicked", lambda *_: self.donate(tid, title, round(scale.get_value())))
        return box

    def _custom_card(self, tid, emoji, title, blurb, lo, hi, default, picks):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("donate-card")
        box.add_css_class(f"tier-{tid}")
        box.set_margin_top(4)
        top = Gtk.Box(spacing=14)
        icon = label(emoji, "donate-emoji", xalign=0.5)
        icon.set_valign(Gtk.Align.START)
        top.append(icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        text.append(label(title, "donate-title", wrap=True))
        text.append(label(t(*blurb), "caption-text", wrap=True))
        top.append(text)
        box.append(top)

        row = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, min_children_per_line=1, max_children_per_line=8,
                          column_spacing=8, row_spacing=8, margin_top=10)
        spin = Gtk.SpinButton.new_with_range(lo, hi, 1)
        spin.set_value(default)
        spin.set_digits(0)
        spin.set_numeric(True)
        spin.set_valign(Gtk.Align.CENTER)
        entry = Gtk.Box(spacing=6)
        cur = label("€", "donate-currency")
        cur.set_valign(Gtk.Align.CENTER)
        entry.append(cur)
        entry.append(spin)
        row.append(entry)
        for v in picks:
            chip = button(f"€{v}", "pill", "donate-chip", cb=lambda v=v: spin.set_value(v))
            chip.set_valign(Gtk.Align.CENTER)
            row.append(chip)
        box.append(row)
        btn = button(t(f"Donate €{default}", f"Yes, Take My €{default}"), "pill", "donate")
        btn.set_halign(Gtk.Align.START)
        btn.set_margin_top(10)
        box.append(btn)

        def changed(s):
            v = int(s.get_value())
            btn.set_label(t(f"Donate €{v}", f"Yes, Take My €{v}"))
        spin.connect("value-changed", changed)
        btn.connect("clicked", lambda *_: (spin.update(), self.donate(tid, title, int(spin.get_value()))))
        return box

    def donate(self, tid, title, amount):
        url = donate.url_for(tid, amount)
        if not url:
            self.win.toast(t("Donations aren't open yet. Thanks for the thought!",
                              "The tip jar isn't set up yet, but it's the thought that counts. Love you."))
            return
        self.win.open_uri(url)
        self.win.toast(t(f"Thank you! Opening checkout for €{amount}…",
                          f"You absolute legend. Opening checkout for €{amount}…"))


# ------------------------------------------------------------------ window

def pages():
    return [
        ("overview", t("Overview", "Vibe Check"), "go-home-symbolic"),
        ("library", t("Library", "The Pile"), "view-grid-symbolic"),
        ("stats", t("Stats", "Nerd Stats"), "preferences-system-time-symbolic"),
        ("drop", t("Drop Folder", "Yeet Zone"), "folder-download-symbolic"),
        ("meta", t("Meta Library", "Meta Stash"), "applications-games-symbolic"),
        ("headset", t("Headset", "Face Computer"), "network-wireless-symbolic"),
        ("runtime", t("Runtime", "The Engine Room"), "input-gaming-symbolic"),
        ("translation", t("Translation", "Babel Fish"), "media-playlist-shuffle-symbolic"),
        ("system", t("Settings", "Settings & Stuff"), "emblem-system-symbolic"),
        ("donate", t("Donate", "Feed the Dev"), "starred-symbolic"),
    ]


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="BifluxVR", default_width=1180, default_height=780)
        self.add_css_class("bifluxvr")
        self.set_size_request(380, 500)
        self.cfg = config.load()
        voice.set_serious(self.cfg.get("serious_mode", False))
        self.state = None
        self._sig = None
        self._refreshing = False

        self.toasts = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView(min_sidebar_width=220, max_sidebar_width=260)
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)

        # sidebar
        side = Adw.ToolbarView()
        sh = Adw.HeaderBar()
        sh.set_title_widget(Adw.WindowTitle(title="BifluxVR"))
        self.theme_btn = Gtk.Button()
        self.theme_btn.connect("clicked", lambda *_: self.toggle_theme())
        sh.pack_start(self.theme_btn)
        style = Adw.StyleManager.get_default()
        style.connect("notify::dark", lambda *_: self._sync_theme_button())
        self.apply_appearance(self.cfg.get("appearance", "system"))
        menu = Gio.Menu()
        menu.append(t("Refresh", "Check Again"), "app.refresh")
        menu.append(t("About BifluxVR", "Who Made This?"), "app.about")
        sh.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text=t("Menu", "More stuff")))
        self.bell = Gtk.MenuButton(icon_name="preferences-system-notifications-symbolic",
                                   tooltip_text=t("Notifications", "News"))
        self.bell_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.bell_list.add_css_class("notice-list")
        pop = Gtk.Popover(child=Gtk.ScrolledWindow(child=self.bell_list, propagate_natural_height=True,
                                                   max_content_height=420, min_content_width=320,
                                                   hscrollbar_policy=Gtk.PolicyType.NEVER))
        pop.connect("show", lambda *_: self._read_notices())
        self.bell.set_popover(pop)
        sh.pack_end(self.bell)
        side.add_top_bar(sh)
        self.nav_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.nav_list.add_css_class("navigation-sidebar")
        for name, title, icon in pages():
            row = Gtk.ListBoxRow()
            row.name = name
            box = Gtk.Box(spacing=12)
            box.append(Gtk.Image(icon_name=icon))
            box.append(label(title))
            row.set_child(box)
            self.nav_list.append(row)
        self.nav_list.connect("row-activated", lambda _l, r: self.navigate(r.name))
        sbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sbox.append(Gtk.ScrolledWindow(child=self.nav_list, vexpand=True,
                                       hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.status_box = Gtk.Box(spacing=10)
        self.status_box.add_css_class("sidebar-status")
        self.status_dot_holder = Gtk.Box(valign=Gtk.Align.CENTER)
        self.status_dot_holder.append(dot(health.INFO))
        st_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.status_title = label(t("Checking…", "Hold on…"), "title")
        self.status_sub = label("", "caption-text", ellipsize=True)
        st_text.append(self.status_title)
        st_text.append(self.status_sub)
        self.status_box.append(self.status_dot_holder)
        self.status_box.append(st_text)
        sbox.append(self.status_box)
        logo = Gtk.Image.new_from_file(os.path.join(HERE, "discord.svg"))
        logo.set_pixel_size(18)
        inner = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        inner.append(logo)
        inner.append(Gtk.Label(label=t("Join our Discord", "Hang out on Discord")))
        self.discord_btn = button("", "pill", "discord", cb=lambda: self.open_uri(DISCORD_URL))
        self.discord_btn.set_child(inner)
        self.discord_btn.set_margin_start(12)
        self.discord_btn.set_margin_end(12)
        self.discord_btn.set_margin_bottom(10)
        sbox.append(self.discord_btn)
        GLib.timeout_add_seconds(9, self._shine)
        credit = label("Made by intramy", "caption-text", xalign=0.5)
        credit.set_margin_bottom(12)
        sbox.append(credit)
        side.set_content(sbox)
        self.split.set_sidebar(Adw.NavigationPage(title="BifluxVR", child=side))

        # content
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=180)
        self.overview = OverviewPage(self)
        self.library = LibraryPage(self)
        self.runtime = RuntimePage(self)
        self.translation = TranslationPage(self)
        self.system = SystemPage(self)
        self.drop = DropPage(self)
        self.donate = DonatePage(self)
        self.headset = HeadsetPage(self)
        self.stats = StatsPage(self)
        self.meta = MetaPage(self)
        for name, widget, wrap in (
            ("overview", self.overview, True),
            ("library", self.library, False),
            ("stats", self.stats, True),
            ("headset", self.headset, True),
            ("runtime", self.runtime, True),
            ("translation", self.translation, True),
            ("system", self.system, True),
            ("drop", self.drop, True),
            ("meta", self.meta, True),
            ("donate", self.donate, True),
        ):
            if wrap:
                tv = Adw.ToolbarView()
                tv.add_top_bar(Adw.HeaderBar(show_title=False))
                tv.set_content(widget)
                widget = tv
            self.stack.add_named(widget, name)
        self.content_page = Adw.NavigationPage(title="Overview", child=self.stack)
        self.split.set_content(self.content_page)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

        self.navigate("overview", show=False)
        self.refresh(force=True)
        self._tick_id = GLib.timeout_add_seconds(3, self._tick)
        sessions.recover()
        self._show_notices()
        if not self.cfg.get("welcomed") and not os.environ.get("BIFLUXVR_SCREENSHOT"):
            GLib.idle_add(self._welcome)
        # updates.check() itself waits hours between real lookups
        GLib.timeout_add_seconds(5, lambda: (self._check_updates(), False)[1])
        self._notice_id = GLib.timeout_add_seconds(1800, self._check_updates)
        self.connect("close-request", self._close_request)

    def _close_request(self, _win):
        if not self.meta.busy():
            return False
        alert = Adw.AlertDialog(heading=t("Stop the Download?", "Bail on the download?"),
                                body=t("A Meta game is still downloading. Closing stops it; Resume picks it up later.",
                                       "A Meta game is still coming down. Close now and it stops, Keep Going resumes it later."))
        alert.add_response("cancel", t("Keep Downloading", "Keep Downloading"))
        alert.add_response("close", t("Stop and Close", "Close Anyway"))
        alert.set_response_appearance("close", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("cancel")

        def on_response(_d, resp):
            if resp == "close":
                self.meta.stop_all()
                self.meta.jobs.clear()
                self.close()
        alert.connect("response", on_response)
        alert.present(self)
        return True

    def _welcome(self):
        dialog = Adw.Dialog(content_width=440, title="")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=30, margin_bottom=26, margin_start=30, margin_end=30)
        icon = Gtk.Image(icon_name=APP_ID, pixel_size=96)
        icon.add_css_class("welcome-icon")
        box.append(icon)
        box.append(label(t("Welcome to BifluxVR", "Welcome to BifluxVR!"), "welcome-title", xalign=0.5, wrap=True))
        box.append(label(t("Made by intramy", "Lovingly made by intramy"), "welcome-by", xalign=0.5))
        box.append(label(t("BifluxVR plays your Windows VR games on Linux: Proton runs the game, xrizer translates "
                           "it to OpenXR and WiVRn streams it to your headset. Pick a game in the Library and "
                           "press Play.",
                           "Windows VR games, on Linux, in your face. Proton runs them, xrizer translates them, "
                           "WiVRn beams them to your headset. Pick something from The Pile and smash Play."),
                         "welcome-text", xalign=0.5, wrap=True))
        box.append(label(t("Free and open source. If you enjoy it, Donate keeps it going.",
                           "Free forever. If it makes you happy, Feed the Dev keeps the lights on."),
                         "caption-text", xalign=0.5, wrap=True))
        go = button(t("Get Started", "Let's Go"), "pill", "suggested-action", cb=dialog.close)
        go.set_halign(Gtk.Align.CENTER)
        go.set_margin_top(8)
        box.append(go)
        tv = Adw.ToolbarView(content=box)
        tv.add_top_bar(Adw.HeaderBar(show_title=False))
        dialog.set_child(tv)
        dialog.connect("closed", lambda *_: self.set_cfg("welcomed", True))
        dialog.present(self)
        return False

    # -- navigation

    def navigate(self, name, show=True):
        self.stack.set_visible_child_name(name)
        title = next(title for n, title, _ in pages() if n == name)
        self.content_page.set_title(title)
        row = next(r for r in self._rows() if r.name == name)
        self.nav_list.select_row(row)
        if show:
            self.split.set_show_content(True)
        if name == "stats":
            self.stats.refresh()
        elif name == "headset":
            self.headset.poll()
        elif name == "meta":
            self.meta.shown()

    def _rows(self):
        r = self.nav_list.get_first_child()
        while r:
            yield r
            r = r.get_next_sibling()

    # -- state

    def _tick(self):
        if self.is_visible():
            self.refresh()
            self._sync_running()
            if self.stack.get_visible_child_name() == "headset":
                self.headset.poll()
        return True

    def _sync_running(self):
        page = self.library.nav.get_visible_page()
        if isinstance(page, GameDetail):
            page.sync_running()

    # -- notifications

    def _check_updates(self):
        if not self.cfg.get("notify", True):
            return True
        games = [(g.appid, g.name) for g in self.library.games]

        def done(new):
            self._show_notices()
            if hasattr(self.system, "updates"):
                self.system.updates.show()
            app = self.get_application()
            for item in new[:3]:
                note = Gio.Notification.new(item["title"])
                note.set_body(item["body"])
                app.send_notification(item["id"], note)
        run_async(lambda: updates.check(games), done)
        return True

    def _show_notices(self):
        clear(self.bell_list)
        items = updates.items()
        if updates.unread():
            self.bell.add_css_class("unread")
        else:
            self.bell.remove_css_class("unread")
        if not items:
            self.bell_list.append(Adw.ActionRow(title=t("No notifications", "All quiet"),
                                                subtitle=t("Update notices show up here", "News shows up here")))
        for item in items:
            if item["kind"] == "app" and not selfupdate.newer(item["version"]):
                continue
            row = Adw.ActionRow(title=escape(item["title"]), subtitle=escape(item["body"]))
            row.set_subtitle_lines(3)
            if item["kind"] == "app":
                row.set_activatable(True)
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                row.connect("activated", lambda *_a: self._show_update())
            elif item.get("url"):
                row.set_activatable(True)
                row.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
                row.connect("activated", lambda *_a, u=item["url"]: self.open_uri(u))
            self.bell_list.append(row)

    def _show_update(self):
        self.bell.popdown()
        self.navigate("system")
        if hasattr(self.system, "updates"):
            if selfupdate.found:
                self.system.updates.show()
            else:
                self.system.updates.check()

    def _read_notices(self):
        updates.mark_read()
        self.bell.remove_css_class("unread")

    def refresh(self, force=False):
        if self._refreshing:
            return
        self._refreshing = True

        def done(st):
            self._refreshing = False
            self.state = st
            sig = st.signature()
            if force or sig != self._sig:
                self._sig = sig
                self.overview.update(st)
                self.runtime.update(st)
                self.translation.update(st)
                self.system.update(st)
                self._update_status(st)

        def fail(e):
            self._refreshing = False
            self.toast(t(f"Status check failed: {e}", f"Couldn't check the status, oops: {e}"))

        run_async(State, done, fail)

    def refresh_soon(self, ms=900):
        GLib.timeout_add(ms, lambda: (self.refresh(force=True), False)[1])

    def _update_status(self, st):
        clear(self.status_dot_holder)
        blocking, _ = health.summary(st.checks)
        if blocking:
            n = len(blocking)
            state, title, sub = (health.FAIL, t("Setup needed", "Something's off"),
                                 t(f"{n} issue(s)", f"{n} thing{'s' if n > 1 else ''} to fix"))
        elif st.server and st.server_running():
            state, title, sub = (health.OK, t(f"{st.server.title} live", f"{st.server.title} is alive!"),
                                 t("Headset can connect", "Headset can hop on"))
        elif st.server:
            state, title, sub = (health.WARN, t("Ready", "All good"),
                                 t(f"{st.server.title} stopped", f"{st.server.title} is napping"))
        else:
            state, title, sub = health.OK, t("Ready", "All good"), st.active.name if st.active else ""
        self.status_dot_holder.append(dot(state))
        self.status_title.set_label(title)
        self.status_sub.set_label(sub)

    def check_row(self, c, on_fix=None):
        row = Adw.ActionRow(title=c.title, subtitle=escape(c.detail))
        row.set_subtitle_lines(2)
        img = Gtk.Image(icon_name=STATE_ICONS[c.state])
        img.add_css_class({"ok": "success", "warn": "warning", "fail": "error", "info": "dim-label"}[c.state])
        row.add_prefix(img)
        if c.fix:
            b = Gtk.Button(label=c.fix, valign=Gtk.Align.CENTER)
            if c.state == health.FAIL:
                b.add_css_class("suggested-action")
            b.connect("clicked", lambda *_: (on_fix or self.apply_fix)(c, b))
            row.add_suffix(b)
        return row

    # -- actions

    def apply_fix(self, check, btn=None):
        if btn:
            btn.set_sensitive(False)
        run_async(lambda: perform_fix(check), lambda m: (self.toast(m), self.refresh_soon()),
                  lambda e: (self.toast(t(f"Couldn't fix {check.title}: {e}", f"Couldn't fix {check.title}, sorry: {e}")),
                              self.refresh(force=True)))

    def fix_all(self, checks):
        def work():
            return [perform_fix(c) for c in checks]
        run_async(work, lambda msgs: (self.toast(" · ".join(msgs)), self.refresh_soon()),
                  lambda e: (self.toast(t(f"Fix failed: {e}", f"The fix didn't stick: {e}")), self.refresh(force=True)))

    def toggle_server(self, server, on):
        def work():
            server.start() if on else server.stop()
        if not on:
            self.toast(t(f"Stopping {server.title}…", f"Telling {server.title} to go to bed…"), timeout=2)
        started = t(f"{server.title} started", f"{server.title} is up!")
        stopped = t(f"{server.title} stopped", f"{server.title} is down. Goodnight, sweet prince")
        run_async(work, lambda _: (self.toast(started if on else stopped),
                                   self.refresh_soon()),
                  lambda e: self.toast(f"{server.title}: {e}"))

    def set_xr_runtime(self, rt):
        try:
            runtimes.set_active(rt.manifest)
            self.toast(t(f"{rt.name} is now your OpenXR runtime", f"{rt.name} is in charge now"))
        except OSError as e:
            self.toast(t(f"Couldn't switch runtime: {e}", f"The runtime refused to switch: {e}"))
        self.refresh(force=True)

    def set_vr_layer(self, layer):
        try:
            openvr.set_runtime(layer.path)
            self.toast(t(f"OpenVR games now use {layer.title}", f"OpenVR games now go through {layer.title}"))
        except OSError as e:
            self.toast(t(f"Couldn't update OpenVR paths: {e}", f"Couldn't touch the OpenVR config: {e}"))
        self.refresh(force=True)

    def set_vr_lock(self, lock):
        try:
            openvr.set_locked(lock)
        except OSError as e:
            self.toast(str(e))
        self.refresh(force=True)

    def apply_appearance(self, mode):
        Adw.StyleManager.get_default().set_color_scheme({
            "light": Adw.ColorScheme.FORCE_LIGHT,
            "dark": Adw.ColorScheme.FORCE_DARK,
        }.get(mode, Adw.ColorScheme.DEFAULT))
        self._sync_theme_button()

    def set_appearance(self, mode):
        self.set_cfg("appearance", mode)
        self.apply_appearance(mode)
        if hasattr(self, "system"):
            self.system.sync_appearance()

    def toggle_theme(self):
        self.set_appearance("light" if Adw.StyleManager.get_default().get_dark() else "dark")

    def _sync_theme_button(self):
        dark = Adw.StyleManager.get_default().get_dark()
        self.theme_btn.set_icon_name("weather-clear-symbolic" if dark else "weather-clear-night-symbolic")
        self.theme_btn.set_tooltip_text(t("Day mode", "Let there be light") if dark
                                        else t("Night mode", "Embrace the darkness"))

    def set_serious(self, on):
        if on == voice.is_serious():
            return
        self.set_cfg("serious_mode", on)
        voice.set_serious(on)
        # Every string is chosen when its widget is built, so build a fresh
        # window in the new voice. Deferred: we're inside the switch's handler.
        GLib.idle_add(self._rebuild)

    def _rebuild(self):
        app = self.get_application()
        page = self.stack.get_visible_child_name()
        new = MainWindow(app)
        new.set_default_size(self.get_width(), self.get_height())
        if self.is_maximized():
            new.maximize()
        new.navigate(page)
        new.present()
        new.toast(t("Serious mode on", "Fun mode is back, baby"))
        GLib.source_remove(self._tick_id)
        GLib.source_remove(self._notice_id)
        self.destroy()
        return False

    def set_cfg(self, key, value):
        self.cfg[key] = value
        config.save(self.cfg)

    def play(self, game, detail=None):
        if launcher.running(game):
            self.stop(game)
            return
        if game.custom:
            entry = next((c for c in self.cfg["custom_games"] if f"custom:{c['id']}" == game.appid), None)
            if not entry:
                return
            protons = steam.proton_builds()
            proton = self.cfg.get("proton") or (protons[0][1] if protons else "")

            def work():
                vr = entry.get("mode", "vr") == "vr"
                notes = launcher.prepare(self.cfg) if vr else []
                proc = launcher.launch_custom(entry, proton, mods.dll_overrides(game))
                launcher.track(game, proc)
                sessions.watch(game, proc, vr, on_end=self._session_ended)
                return notes
            self._launch(game, work, vr=entry.get("mode", "vr") == "vr")
            return

        env = runtimes.vr_env()
        opts = steam.get_launch_options(game.appid)
        if steam.has_env(opts, env):
            self._launch(game, lambda: self._start_steam(game))
            return

        merged = steam.merge_env(opts, env, runtimes.ENV_KEYS)
        steam_up = steam.running()
        body = t("Steam needs these launch options so the game can reach your headset from inside "
                 "the Steam Runtime:",
                 "Steam needs these magic words so the game can find your headset from inside its "
                 "little sandbox:") + f"\n\n<tt>{escape(merged)}</tt>"
        if steam_up:
            body += t("\n\nQuit Steam to apply them automatically, or copy them into the game's "
                      "Properties → General → Launch Options.",
                      "\n\nClose Steam and I'll add them myself, or copy them into the game's "
                      "Properties → General → Launch Options.")
        alert = Adw.AlertDialog(heading=t("Set Up VR Launch Options?", "Add the Magic Words?"),
                                body=body, body_use_markup=True)
        alert.add_response("cancel", t("Cancel", "Nevermind"))
        alert.add_response("anyway", t("Play Anyway", "YOLO, Play Anyway"))
        if steam_up:
            alert.add_response("copy", t("Copy Options", "Copy Them"))
            alert.set_response_appearance("copy", Adw.ResponseAppearance.SUGGESTED)
            alert.set_default_response("copy")
        else:
            alert.add_response("apply", t("Apply & Play", "Do It & Play"))
            alert.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
            alert.set_default_response("apply")
        alert.set_close_response("cancel")

        def on_response(_d, resp):
            if resp == "anyway":
                self._launch(game, lambda: self._start_steam(game))
            elif resp == "copy":
                self.copy(merged, t("Copied — paste into Steam → Properties → Launch Options",
                                   "Copied! Paste into Steam → Properties → Launch Options"))
            elif resp == "apply":
                def work():
                    steam.set_launch_options(game.appid, lambda o: steam.merge_env(o, env, runtimes.ENV_KEYS))
                    return self._start_steam(game)
                self._launch(game, work)
                if detail:
                    GLib.timeout_add(600, lambda: (detail.update_options(), False)[1])

        alert.connect("response", on_response)
        alert.present(self)

    def _start_steam(self, game):
        notes = launcher.prepare(self.cfg)
        launcher.launch_steam(game.appid)
        sessions.watch(game, on_end=self._session_ended)
        return notes

    def _session_ended(self):
        def update():
            if self.stack.get_visible_child_name() == "stats":
                self.stats.refresh()
            self._sync_running()
            return False
        GLib.idle_add(update)
        self._sound_back(audio.restore(self.cfg.get("audio_after", "")))

    def _sound_back(self, title):
        """Called from any thread once the output has been moved back."""
        if not title:
            return

        def show():
            toast = Adw.Toast(title=escape(t(f"Sound is back on {title}", f"Sound's back on {title}")),
                              button_label=t("Change", "Nope, Elsewhere"), timeout=6)
            toast.connect("button-clicked", lambda *_: self.navigate("system"))
            self.toasts.add_toast(toast)
            return False
        GLib.idle_add(show)

    def stop(self, game):
        alert = Adw.AlertDialog(heading=t(f"Stop {game.name}?", f"Pull the plug on {game.name}?"),
                                body=t("The game is closed immediately. Anything you haven't saved will be lost.",
                                       "This kills it on the spot. Anything you didn't save is gone forever. "
                                       "Like tears in rain."))
        alert.add_response("cancel", t("Cancel", "Wait, No"))
        alert.add_response("stop", t("Stop App", "Kill It"))
        alert.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("cancel")
        alert.set_close_response("cancel")

        def on_response(_d, resp):
            if resp != "stop":
                return

            def work():
                launcher.stop(game)
                return audio.restore(self.cfg.get("audio_after", ""))

            def done(title):
                self.toast(t(f"{game.name} stopped", f"{game.name} has left the building"))
                self._sound_back(title)
                GLib.timeout_add(500, lambda: (self._sync_running(), False)[1])
            run_async(work, done, lambda e: self.toast(t(f"Couldn't stop {game.name}: {e}",
                                                         f"{game.name} won't die: {e}")))
        alert.connect("response", on_response)
        alert.present(self)

    def _launch(self, game, work, vr=True):
        self.toast(t(f"Starting {game.name}…", f"Booting up {game.name}…"))

        def done(notes):
            extra = f" · {' · '.join(notes)}" if notes else ""
            self.toast(t(f"Launched {game.name}{extra}", f"{game.name} is launching. Have fun!{extra}"))
            self.refresh_soon(1500)
            GLib.timeout_add(1500, lambda: (self._sync_running(), False)[1])
            if vr and self.cfg.get("vr_audio", True):
                run_async(audio.to_headset, lambda title: title and self.toast(
                    t(f"Sound now plays on {title}", f"Sound's now in your face: {title}")))

        run_async(work, done, lambda e: self.toast(t(f"Couldn't start {game.name}: {e}",
                                                     f"{game.name} refused to start: {e}")))

    # -- utilities

    def toast(self, text, timeout=4):
        self.toasts.add_toast(Adw.Toast(title=escape(text), timeout=timeout))

    def copy(self, text, msg=None):
        self.get_clipboard().set(text)
        self.toast(msg or t("Copied", "Copied!"))

    def _shine(self):
        # One sweep at a time and only while focused: an endless CSS animation
        # would keep redrawing the app behind a running VR game.
        if self.is_active():
            self.discord_btn.add_css_class("shine")
            GLib.timeout_add(1300, lambda: self.discord_btn.remove_css_class("shine"))
        return True

    def open_uri(self, uri):
        Gtk.UriLauncher.new(uri).launch(self, None, None)

    def open_path(self, path):
        if not os.path.exists(path):
            self.toast(t(f"{path} doesn't exist yet", f"{path} doesn't exist yet. Spooky."))
            return
        Gtk.FileLauncher.new(Gio.File.new_for_path(path)).launch(self, None, None)


class BifluxVRApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.restarting = False

    def restart(self):
        """Quit, and start again once the main loop is gone (see main())."""
        self.restarting = True
        self.quit()

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(HERE, "style.css"))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        for name, cb, accels in (
            ("quit", lambda *_: self.quit(), ["<primary>q"]),
            ("about", lambda *_: self.about(), []),
            ("refresh", lambda *_: self._win().refresh(force=True), ["<primary>r"]),
            ("search", lambda *_: self._search(), ["<primary>f"]),
        ):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)
        self.set_accels_for_action("window.close", ["<primary>w"])

    def _win(self):
        return self.props.active_window

    def _search(self):
        win = self._win()
        win.navigate("library")
        win.library.focus_search()

    def do_activate(self):
        win = self.props.active_window or MainWindow(self)
        size = os.environ.get("BIFLUXVR_SIZE")
        if size:
            win.set_default_size(*map(int, size.split("x")))
        win.present()
        if os.environ.get("BIFLUXVR_DROP"):
            win.drop.add_paths(os.environ["BIFLUXVR_DROP"].split(":"))
        shot = os.environ.get("BIFLUXVR_SCREENSHOT")
        if shot:
            GLib.timeout_add(int(os.environ.get("BIFLUXVR_SCREENSHOT_DELAY", "2500")),
                             lambda: (self._screenshot(win, shot), False)[1])

    def _screenshot(self, win, path):
        page = os.environ.get("BIFLUXVR_PAGE")
        if page and page != win.stack.get_visible_child_name():
            win.navigate(page)
            GLib.timeout_add(2000, lambda: (self._screenshot(win, path), False)[1])
            return
        game = os.environ.get("BIFLUXVR_GAME")
        if game and not getattr(self, "_opened", False):
            match = [g for g in win.library.games if g.appid == game]
            if match:
                self._opened = True
                win.library.open_game(match[0])
                GLib.timeout_add(1500, lambda: (self._screenshot(win, path), False)[1])
                return
        paintable = Gtk.WidgetPaintable.new(win)
        w, h = win.get_width(), win.get_height()
        snap = Gtk.Snapshot()
        paintable.snapshot(snap, w, h)
        node = snap.to_node()
        if node:
            win.get_renderer().render_texture(node, None).save_to_png(path)
        self.quit()

    def about(self):
        about = Adw.AboutDialog(
            application_name="BifluxVR", application_icon=APP_ID, version=__version__,
            developer_name="intramy", copyright="Made by intramy\nCopyright © 2026 intramy",
            license_type=Gtk.License.GPL_3_0_ONLY, website="https://github.com/Intramy/BifluxVR",
            issue_url="https://github.com/Intramy/BifluxVR/issues",
            comments=t("Play Windows VR games on Linux.\n\nBifluxVR orchestrates Proton, DXVK, VKD3D-Proton, "
                       "xrizer and WiVRn/Monado — the fastest translation path available on Linux today.",
                       "Windows VR games on Linux.\n\nHeld together with Proton, DXVK, VKD3D-Proton, xrizer, "
                       "WiVRn/Monado and a whole lot of hope. Somehow it's fast."),
        )
        about.set_developers(["intramy"])
        about.add_link("Discord", DISCORD_URL)
        about.add_credit_section(t("Built on", "Standing on the shoulders of"), [
            "Proton & Wine https://github.com/ValveSoftware/Proton",
            "xrizer https://github.com/Supreeeme/xrizer",
            "WiVRn https://github.com/WiVRn/WiVRn",
            "Monado https://monado.freedesktop.org",
            "umu-launcher https://github.com/Open-Wine-Components/umu-launcher",
        ])
        about.add_credit_section(t("Meta store downloads", "Meta downloads figured out by"), [
            "BSManager (Zagrios) https://github.com/Zagrios/bs-manager",
            "OculusGraphQLApiLib (ComputerElite) https://github.com/ComputerElite/OculusGraphQLApiLib",
        ])
        about.add_credit_section(t("App icon", "App icon"), ["Based on Tux by Larry Ewing, created with The GIMP"])
        about.add_credit_section(t("Compatibility data", "Compatibility data"), [
            "AreWeAntiCheatYet https://areweanticheatyet.com",
            "BeatMods https://beatmods.com",
            "ProtonDB https://www.protondb.com",
        ])
        about.present(self._win())


def main():
    GLib.set_application_name("BifluxVR")
    app = BifluxVRApp()
    code = app.run(sys.argv)
    if app.restarting:
        launcher = shutil.which("bifluxvr")
        if launcher:
            os.execv(launcher, [launcher])
        os.execv(sys.executable, [sys.executable, "-m", "bifluxvr"])
    return code
