import os
import uuid
from html import escape

from gi.repository import Adw, Gio, GLib, Gtk

from . import config, riftlift, storage
from .voice import t
from .widgets import button, label, run_async


def pile_entry(cfg, app_id):
    return next((c for c in cfg["custom_games"] if c.get("engine") == "riftlift" and c.get("riftlift") == app_id), None)


def sync_pile(cfg, games):
    changed = False
    for g in games:
        inst = g.get("install") or {}
        if not inst.get("complete") or inst.get("source") == "steam":
            continue
        exe = inst.get("executable", "")
        if exe and not os.path.isabs(exe):
            exe = os.path.join(inst.get("directory", ""), exe)
        entry = pile_entry(cfg, g["id"])
        if entry is None:
            cfg["custom_games"].append({"id": uuid.uuid4().hex[:12], "name": g["name"], "exe": exe, "mode": "vr",
                                        "kind": "game", "engine": "riftlift", "riftlift": g["id"]})
            changed = True
        elif entry.get("exe") != exe:
            entry["exe"] = exe
            changed = True
    ids = {g["id"] for g in games if (g.get("install") or {}).get("complete")}
    keep = [c for c in cfg["custom_games"] if c.get("engine") != "riftlift" or c.get("riftlift") in ids]
    if len(keep) != len(cfg["custom_games"]):
        cfg["custom_games"] = keep
        changed = True
    return changed


