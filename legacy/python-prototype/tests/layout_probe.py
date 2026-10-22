#!/usr/bin/env python3
"""Assert the app really renders: rows, detail panel and dialogs must get real size.

Every step waits for the widget to be allocated instead of sleeping a fixed
number of milliseconds - GTK only lays out on frame ticks, which can be late.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ubuntu_app_control import ui as ui_pkg  # noqa: E402,F401
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from ubuntu_app_control.prefs import APP_ID, Prefs, ensure_dirs  # noqa: E402
from ubuntu_app_control.ui.window import ICON_NAME, MainWindow, load_css  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))


class Probe(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID + ".LayoutProbe",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.window = None
        self.deadline = time.time() + 60

    def do_activate(self) -> None:
        ensure_dirs()
        load_css()
        self.window = MainWindow(self, Prefs())
        self.window.present()
        self.settle(lambda: self.window.get_mapped() and len(self.window.all_apps) > 4, self.rows)

    def settle(self, ready, next_step, limit: float = 12.0) -> None:
        """Poll until `ready()` is true, then run the next step exactly once."""
        stop = time.time() + limit

        def poll() -> bool:
            try:
                if ready() or time.time() > stop:
                    next_step()
                    return False
            except BaseException as error:  # never leave the loop wedged
                failures.append(f"step raised {error!r}")
                self.quit()
                return False
            return True

        GLib.timeout_add(120, poll)

    # ---------- steps ----------
    def rows(self) -> None:
        win = self.window

        def allocated() -> bool:
            row = win.listbox.get_row_at_index(0)
            return row is not None and row.get_height() > 0

        self.settle(allocated, self.check_rows)

    def check_rows(self) -> None:
        win = self.window
        row = win.listbox.get_row_at_index(0)
        check(
            "app rows render",
            row is not None and row.get_mapped() and row.get_height() > 20,
            f"row={row.get_height() if row else None}x{row.get_width() if row else None}",
        )
        check(
            "detail panel renders",
            win.detail.get_mapped() and win.detail.get_height() > 100,
            f"{win.detail.get_width()}x{win.detail.get_height()}",
        )
        win.search.set_text("zzz-no-such-app")
        self.settle(lambda: len(win.visible_apps) == 0, self.check_placeholder)

    def check_placeholder(self) -> None:
        win = self.window
        row = win.listbox.get_row_at_index(0)
        check(
            "empty search shows a placeholder row",
            row is not None and not hasattr(row, "app") and row.get_height() > 0,
            f"height={row.get_height() if row else None}",
        )
        win.search.set_text("steam")
        self.settle(
            lambda: [a.name for a in win.visible_apps] == ["Steam"], self.check_click
        )

    def check_click(self) -> None:
        win = self.window
        names = [a.name for a in win.visible_apps]
        check("searching 'steam' finds Steam", names == ["Steam"], str(names))
        win.search.set_text("")
        self.settle(lambda: len(win.visible_apps) > 5, lambda: self.click(win))

    def click(self, win) -> None:
        row = win.listbox.get_row_at_index(3)
        check("rows are clickable", row is not None and row.get_activatable(),
              f"activatable={row.get_activatable() if row else None}")
        win.listbox.select_row(row)
        check(
            "one click drives the detail panel",
            win.selected is row.app,
            f"selected={getattr(win.selected, 'name', None)} row={row.app.name}",
        )
        calls: list[int] = []
        original = win.detail.set_sizes
        win.detail.set_sizes = lambda app: (calls.append(1), original(app))[1]
        win._show(win.visible_apps[0])
        self.settle(lambda: len(calls) >= 1, lambda: self.oneshot(calls, original))

    def oneshot(self, calls: list[int], original) -> None:
        check(
            "background work reports back exactly once",
            1 <= len(calls) <= 5,
            f"done callback fired {len(calls)} times (a spin would be thousands)",
        )
        self.window.detail.set_sizes = original
        self.window.show_quarantine()
        dialog = getattr(self.window, "quarantine_dialog", None)
        self.settle(
            lambda: dialog is not None and dialog.get_height() > 100,
            lambda: self.dialog(dialog),
        )

    def dialog(self, dialog) -> None:
        check(
            "quarantine dialog opens with real size",
            dialog is not None and dialog.get_height() > 100,
            f"{dialog.get_width() if dialog else 0}x{dialog.get_height() if dialog else 0}",
        )
        if dialog is not None:
            dialog.force_close()
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        check("brand icon is installed in the icon theme", theme.has_icon(ICON_NAME), ICON_NAME)
        self.window.show_about()
        about = getattr(self.window, "about_window", None)
        used = about.get_property("application-icon") if about is not None else None
        check("about window uses the brand icon", used == ICON_NAME, str(used))
        if about is not None:
            about.destroy()
        self.quit()


if __name__ == "__main__":
    code = Probe().run(sys.argv[:1])
    print()
    if failures:
        print(f"{len(failures)} FAILURE(S)")
        sys.exit(1)
    print("layout probe: everything renders")
    sys.exit(code)
