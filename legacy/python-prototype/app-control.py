#!/usr/bin/env python3
"""Ubuntu App Control - manage installed apps, their cache and data, and the GPU they launch on."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ubuntu_app_control import ui  # noqa: F401  pins Gtk/Adw versions before any widget import
from gi.repository import Adw, Gio  # noqa: E402

from ubuntu_app_control.prefs import APP_ID, Prefs, ensure_dirs  # noqa: E402
from ubuntu_app_control.ui.window import MainWindow, load_css  # noqa: E402


class UbuntuAppControl(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.prefs = Prefs()
        self.main_window: MainWindow | None = None

    def do_activate(self) -> None:
        ensure_dirs()
        load_css()
        if self.main_window is None:
            self.main_window = MainWindow(self, self.prefs)
        self.main_window.present()


def main() -> int:
    return UbuntuAppControl().run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
