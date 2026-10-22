from __future__ import annotations

import os
import threading

from gi.repository import Gdk, Gio, GLib, Gtk, Pango, Adw

from .. import actions, scanner
from .. import gpu as gpu_mod
from ..models import APPIMAGE, CACHE, CONFIG, DATA, DEB, HOME, SNAP, InstalledApp, human_size
from ..prefs import Prefs

STYLE_PATH = os.path.join(os.path.dirname(__file__), "style.css")
ICON_NAME = "com.local.UbuntuAppControl"
SOURCE_FILTERS = ("All apps", "Snap", "AppImage", "Package")
CATEGORY_FILTERS = SOURCE_FILTERS + ("Utilities",)


def load_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_path(STYLE_PATH)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


def run_async(work, done) -> None:
    """Do slow work off the UI thread, then call done(result, error) back on it."""

    def runner():
        result, error = None, None
        try:
            result = work()
        except Exception as exception:  # surfaced to the user as a toast
            result, error = None, exception

        def invoke() -> bool:
            done(result, error)
            return False  # one-shot: a truthy return here would spin the UI thread

        GLib.idle_add(invoke)

    threading.Thread(target=runner, daemon=True).start()


def app_icon(app: InstalledApp | None, size: int) -> Gtk.Widget:
    image = Gtk.Image()
    image.set_pixel_size(size)
    path = app.icon_path if app else ""
    if path and os.path.exists(path):
        try:
            image.set_from_paintable(Gdk.Texture.new_from_filename(path))
            image.set_pixel_size(size)
            return image
        except GLib.Error:
            pass
    image.set_from_icon_name((app.icon_name if app else "") or "application-x-executable")
    return image


def clear_box(box: Gtk.Box) -> None:
    child = box.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        box.remove(child)
        child = following


def confirm(parent: Gtk.Window, heading: str, body: str, ok_label: str, destructive: bool, callback):
    if hasattr(Adw, "AlertDialog"):
        dialog = Adw.AlertDialog(heading=heading, body=body)
    else:
        dialog = Adw.MessageDialog(heading=heading, body=body)
    dialog.set_transient_for(parent)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("ok", ok_label)
    dialog.set_response_appearance(
        "ok",
        Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED,
    )
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")
    if hasattr(dialog, "set_body_use_markup"):
        dialog.set_body_use_markup(False)
    dialog.connect("response", lambda _d, response: callback(response == "ok"))
    dialog.present()


class AppRow(Adw.ActionRow):
    def __init__(self, app: InstalledApp):
        super().__init__(title=app.name)
        self.app = app
        subtitle = f"{app.source_label} - {app.version or 'version unknown'}"
        if app.publisher:
            subtitle += f" - {app.publisher}"
        self.set_subtitle(subtitle)
        self.set_title_lines(1)
        self.set_subtitle_lines(2)
        self.set_activatable(True)
        self.set_selectable(True)
        self.add_prefix(app_icon(app, 24))
        self.add_css_class("rounded")

        self.dot = Gtk.Box()
        self.dot.set_size_request(10, 10)
        self.dot.set_valign(Gtk.Align.CENTER)
        self.dot.set_margin_start(6)
        self.dot.set_margin_end(6)
        self.dot.add_css_class("running-dot" if app.running else "idle-dot")
        self.add_suffix(self.dot)

        if app.gpu != "auto":
            badge = Gtk.Label(label="GPU" if app.gpu.startswith("dgpu") else "iGPU")
            badge.add_css_class("app-row-gpu")
            self.add_suffix(badge)

        self.size_label = Gtk.Label(label=app.size_detail())
        self.size_label.add_css_class("app-row-size")
        self.size_label.set_valign(Gtk.Align.CENTER)
        self.size_label.set_xalign(1.0)
        self.size_label.set_max_width_chars(18)
        self.size_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.add_suffix(self.size_label)

    def update_sizes(self, app: InstalledApp) -> None:
        self.size_label.set_text(app.size_detail())
        self.dot.remove_css_class("running-dot")
        self.dot.remove_css_class("idle-dot")
        self.dot.add_css_class("running-dot" if app.running else "idle-dot")


