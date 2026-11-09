import os
import time
from html import escape

from gi.repository import Adw, Gio, GLib, Gtk

from . import __version__, anticheat, beatsaber, bindings, compat, config, health, mods, perf, selfupdate, steam
from .voice import t
from .widgets import button, label, run_async

RATINGS = (("works", "Works", "Works"), ("issues", "Has issues", "Kinda"), ("broken", "Broken", "Nope"))
TIER_NAMES = {"platinum": "Platinum", "gold": "Gold", "silver": "Silver", "bronze": "Bronze", "borked": "Borked"}


def suffix_button(text, *classes, icon=None, cb=None, tooltip=None):
    b = button(text, *classes, icon=icon, cb=cb, tooltip=tooltip)
    b.set_valign(Gtk.Align.CENTER)
    return b


def busy(btn, on=True):
    btn.set_sensitive(not on)
    if on:
        btn.set_child(Adw.Spinner())
    return btn


class HealthGroup(Adw.PreferencesGroup):
    def __init__(self, win, detail):
        super().__init__(title=t("Health Check", "Will it run?"),
                         description=t("Everything that decides whether this game starts in VR.",
                                       "Everything this game needs to start."))
        self.win, self.detail = win, detail
        self.rows = []
        self.set_header_suffix(button("", "flat", icon="view-refresh-symbolic", cb=self.reload,
                                      tooltip=t("Check again", "Check Again")))
        self.reload()

    @property
    def game(self):
        return self.detail.game

    def reload(self):
        run_async(lambda: health.check_game(self.game, self.win.cfg), self._show,
                  lambda e: self.win.toast(t(f"Health check failed: {e}", f"The health check failed: {e}")))

    def _show(self, checks):
        for row in self.rows:
            self.remove(row)
        self.rows = [self.win.check_row(c, self.fix) for c in checks]
        for row in self.rows:
            self.add(row)

    def reload_soon(self, ms=800):
        GLib.timeout_add(ms, lambda: (self.reload(), False)[1])

    def fix(self, check, btn):
        game = self.game
        if check.id == "openvr":
            self.win.apply_fix(check, btn)
            self.reload_soon(1500)
        elif check.id == "game-files":
            self.win.open_uri(f"steam://validate/{game.appid}")
        elif check.id == "game-log":
            self.win.open_path(health.game_log(game))
        elif check.id == "game-options":
            self.detail.apply_options(True)
        elif check.id == "game-ac":
            appid = game.store_id or game.appid
            self.win.get_clipboard().set(anticheat.request_text(game, anticheat.lookup(game, False)))
            self.win.open_uri(f"https://steamcommunity.com/app/{appid}/discussions/0/")
            self.win.toast(t("A polite request is on your clipboard. Start a new discussion and paste it.",
                             "Request copied. Start a new discussion and paste it."))
        elif check.id == "game-dll":
            btn.set_sensitive(False)
            run_async(lambda: mods.sync_launch_options(game),
                      lambda _: (self.win.toast(t("Mod DLLs added to the launch options",
                                                  "Mod DLLs are in. They'll load now.")), self.reload()),
                      lambda e: (self.win.toast(str(e)), btn.set_sensitive(True)))