class RiftLiftSection:
    def __init__(self, win, page):
        self.win, self.page = win, page
        self.jobs = {}
        self.signin = None
        self.engine = Adw.PreferencesGroup(
            title=t("RiftLift engine", "RiftLift engine"),
            description=t("Runs Rift games, including Oculus SDK and Meta-platform ones, on your OpenXR runtime. "
                          "Ownership is checked live with Meta for the signed-in account: it's compatibility "
                          "software, not a DRM bypass. Made by Villagers654, GPL-3.0.",
                          "Runs Rift games (yes, the Oculus SDK and Meta-platform ones too) on WiVRn. Meta still "
                          "checks you own them, live. Compatibility, not piracy. Made by Villagers654, GPL-3.0."))
        self.accounts = Adw.PreferencesGroup(title=t("Meta accounts", "Meta accounts"))
        self.library = Adw.PreferencesGroup(title=t("Rift library", "Rift library"))
        self.library.set_header_suffix(button("", "flat", icon="view-refresh-symbolic", cb=self.refresh,
                                              tooltip=t("Check again", "Look again")))
        self.adding = Adw.PreferencesGroup(title=t("Add games", "Add more"))
        for g in (self.engine, self.accounts, self.library, self.adding):
            page.add(g)
        self.rows = {g: [] for g in (self.engine, self.accounts, self.library, self.adding)}
        self._build_adding()

    def _set(self, group, rows):
        for r in self.rows[group]:
            group.remove(r)
        self.rows[group] = rows
        for r in rows:
            group.add(r)

    def _note(self, text, icon="dialog-information-symbolic"):
        row = Adw.ActionRow(title=escape(text))
        row.set_title_lines(3)
        row.add_prefix(Gtk.Image(icon_name=icon))
        return row

    def refresh(self):
        ready = riftlift.installed()
        for g in (self.accounts, self.library, self.adding):
            g.set_visible(ready)
        if not ready:
            row = Adw.ActionRow(title=t("Not installed", "Not installed yet"),
                                subtitle=t(f"Downloads RiftLift {riftlift.VERSION} from its GitHub release "
                                           "(about 12 MB) and checks it against a pinned checksum.",
                                           f"Grabs RiftLift {riftlift.VERSION} from GitHub (about 12 MB) and makes "
                                           "sure it's the real file before using it."))
            row.set_subtitle_lines(3)
            row.add_prefix(Gtk.Image(icon_name="system-software-install-symbolic"))
            self.install_btn = button(t("Install", "Get It"), "suggested-action", cb=self.install_engine)
            self.install_btn.set_valign(Gtk.Align.CENTER)
            row.add_suffix(self.install_btn)
            self.engine_row = row
            self._set(self.engine, [row])
            return
        row = Adw.ActionRow(title=t(f"RiftLift {riftlift.VERSION}", f"RiftLift {riftlift.VERSION}"),
                            subtitle=t("Installed. Games go to your VR games folder.",
                                       "Ready. Games land in your VR folder."))
        row.add_prefix(Gtk.Image(icon_name="object-select-symbolic"))
        row.add_suffix(button("", "flat", icon="web-browser-symbolic", tooltip=t("Project page", "Project page"),
                              cb=lambda: self.win.open_uri(riftlift.HOME_PAGE)))
        self._set(self.engine, [row])
        self._set(self.accounts, [self._note(t("Loading…", "Loading…"))])
        self._set(self.library, [self._note(t("Asking RiftLift…", "Asking RiftLift…"))])
        run_async(riftlift.accounts, self._show_accounts,
                  lambda e: self._set(self.accounts, [self._note(str(e), "dialog-warning-symbolic")]))
        run_async(riftlift.library, self._show_library,
                  lambda e: self._set(self.library, [self._note(str(e), "dialog-warning-symbolic")]))

    def install_engine(self):
        self.install_btn.set_sensitive(False)

        def progress(f):
            GLib.idle_add(lambda: self.engine_row.set_subtitle(t(f"Downloading… {int(f * 100)}%",
                                                                 f"Downloading… {int(f * 100)}%")) and False)

        def done(_path):
            self.win.toast(t("RiftLift installed", "RiftLift is in"))
            self.refresh()

        def fail(e):
            self.install_btn.set_sensitive(True)
            self.engine_row.set_subtitle(escape(str(e)))
        run_async(lambda: riftlift.install(progress), done, fail)

    def _show_accounts(self, accounts):
        rows = []
        for a in accounts:
            state = (t("Needs signing in again", "Needs a fresh sign-in") if a.get("needsReconnect") else
                     t("Signed in", "Signed in") if a.get("signedIn") else t("Signed out", "Signed out"))
            tags = [state] + ([t("active", "active")] if a.get("active") else []) + \
                   ([t("primary", "primary")] if a.get("primary") else [])
            row = Adw.ActionRow(title=escape(a.get("name") or a.get("alias") or a["key"]), subtitle=" · ".join(tags))
            row.add_prefix(Gtk.Image(icon_name="avatar-default-symbolic"))
            if not a.get("active"):
                use = button(t("Use", "Use"), "flat", cb=lambda k=a["key"]: self._account("use", k))
                use.set_valign(Gtk.Align.CENTER)
                row.add_suffix(use)
            menu = Gio.Menu()
            for action, text in (("primary", t("Make primary", "Make primary")),
                                 ("sign-out", t("Sign out", "Sign out")),
                                 ("remove", t("Remove", "Remove"))):
                menu.append(text, f"rift.{action}::{a['key']}")
            more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, valign=Gtk.Align.CENTER)
            more.add_css_class("flat")
            row.add_suffix(more)
            rows.append(row)
        if len(accounts) < riftlift.MAX_ACCOUNTS:
            add = Adw.ButtonRow(title=t("Add a Meta account…", "Add a Meta account…"),
                                start_icon_name="list-add-symbolic")
            add.connect("activated", lambda *_: self.sign_in())
            rows.append(add)
        self._set(self.accounts, rows)
        group = Gio.SimpleActionGroup()
        for action in ("primary", "sign-out", "remove", "use"):
            act = Gio.SimpleAction.new(action, GLib.VariantType.new("s"))
            act.connect("activate", lambda a, key: self._account(a.get_name(), key.get_string()))
            group.add_action(act)
        self.accounts.insert_action_group("rift", group)

    def _account(self, action, key):
        run_async(lambda: riftlift.account_action(action, key), lambda _r: self.refresh(),
                  lambda e: self.win.toast(str(e)))

    def sign_in(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        steps = label(t("1. Press Open Meta and sign in there.\n2. Press Continue. When the browser asks to open "
                        "the link with BifluxVR Meta sign-in, allow it. This window finishes by itself.\n\n"
                        "If nothing happens: open the browser console before Continue (Firefox: Ctrl+Shift+K), "
                        "right-click the oculus:// message, choose Copy Message and paste it below. A token or "
                        "password won't work.",
                        "1. Hit Open Meta and log in.\n2. Hit Continue. If the browser asks to open it with BifluxVR "
                        "Meta sign-in, say yes. Done, this window handles the rest.\n\n"
                        "Nothing happened? Open the browser console before Continue (Firefox: Ctrl+Shift+K), "
                        "right-click the oculus:// message, Copy Message, paste it here. Tokens won't work."),
                      wrap=True)
        box.append(steps)
        opener = Gtk.Button(label=t("Getting the link…", "Getting the link…"), sensitive=False, halign=Gtk.Align.START)
        opener.add_css_class("pill")
        box.append(opener)
        entry = Gtk.Entry(placeholder_text="oculus://…")
        box.append(entry)
        hint = label("", wrap=True)
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        box.append(hint)
        dialog = Adw.AlertDialog(heading=t("Add a Meta Account", "Add a Meta Account"), extra_child=box)
        dialog.add_response("cancel", t("Cancel", "Cancel"))
        dialog.add_response("done", t("Sign In", "Sign In"))
        dialog.set_response_appearance("done", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")
        dialog.set_response_enabled("done", False)
        session = {"s": None}

        def check(*_):
            text = entry.get_text().strip()
            ok = bool(riftlift.address_in(text))
            dialog.set_response_enabled("done", ok and session["s"] is not None)
            hint.set_label("" if ok or not text else
                           t("That's not an oculus:// address. RiftLift needs the address from the browser console, "
                             "not a token.",
                             "No oculus:// address in there. Needs the console message, not a token."))
        entry.connect("changed", check)

        def got_link(url):
            opener.set_label(t("Open Meta", "Open Meta"))
            opener.set_sensitive(True)
            opener.connect("clicked", lambda *_: self.win.open_uri(url))
            if riftlift.take_link():
                session["link"] = True
                session["poll"] = GLib.timeout_add(400, poll)
            check()

        def poll():
            address = riftlift.take_address()
            if address and riftlift.address_in(address):
                session["poll"] = None
                entry.set_text(address)
                submit(address)
                dialog.close()
                return False
            return True

        def stop():
            if session.get("poll"):
                GLib.source_remove(session["poll"])
                session["poll"] = None
            if session.pop("link", False):
                riftlift.give_back_link()

        def failed(e):
            stop()
            dialog.close()
            self.win.toast(str(e))

        def start():
            session["s"] = riftlift.SignIn()
            return session["s"].link()
        run_async(start, got_link, failed)

        def submit(text):
            s = session["s"]
            session["sent"] = True
            stop()
            self.win.toast(t("Signing in…", "Signing in…"))
            run_async(lambda: s.finish(text),
                      lambda _r: (self.win.toast(t("Account added", "Account added")), self.refresh()),
                      lambda e: self.win.toast(str(e)))

        def on_response(d, resp):
            s = session["s"]
            if session.get("sent"):
                return
            if resp == "done" and s and riftlift.address_in(entry.get_text()):
                submit(entry.get_text())
            else:
                stop()
                if s:
                    s.cancel()

        dialog.connect("response", on_response)
        dialog.present(self.win)

    def _show_library(self, games):
        if sync_pile(self.win.cfg, games):
            config.save(self.win.cfg)
            self.win.library.reload()
        if not games:
            self._set(self.library, [self._note(t("No games yet. Add an account, or add games below.",
                                                  "Nothing here yet. Add an account or some games below."))])
            return
        self._set(self.library, [self._game_row(g) for g in games])

    def _game_row(self, g):
        inst = g.get("install") or {}
        status = g.get("status", "")
        bits = [status]
        if inst.get("version"):
            bits.append(inst["version"])
        if inst.get("bytes"):
            bits.append(storage.human(inst["bytes"]))
        if g.get("owners"):
            bits.append(t("owned by ", "owned by ") + ", ".join(g["owners"]))
        row = Adw.ActionRow(title=escape(g["name"]), subtitle=escape(" · ".join(b for b in bits if b)))
        row.set_subtitle_lines(2)
        row.add_prefix(Gtk.Image(icon_name="applications-games-symbolic"))
        row.game = g
        if g["id"] in self.jobs:
            self._busy(row)
        elif status == "installed":
            play = button(t("Play", "Play"), "pill", "suggested-action", cb=lambda: self.play(g))
            play.set_valign(Gtk.Align.CENTER)
            row.add_suffix(play)
        elif status in ("not installed", "paused", "needs attention"):
            get = button(t("Install", "Grab It") if status == "not installed" else t("Resume", "Resume"),
                         "pill", cb=lambda: self.download(row))
            get.set_valign(Gtk.Align.CENTER)
            row.add_suffix(get)
        row.add_suffix(self._game_menu(g))
        return row

    def _game_menu(self, g):
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        items = [(t("Versions…", "Versions…"), lambda: self.versions(g))]
        if g.get("install"):
            items += [(t("Last log", "Last log"), lambda: self.show_text(g, riftlift.logs, t("Log", "Log"))),
                      (t("Debug report", "Debug report"), lambda: self.show_text(g, riftlift.report,
                                                                                 t("Report", "Report"))),
                      (t("Uninstall", "Uninstall"), lambda: self.uninstall(g))]
        for text, cb in items:
            b = Gtk.Button(label=text)
            b.add_css_class("flat")
            b.connect("clicked", lambda _b, cb=cb: (pop.popdown(), cb()))
            box.append(b)
        pop.set_child(box)
        more = Gtk.MenuButton(icon_name="view-more-symbolic", popover=pop, valign=Gtk.Align.CENTER)
        more.add_css_class("flat")
        return more

    def _busy(self, row):
        job = self.jobs[row.game["id"]]
        stop = button(t("Cancel", "Abort"), "pill", "destructive-action",
                      cb=lambda: run_async(lambda: job.cancel(row.game["id"])))
        stop.set_valign(Gtk.Align.CENTER)
        row.add_suffix(stop)

    def download(self, row, build=None):
        g = row.game
        job = self.jobs[g["id"]] = riftlift.Job()
        folder = self.win.cfg.get("install_dir_vr")
        os.makedirs(folder, exist_ok=True)
        self.refresh_rows()

        def progress(f):
            GLib.idle_add(lambda: self._progress(g["id"], f) and False)

        def done(_r):
            self.jobs.pop(g["id"], None)
            self.win.toast(t(f"{g['name']} installed", f"{g['name']} is in The Pile"))
            self.refresh()

        def fail(e):
            self.jobs.pop(g["id"], None)
            if str(e) != "cancelled":
                self.win.toast(t(f"Couldn't install {g['name']}: {e}", f"{g['name']} didn't install: {e}"))
            self.refresh()
        run_async(lambda: riftlift.download(g["id"], folder, build, progress, job), done, fail)

    def refresh_rows(self):
        games = [r.game for r in self.rows[self.library] if hasattr(r, "game")]
        if games:
            self._set(self.library, [self._game_row(g) for g in games])

    def _progress(self, app_id, f):
        for r in self.rows[self.library]:
            if getattr(r, "game", {}).get("id") == app_id:
                r.set_subtitle(t(f"Downloading… {int(f * 100)}%", f"Downloading… {int(f * 100)}%"))

    def play(self, g):
        run_async(riftlift.library, lambda games: self._play_after(g, games), lambda e: self.win.toast(str(e)))

    def _play_after(self, g, games):
        if sync_pile(self.win.cfg, games):
            config.save(self.win.cfg)
            self.win.library.reload()
        entry = pile_entry(self.win.cfg, g["id"])
        game = next((x for x in self.win.library.games if entry and x.appid == f"custom:{entry['id']}"), None)
        if game is None:
            self.win.toast(t("Give the library a second to catch up, then press Play again",
                             "The Pile is still catching up. Hit Play again in a sec"))
            return
        self.win.play(game)

    def versions(self, g):
        def show(builds):
            if not builds:
                self.win.toast(t("No other versions offered", "No other versions on offer"))
                return
            dialog = Adw.AlertDialog(heading=t(f"{g['name']} versions", f"{g['name']} versions"),
                                     body=t("Installs that version next to the others, in its own folder.",
                                            "Puts that version next to the others, in its own folder."))
            dialog.add_response("cancel", t("Cancel", "Cancel"))
            for i, b in enumerate(builds[:8]):
                chan = ", ".join(b.get("channels") or [])
                text = f"{b.get('version', '?')}" + (f" ({chan})" if chan else "") + \
                       (t(" · installed", " · installed") if b.get("installed") else "")
                dialog.add_response(str(i), text)
                dialog.set_response_enabled(str(i), not b.get("installed"))
            dialog.set_close_response("cancel")

            def on_response(_d, resp):
                if resp != "cancel":
                    row = next((r for r in self.rows[self.library] if getattr(r, "game", {}).get("id") == g["id"]),
                               None)
                    if row:
                        self.download(row, builds[int(resp)].get("version"))
            dialog.connect("response", on_response)
            dialog.present(self.win)
        run_async(lambda: riftlift.builds(g["id"]), show, lambda e: self.win.toast(str(e)))

    def show_text(self, g, fetch, title):
        def show(text):
            view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
            view.get_buffer().set_text(text or t("Nothing yet", "Nothing yet"))
            scroller = Gtk.ScrolledWindow(child=view, min_content_height=360, min_content_width=560)
            dialog = Adw.AlertDialog(heading=f"{g['name']} · {title}", extra_child=scroller)
            dialog.add_response("copy", t("Copy", "Copy"))
            dialog.add_response("close", t("Close", "Close"))
            dialog.set_close_response("close")
            dialog.connect("response", lambda _d, r: r == "copy" and self.win.copy(text, t("Copied", "Copied")))
            dialog.present(self.win)
        run_async(lambda: fetch(g["id"]), show, lambda e: self.win.toast(str(e)))

    def uninstall(self, g):
        dialog = Adw.AlertDialog(heading=t(f"Uninstall {g['name']}?", f"Uninstall {g['name']}?"),
                                 body=t("RiftLift deletes the files it downloaded. Games you added from elsewhere "
                                        "keep their files.",
                                        "RiftLift deletes what it downloaded. Stuff you added from elsewhere keeps "
                                        "its files."))
        dialog.add_response("cancel", t("Cancel", "Cancel"))
        dialog.add_response("go", t("Uninstall", "Uninstall"))
        dialog.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: r == "go" and run_async(
            lambda: riftlift.uninstall(g["id"]), lambda _r: self.refresh(), lambda e: self.win.toast(str(e))))
        dialog.present(self.win)

    def _build_adding(self):
        exe = Adw.ButtonRow(title=t("Add a Rift game on this PC…", "Add a Rift game on this PC…"),
                            start_icon_name="document-open-symbolic")
        exe.connect("activated", lambda *_: self.add_exe())
        steam_row = Adw.ButtonRow(title=t("Add Steam games with an Oculus mode", "Add Steam games with an Oculus mode"),
                                  start_icon_name="applications-games-symbolic")
        steam_row.connect("activated", lambda *_: self._add(riftlift.add_steam, t("Steam games added",
                                                                                 "Steam games added")))
        link = Adw.ButtonRow(title=t("Add a cross-buy game by store link…", "Add a cross-buy game by store link…"),
                             start_icon_name="insert-link-symbolic")
        link.connect("activated", lambda *_: self.add_link())
        self._set(self.adding, [exe, steam_row, link])

    def _add(self, fn, note):
        run_async(fn, lambda _r: (self.win.toast(note), self.refresh()), lambda e: self.win.toast(str(e)))

    def add_exe(self):
        dialog = Gtk.FileDialog(title=t("Choose the Game's .exe", "Which .exe?"))
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
                self._add(lambda: riftlift.add_exe(path), t("Game added", "Game added"))
        dialog.open(self.win, None, picked)

    def add_link(self):
        entry = Gtk.Entry(placeholder_text="https://www.meta.com/experiences/…")
        dialog = Adw.AlertDialog(heading=t("Add by Store Link", "Add by Store Link"),
                                 body=t("For cross-buy games Meta doesn't list in your PC library. Your account "
                                        "still has to own it.",
                                        "For cross-buy games Meta hides from your PC list. You still have to own it."),
                                 extra_child=entry)
        dialog.add_response("cancel", t("Cancel", "Cancel"))
        dialog.add_response("add", t("Add", "Add"))
        dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: r == "add" and entry.get_text().strip() and self._add(
            lambda: riftlift.add_link(entry.get_text().strip()), t("Game added", "Game added")))
        dialog.present(self.win)