class DetailPanel(Gtk.Box):
    def __init__(self, window: "MainWindow"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.window = window
        self.app: InstalledApp | None = None
        self._suppress_gpu_signal = False
        self.set_margin_top(18)
        self.set_margin_bottom(18)
        self.set_margin_start(22)
        self.set_margin_end(22)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self.icon_box = Gtk.Box()
        self.icon_box.append(app_icon(None, 84))
        header.append(self.icon_box)

        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        titles.set_valign(Gtk.Align.CENTER)
        titles.set_hexpand(True)
        self.title = Gtk.Label(label="No app selected")
        self.title.add_css_class("detail-title")
        self.title.set_xalign(0)
        self.title.set_wrap(True)
        self.subtitle = Gtk.Label(label="Pick an application on the left.")
        self.subtitle.add_css_class("detail-subtitle")
        self.subtitle.set_xalign(0)
        self.subtitle.set_wrap(True)
        self.status = Gtk.Label(label="")
        self.status.set_xalign(0)
        self.status.set_visible(False)
        titles.append(self.title)
        titles.append(self.subtitle)
        titles.append(self.status)
        header.append(titles)
        self.append(header)

        self.groups = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self._build_launch_group()
        self._build_gpu_group()
        self._build_storage_group()
        self._build_folders_group()
        self._build_remove_group()
        self.append(self.groups)

    def _new_group(self, title: str = "", description: str = "") -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=title, description=description)
        self.groups.append(group)
        return group

    def _build_launch_group(self) -> None:
        self.launch_group = self._new_group("Launch")
        self.launch_row = Adw.ActionRow(title="Start the app", subtitle="Uses the GPU choice below")
        self.launch_button = Gtk.Button(label="Launch")
        self.launch_button.add_css_class("suggested-action")
        self.launch_button.set_valign(Gtk.Align.CENTER)
        self.launch_button.connect("clicked", lambda *_: self.window.launch(self.app, None))
        self.launch_row.add_suffix(self.launch_button)

        self.oneshot_button = Gtk.MenuButton(label="Launch on another GPU")
        self.oneshot_button.set_valign(Gtk.Align.CENTER)
        self.oneshot_button.add_css_class("flat")
        menu = Gio.Menu()
        self.oneshot_menu = menu
        self.oneshot_button.set_menu_model(menu)
        self.launch_row.add_suffix(self.oneshot_button)
        self.launch_group.add(self.launch_row)

    def _build_gpu_group(self) -> None:
        self.gpu_group = self._new_group(
            "Graphics card", "Stored by rewriting this app's launcher, so GNOME honours it too."
        )
        self.gpu_row = Adw.ComboRow(title="GPU to launch with")
        self.gpu_row.set_model(Gtk.StringList.new([]))
        self.gpu_row.connect("notify::selected", self._gpu_changed)
        self.gpu_group.add(self.gpu_row)
        self.gpu_detail = Adw.ActionRow(title="Environment", subtitle="")
        self.gpu_detail.add_css_class("muted")
        self.gpu_group.add(self.gpu_detail)

    def _build_storage_group(self) -> None:
        self.storage_group = self._new_group("Space used")
        self.installed_row = Adw.ActionRow(title="Installed")
        self.cache_row = Adw.ActionRow(title="Cache")
        cache_button = Gtk.Button(label="Clear cache")
        cache_button.set_valign(Gtk.Align.CENTER)
        cache_button.connect("clicked", lambda *_: self.window.confirm_clear_cache(self.app))
        self.cache_row.add_suffix(cache_button)
        self.data_row = Adw.ActionRow(title="App data and settings")
        data_button = Gtk.Button(label="Clear data")
        data_button.set_valign(Gtk.Align.CENTER)
        data_button.connect("clicked", lambda *_: self.window.confirm_clear_data(self.app))
        self.data_row.add_suffix(data_button)
        for row in (self.installed_row, self.cache_row, self.data_row):
            value = Gtk.Label(label="-")
            value.add_css_class("app-row-size")
            value.set_valign(Gtk.Align.CENTER)
            row.add_suffix(value)
            setattr(row, "_value", value)
            self.storage_group.add(row)

    def _build_folders_group(self) -> None:
        self.folders_group = self._new_group("Folders", "Only these paths are ever touched.")
        self.folders_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.folders_group.add(self.folders_box)

    def _build_remove_group(self) -> None:
        self.remove_group = self._new_group("Remove")
        self.uninstall_row = Adw.ActionRow(title="Uninstall", subtitle="")
        button = Gtk.Button(label="Uninstall")
        button.add_css_class("destructive-action")
        button.set_valign(Gtk.Align.CENTER)
        button.connect("clicked", lambda *_: self.window.confirm_uninstall(self.app))
        self.uninstall_row.add_suffix(button)
        self.remove_group.add(self.uninstall_row)

    def _gpu_changed(self, row, _param) -> None:
        if self._suppress_gpu_signal or self.app is None or not row.get_sensitive():
            return
        index = row.get_selected()
        profiles = self.window.profiles
        if not (0 <= index < len(profiles)):
            return
        profile = profiles[index]
        if profile.id == self.app.gpu:
            return
        self.window.set_gpu(self.app, profile.id)

    def bind(self, app: InstalledApp | None) -> None:
        self.app = app
        sensitive = app is not None
        if app is None:
            self.title.set_text("No app selected")
            self.subtitle.set_text("Pick an application on the left.")
            clear_box(self.icon_box)
            self.icon_box.append(app_icon(None, 84))
        else:
            self.title.set_text(app.name)
            bits = [app.source_label]
            if app.version:
                bits.append(app.version)
            if app.publisher:
                bits.append(app.publisher)
            self.subtitle.set_text(" - ".join(bits))
            clear_box(self.icon_box)
            self.icon_box.append(app_icon(app, 84))
        for group in (self.launch_group, self.gpu_group, self.storage_group, self.folders_group, self.remove_group):
            group.set_sensitive(sensitive)
        self.status.set_visible(False)
        if app is None:
            return
        running = "Running now" if app.running else "Not running"
        if app.runtime:
            running += " - system utility"
        self.status.set_label(f"{running} - {app.size_detail()}")
        self.status.add_css_class("muted")
        self.status.set_visible(True)

        profiles = self.window.profiles
        self._suppress_gpu_signal = True
        self.gpu_row.set_model(Gtk.StringList.new([p.label for p in profiles]))
        self.gpu_row.set_sensitive(True)
        index = next((i for i, p in enumerate(profiles) if p.id == app.gpu), 0)
        self.gpu_row.set_selected(index)
        self._suppress_gpu_signal = False
        profile = gpu_mod.profile_by_id(app.gpu, profiles)
        env_text = profile.env if profile else {}
        self.gpu_detail.set_subtitle(
            ", ".join(f"{key}={value}" for key, value in sorted(env_text.items()))
            or "No overrides - Ubuntu decides."
        )
        self._rebuild_oneshot_menu(app)

        self.installed_row._value.set_text(
            human_size(app.install_bytes) if app.install_bytes else "measuring..."
        )
        self.cache_row._value.set_text(human_size(app.cache_bytes))
        self.data_row._value.set_text(human_size(app.data_bytes))
        clear_box(self.folders_box)
        if not app.dirs:
            row = Adw.ActionRow(title="No personal folders found", subtitle="This app keeps nothing in your home folder")
            row.add_css_class("muted")
            self.folders_box.append(row)
        for directory in app.dirs:
            row = Adw.ActionRow(title=directory.path)
            row.set_subtitle(directory.label)
            row.add_css_class("muted")
            label = row.get_first_child()
            open_button = Gtk.Button.new_from_icon_name("folder-open-symbolic")
            open_button.set_valign(Gtk.Align.CENTER)
            open_button.add_css_class("flat")
            open_button.connect(
                "clicked", lambda *_: Gtk.show_uri(self.window, f"file://{directory.path}", Gdk.CURRENT_TIME)
            )
            row.add_suffix(open_button)
            self.folders_box.append(row)
        self.uninstall_row.set_subtitle(
            {
                SNAP: f"Runs: pkexec snap remove {app.snap_name}",
                APPIMAGE: "Moves the .AppImage file to the Trash",
                DEB: "Runs: pkexec apt-get remove",
            }.get(app.source, "")
        )

    def _rebuild_oneshot_menu(self, app: InstalledApp) -> None:
        menu = self.oneshot_menu
        menu.remove_all()
        section = Gio.Menu()
        for profile in self.window.profiles:
            section.append(profile.label, f"win.launch_gpu::{app.uid}::{profile.id}")
        menu.append_section(None, section)

    def set_sizes(self, app: InstalledApp) -> None:
        if self.app is not app:
            return
        self.installed_row._value.set_text(
            human_size(app.install_bytes) if app.install_bytes else "not tracked"
        )
        self.cache_row._value.set_text(human_size(app.cache_bytes))
        self.data_row._value.set_text(human_size(app.data_bytes))
        self.status.set_label(
            f"{'Running now' if app.running else 'Not running'} - {app.size_detail()}"
        )


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application, prefs: Prefs):
        super().__init__(application=application)
        self.set_title("Ubuntu App Control")
        self.set_icon_name(ICON_NAME)
        self.set_default_size(1200, 780)
        self.prefs = prefs
        self.all_apps: list[InstalledApp] = []
        self.visible_apps: list[InstalledApp] = []
        self.profiles: list[gpu_mod.Profile] = []
        self.gpus: list[gpu_mod.Gpu] = []
        self.selected: InstalledApp | None = None
        self.show_utilities = False
        self.filter_index = 0
        self.search_text = ""

        self._build_ui()
        self.reload_gpu()
        self.refresh()

    # ---------- layout ----------
    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()
        self.set_content(toolbar)

        header = Adw.HeaderBar()
        self.spinner = Gtk.Spinner()
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.header_title = Gtk.Label(label="Ubuntu App Control")
        self.header_title.add_css_class("title")
        self.header_subtitle = Gtk.Label(label="scanning installed apps...")
        self.header_subtitle.add_css_class("caption")
        self.header_subtitle.add_css_class("muted")
        title_box.append(self.header_title)
        title_box.append(self.header_subtitle)
        header.set_title_widget(title_box)

        self.search = Gtk.SearchEntry()
        self.search.set_width_chars(22)
        self.search.set_placeholder_text("Search apps")
        self.search.connect("search-changed", lambda *_: self.apply_filter())
        header.pack_start(self.search)

        menu = Gio.Menu()
        menu.append("Rescan apps", "win.refresh")
        menu.append("Show system utilities", "win.utilities")
        menu.append("Restorable app data", "win.quarantine")
        menu.append("About Ubuntu App Control", "win.about")
        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("open-menu-symbolic")
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)
        toolbar.add_top_bar(header)

        self.toasts = Adw.ToastOverlay()
        toolbar.set_content(self.toasts)

        self.split = (
            Adw.OverlaySplitView()
            if hasattr(Adw, "OverlaySplitView")
            else _PanedSplit()
        )
        self.toasts.set_child(self.split)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar.add_css_class("sidebar-list")
        chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        chips.set_margin_top(8)
        chips.set_margin_bottom(8)
        chips.set_margin_start(8)
        chips.set_margin_end(8)
        self.filter_buttons: list[Gtk.ToggleButton] = []
        for index, label in enumerate(CATEGORY_FILTERS):
            button = Gtk.ToggleButton(label=label)
            button.set_hexpand(True)
            button.connect("toggled", self._filter_toggled, index)
            chips.append(button)
            self.filter_buttons.append(button)
        sidebar.append(chips)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_activate_on_single_click(True)
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.connect("row-selected", self._row_selected)
        scrolled.set_child(self.listbox)
        sidebar.append(scrolled)

        detail_scrolled = Gtk.ScrolledWindow()
        detail_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        detail_scrolled.set_hexpand(True)
        detail_scrolled.set_vexpand(True)
        self.detail = DetailPanel(self)
        detail_scrolled.set_child(self.detail)

        if isinstance(self.split, Adw.OverlaySplitView):
            self.split.set_sidebar(sidebar)
            self.split.set_content(detail_scrolled)
            self.split.set_collapsed(False)
            self.split.set_min_sidebar_width(300)
            self.split.set_max_sidebar_width(400)
        else:
            self.split.set_sidebar(sidebar)
            self.split.set_content(detail_scrolled)

        self._install_actions()
        self.filter_buttons[0].set_active(True)

    def _install_actions(self) -> None:
        for name, callback in (
            ("refresh", lambda *_: self.refresh()),
            ("quarantine", lambda *_: self.show_quarantine()),
            ("about", lambda *_: self.show_about()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        state = Gio.SimpleAction.new_stateful(
            "utilities", None, GLib.Variant("b", self.show_utilities)
        )
        state.connect("change-state", self._toggle_utilities)
        state.connect("activate", lambda a, p: a.set_state(GLib.Variant("b", not a.get_state().get_boolean())))
        self.add_action(state)
        launch_gpu = Gio.SimpleAction.new("launch_gpu", GLib.VariantType("s"))
        launch_gpu.connect("activate", self._action_launch_gpu)
        self.add_action(launch_gpu)

    # ---------- data ----------
    def reload_gpu(self) -> None:
        self.gpus = gpu_mod.detect_gpus()
        self.profiles = gpu_mod.build_profiles(self.gpus)

    def _toggle_utilities(self, action, _param) -> None:
        value = not action.get_state().get_boolean()
        action.set_state(GLib.Variant("b", value))
        self.show_utilities = value
        self.apply_filter()

    def _filter_toggled(self, button, index: int) -> None:
        if not button.get_active():
            return
        for other in self.filter_buttons:
            if other is not button and other.get_active():
                other.set_active(False)
        self.set_filter(index)

    def set_filter(self, index: int) -> None:
        self.filter_index = index
        self.apply_filter()

    def refresh(self) -> None:
        self.spinner.set_spinning(True)
        self.header_subtitle.set_text("scanning installed apps...")

        def work():
            apps = scanner.scan(include_runtimes=True)
            for app in apps:
                scanner.measure_app(app)
            return apps

        def done(result, error):
            self.spinner.set_spinning(False)
            if error is not None:
                self.toast(f"Scan failed: {error}")
                return True
            self.all_apps = result
            self.header_subtitle.set_text(
                f"{len([a for a in result if not a.runtime])} apps - "
                f"{len([a for a in result if a.runtime])} utilities"
            )
            self.apply_filter()
            return True

        run_async(work, done)

    def apply_filter(self) -> None:
        wanted = self.search.get_text().lower()
        self.search_text = wanted
        keep = []
        for app in self.all_apps:
            if app.runtime and not self.show_utilities and self.filter_index != 4:
                continue
            if self.filter_index == 1 and app.source != SNAP:
                continue
            if self.filter_index == 2 and app.source != APPIMAGE:
                continue
            if self.filter_index == 3 and app.source != DEB:
                continue
            if self.filter_index == 4 and not app.runtime:
                continue
            if wanted:
                haystack = " ".join(
                    [app.name, app.source_label, app.binary, app.version, app.publisher, *app.categories]
                ).lower()
                if wanted not in haystack:
                    continue
            keep.append(app)
        keep.sort(key=lambda a: (a.runtime, a.name.lower()))
        self.visible_apps = keep

        self.listbox.remove_all()
        for app in keep:
            self.listbox.append(AppRow(app))

        if not keep:
            self.selected = None
            self.detail.bind(None)
            self.listbox.append(self._placeholder(wanted))
            return

        index = next((i for i, a in enumerate(keep) if a is self.selected), 0)
        row = self.listbox.get_row_at_index(index)
        if row is not None:
            self.listbox.select_row(row)
            self._row_selected(self.listbox, row)
        else:
            self.selected = keep[index]
            self._show(self.selected)

    def _placeholder(self, wanted: str) -> Gtk.ListBoxRow:
        label = Gtk.Label(
            label=f'No apps match "{wanted}"' if wanted else "No apps found - try Rescan."
        )
        label.set_margin_top(18)
        label.set_margin_bottom(18)
        label.add_css_class("muted")
        row = Gtk.ListBoxRow()
        row.set_child(label)
        row.set_activatable(False)
        row.set_selectable(False)
        return row

    def _row_selected(self, _list, row) -> None:
        if row is None or not hasattr(row, "app") or row.app is self.selected:
            return
        self.selected = row.app
        self._show(row.app)

    def _show(self, app: InstalledApp) -> None:
        self.detail.bind(app)

        def work():
            scanner.measure_app(app)
            if app.source == DEB and not app.install_bytes:
                package, size = actions.find_package(app)
                if package:
                    app.install_bytes = size
                    app.publisher = f"dpkg: {package}"
            return app

        def done(result, error):
            if error is not None:
                self.detail.set_sizes(app)
                return True
            self.detail.set_sizes(result)
            for index, visible in enumerate(self.visible_apps):
                if visible is app:
                    row = self.listbox.get_row_at_index(index)
                    if row:
                        row.update_sizes(app)
            return True

        run_async(work, done)

    # ---------- actions ----------
    def launch(self, app: InstalledApp | None, profile_id: str | None) -> None:
        if app is None:
            return
        env = None
        if profile_id:
            profile = gpu_mod.profile_by_id(profile_id, self.profiles)
            env = profile.env if profile else None
            label = profile.label if profile else profile_id
        else:
            profile = gpu_mod.profile_by_id(app.gpu, self.profiles)
            env = profile.env if profile else None
            label = profile.label if profile else "default GPU"

        def work():
            return actions.launch(app, env)

        def done(result, error):
            if error is not None:
                self.toast(str(error))
            else:
                self.toast(f"{app.name} launched on {label}.")
            return True

        run_async(work, done)

    def _action_launch_gpu(self, _action, parameter) -> None:
        payload = parameter.get_string()
        uid, _, profile_id = payload.partition("::")
        app = next((a for a in self.all_apps if a.uid == uid), None)
        if app is not None:
            self.launch(app, profile_id)

    def set_gpu(self, app: InstalledApp, profile_id: str) -> None:
        if app.gpu == profile_id:
            return
        message = gpu_mod.apply_profile(app, profile_id, self.profiles)
        app.gpu = scanner.gpu_profile(app)
        self.prefs.set_gpu(app.uid, app.gpu)
        self.toast(message)
        for index, visible in enumerate(self.visible_apps):
            if visible is app:
                row = self.listbox.get_row_at_index(index)
                if row:
                    self.listbox.remove(row)
                    self.listbox.insert(AppRow(app), index)
                break
        self.detail.bind(app)

    def confirm_clear_cache(self, app: InstalledApp | None) -> None:
        if app is None:
            return
        targets = scanner.cache_targets(app)
        if not targets:
            self.toast(f"{app.name} has no cache folder.")
            return
        body = "\n".join(targets)
        confirm(
            self,
            f"Clear cache for {app.name}?",
            f"Deletes the contents of:\n{body}\n\n{human_size(app.cache_bytes)} freed. "
            "The app rebuilds this on next launch.",
            "Clear cache",
            False,
            lambda ok: self._clear_cache(app) if ok else None,
        )

    def _clear_cache(self, app: InstalledApp) -> None:
        def work():
            return actions.clear_cache(app)

        def done(result, error):
            if error is not None:
                self.toast(str(error))
                return True
            freed, done_list, failed = result
            self.prefs.remember_size(app.uid, freed)
            message = f"Freed {human_size(freed)} of cache"
            if failed:
                message += f" - {len(failed)} folder(s) skipped"
            self.toast(message)
            scanner.measure_app(app)
            self.detail.set_sizes(app)
            return True

        run_async(work, done)

    def confirm_clear_data(self, app: InstalledApp | None) -> None:
        if app is None:
            return
        targets = scanner.data_targets(app)
        if not targets:
            self.toast(f"{app.name} has no stored data in your home folder.")
            return
        body = "\n".join(targets)
        warning = "\n\nWARNING: settings and saved state go too." if app.running else ""
        confirm(
            self,
            f"Clear data for {app.name}?",
            f"Moves these folders to the restorable quarantine area:\n{body}{warning}\n\n"
            f"{human_size(app.data_bytes)} freed. You can undo this from the menu.",
            "Clear data",
            True,
            lambda ok: self._clear_data(app) if ok else None,
        )

    def _clear_data(self, app: InstalledApp) -> None:
        def work():
            return actions.clear_data(app, self.prefs)

        def done(result, error):
            if error is not None:
                self.toast(str(error))
                return True
            freed, qid, moved, failed = result
            if not qid:
                self.toast("Nothing could be cleared - permissions blocked it.")
                return True
            scanner.measure_app(app)
            self.detail.set_sizes(app)
            self.toast_undo(f"Freed {human_size(freed)} from {app.name}", qid)
            return True

        run_async(work, done)

    def confirm_uninstall(self, app: InstalledApp | None) -> None:
        if app is None:
            return
        extra = ""
        if app.source in (SNAP, DEB):
            extra = "\n\nA polkit prompt will ask for your password."
        confirm(
            self,
            f"Uninstall {app.name}?",
            f"This removes the application itself, not just its data.{extra}",
            "Uninstall",
            True,
            lambda ok: self._uninstall(app) if ok else None,
        )

    def _uninstall(self, app: InstalledApp) -> None:
        # Give immediate feedback while pkexec waits for the system
        # authentication agent or apt/snap completes in the worker thread.
        if app.source in (SNAP, DEB):
            self.toast(f"Waiting for authorization to uninstall {app.name}…")
        else:
            self.toast(f"Removing {app.name}…")

        def work():
            return actions.uninstall(app, self.prefs)

        def done(result, error):
            if error is not None:
                self.toast(str(error))
                return True
            ok, message = result
            self.toast(message)
            if ok:
                self.selected = None
                self.refresh()
            return True

        run_async(work, done)

    def show_quarantine(self) -> None:
        entries = self.prefs.quarantine
        dialog = (
            Adw.Dialog()
            if hasattr(Adw, "Dialog")
            else None
        )
        if dialog is None:
            self.toast("No restorable data." if not entries else f"{len(entries)} batches stored.")
            return
        dialog.set_title("Restorable app data")
        dialog.set_content_width(560)
        dialog.set_content_height(420)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        group = Adw.PreferencesGroup()
        if not entries:
            group.add(Adw.ActionRow(title="Nothing stored", subtitle="Cleared app data appears here"))
        for entry in entries:
            paths = "\n".join(item["from"] for item in entry.get("items", []))
            row = Adw.ActionRow(title=entry.get("app", "App"), subtitle=f"{entry.get('created', '')} - {paths}")
            row.add_css_class("muted")
            restore = Gtk.Button(label="Restore")
            restore.set_valign(Gtk.Align.CENTER)
            qid = entry.get("id", "")
            restore.connect("clicked", lambda _b, key=qid: self._restore(key))
            row.add_suffix(restore)
            group.add(row)
        box.append(group)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(box)
        toolbar.set_content(scrolled)
        dialog.set_child(toolbar)
        self.quarantine_dialog = dialog
        dialog.present(self)

    def _restore(self, qid: str) -> None:
        entry = next((item for item in self.prefs.quarantine if item.get("id") == qid), None)
        if entry is None:
            return

        def work():
            return actions.restore_quarantine(entry)

        def done(result, error):
            if error is not None:
                self.toast(str(error))
                return True
            restored, failed = result
            self.prefs.pop_quarantine(qid)
            message = f"Restored {human_size(restored)} to {entry.get('app', 'the app')}"
            if failed:
                message += f" - {len(failed)} could not be put back"
            self.toast(message)
            self.refresh()
            return True

        run_async(work, done)

    def show_about(self) -> None:
        maker = getattr(Adw, "AboutWindow", None) or getattr(Adw, "AboutDialog", None)
        if maker is None:
            self.toast("Ubuntu App Control 1.0")
            return
        about = maker()
        text = {
            "application-name": "Ubuntu App Control",
            "application-icon": ICON_NAME,
            "version": "1.0.0",
            "developer-name": "Local build",
            "comments": (
                "See the apps you installed, clear their cache and data, uninstall them, "
                "and choose which GPU launches each one."
            ),
        }
        for name, value in text.items():
            if about.find_property(name) is not None:
                about.set_property(name, value)
        about.set_transient_for(self)
        about.present()
        self.about_window = about

    # ---------- helpers ----------
    def toast(self, message: str) -> None:
        notification = Adw.Toast(title=GLib.markup_escape_text(message, -1))
        notification.set_timeout(5)
        self.toasts.add_toast(notification)

    def toast_undo(self, message: str, qid: str) -> None:
        notification = Adw.Toast(title=GLib.markup_escape_text(message, -1))
        notification.set_button_label("Undo")
        notification.connect("button-clicked", lambda _t, key=qid: self._restore(key))
        notification.set_timeout(9)
        self.toasts.add_toast(notification)


class _PanedSplit(Gtk.Paned):
    """Fallback when libadwaita is too old for OverlaySplitView."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.set_wide_handle(True)
        self.set_position(330)

    def set_sidebar(self, widget):
        self.pack1(widget, False, False)

    def set_content(self, widget):
        self.pack2(widget, True, False)