def protondb_row(win, game):
    row = Adw.ActionRow(title="ProtonDB", subtitle=t("Looking it up…", "Asking the internet…"))
    row.add_prefix(Gtk.Image(icon_name="emblem-default-symbolic"))
    appid = game.store_id
    if not win.cfg.get("online", True):
        row.set_subtitle(t("Online lookups are off in Settings", "You turned the internet bits off in Settings"))
        return row
    if not appid:
        row.set_subtitle(t("Not matched to a Steam game", "Can't tell which Steam game this is"))
        return row
    row.set_activatable(True)
    row.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
    row.connect("activated", lambda *_: win.open_uri(f"https://www.protondb.com/app/{appid}"))

    def show(summary):
        if not summary or summary.get("tier") not in TIER_NAMES:
            row.set_subtitle(t("No reports yet", "Nobody's reported on it yet. Be the first?"))
            return
        tier = summary["tier"]
        badge = label(TIER_NAMES[tier], "tier-badge", f"tier-{tier}", xalign=0.5)
        badge.set_valign(Gtk.Align.CENTER)
        row.add_suffix(badge)
        n = summary.get("total", 0)
        trend = summary.get("trending")
        extra = (t(f" · trending {TIER_NAMES[trend]}", f" · lately more like {TIER_NAMES[trend]}")
                 if trend in TIER_NAMES and trend != tier else "")
        row.set_subtitle(t(f"{n} reports · {summary.get('confidence', '')} confidence{extra}",
                           f"{n} people chimed in{extra}"))

    run_async(lambda: compat.protondb(appid), show,
              lambda e: row.set_subtitle(t("ProtonDB couldn't be reached", "ProtonDB isn't answering")))
    return row


def perf_row(win, game):
    row = Adw.ExpanderRow(title=t("Expected frame rate", "How smooth will it be?"),
                          subtitle=t("Estimating…", "Estimating…"))
    row.add_prefix(Gtk.Image(icon_name="power-profile-performance-symbolic"))
    appid = game.store_id
    if not win.cfg.get("online", True):
        row.set_subtitle(t("Needs online lookups, which are off in Settings",
                           "Needs the internet bits, which you turned off in Settings"))
        row.set_enable_expansion(False)
        return row
    if not appid:
        row.set_subtitle(t("Not matched to a Steam game, so its requirements are unknown",
                           "Can't tell which Steam game this is, so no idea what it needs"))
        row.set_enable_expansion(False)
        return row

    def show(est):
        if not est:
            row.set_subtitle(t("Its store page doesn't name a graphics card to compare with",
                               "The store page doesn't say what GPU it wants. "))
            row.set_enable_expansion(False)
            return
        if "unknown_gpu" in est:
            row.set_subtitle(t(f"Your GPU ({est['unknown_gpu']}) isn't in BifluxVR's benchmark table yet",
                               f"Don't know your {est['unknown_gpu']}, so no estimate."))
            row.set_enable_expansion(False)
            return
        fps = (f"{est['low']}–{est['high']} fps" if est["low"] != est["high"] else f"{est['fps']} fps")
        badge = label(f"~{est['fps']} fps", "fps-badge", f"fps-{est['state']}", xalign=0.5)
        badge.set_valign(Gtk.Align.CENTER)
        row.add_suffix(badge)
        kind = t("measured and estimated", "based on your sessions") if est["measured"] \
            else t("estimate", "estimate")
        verdict = {"ok": t("smooth", "smooth"), "warn": t("some dropped frames", "a bit choppy"),
                   "fail": t("well below the refresh rate", "rough, lower the settings")}[est["state"]]
        row.set_subtitle(t(f"{fps} of {est['refresh']} Hz · {verdict} · {kind}",
                           f"{fps} of {est['refresh']} Hz · {verdict} · {kind}"))
        for title, text in est["lines"]:
            line = Adw.ActionRow(title=escape(title), subtitle=escape(text))
            line.set_subtitle_lines(3)
            row.add_row(line)
        if not est["measured"]:
            note = Adw.ActionRow(title=t("How sure is this?", "How sure am I?"),
                                 subtitle=t("Roughly ±25%. Many VR games list the same minimum card whether "
                                            "they are light or heavy, so play it once: BifluxVR measures the "
                                            "GPU load and corrects the estimate.",
                                            "Give or take 25%. Lots of VR games list the same minimum GPU "
                                            "whether they're light or heavy. Play it once and I'll measure the "
                                            "real thing."))
            note.set_subtitle_lines(4)
            row.add_row(note)

    run_async(lambda: perf.estimate(game, appid), show,
              lambda e: row.set_subtitle(t("The Steam store couldn't be reached", "Steam's store isn't answering")))
    return row


