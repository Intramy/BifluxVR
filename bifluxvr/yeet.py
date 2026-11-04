import os
import threading
import uuid
from html import escape

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import config, installer, setups, steam
from .voice import t
from .widgets import button, clear, label, run_async, scrolled


class QueueItem:
    def __init__(self, info):
        self.info = info
        self.state = "pending"
        self.pct = -1
        self.job = None
        self.how = None
        self.kind = None


# UI
class YeetPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.items = []
        self.found = []
        self.busy = False
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                       margin_top=24, margin_bottom=36, margin_start=28, margin_end=28)
        body.append(label(t("Drop Folder", "Yeet Zone"), "large-title"))
        body.append(label(t("Drop a game or app here: an archive, or an installer. Archives get unpacked, installers "
                            "get run, and the result lands in your library.",
                            "Throw a game or app at it: an archive or an installer. Archives get unpacked, "
                            "installers get run, and it all ends up in The Pile."), "page-subtitle", wrap=True))

        self.zone = Gtk.CenterBox(orientation=Gtk.Orientation.VERTICAL, margin_top=10)
        self.zone.add_css_class("dropzone")
        self.zone.set_size_request(-1, 240)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER)
        glyph = Gtk.Image(icon_name="folder-download-symbolic", pixel_size=56)
        glyph.add_css_class("drop-glyph")
        inner.append(glyph)
        inner.append(label(t("Drop files here", "Yeet it here"), "drop-title", xalign=0.5))
        inner.append(label(".zip   .rar   .7z   .exe   .msi", "caption-text", xalign=0.5))
        choose = button(t("Choose Files…", "Choose Files…"), "pill", cb=self.choose_files)
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

        self.queue_title = label(t("Ready to install", "Waiting in line"), "section-title")
        self.queue = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.queue.add_css_class("boxed-list")
        body.append(self.queue_title)
        body.append(self.queue)
        self.install_btn = button(t("Install", "Yeet It In"), "pill", "suggested-action", cb=self.install_all)
        self.install_btn.set_halign(Gtk.Align.END)
        self.install_btn.set_margin_top(6)
        body.append(self.install_btn)
        self.done_title = label(t("Installed", "Done"), "section-title")
        self.done = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.done.add_css_class("boxed-list")
        body.append(self.done_title)
        body.append(self.done)

        body.append(label(t("Find installers", "Find installers"), "section-title"))
        body.append(label(t("Looks through a folder on this PC for installers, including ones inside archives. "
                            "It doesn't download anything.",
                            "Digs through a folder on this PC for installers, even inside archives. "
                            "Nothing gets downloaded."), "caption-text", wrap=True))
        bar = Gtk.Box(spacing=8, margin_top=6)
        self.find = Gtk.SearchEntry(placeholder_text=t("Name of a game or app", "Name of a game or app"), hexpand=True)
        self.find.connect("search-changed", lambda *_: self._show_found())
        bar.append(self.find)
        self.scan_folder_btn = button("", "pill", cb=self.choose_scan_folder)
        bar.append(self.scan_folder_btn)
        self.scan_btn = button(t("Search", "Search"), "pill", "suggested-action", cb=self.scan)
        bar.append(self.scan_btn)
        body.append(bar)
        self.results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, margin_top=4, visible=False)
        self.results.add_css_class("boxed-list")
        body.append(self.results)

        setup = Adw.PreferencesGroup(title=t("Where things go", "Where things go"), margin_top=14)
        type_row = Adw.ActionRow(title=t("Games launch in", "Games launch in"))
        self.type_row = type_row
        self.mode = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        self.mode.add(Adw.Toggle(name="vr", label="VR"))
        self.mode.add(Adw.Toggle(name="simple", label="Simple"))
        self.mode.set_active_name(win.cfg.get("install_mode", "vr"))
        self.mode.connect("notify::active-name", lambda *_: self._mode_changed())
        type_row.add_suffix(self.mode)
        setup.add(type_row)
        self.loc_row = self._folder_row(setup, t("Unpacked games", "Unpacked games"), self._mode_key)
        self.setup_row = self._folder_row(setup, t("Installed by an installer", "Installed by an installer"),
                                          lambda: "install_dir_setup")
        body.append(setup)
        body.append(label(t("Installers run in their own Wine prefix inside that folder, so uninstalling leaves nothing "
                            "behind. They still run as your user, so only run ones you trust.",
                            "Each installer gets its own Wine prefix inside that folder, so uninstalling leaves "
                            "nothing behind. They still run as you, so only run ones you trust."), "caption-text", wrap=True))
        self.append(scrolled(body))
        self._mode_changed(save=False)
        self._scan_label()
        self._sync()

    def _folder_row(self, group, title, key):
        row = Adw.ActionRow(title=title)
        row.set_subtitle_selectable(True)
        change = Gtk.Button(label=t("Change…", "Change…"), valign=Gtk.Align.CENTER)
        change.connect("clicked", lambda *_: self.choose_folder(key, row))
        row.add_suffix(change)
        row.set_subtitle(escape(self.win.cfg[key()]))
        group.add(row)
        return row

    def _mode_key(self):
        return f"install_dir_{self.mode.get_active_name()}"

    def _mode_changed(self, save=True):
        mode = self.mode.get_active_name()
        if save:
            self.win.set_cfg("install_mode", mode)
        self.type_row.set_subtitle(
            t("Starts your headset stack and routes the game to VR", "Starts the headset stack and sends the game to VR")
            if mode == "vr" else t("Launches on your desktop, no VR setup", "Runs on your monitor, no headset"))
        self.loc_row.set_subtitle(escape(self.win.cfg[self._mode_key()]))

    def choose_folder(self, key, row):
        dialog = Gtk.FileDialog(title=t("Choose Folder", "Choose Folder"))
        current = self.win.cfg[key()]
        if os.path.isdir(current):
            dialog.set_initial_folder(Gio.File.new_for_path(current))

        def picked(d, res):
            try:
                folder = d.select_folder_finish(res)
            except GLib.Error:
                return
            if folder and folder.get_path():
                self.win.set_cfg(key(), folder.get_path())
                row.set_subtitle(escape(folder.get_path()))
        dialog.select_folder(self.win, None, picked)

    def _scan_label(self):
        self.scan_folder_btn.set_label(os.path.basename(self.win.cfg["scan_dir"].rstrip(os.sep)) or "/")
        self.scan_folder_btn.set_tooltip_text(self.win.cfg["scan_dir"])

    def choose_scan_folder(self):
        dialog = Gtk.FileDialog(title=t("Search In", "Search In"))
        if os.path.isdir(self.win.cfg["scan_dir"]):
            dialog.set_initial_folder(Gio.File.new_for_path(self.win.cfg["scan_dir"]))

        def picked(d, res):
            try:
                folder = d.select_folder_finish(res)
            except GLib.Error:
                return
            if folder and folder.get_path():
                self.win.set_cfg("scan_dir", folder.get_path())
                self._scan_label()
                self.scan()
        dialog.select_folder(self.win, None, picked)

    def scan(self):
        folder = self.win.cfg["scan_dir"]
        if not os.path.isdir(folder):
            self.win.toast(t(f"{folder} doesn't exist", f"{folder} doesn't exist"))
            return
        self.scan_btn.set_sensitive(False)
        self.scan_btn.set_label(t("Searching…", "Searching…"))
        skip = [self.win.cfg[k] for k in ("install_dir_vr", "install_dir_simple", "install_dir_setup")]

        def done(found):
            self.found = found
            self.scan_btn.set_sensitive(True)
            self.scan_btn.set_label(t("Search", "Search"))
            self._show_found()

        def fail(e):
            self.scan_btn.set_sensitive(True)
            self.scan_btn.set_label(t("Search", "Search"))
            self.win.toast(str(e))
        run_async(lambda: setups.scan(folder, skip), done, fail)

    def _show_found(self):
        self.results.remove_all()
        words = self.find.get_text().lower().split()
        shown = [i for i in self.found
                 if all(w in f"{i.name} {os.path.basename(i.path)}".lower() for w in words)]
        for info in shown[:60]:
            inside = "" if info.setup == os.path.basename(info.path) else f" · {t('contains', 'contains')} {os.path.basename(info.setup)}"
            row = Adw.ActionRow(title=escape(info.name),
                                subtitle=escape(f"{os.path.basename(info.path)} · {installer.human(info.size)}{inside}"))
            add = button(t("Add", "Add"), "pill", cb=lambda i=info: self._add_item(i))
            add.set_valign(Gtk.Align.CENTER)
            row.add_suffix(add)
            self.results.append(row)
        if self.found and not shown:
            self.results.append(Adw.ActionRow(title=t("No match", "No match"),
                                              subtitle=t("Nothing in that folder has that name",
                                                         "Nothing in that folder has that name")))
        elif not self.found:
            self.results.append(Adw.ActionRow(title=t("No installers found", "No installers in there"),
                                              subtitle=escape(self.win.cfg["scan_dir"])))
        self.results.set_visible(True)

    def _on_enter(self, *_):
        self.zone.add_css_class("hover")
        return Gdk.DragAction.COPY

    def _on_drop(self, _target, value, _x, _y):
        self.zone.remove_css_class("hover")
        self.add_paths([f.get_path() for f in value.get_files() if f.get_path()])
        return True

    def choose_files(self):
        dialog = Gtk.FileDialog(title=t("Choose Archives or Installers", "Choose Archives or Installers"))
        flt = Gtk.FileFilter(name=t("Archives and installers", "Archives and installers"))
        for ext in installer.ARCHIVE_EXTS + installer.SETUP_EXTS:
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
        usable = [p for p in paths if p and (installer.supported(p) or installer.runnable(p))]
        if len(usable) < len(paths):
            self.win.toast(t("Only .zip, .rar, .7z, .exe and .msi files work here",
                             "Only .zip, .rar, .7z, .exe and .msi files work here"))
        picked = []
        for p in usable:
            if (installer.volume_number(p) or 1) > 1:
                p = installer.first_volume(p)
                if not p:
                    self.win.toast(t("Part 1 of that multi-part archive is missing",
                                     "Part 1 of that multi-part archive is missing"))
                    continue
            if p not in picked:
                picked.append(p)
        for p in picked:
            if installer.runnable(p):
                self._add_item(installer.inspect_setup(p))
            else:
                run_async(lambda p=p: installer.inspect(p), self._add_item, lambda e: self.win.toast(str(e)))

    def _add_item(self, info):
        if any(i.info.path == info.path for i in self.items):
            return
        item = QueueItem(info)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("queue-item")
        top = Gtk.Box(spacing=8)
        item.name = Gtk.Entry(text=info.name, hexpand=True)
        item.name.add_css_class("flat")
        item.name.add_css_class("queue-name")
        top.append(item.name)
        item.status = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        top.append(item.status)
        box.append(top)
        item.note = label(self._note(info), "caption-text", wrap=True)
        box.append(item.note)
        item.options = Gtk.Box(spacing=8)
        item.kind = Adw.ToggleGroup()
        item.kind.add(Adw.Toggle(name="game", label=t("Game", "Game")))
        item.kind.add(Adw.Toggle(name="app", label=t("App", "App")))
        item.kind.set_active_name("game")
        item.options.append(item.kind)
        if info.setup and not installer.runnable(info.path):
            item.how = Adw.ToggleGroup()
            item.how.add(Adw.Toggle(name="setup", label=t("Run installer", "Run installer")))
            item.how.add(Adw.Toggle(name="unpack", label=t("Just unpack", "Just unpack")))
            item.how.set_active_name("setup")
            item.options.append(item.how)
        box.append(item.options)
        item.row = Gtk.ListBoxRow(child=box, activatable=False)
        self._idle_status(item)
        self.items.append(item)
        self.queue.append(item.row)
        self._sync()

    def _idle_status(self, item):
        clear(item.status)
        item.status.append(button("", "flat", icon="window-close-symbolic", tooltip=t("Remove", "Remove"),
                                  cb=lambda: self.remove(item)))

    def _note(self, info, extra=""):
        base = os.path.basename(info.path)
        size = installer.human(info.size) if info.size else ""
        if info.setup == base:
            what = t("installer", "installer")
        elif info.setup:
            what = t(f"contains {os.path.basename(info.setup)}", f"contains {os.path.basename(info.setup)}")
        elif info.exes:
            n = len(info.exes)
            what = t(f"{n} executable{'s' if n > 1 else ''}", f"{n} .exe file{'s' if n > 1 else ''}")
        else:
            what = t("no .exe found", "no .exe found")
        return " · ".join(filter(None, (base, size, what, extra)))

    def remove(self, item):
        if item.state == "installing":
            return
        self.queue.remove(item.row)
        self.items.remove(item)
        self._sync()

    def _sync(self):
        pending = [i for i in self.items if i.state in ("pending", "failed")]
        self.queue_title.set_visible(bool(self.items))
        self.queue.set_visible(bool(self.items))
        self.install_btn.set_visible(bool(pending))
        self.install_btn.set_sensitive(not self.busy)
        n = len(pending)
        self.install_btn.set_label(t(f"Install {n} Item{'s' if n != 1 else ''}", f"Yeet {n} In"))
        has_done = self.done.get_first_child() is not None
        self.done_title.set_visible(has_done)
        self.done.set_visible(has_done)

    def install_all(self):
        if self.busy:
            return
        jobs = []
        for item in self.items:
            if item.state not in ("pending", "failed"):
                continue
            item.state = "installing"
            item.job = setups.Job()
            item.name.set_sensitive(False)
            item.options.set_visible(False)
            item.row.remove_css_class("failed")
            clear(item.status)
            item.status.append(Adw.Spinner())
            item.pct_label = label(t("Waiting", "Waiting"), "caption-text")
            item.status.append(item.pct_label)
            uses_setup = installer.runnable(item.info.path) or (item.how and item.how.get_active_name() == "setup")
            jobs.append((item, item.name.get_text().strip() or item.info.name,
                         item.kind.get_active_name(), uses_setup))
        if not jobs:
            return
        self.busy = True
        self._sync()
        cfg = self.win.cfg
        game_mode = self.mode.get_active_name()
        protons = steam.proton_builds()
        proton = cfg.get("proton") or (protons[0][1] if protons else "")

        def progress(item, fraction):
            pct = int(fraction * 100)
            if pct != item.pct:
                item.pct = pct
                GLib.idle_add(lambda: (item.pct_label.set_label(t(f"Unpacking {pct}%", f"Unpacking {pct}%")), False)[1])

        def work():
            for item, name, kind, uses_setup in jobs:
                mode = "simple" if kind == "app" else game_mode
                try:
                    if uses_setup:
                        root, exe, prefix, apis = setups.install(
                            item.info, cfg["install_dir_setup"], name, proton, item.job,
                            lambda f, item=item: progress(item, f),
                            lambda item=item: GLib.idle_add(self._running, item))
                    else:
                        root, exe, apis = installer.install(item.info.path, cfg[f"install_dir_{mode}"], name,
                                                            lambda f, item=item: progress(item, f))
                        prefix = ""
                    GLib.idle_add(self._installed, item, name, kind, mode, root, exe, prefix, apis)
                except setups.Cancelled:
                    GLib.idle_add(self._cancelled, item)
                except Exception as e:
                    GLib.idle_add(self._failed, item, str(e))
            GLib.idle_add(self._finished)

        threading.Thread(target=work, daemon=True).start()

    def _running(self, item):
        clear(item.status)
        item.note.set_label(t("The installer's window opens in a moment. Follow it to the end, and this "
                              "continues when it closes. The first run takes a minute while Wine sets up.",
                              "The installer's window opens in a moment. Click through it and this picks up "
                              "when it closes. The first run takes a minute while Wine sets up."))
        finished = button(t("I'm Finished", "I'm Finished"), "pill",
                          tooltip=t("Use this if the installer or program is still open after installing",
                                    "Use this if the installer or program is still open after installing"),
                          cb=lambda: setattr(item.job, "finish", True))
        cancel = button(t("Cancel", "Cancel"), "pill", "destructive-action", cb=lambda: setattr(item.job, "cancel", True))
        item.status.append(finished)
        item.status.append(cancel)
        return False

    def _installed(self, item, name, kind, mode, root, exe, prefix, apis):
        gid = uuid.uuid4().hex[:12]
        entry = {"id": gid, "name": name, "exe": exe, "mode": mode, "kind": kind, "root": root}
        if prefix:
            entry["prefix"] = prefix
        self.win.cfg["custom_games"].append(entry)
        config.save(self.win.cfg)
        self.queue.remove(item.row)
        self.items.remove(item)
        game = steam.Game(f"custom:{gid}", name, os.path.dirname(exe), "", "", list(apis), [], True, exe, mode,
                          prefix=prefix, kind=kind, root=root)
        if kind == "game" and mode == "vr" and not apis:
            note = t("No VR runtime found in the files. It may only run on a virtual screen",
                     "No VR runtime in the files. It'll probably run on a virtual screen")
        elif kind == "game" and mode == "simple" and apis:
            note = t("This game supports VR. Switch it to VR on its library page",
                     "This one supports VR. Switch it to VR on its page")
        else:
            note = t(f"Starts {os.path.basename(exe)}. Change it on its library page if that's wrong",
                     f"Starts {os.path.basename(exe)}. Change it on its page if that's wrong")
        row = Adw.ActionRow(title=escape(name), subtitle=escape(note))
        row.set_subtitle_lines(2)
        show = button("", "flat", icon="folder-open-symbolic", tooltip=t("Show files", "Show files"),
                      cb=lambda: self.win.open_path(root))
        show.set_valign(Gtk.Align.CENTER)
        go = button(t("Open", "Open") if kind == "app" else t("Play", "Play"), "pill", "suggested-action",
                    cb=lambda: self.win.play(game))
        go.set_valign(Gtk.Align.CENTER)
        row.add_suffix(show)
        row.add_suffix(go)
        self.done.append(row)
        self.win.toast(t(f"{name} installed", f"{name} is in"))
        return False

    def _reset(self, item):
        item.state = "failed"
        item.name.set_sensitive(True)
        item.options.set_visible(True)
        self._idle_status(item)

    def _cancelled(self, item):
        self._reset(item)
        item.state = "pending"
        item.note.set_label(self._note(item.info))
        self.win.toast(t("Install cancelled", "Cancelled"))
        return False

    def _failed(self, item, msg):
        self._reset(item)
        item.row.add_css_class("failed")
        item.note.set_label(msg)
        self.win.toast(msg)
        return False

    def _finished(self):
        self.busy = False
        self._sync()
        self.win.library.reload()
        return False
