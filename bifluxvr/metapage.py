# Copyright (C) 2026 intramy. GPL-3.0-only with additional terms: see LICENSE and NOTICE.
"""The Meta page: PC games bought on Meta's store, with what their files say
about running on Linux, and a download into the library."""

import os
import threading
import time
import uuid
from html import escape

from gi.repository import Adw, Gio, GLib, Gtk

from . import compat, config, installer, meta
from .voice import t
from .widgets import button, label, run_async

BADGES = {
    meta.OPENXR: ("success", lambda: t("Available for download", "Available for download")),
    meta.DRM: ("warning", lambda: t("DRM protected", "DRM protected")),
    meta.OCULUS: ("error", lambda: t("Oculus SDK only", "Oculus SDK only")),
    meta.UNKNOWN: ("dim-label", lambda: t("Unknown", "No idea")),
}


def explain(kind, gb):
    return {
        meta.OPENXR: t(f"Uses OpenXR, so it can talk to WiVRn · {gb:.1f} GB",
                       f"Speaks OpenXR, so WiVRn can take it from here · {gb:.1f} GB"),
        meta.DRM: t(f"Checks ownership through the Meta app, which can't run on Linux. It probably won't start · {gb:.1f} GB",
                    f"Asks the Meta app if you own it, and that app doesn't do Linux. Probably won't start · {gb:.1f} GB"),
        meta.OCULUS: t(f"Built on Meta's old Rift SDK. Nothing on Linux can run that yet · {gb:.1f} GB",
                       f"Uses Meta's old Rift SDK, and Linux has nothing that speaks it. Sorry · {gb:.1f} GB"),
        meta.UNKNOWN: t(f"No VR runtime found in its files. Might be a flat game · {gb:.1f} GB",
                        f"Couldn't spot any VR bits in it. Maybe it's flat? · {gb:.1f} GB"),
    }[kind]