def rating_row(win, game):
    row = Adw.ActionRow(title=t("Your rating", "Your verdict"),
                        subtitle=t("How it ran for you, for your own reference",
                                   "How did it go? Only you will see this."))
    row.add_prefix(Gtk.Image(icon_name="face-smile-symbolic"))
    group = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
    for name, serious, funny in RATINGS:
        group.add(Adw.Toggle(name=name, label=t(serious, funny)))
    current = win.cfg.get("ratings", {}).get(game.appid)
    if current:
        group.set_active_name(current)
    else:
        group.set_active(Gtk.INVALID_LIST_POSITION)

    def changed(g, _p):
        name = g.get_active_name()
        if name:
            win.cfg.setdefault("ratings", {})[game.appid] = name
            config.save(win.cfg)
    group.connect("notify::active-name", changed)
    row.add_suffix(group)
    return row


def store_row(win, game, detail):
    row = Adw.ActionRow(title=t("Steam match", "Steam twin"))
    row.set_subtitle(t(f"Steam app {game.store_id}", f"Looks like Steam app {game.store_id}") if game.store_id
                     else t("None. Art and ratings need one", "None yet. No art or ratings without one"))
    row.add_prefix(Gtk.Image(icon_name="edit-find-symbolic"))
    row.add_suffix(suffix_button(t("Change…", "Fix It…"), cb=lambda: ask_store_id(win, game, detail)))
    return row


def ask_store_id(win, game, detail):
    entry = Adw.EntryRow(title=t("Steam app ID", "Steam app ID"), text=game.store_id)
    entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
    lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    lst.add_css_class("boxed-list")
    lst.append(entry)
    alert = Adw.AlertDialog(heading=t("Match to a Steam Game", "Which Steam game is it?"),
                            body=t("The number in the game's store URL. Leave empty to clear.",
                                   "It's the number in the game's store page URL. Empty means no match."),
                            extra_child=lst)
    alert.add_response("cancel", t("Cancel", "Cancel"))
    alert.add_response("save", t("Save", "Save"))
    alert.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

    def on_response(_d, resp):
        if resp != "save":
            return
        value = "".join(c for c in entry.get_text() if c.isdigit())
        detail.update_entry(store_id=value, store_id_manual=True)
        win.toast(t("Steam match saved", "Got it. Reloading the art."))
    alert.connect("response", on_response)
    alert.present(win)


