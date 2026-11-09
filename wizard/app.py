import os
import subprocess
import sys
import threading
from html import escape

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from bifluxvr import config, tripwire
from bifluxvr.widgets import run_async

from . import packages, report, runner, system

APP_ID = "io.github.intramy.BifluxVR.Installer"
HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(packages.SOURCE, "data", "io.github.intramy.BifluxVR.svg")
LAUNCHER = os.path.join(packages.LOCAL_BIN, "bifluxvr")

STEPS = [("welcome", "Welcome"), ("system", "Your system"), ("plan", "What gets installed"),
         ("install", "Installing"), ("settings", "Make it yours")]
BADGES = {"install": ("Install", "badge-install"), "update": ("Update", "badge-update"),
          "skip": ("Up to date", "badge-skip"), "fail": ("Can't install", "badge-fail")}


def label(text="", *classes, wrap=False, xalign=0.0, markup=False):
    w = Gtk.Label(xalign=xalign)
    w.set_markup(text) if markup else w.set_label(text)
    for c in classes:
        w.add_css_class(c)
    if wrap:
        w.set_wrap(True)
        w.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    return w


def page(title, subtitle, *children):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                  margin_top=36, margin_bottom=24, margin_start=40, margin_end=40)
    box.append(label(title, "page-title", wrap=True))
    if subtitle:
        box.append(label(subtitle, "page-subtitle", wrap=True))
    for c in children:
        box.append(c)
    clamp = Adw.Clamp(maximum_size=720, child=box)
    return Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)


