#!/usr/bin/env python3
"""Drive the real widget tree headlessly: filters, selection, dialogs, GPU pinning."""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ubuntu_app_control import ui as ui_pkg  # noqa: E402,F401  (pins Gtk/Adw versions)
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from ubuntu_app_control import actions, scanner  # noqa: E402
from ubuntu_app_control.models import CACHE, CONFIG, DATA, DEB, DataDir, InstalledApp  # noqa: E402
from ubuntu_app_control.prefs import APP_ID, Prefs, ensure_dirs  # noqa: E402
from ubuntu_app_control.ui import window as window_module  # noqa: E402
from ubuntu_app_control.ui.window import MainWindow, load_css  # noqa: E402

FIXTURE = "uac-ui-fixture"
HOME = os.path.expanduser("~")
steps: list = []
problems: list[str] = []
captured_dialogs: list = []


def fixture_app() -> InstalledApp:
    dirs = []
    for parent, kind in (
        (os.path.join(HOME, ".cache"), CACHE),
        (os.path.join(HOME, ".config"), CONFIG),
        (os.path.join(HOME, ".local/share"), DATA),
    ):
        path = os.path.join(parent, FIXTURE)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "junk.bin"), "wb") as handle:
            handle.write(os.urandom(200_000))
        dirs.append(DataDir(path, kind))
    return InstalledApp(
        uid=f"deb:{FIXTURE}", name="UI Fixture", source=DEB, binary=FIXTURE, dirs=dirs
    )


def fake_confirm(parent, heading, body, ok_label, destructive, callback):
    """Record what the user would see, then answer as if they pressed the button."""
    captured_dialogs.append(
        {"heading": heading, "body": body, "ok": ok_label, "destructive": destructive}
    )
    callback(True)


def report(message: str) -> None:
    problems.append(message)
    print("PROBLEM:", message)


class Harness(Adw.Application):
    def __init__(self):
        # A distinct id: reusing APP_ID would hand activation to a running copy
        # of the app and the exercise would silently do nothing.
        super().__init__(
            application_id=APP_ID + ".Exercise", flags=Gio.ApplicationFlags.DEFAULT_FLAGS
        )
        self.window = None

    def do_activate(self) -> None:
        ensure_dirs()
        load_css()
        window_module.confirm = fake_confirm
        self.window = MainWindow(self, Prefs())
        self.window.set_visible(False)
        GLib.timeout_add(200, self.wait_for_scan)

    def wait_for_scan(self) -> bool:
        if not self.window.all_apps:
            if time.time() > deadline:
                report("scan never returned")
                self.quit()
                return False
            return True
        GLib.idle_add(self.run_steps)
        return False

    def run_steps(self) -> bool:
        try:
            self.exercise()
        except Exception as error:  # never hang the suite on a UI fault
            report(f"{type(error).__name__}: {error}")
        print()
        if problems:
            print(f"{len(problems)} PROBLEM(S)")
        else:
            print("UI exercise finished with no errors")
        self.quit()
        return False

    def exercise(self) -> None:
        win = self.window
        apps = win.all_apps
        print(f"scanned {len(apps)} entries, {len([a for a in apps if not a.runtime])} apps")

        # every filter tab
        for index, button in enumerate(win.filter_buttons):
            button.set_active(True)
            shown = len(win.visible_apps)
            print(f"  filter {index} -> {shown} rows")
            if button.get_active() is False:
                report(f"filter {index} did not stay active")
        win.filter_buttons[0].set_active(True)

        # search
        win.search.set_text("vlc")
        win.apply_filter()
        if not win.visible_apps:
            report("searching 'vlc' returned nothing")
        win.search.set_text("zzzz-no-such-app")
        win.apply_filter()
        if win.visible_apps:
            report("empty search still shows rows")
        if win.detail.app is not None:
            report("detail panel not cleared when the list is empty")
        win.search.set_text("")
        win.apply_filter()

        # bind the detail panel across a sample that covers launchers and apps without folders
        sample = [a for a in apps if a.desktop_file][:14] + [a for a in apps if not a.dirs][:4]
        for app in sample:
            win.selected = app
            win._show(app)
        print(f"  bound detail for {len(sample)} apps")

        # rows in the list actually exist and mirror the model
        rows = [win.listbox.get_row_at_index(i) for i in range(len(win.visible_apps))]
        if any(row is None for row in rows):
            report("list box is missing rows")

        run_destructive_flows(win)
        run_gpu_flow(win)
        run_dialog_flows(win)


def run_destructive_flows(win: MainWindow) -> None:
    app = fixture_app()
    win.all_apps.append(app)
    win.detail.bind(app)
    scanner.measure_app(app)
    before = app.cache_bytes + app.data_bytes
    if before < 100_000:
        report(f"fixture too small: {before}")

    win.confirm_clear_cache(app)
    if not captured_dialogs or "Clear cache" not in captured_dialogs[-1]["heading"]:
        report("clear-cache confirmation was not built")

    win.confirm_clear_data(app)
    if "Clear data" not in captured_dialogs[-1]["heading"]:
        report("clear-data confirmation was not built")
    if not captured_dialogs[-1]["destructive"]:
        report("clear-data dialog should be marked destructive")
    win.confirm_uninstall(app)
    if "Uninstall" not in captured_dialogs[-1]["heading"]:
        report("uninstall confirmation was not built")
    print(f"  built {len(captured_dialogs)} confirmation dialogs")


def run_gpu_flow(win: MainWindow) -> None:
    if len(win.profiles) < 2:
        print("  single GPU - skipping GPU pinning")
        return
    target = next((a for a in win.all_apps if a.source == "snap" and a.desktop_file), None)
    if target is None:
        report("no snap available to pin")
        return
    win.selected = target
    win._show(target)
    win.set_gpu(target, win.profiles[-1].id)
    if not os.path.exists(os.path.join(os.path.expanduser("~/.local/share/applications"), os.path.basename(target.desktop_file))):
        report("GPU pin did not create a launcher override")
    win.set_gpu(target, "auto")
    print(f"  pinned and reset GPU for {target.name}")


def run_dialog_flows(win: MainWindow) -> None:
    win.show_quarantine()
    dialog = getattr(win, "quarantine_dialog", None)
    if dialog is None:
        report("restorable-data dialog was not created")
    else:
        dialog.force_close()
    win.show_about()
    about = getattr(win, "about_window", None)
    if about is None:
        report("about window was not created")
    else:
        about.destroy()
    win.toast("toast check")
    win.toast_undo("undo toast check", "no-such-id")
    print("  dialogs and toasts built")


deadline = time.time() + 40


def cleanup() -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from verify import purge_fixture

    purge_fixture(FIXTURE)


def main() -> int:
    cleanup()
    code = Harness().run(sys.argv[:1])
    cleanup()
    return code if problems else 0


if __name__ == "__main__":
    sys.exit(main())