class ModsGroup(Adw.PreferencesGroup):
    def __init__(self, win, detail):
        super().__init__(title=escape(t("Mods & VR Fixes", "Mods & VR fixes")),
                         description=t("Installed mods can be rolled back at any time. Replaced files are kept.",
                                       "Every mod can be rolled back. The originals are kept."))
        self.win, self.detail = win, detail
        self.rows = []
        self.unreal = False
        self.beat_saber = False
        self.set_header_suffix(button(t("Install…", "Add Mod…"), "flat", icon="list-add-symbolic",
                                      cb=self.pick_archive,
                                      tooltip=t("Install a mod from a .zip or .rar", "Install a mod .zip or .rar")))
        run_async(lambda: (mods.is_unreal(self.game), bool(beatsaber.game_root(self.game))), self._loaded)

    @property
    def game(self):
        return self.detail.game

    def _loaded(self, found):
        self.unreal, self.beat_saber = found
        self.rebuild()

    def _add(self, row):
        self.add(row)
        self.rows.append(row)

    def rebuild(self):
        for row in self.rows:
            self.remove(row)
        self.rows = []
        game = self.game
        installed = mods.installed(game)
        for mod in installed:
            if self.beat_saber and mod.get("source", "").startswith(beatsaber.SOURCE):
                continue
            n = len(mod["added"]) + len(mod["replaced"])
            when = time.strftime("%-d %b", time.localtime(mod["installed"]))
            sub = t(f"{n} files · installed {when}", f"{n} files · since {when}")
            if mod.get("dll"):
                sub += f" · {mod['dll']}"
            row = Adw.ActionRow(title=escape(mod["name"]), subtitle=escape(sub))
            row.add_prefix(Gtk.Image(icon_name="application-x-addon-symbolic"))
            row.add_suffix(suffix_button(t("Roll Back", "Undo It"), "destructive-action",
                                         cb=lambda m=mod: self.confirm_rollback(m)))
            self._add(row)

        asset = mods.reframework_asset(game)
        if asset and not any(m["name"].startswith("REFramework") for m in installed):
            row = Adw.ActionRow(title="REFramework VR",
                                subtitle=t("praydog's mod with full 6DoF VR for this game",
                                           "praydog's mod that turns this game into proper VR. "))
            row.add_prefix(Gtk.Image(icon_name="starred-symbolic"))
            btn = suffix_button(t("Install", "Get It"), "suggested-action")
            btn.connect("clicked", lambda b: self.install_reframework(b))
            row.add_suffix(btn)
            self._add(row)

        if self.beat_saber:
            self._add(self._beatmods_row())

        if self.unreal and not game.is_vr:
            self._add(self._uevr_row())

        if not self.rows:
            row = Adw.ActionRow(title=t("No mods installed", "No mods yet"),
                                subtitle=t("Use Install… to add one from an archive",
                                           "Hit Add Mod… and give it a .zip or .rar"))
            row.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
            self._add(row)

    def _beatmods_row(self):
        row = Adw.ActionRow(title=t("Custom Songs (BeatMods)", "Custom songs"),
                            subtitle=t("Checking BeatMods…", "Asking BeatMods what fits…"))
        row.set_subtitle_lines(3)
        row.add_prefix(Gtk.Image(icon_name="audio-x-generic-symbolic"))
        run_async(lambda: beatsaber.status(self.game), lambda st: self._beatmods_fill(row, st),
                  lambda e: row.set_subtitle(escape(t(f"Couldn't reach BeatMods: {e}", f"BeatMods isn't answering: {e}"))))
        return row

    def _beatmods_fill(self, row, st):
        have = ", ".join(m["name"] for m in st["installed"])
        ver = st["version"] or "?"
        if st["state"] == "unsupported":
            sub = t(f"No mods for Beat Saber {ver} on BeatMods yet. They usually follow a game update within days.",
                    f"BeatMods has nothing for {ver} yet. Modders need a few days after each update.")
        elif st["state"] == "current":
            sub = t(f"{have} · up to date for {ver}", f"{have}. All fresh for {ver}.")
        elif st["state"] == "update":
            sub = t(f"A matching set for Beat Saber {ver} is ready. Old or hand-installed copies are replaced.",
                    f"Got a fresh set matched to {ver}. Old copies get swapped out, even hand-installed ones.")
        else:
            sub = t(f"BSIPA and SongCore with their libraries, matched to Beat Saber {ver}. "
                    "Songs go in Beat Saber_Data/CustomLevels.",
                    f"BSIPA + SongCore and friends, picked for {ver}. Songs go in Beat Saber_Data/CustomLevels.")
        row.set_subtitle(escape(sub))
        if st["installed"]:
            row.add_suffix(suffix_button("", "flat", icon="user-trash-symbolic", cb=self.remove_beatmods,
                                         tooltip=t("Roll back all BeatMods mods", "Undo all of them")))
        if st["state"] in ("install", "update"):
            btn = suffix_button(t("Install", "Get Them") if st["state"] == "install" else t("Update", "Update"),
                                "suggested-action")
            btn.connect("clicked", lambda b: self.install_beatmods(b))
            row.add_suffix(btn)

    def _uevr_row(self):
        injector = mods.uevr_path()
        row = Adw.ActionRow(title="UEVR",
                            subtitle=t("Universal VR mod for Unreal Engine games. Start the game, then run the "
                                       "injector and attach it. Needs the .NET 6 Desktop Runtime in the game's "
                                       "prefix (protontricks: dotnetdesktop6).",
                                       "Makes flat Unreal games VR. Start the game, run the injector, attach. "
                                       "It needs .NET 6 Desktop in the game's prefix (protontricks: dotnetdesktop6). "
                                       "Experimental on Linux."))
        row.set_subtitle_lines(4)
        row.add_prefix(Gtk.Image(icon_name="applications-engineering-symbolic"))
        if injector:
            row.add_suffix(suffix_button("", "flat", icon="user-trash-symbolic", cb=self.remove_uevr,
                                         tooltip=t("Delete the downloaded UEVR", "Delete UEVR")))
            row.add_suffix(suffix_button(t("Run Injector", "Inject"), "suggested-action", cb=self.run_uevr))
        else:
            btn = suffix_button(t("Download", "Download"))
            btn.connect("clicked", lambda b: self.download_uevr(b))
            row.add_suffix(btn)
        return row


    def _changed(self, msg):
        self.win.toast(msg)
        self.rebuild()
        self.detail.health.reload()

    def _sync_steam(self):
        if self.game.custom:
            return ""
        try:
            mods.sync_launch_options(self.game)
        except RuntimeError:
            return t(" Close Steam, then press Apply in the health check to load it.",
                     " Close Steam and hit Do It in the health check so it loads.")
        return ""

    def confirm_rollback(self, mod):
        alert = Adw.AlertDialog(heading=t(f"Roll Back {mod['name']}?", f"Undo {mod['name']}?"),
                                body=t("Its files are removed and the originals it replaced are put back.",
                                       "Its files go, the originals come back. "))
        alert.add_response("cancel", t("Cancel", "Cancel"))
        alert.add_response("undo", t("Roll Back", "Undo It"))
        alert.set_response_appearance("undo", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, resp):
            if resp != "undo":
                return

            def work():
                mods.rollback(self.game, mod["id"])
                return self._sync_steam() if mod.get("dll") else ""
            run_async(work, lambda note: self._changed(t(f"{mod['name']} rolled back.", f"{mod['name']} is gone.") + note),
                      lambda e: self.win.toast(t(f"Rollback failed: {e}", f"Couldn't undo it: {e}")))
        alert.connect("response", on_response)
        alert.present(self.win)

    def install_reframework(self, btn):
        busy(btn)

        def work():
            rec = mods.install_reframework(self.game)
            return rec, self._sync_steam()
        run_async(work, lambda r: self._changed(t(f"{r[0]['name']} installed.", f"{r[0]['name']} is in.") + r[1]),
                  lambda e: (self.win.toast(t(f"Install failed: {e}", f"That didn't work: {e}")), self.rebuild()))

    def install_beatmods(self, btn):
        busy(btn)

        def work():
            recs = beatsaber.install(self.game)
            return recs, self._sync_steam()
        run_async(work, lambda r: self._changed(t(f"{len(r[0])} mods installed.", f"{len(r[0])} mods in. ") + r[1]),
                  lambda e: (self.win.toast(t(f"Install failed: {e}", f"That didn't work: {e}")), self.rebuild()))

    def remove_beatmods(self):
        alert = Adw.AlertDialog(heading=t("Roll Back All BeatMods Mods?", "Remove all mods?"),
                                body=t("BSIPA, SongCore and their libraries are removed. Your songs stay.",
                                       "The mods go, your songs stay put."))
        alert.add_response("cancel", t("Cancel", "Cancel"))
        alert.add_response("undo", t("Roll Back", "Undo It"))
        alert.set_response_appearance("undo", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, resp):
            if resp != "undo":
                return

            def work():
                beatsaber.remove(self.game)
                return self._sync_steam()
            run_async(work, lambda note: self._changed(t("BeatMods mods rolled back.", "Mods removed.") + note),
                      lambda e: self.win.toast(t(f"Rollback failed: {e}", f"Couldn't undo it: {e}")))
        alert.connect("response", on_response)
        alert.present(self.win)

    def download_uevr(self, btn):
        busy(btn)
        run_async(mods.install_uevr, lambda _: self._changed(t("UEVR downloaded", "UEVR is ready to inject")),
                  lambda e: (self.win.toast(t(f"Download failed: {e}", f"Download failed: {e}")), self.rebuild()))

    def remove_uevr(self):
        mods.remove_uevr()
        self._changed(t("UEVR deleted", "UEVR is gone"))

    def run_uevr(self):
        protons = steam.proton_builds()
        proton = self.win.cfg.get("proton") or (protons[0][1] if protons else "")
        run_async(lambda: mods.run_in_prefix(self.game, mods.uevr_path(), proton),
                  lambda _: self.win.toast(t("Injector starting. Pick the game's process and press Inject.",
                                             "Injector is opening. Pick the game and press Inject.")),
                  lambda e: self.win.toast(str(e)))

    def pick_archive(self):
        dialog = Gtk.FileDialog(title=t("Choose a Mod Archive", "Which mod?"))
        flt = Gtk.FileFilter(name=t("Mod archives", "Mod archives (.zip, .rar)"))
        for ext in ("zip", "rar"):
            flt.add_suffix(ext)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        dialog.set_filters(store)

        def picked(d, res):
            try:
                path = d.open_finish(res).get_path()
            except GLib.Error:
                return
            if path:
                self.confirm_archive(path)
        dialog.open(self.win, None, picked)

    def confirm_archive(self, path):
        name = Adw.EntryRow(title=t("Name", "What's it called?"), text=os.path.splitext(os.path.basename(path))[0])
        dll = Adw.EntryRow(title=t("DLL overrides (optional, e.g. dinput8=n,b)",
                                   "DLL overrides, if the mod says so (e.g. dinput8=n,b)"))
        lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lst.add_css_class("boxed-list")
        lst.append(name)
        lst.append(dll)
        target = mods.game_dir(self.game)
        alert = Adw.AlertDialog(heading=t("Install Mod", "Install this mod?"),
                                body=t(f"Files are copied into {target}. Anything they replace is backed up.",
                                       f"It goes into {target}. Anything it overwrites gets backed up first."),
                                extra_child=lst)
        alert.add_response("cancel", t("Cancel", "Cancel"))
        alert.add_response("install", t("Install", "Do It"))
        alert.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, resp):
            if resp != "install":
                return
            title = name.get_text().strip() or os.path.basename(path)
            overrides = "".join(dll.get_text().split())

            def work():
                rec = mods.install_archive(self.game, path, title, dll=overrides)
                return rec, self._sync_steam() if overrides else ""
            self.win.toast(t(f"Installing {title}…", f"Installing {title}…"), timeout=2)
            run_async(work, lambda r: self._changed(t(f"{title} installed.", f"{title} is in.") + r[1]),
                      lambda e: self.win.toast(t(f"Install failed: {e}", f"That didn't work: {e}")))
        alert.connect("response", on_response)
        alert.present(self.win)