class MetaPage(Adw.PreferencesPage):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.loaded = False
        self.jobs = {}  # app id → {"cancel", "done", "total", "row"}
        head = Adw.PreferencesGroup()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(label(t("Meta Library", "Meta Stash"), "large-title"))
        box.append(label(t("PC games you bought on Meta's store, downloaded straight from Meta into your library. "
                           "Quest-only games aren't listed: those run on the headset itself.",
                           "The PC games you bought from Meta, pulled straight into The Pile. "
                           "Quest-only stuff isn't here, that runs on the headset already."),
                         "page-subtitle", wrap=True))
        head.add(box)
        self.add(head)
        self.account = Adw.PreferencesGroup(title=t("Account", "Who are you"))
        self.add(self.account)
        self.games = Adw.PreferencesGroup(title=t("Your PC Games", "Your PC games"))
        self.games.set_header_suffix(button("", "flat", icon="view-refresh-symbolic", cb=self.refresh,
                                            tooltip=t("Check again", "Look again")))
        self.add(self.games)
        self.rows = {"account": [], "games": []}

    def shown(self):
        if not self.loaded:
            self.refresh()

    def _set(self, group, key, rows):
        for row in self.rows[key]:
            group.remove(row)
        self.rows[key] = rows
        for row in rows:
            group.add(row)

    def refresh(self):
        self.loaded = True
        signed_in = bool(meta.token())
        row = Adw.ActionRow(title=t("Signed in to Meta", "Signed in to Meta") if signed_in else
                            t("Not signed in", "Not signed in"))
        row.set_subtitle_lines(4)
        if signed_in:
            row.set_subtitle(t("The session is kept in a private file and only sent to Meta. Signing out here "
                               "deletes it; log out on oculus.com too to cancel it everywhere.",
                               "Your session lives in a private file and only ever goes back to Meta. Sign out "
                               "here deletes it, logging out on oculus.com kills it for good."))
            row.add_prefix(Gtk.Image(icon_name="avatar-default-symbolic"))
            row.add_suffix(button(t("Sign Out", "Sign Out"), "flat", cb=self.sign_out))
        else:
            row.set_subtitle(t("You sign in on Meta's own website, so BifluxVR never sees your password. "
                               "Then you give it the session token from that page.",
                               "You log in on Meta's own site, BifluxVR never sees your password. "
                               "Then you hand it the session token from that page."))
            row.add_prefix(Gtk.Image(icon_name="system-lock-screen-symbolic"))
            b = button(t("Sign In…", "Sign In…"), "suggested-action", cb=self.sign_in)
            b.set_valign(Gtk.Align.CENTER)
            row.add_suffix(b)
        self._set(self.account, "account", [row])
        if not signed_in:
            self._set(self.games, "games", [self._note(t("Sign in to see your games", "Sign in and your games show up here"))])
            return
        self._set(self.games, "games", [self._note(t("Asking Meta…", "Asking Meta what you own…"))])
        run_async(meta.owned, self._show, self._failed)

    def _note(self, text):
        row = Adw.ActionRow(title=escape(text))
        row.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
        return row

    def _failed(self, e):
        if isinstance(e, meta.LoginNeeded):
            meta.forget()
            self.win.toast(str(e))
            self.refresh()
            return
        self._set(self.games, "games", [self._note(t(f"Meta couldn't be reached: {e}", f"Meta isn't answering: {e}"))])

    def _show(self, result):
        games, _skipped = result
        if not games:
            self._set(self.games, "games", [self._note(t("No PC games on this account", "No PC games on this account"))])
            return
        rows = []
        for g in games:
            row = Adw.ActionRow(title=escape(g["name"]), subtitle=t("Reading its file list…", "Peeking at the files…"))
            row.set_subtitle_lines(3)
            row.add_prefix(Gtk.Image(icon_name="applications-games-symbolic"))
            row.game, row.extra = g, []
            rows.append(row)
            run_async(lambda g=g: meta.manifest(g), lambda m, row=row: self._fill(row, m),
                      lambda e, row=row: row.set_subtitle(escape(str(e))))
        self._set(self.games, "games", rows)

    # -- one game

    def _record(self, g):
        return self.win.cfg.get("meta_games", {}).get(g["id"])

    def _fill(self, row, m):
        g = row.game
        row.manifest = m
        for w in row.extra:
            row.remove(w)
        row.extra = []
        kind = meta.verdict(m)
        css, name = BADGES[kind]
        badge = label(name(), "caption", css)
        badge.set_valign(Gtk.Align.CENTER)
        sub = explain(kind, meta.size(m) / 1e9)
        rec = self._record(g)
        ids = {c["id"] for c in self.win.cfg["custom_games"]}
        installed = rec and rec.get("complete") and rec.get("custom_id") in ids and os.path.isfile(rec.get("exe", ""))
        job = self.jobs.get(g["id"])
        acts = []
        if job:
            sub = self._progress_text(job)
            acts.append(button(t("Stop", "Stop"), "flat", cb=lambda: job["cancel"].set()))
            job["row"] = row
        elif installed and rec.get("version_code") == g["version_code"]:
            if kind not in (meta.DRM, meta.OCULUS):
                badge = label(t("Installed", "In The Pile"), "caption", "success")
                badge.set_valign(Gtk.Align.CENTER)
            acts.append(button(t("Show", "Show Me"), cb=lambda: self.show_game(rec)))
        else:
            if installed:
                text = t("Update", "Update")
            elif rec and os.path.isdir(rec["dir"]):
                text = t("Resume", "Keep Going")
            else:
                text = t("Download", "Get It") if kind == meta.OPENXR else t("Try Anyway", "Try Anyway")
            acts.append(button(text, "suggested-action" if kind == meta.OPENXR else "flat",
                               cb=lambda: self.start(row)))
        row.set_subtitle(escape(sub))
        if kind != meta.OPENXR and not job and not installed and self.win.cfg.get("online", True):
            run_async(lambda: self._twin(g["name"]), lambda twin, row=row, sub=sub: self._show_twin(row, sub, twin))
        for w in [badge] + acts:
            w.set_valign(Gtk.Align.CENTER)
            row.add_suffix(w)
            row.extra.append(w)

    @staticmethod
    def _twin(name):
        twin = compat.steam_twin(name)
        if not twin:
            return None
        summary = compat.protondb(twin[0]) or {}
        return twin[0], twin[1], summary.get("tier", "")

    def _show_twin(self, row, sub, twin):
        if not twin or row.get_parent() is None:
            return
        appid, name, tier = twin
        rating = t(f", rated {tier.title()} on ProtonDB", f", ProtonDB says {tier.title()}") if tier else ""
        row.set_subtitle(escape(sub + "\n" + t(f"Also on Steam{rating}. The Steam version may run.",
                                               f"It's on Steam too{rating}. That one might run.")))
        link = button("", "flat", icon="adw-external-link-symbolic", tooltip=t(f"{name} on Steam", f"{name} on Steam"),
                      cb=lambda: self.win.open_uri(f"https://store.steampowered.com/app/{appid}/"))
        link.set_valign(Gtk.Align.CENTER)
        row.add_suffix(link)
        row.extra.append(link)

    def _notify(self, key, title, body):
        note = Gio.Notification.new(title)
        note.set_body(body)
        self.win.get_application().send_notification(f"meta-{key}", note)

    def busy(self):
        return bool(self.jobs)

    def stop_all(self):
        for job in self.jobs.values():
            job["cancel"].set()

    def _progress_text(self, job):
        if not job["total"]:
            return t("Starting…", "Warming up…")
        pct = job["done"] * 100 // job["total"]
        return t(f"Downloading {pct}% · {job['done'] / 1e9:.1f} of {job['total'] / 1e9:.1f} GB",
                 f"Downloading {pct}% · {job['done'] / 1e9:.1f} of {job['total'] / 1e9:.1f} GB")

    def start(self, row):
        g = row.game
        recs = self.win.cfg.setdefault("meta_games", {})
        rec = recs.get(g["id"])
        if not rec:
            rec = recs[g["id"]] = {"dir": installer._unique_dir(self.win.cfg["install_dir_vr"], g["name"]),
                                   "complete": False}
            config.save(self.win.cfg)
        job = self.jobs[g["id"]] = {"cancel": threading.Event(), "done": 0, "total": 0, "row": row, "shown": 0}
        self._fill(row, row.manifest)

        def progress(done, total):
            job["done"], job["total"] = done, total
            now = time.monotonic()
            if now - job["shown"] > 0.5:
                job["shown"] = now
                GLib.idle_add(lambda: (job["row"].set_subtitle(escape(self._progress_text(job))), False)[1])

        run_async(lambda: meta.download(g, rec["dir"], progress, job["cancel"]),
                  lambda exe: self._done(g, rec, exe), lambda e: self._stopped(g, e))

    def _stopped(self, g, e):
        job = self.jobs.pop(g["id"], None)
        if isinstance(e, meta.LoginNeeded):
            self._failed(e)
            return
        self.win.toast(str(e) if isinstance(e, meta.Stopped) else
                       t(f"{g['name']} download failed: {e}", f"{g['name']} download faceplanted: {e}"))
        if not isinstance(e, meta.Stopped):
            self._notify(g["id"], t(f"{g['name']} download failed", f"{g['name']} download faceplanted"),
                         t(f"{e}. Press Resume to continue where it stopped.", f"{e}. Hit Keep Going to pick it back up."))
        if job:
            self._fill(job["row"], job["row"].manifest)

    def _done(self, g, rec, exe):
        job = self.jobs.pop(g["id"], None)
        cfg = self.win.cfg
        entry = next((c for c in cfg["custom_games"] if c["id"] == rec.get("custom_id")), None)
        if entry:
            entry["exe"] = exe
        else:
            rec["custom_id"] = uuid.uuid4().hex[:12]
            cfg["custom_games"].append({"id": rec["custom_id"], "name": g["name"], "exe": exe, "mode": "vr"})
        rec.update(complete=True, version_code=g["version_code"], exe=exe)
        config.save(cfg)
        self.win.toast(t(f"{g['name']} is in your library", f"{g['name']} landed in The Pile"))
        self._notify(g["id"], t(f"{g['name']} is ready", f"{g['name']} is ready"),
                     t("Downloaded and added to your library.", "Downloaded and sitting in The Pile. Go play."))
        self.win.library.reload()
        if job:
            self._fill(job["row"], job["row"].manifest)

    def show_game(self, rec):
        self.win.navigate("library")
        key = f"custom:{rec.get('custom_id')}"
        game = next((g for g in self.win.library.games if g.appid == key), None)
        if game:
            self.win.library.open_game(game)

    # -- account

    def sign_in(self):
        steps = label(t("1. Press Open Meta and log in there.\n"
                        "2. Press F12 and open Storage (Firefox) or Application (Chrome), then Cookies.\n"
                        "3. Pick secure.oculus.com and copy the value of oc_ac_at. It starts with OC.\n"
                        "4. Paste it below.",
                        "1. Hit Open Meta and log in.\n"
                        "2. F12, then Storage (Firefox) or Application (Chrome), then Cookies.\n"
                        "3. Pick secure.oculus.com, copy the oc_ac_at value. Starts with OC.\n"
                        "4. Paste it here."), "caption-text", wrap=True)
        entry = Adw.PasswordEntryRow(title=t("Session token (oc_ac_at)", "Session token (oc_ac_at)"))
        lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lst.add_css_class("boxed-list")
        lst.append(entry)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(steps)
        opener = button(t("Open Meta", "Open Meta"), "pill", cb=lambda: self.win.open_uri(meta.LOGIN_URL))
        opener.set_halign(Gtk.Align.CENTER)
        box.append(opener)
        box.append(lst)
        box.append(label(t("Treat it like a password: anyone with it can act as your Meta account.",
                           "Guard it like a password. Whoever has it is you, as far as Meta cares."),
                         "caption-text", "warning", wrap=True))
        alert = Adw.AlertDialog(heading=t("Sign In to Meta", "Sign in to Meta"), extra_child=box)
        alert.add_response("cancel", t("Cancel", "Nevermind"))
        alert.add_response("save", t("Sign In", "Sign In"))
        alert.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        alert.set_default_response("save")

        def on_response(_d, resp):
            if resp != "save":
                return
            try:
                meta.save_token(entry.get_text())
            except meta.MetaError as e:
                self.win.toast(str(e))
                return
            self.refresh()
        alert.connect("response", on_response)
        alert.present(self.win)

    def sign_out(self):
        meta.forget()
        self.stop_all()
        self.win.toast(t("Signed out. Log out on oculus.com too to cancel the session.",
                         "Signed out. Log out on oculus.com too to kill the session for good."))
        self.refresh()