# UI
class Installer(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="BifluxVR Setup", default_width=980, default_height=700)
        self.add_css_class("bifluxvr-setup")
        self.set_size_request(360, 520)
        self.info = None
        self.plan = None
        self.run = None
        self.failed = None
        self.cfg = config.load()

        root = Gtk.Box()
        root.append(self._sidebar())
        main = Adw.ToolbarView(hexpand=True)
        header = Adw.HeaderBar(show_title=False)
        main.add_top_bar(header)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT,
                               transition_duration=320, vexpand=True)
        main.set_content(self.stack)
        bar = Gtk.Box(spacing=10, margin_top=12, margin_bottom=16, margin_start=24, margin_end=24)
        bar.add_css_class("nav-bar")
        self.back = Gtk.Button(label="Back")
        self.back.add_css_class("pill")
        self.back.connect("clicked", lambda *_: self.go(-1))
        self.next = Gtk.Button(label="Next")
        self.next.add_css_class("pill")
        self.next.add_css_class("suggested-action")
        self.next.connect("clicked", lambda *_: self.go(1))
        bar.append(self.back)
        bar.append(Gtk.Box(hexpand=True))
        bar.append(self.next)
        main.add_bottom_bar(bar)
        root.append(main)
        self.set_content(root)

        self.stack.add_named(self._welcome(), "welcome")
        self.system_bin = Adw.Bin()
        self.stack.add_named(self.system_bin, "system")
        self.stack.add_named(self._plan_page(), "plan")
        self.stack.add_named(self._install_page(), "install")
        self.stack.add_named(self._settings_page(), "settings")
        self._apply_theme()
        self.show_step("welcome")
        run_async(system.detect, self._detected, self._detect_failed)


    def _sidebar(self):
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, width_request=250)
        side.add_css_class("setup-sidebar")
        logo = Gtk.Image.new_from_file(ICON) if os.path.exists(ICON) else Gtk.Image(icon_name="applications-games")
        logo.set_pixel_size(64)
        logo.set_halign(Gtk.Align.START)
        logo.set_margin_bottom(6)
        side.append(logo)
        side.append(label("BifluxVR", "brand"))
        side.append(label("Setup", "brand-sub"))
        steps = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=28)
        self.step_rows = {}
        for i, (name, title) in enumerate(STEPS, 1):
            row = Gtk.Box(spacing=12)
            row.add_css_class("step")
            num = label(str(i), "step-num", xalign=0.5)
            num.set_size_request(28, 28)
            num.set_valign(Gtk.Align.CENTER)
            row.append(num)
            row.append(label(title, "step-title"))
            steps.append(row)
            self.step_rows[name] = (row, num)
        side.append(steps)
        side.append(Gtk.Box(vexpand=True))
        side.append(label("Made by intramy", "made-by"))
        return side

    def show_step(self, name):
        self.stack.set_visible_child_name(name)
        names = [n for n, _ in STEPS]
        here = names.index(name)
        for i, n in enumerate(names):
            row, num = self.step_rows[n]
            for c in ("current", "done"):
                row.remove_css_class(c)
            if i < here:
                row.add_css_class("done")
                num.set_label("✓")
            else:
                num.set_label(str(i + 1))
                if i == here:
                    row.add_css_class("current")
        self.back.set_visible(name in ("system", "plan"))
        self.next.get_parent().set_visible(name != "settings")
        self.next.set_label({"plan": "Install", "install": "Continue"}.get(name, "Next"))
        self.next.set_sensitive({"welcome": self.info is not None, "plan": self.plan is not None,
                                 "install": self.failed is not None and "bifluxvr" not in self.failed}.get(name, True))

    def go(self, delta):
        names = [n for n, _ in STEPS]
        here = names.index(self.stack.get_visible_child_name())
        target = names[here + delta]
        if names[here] == "system" and delta > 0 and self.report_switch.get_active():
            run_async(lambda: report.start(self.info), lambda r: setattr(self, "run", r))
        if target == "install":
            self.start_install()
        self.show_step(target)


    def _welcome(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, valign=Gtk.Align.CENTER,
                      margin_start=40, margin_end=40)
        art = Gtk.Image.new_from_file(ICON) if os.path.exists(ICON) else Gtk.Image(icon_name="applications-games")
        art.set_pixel_size(128)
        art.add_css_class("hero-icon")
        box.append(art)
        box.append(label("Welcome to BifluxVR", "hero-title", xalign=0.5, wrap=True))
        box.append(label("Play Windows VR games on Linux. This installer sets up BifluxVR and everything "
                         "it needs, then gets out of your way.", "hero-sub", xalign=0.5, wrap=True))
        self.scan = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER, margin_top=18)
        self.scan.add_css_class("scan-pill")
        self.scan_spin = Adw.Spinner()
        self.scan.append(self.scan_spin)
        self.scan_text = label("Looking at your system…")
        self.scan.append(self.scan_text)
        box.append(self.scan)
        return box

    def _detected(self, info):
        self.info = info
        self.scan_spin.set_visible(False)
        self.scan_text.set_label(f"{info['os']} · {info['distro']} · ready to go")
        self.scan.add_css_class("ok")
        self.system_bin.set_child(self._system_page(info))
        self.show_step(self.stack.get_visible_child_name())
        run_async(lambda: packages.plan(info["manager"]), self._planned,
                  lambda e: self._planned(None, str(e)))

    def _detect_failed(self, e):
        self.scan_spin.set_visible(False)
        self.scan_text.set_label(f"Couldn't read your system: {e}")


    def _system_page(self, info):
        found = Adw.PreferencesGroup(title="What the installer read",
                                     description="Read from this computer to pick the right packages. "
                                                 "Your IP address isn't read or shown.")
        rows = [("computer-symbolic", "Operating system", info["os"]),
                ("package-x-generic-symbolic", "Distribution", info["distro"]),
                ("system-run-symbolic", "Kernel", info["kernel"]),
                ("application-x-executable-symbolic", "Architecture", info["arch"]),
                ("preferences-desktop-display-symbolic", "Desktop", info["desktop"]),
                ("system-run-symbolic", "Processor", info["cpu"]),
                ("drive-harddisk-symbolic", "Memory", info["memory"]),
                ("video-display-symbolic", "Graphics", info["gpu"]),
                ("system-software-install-symbolic", "Package manager", info["manager"] or "None found"),
                ("utilities-terminal-symbolic", "Python and toolkit", f"Python {info['python']} · {info['toolkit']}")]
        for icon, title, value in rows:
            row = Adw.ActionRow(title=title, subtitle=escape(value))
            row.set_subtitle_selectable(True)
            row.add_prefix(Gtk.Image(icon_name=icon))
            found.add(row)

        sent = Adw.PreferencesGroup(title="Install report")
        self.report_switch = Adw.SwitchRow(title="Send an anonymous install report",
                                           subtitle="Helps the developer see which systems BifluxVR runs on",
                                           active=report.enabled())
        self.report_switch.set_sensitive(report.enabled())
        sent.add(self.report_switch)
        preview = label(escape(report.message(info, "#", "SUCCESSFUL")), "report-preview", wrap=True)
        preview.set_selectable(True)
        if report.enabled():
            text = ("This exact line is the whole report: operating system, distribution and kernel, a user "
                    "number, and whether the install worked (or why it didn't). It's sent once when you press "
                    "Next and updated when the install finishes.\n\nYour IP address is not part of the report "
                    "and is never passed on. Like any website, the report server sees your connection; it keeps "
                    "only a scrambled form of the address for 10 minutes to block spam.")
        else:
            text = "Reports are switched off in this build. Nothing on this page leaves your computer."
        note = label(text, "dim-label", wrap=True)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.add_css_class("report-card")
        card.append(preview)
        card.append(note)
        return page("Here's what I found", "Everything below stays on this computer, except the report line "
                    "at the bottom if you leave it on.", found, sent, card)


    def _plan_page(self):
        self.plan_group = Adw.PreferencesGroup()
        self.plan_status = Gtk.Box(spacing=10, halign=Gtk.Align.START)
        self.plan_status.append(Adw.Spinner())
        self.plan_status.append(label("Checking what's installed and what has updates…", "dim-label"))
        self.plan_note = label("", "dim-label", wrap=True)
        return page("What gets installed", "Everything BifluxVR needs. Things you already have are updated if "
                    "there's a newer version, and skipped if not.",
                    self.plan_status, self.plan_group, self.plan_note)

    def _planned(self, items, error=""):
        self.plan_status.set_visible(False)
        if items is None:
            self.plan_note.set_label(f"Couldn't check your packages: {error}")
            return
        self.plan = items
        for it in items:
            row = Adw.ActionRow(title=escape(it.title), subtitle=escape(it.why))
            row.set_subtitle_lines(2)
            text, css = BADGES[it.action]
            badge = label(text, "badge", css)
            badge.set_valign(Gtk.Align.CENTER)
            badge.set_tooltip_text(it.detail)
            row.add_suffix(badge)
            if it.detail:
                row.set_tooltip_text(it.detail)
            self.plan_group.add(row)
        mgr = self.info["manager"]
        todo = [it for it in items if it.action in ("install", "update")]
        rooted = [it for it in todo if it.method == "pkg" or it.needs_flatpak]
        notes = []
        if not todo:
            notes.append("Everything is already installed and up to date.")
        if rooted:
            notes.append(f"System packages are installed with {mgr}. You'll be asked for your password once.")
        if mgr == "emerge" and rooted:
            notes.append("Gentoo builds from source, so this can take a while. The log shows the progress.")
        if not mgr:
            notes.append("Your distribution's package manager isn't supported, so only the parts that don't "
                         "need one are installed.")
        self.plan_note.set_label(" ".join(notes))
        if self.stack.get_visible_child_name() == "plan":
            self.show_step("plan")


    def _install_page(self):
        self.current = Gtk.Box(spacing=12)
        self.current.add_css_class("current-task")
        self.current_spin = Adw.Spinner()
        self.current.append(self.current_spin)
        self.current_text = label("Getting ready…", "current-text", wrap=True)
        self.current.append(self.current_text)
        self.progress = Gtk.ProgressBar(show_text=True)
        self.progress.add_css_class("setup-progress")
        self.task_group = Adw.PreferencesGroup()
        self.result = Adw.Bin()
        self.logbuf = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=self.logbuf, editable=False, monospace=True, cursor_visible=False,
                            wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=10, bottom_margin=10,
                            left_margin=12, right_margin=12)
        view.add_css_class("log-view")
        self.logscroll = Gtk.ScrolledWindow(child=view, min_content_height=220)
        exp = Gtk.Expander(label="Detailed log", child=self.logscroll)
        exp.add_css_class("log-expander")
        return page("Installing", None, self.current, self.progress, self.result, self.task_group, exp)

    def start_install(self):
        if self.failed is not None or getattr(self, "_running", False):
            return
        self._running = True
        self.task_rows = {}
        for it in self.plan:
            row = Adw.ActionRow(title=escape(it.title))
            icon = Gtk.Image(icon_name="content-loading-symbolic")
            row.add_prefix(icon)
            state = label("Up to date" if it.action == "skip" else "Waiting", "dim-label")
            row.add_suffix(state)
            self.task_group.add(row)
            self.task_rows[it.key] = (row, icon, state)
        self.todo = [it for it in self.plan if it.action != "skip"]
        self.done = 0
        self.titles = {it.key: it.title for it in self.plan}
        self._set_progress()

        def emit(event, key, payload):
            GLib.idle_add(self._event, event, key, payload)

        run_async(lambda: runner.run(self.plan, self.info["manager"], emit, self._ask_password), self._installed,
                  lambda e: self._installed({"bifluxvr": f"installer crashed: {e}"}))

    def _ask_password(self, retry):
        done = threading.Event()
        answer = []

        def show():
            names = ", ".join(self.titles[it.key] for it in self.plan
                              if it.action in ("install", "update") and (it.method == "pkg" or it.needs_flatpak))
            body = (f"Installing {names} needs administrator rights. Your password goes straight to sudo "
                    "and isn't saved anywhere.")
            if retry:
                body = ("That password didn't work. One more try: after that, sudo may lock your account for a "
                        "few minutes.\n\n" + body)
            dialog = Adw.AlertDialog(heading="Enter your password", body=body)
            entry = Gtk.PasswordEntry(show_peek_icon=True, activates_default=True)
            dialog.set_extra_child(entry)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("ok", "Install")
            dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("ok")
            dialog.set_close_response("cancel")

            def on_response(_d, resp):
                answer.append(entry.get_text() if resp == "ok" else None)
                done.set()
            dialog.connect("response", on_response)
            dialog.present(self)
            entry.grab_focus()
            self.current_text.set_label("Waiting for your password…")
            return False
        GLib.idle_add(show)
        done.wait()
        return answer[0]

    def _set_progress(self, part=0.0):
        total = max(len(self.todo), 1)
        self.progress.set_fraction(min((self.done + part) / total, 1.0))
        self.progress.set_text(f"{self.done} of {len(self.todo)} done" if self.todo else "Nothing to do")

    def _event(self, event, key, payload):
        row, icon, state = self.task_rows.get(key, (None, None, None))
        title = self.titles.get(key, key)
        if event == "log":
            end = self.logbuf.get_end_iter()
            self.logbuf.insert(end, f"[{title}] {payload}\n")
            adj = self.logscroll.get_vadjustment()
            GLib.idle_add(lambda: adj.set_value(adj.get_upper()) or False)
        elif event == "start" and row:
            self.current_text.set_label(f"Installing {title}…")
            spin = Adw.Spinner()
            row.remove(icon)
            row.add_prefix(spin)
            self.task_rows[key] = (row, spin, state)
            state.set_label("Working…")
        elif event == "fraction":
            self._set_progress(payload)
        elif event == "done" and row:
            mark = Gtk.Image(icon_name="object-select-symbolic" if not payload else "dialog-error-symbolic")
            mark.add_css_class("success" if not payload else "error")
            row.remove(icon)
            row.add_prefix(mark)
            self.task_rows[key] = (row, mark, state)
            state.set_label("Done" if not payload else "Failed")
            if payload:
                row.set_subtitle(escape(payload))
                row.set_subtitle_lines(3)
            self.done += 1
            self._set_progress()
        elif event == "skip" and row:
            mark = Gtk.Image(icon_name="object-select-symbolic")
            mark.add_css_class("dim-label")
            row.remove(icon)
            row.add_prefix(mark)
            self.task_rows[key] = (row, mark, state)
        return False

    def _installed(self, failed):
        self._running = False
        self.failed = failed
        self.current_spin.set_visible(False)
        self.progress.set_fraction(1.0)
        reason = "; ".join(f"{self.titles.get(k, k)}: {v}" for k, v in failed.items())
        if self.run:
            run_async(lambda: report.finish(self.run, not failed, reason))
        banner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        banner.add_css_class("result-card")
        if not failed:
            banner.add_css_class("ok")
            self.current_text.set_label("All done!")
            banner.append(label("BifluxVR is installed", "result-title"))
            banner.append(label("Everything it needs is in place. One last step: make it yours.", wrap=True))
        else:
            banner.add_css_class("bad")
            self.current_text.set_label("Finished with problems")
            banner.append(label("Some parts couldn't be installed", "result-title"))
            banner.append(label(reason, wrap=True))
            banner.append(label(f"The full log is in {runner.LOG}", "dim-label", wrap=True))
            if "bifluxvr" not in failed:
                banner.append(label("BifluxVR itself is installed, so you can continue and fix the rest later.",
                                    wrap=True))
        self.result.set_child(banner)
        self.show_step("install")


    def _settings_page(self):
        g = Adw.PreferencesGroup(title="Personality")
        voice = Adw.ActionRow(title="Voice", subtitle="How BifluxVR talks to you")
        self.voice = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        self.voice.add(Adw.Toggle(name="funny", label="Funny"))
        self.voice.add(Adw.Toggle(name="serious", label="Professional"))
        self.voice.set_active_name("serious" if self.cfg.get("serious_mode") else "funny")
        voice.add_suffix(self.voice)
        g.add(voice)
        self.voice_preview = label("", "voice-preview", wrap=True)
        self.voice.connect("notify::active-name", lambda *_: self._preview_voice())
        self._preview_voice()

        theme = Adw.ActionRow(title="Theme", subtitle="Night and day mode")
        self.theme = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        for name, text in (("system", "Auto"), ("light", "Day"), ("dark", "Night")):
            self.theme.add(Adw.Toggle(name=name, label=text))
        self.theme.set_active_name(self.cfg.get("appearance", "system"))
        self.theme.connect("notify::active-name", lambda *_: self._apply_theme())
        theme.add_suffix(self.theme)
        g.add(theme)

        p = Adw.PreferencesGroup(title="Playing")
        self.switches = {}
        for key, title, sub in (
            ("auto_start_server", "Start WiVRn when you press Play", "No need to start the streaming server yourself"),
            ("vr_audio", "Sound in the headset", "Moves the sound to the headset while you play, and back after"),
            ("notify", "Update notifications", "New BifluxVR, WiVRn, xrizer and Proton releases"),
            ("online", "Ratings and artwork", "ProtonDB ratings and Steam cover art for your games"),
        ):
            row = Adw.SwitchRow(title=title, subtitle=sub, active=self.cfg.get(key, True))
            p.add(row)
            self.switches[key] = row

        go = Gtk.Button(halign=Gtk.Align.CENTER, margin_top=10)
        go.set_child(Adw.ButtonContent(label="Launch BifluxVR", icon_name="media-playback-start-symbolic"))
        go.add_css_class("pill")
        go.add_css_class("suggested-action")
        go.add_css_class("launch-button")
        go.connect("clicked", lambda *_: self.launch())
        return page("Make it yours", "You can change all of this later in Settings.",
                    g, self.voice_preview, p, go)

    def _preview_voice(self):
        funny = self.voice.get_active_name() == "funny"
        self.voice_preview.set_label("“Enter the Matrix” · “Yeet Zone” · “WiVRn is alive!”" if funny
                                     else "“Play in VR” · “Drop Folder” · “WiVRn is running”")

    def _apply_theme(self):
        Adw.StyleManager.get_default().set_color_scheme({
            "light": Adw.ColorScheme.FORCE_LIGHT, "dark": Adw.ColorScheme.FORCE_DARK,
        }.get(self.theme.get_active_name(), Adw.ColorScheme.DEFAULT))

    def save_settings(self):
        cfg = config.load()
        cfg["serious_mode"] = self.voice.get_active_name() == "serious"
        cfg["appearance"] = self.theme.get_active_name()
        for key, row in self.switches.items():
            cfg[key] = row.get_active()
        config.save(cfg)

    def launch(self):
        self.save_settings()
        argv = [LAUNCHER] if os.path.exists(LAUNCHER) else [sys.executable, "-m", "bifluxvr"]
        subprocess.Popen(argv, cwd=packages.SOURCE, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        self.get_application().quit()


class Unsupported(Adw.ApplicationWindow):
    def __init__(self, app, name):
        super().__init__(application=app, title="BifluxVR Setup", default_width=560, default_height=420)
        self.add_css_class("bifluxvr-setup")
        status = Adw.StatusPage(icon_name="dialog-error-symbolic", title="Unsupported OS",
                                description=f"BifluxVR runs on Linux only, and this is {escape(name)}. "
                                            "Nothing was installed.")
        close = Gtk.Button(label="Close", halign=Gtk.Align.CENTER)
        close.add_css_class("pill")
        close.connect("clicked", lambda *_: app.quit())
        status.set_child(close)
        tv = Adw.ToolbarView(content=status)
        tv.add_top_bar(Adw.HeaderBar(show_title=False))
        self.set_content(tv)


class SetupApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(HERE, "style.css"))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        win = self.props.active_window
        if not win and tripwire.locked():
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
            box.add_css_class("nice-try")
            box.append(Gtk.Label(label="Nice try!", css_classes=["nice-try-text"], valign=Gtk.Align.CENTER,
                                 vexpand=True))
            win = Adw.ApplicationWindow(application=self, title="BifluxVR Setup", default_width=900,
                                        default_height=620, content=box)
        if not win:
            bad = system.supported()
            win = Unsupported(self, bad) if bad else Installer(self)
        win.present()


def main():
    GLib.set_application_name("BifluxVR Setup")
    return SetupApp().run(sys.argv[:1])