class ControlsGroup(Adw.PreferencesGroup):
    def __init__(self, win, detail):
        super().__init__(title=t("Controls", "Button remapping"),
                         description=t("Remaps the game's own Touch and Index bindings. Turn everything off to "
                                       "restore the originals.",
                                       "Swap buttons around. Turn it all off and the game's own setup comes back."))
        self.win, self.detail = win, detail
        self.manifest = ""
        self.switches = {}
        self.updating = False
        self.set_visible(False)
        if "openvr" in detail.game.vr_apis:
            run_async(lambda: bindings.find_manifest(detail.game.install_dir), self._found)

    def _found(self, manifest):
        self.manifest = manifest
        self.set_visible(True)
        if not manifest:
            row = Adw.ActionRow(title=t("Can't remap this game", "No remapping for this one"),
                                subtitle=t("It uses the legacy OpenVR input API, so its buttons are fixed. "
                                           "Check the game's own settings.",
                                           "It uses old-school OpenVR input with hard-wired buttons. "
                                           "Try the game's own settings."))
            row.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
            self.add(row)
            return
        current = set(bindings.active(self.detail.game))
        for key, (serious, funny, _) in bindings.SWAPS.items():
            row = Adw.SwitchRow(title=t(serious, funny), active=key in current)
            row.connect("notify::active", lambda *_: self.updating or self.apply())
            self.switches[key] = row
            self.add(row)

    def apply(self):
        swaps = [k for k, row in self.switches.items() if row.get_active()]
        game = self.detail.game

        def undo(e):
            self.win.toast(t(f"Couldn't change the bindings: {e}", f"Couldn't remap the buttons: {e}"))
            self.updating = True
            current = set(bindings.active(game))
            for k, row in self.switches.items():
                row.set_active(k in current)
            self.updating = False
        run_async(lambda: bindings.apply(game, self.manifest, swaps),
                  lambda _: self.win.toast(t("Controls updated. Restart the game to use them.",
                                             "Buttons remapped. Restart the game.")
                                           if swaps else t("Original controls restored", "Original buttons are back")),
                  undo)


class UpdateGroup(Adw.PreferencesGroup):
    def __init__(self, win):
        super().__init__(title=t("BifluxVR Updates", "Updates"))
        self.win, self.btn = win, None
        self.row = Adw.ActionRow(title=f"BifluxVR {__version__}")
        self.row.add_prefix(Gtk.Image(icon_name="software-update-available-symbolic"))
        self.add(self.row)
        self.show()

    def show(self):
        if self.row.get_activatable():
            self.row.disconnect_by_func(self._open_notes)
            self.row.set_activatable(False)
        if self.btn:
            self.row.remove(self.btn)
        rel, state = selfupdate.found, selfupdate.state
        if state == "installed":
            self.row.set_subtitle(t(f"Version {rel['version']} is installed. Restart to use it.",
                                    f"{rel['version']} is in. Restart to use it."))
            self.btn = suffix_button(t("Restart", "Restart"), "suggested-action", cb=self.win.get_application().restart)
        elif rel:
            self.row.set_subtitle(t(f"Version {rel['version']} is available. Click for what's new.",
                                    f"{rel['version']} is out. Click to see what changed."))
            self.row.set_activatable(True)
            self.row.connect("activated", self._open_notes)
            self.btn = suffix_button(t("Update", "Download"), "suggested-action", cb=self.install)
        else:
            self.row.set_subtitle(t("Up to date", "Up to date") if state == "checked"
                                  else t("Not checked yet", "Not checked yet"))
            self.btn = suffix_button(t("Check for Updates", "Check Now"), cb=self.check)
        self.row.add_suffix(self.btn)
        if hasattr(self.win, "overview"):
            self.win.overview.show_update()

    def _open_notes(self, *_):
        self.win.open_uri(selfupdate.found["url"])

    def check(self):
        busy(self.btn)
        run_async(selfupdate.check, lambda _: self.show(),
                  lambda e: (self.win.toast(t(f"Couldn't check for updates: {e}", f"Update check failed: {e}")),
                             self.show()))

    def install(self):
        busy(self.btn)
        rel = selfupdate.found

        def progress(p):
            GLib.idle_add(self.row.set_subtitle, t(f"Downloading… {p:.0%}", f"Downloading… {p:.0%}"))
        run_async(lambda: selfupdate.install(rel, progress), lambda _: self.show(),
                  lambda e: (self.win.toast(t(f"Update failed: {e}", f"Update failed: {e}")), self.show()))
