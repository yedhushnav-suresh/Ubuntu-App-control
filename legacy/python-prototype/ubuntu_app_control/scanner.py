from __future__ import annotations

import os
import re
import subprocess

from . import desktop as desktop_mod
from .diskspace import measure, measure_exclusive
from .models import (
    APPIMAGE,
    CACHE,
    CONFIG,
    DATA,
    DEB,
    HOME,
    SNAP,
    DataDir,
    InstalledApp,
)
from .prefs import OVERRIDE_APP_DIR

HOME_DIR = os.path.expanduser("~")
SNAP_DESKTOP_DIR = "/var/lib/snapd/desktop/applications"
SYSTEM_DESKTOP_DIRS = [
    "/usr/share/applications",
    "/usr/local/share/applications",
    os.path.join(HOME_DIR, ".local/share/applications"),
]
APPIMAGE_ROOTS = [
    os.path.join(HOME_DIR, "Applications"),
    os.path.join(HOME_DIR, "Downloads"),
    os.path.join(HOME_DIR, "Desktop"),
    os.path.join(HOME_DIR, ".local/bin"),
    os.path.join(HOME_DIR, "bin"),
    "/opt",
    "/opt/bin",
    "/usr/local/bin",
    "/snap/bin",
]

# Names that can never be treated as an individual app's own folder.
DENY_SEGMENTS = {
    "applications",
    "backgrounds",
    "calendar",
    "certs",
    "certificates",
    "desktop",
    "doc",
    "docs",
    "documents",
    "download",
    "downloads",
    "fonts",
    "icons",
    "images",
    "keyrings",
    "locale",
    "music",
    "pictures",
    "public",
    "screenshot",
    "screenshots",
    "templates",
    "themes",
    "videos",
    "xdg",
    "go",
    "npm",
    "pip",
    "gnome",
    "gtk-3.0",
    "gtk-4.0",
    "microsoft",
}

CACHE_ROOTS = [os.path.join(HOME_DIR, ".cache")]
CONFIG_ROOTS = [os.path.join(HOME_DIR, ".config")]
DATA_ROOTS = [
    os.path.join(HOME_DIR, ".local/share"),
    os.path.join(HOME_DIR, ".local/state"),
    os.path.join(HOME_DIR, ".var/app"),
]

RUNTIME_HINTS = re.compile(
    r"^(core\d*|bare|gtk-common-themes|gnome-\d+|kf\d+-|platform-|mesa-|snapd$|firmware-updater$|prompting-client$|snapd-desktop-integration$|desktop-security-center$|hotspot$|gtk-\d)"
)


def _snap_list() -> dict[str, dict]:
    table: dict[str, dict] = {}
    try:
        out = subprocess.run(
            ["snap", "list"], capture_output=True, text=True, timeout=20
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return table
    lines = [line for line in out.splitlines() if line.strip()]
    for line in lines[1:]:
        parts = line.split(None, 5)
        if len(parts) < 5:
            continue
        name, version, revision, tracking, publisher = parts[:5]
        notes = parts[5] if len(parts) > 5 else "-"
        if name in table:
            continue
        table[name] = {
            "name": name,
            "version": version,
            "revision": revision,
            "channel": tracking,
            "publisher": publisher.replace("**", "").strip(),
            "classic": "classic" in notes,
            "base": "base" in notes,
            "disabled": "disabled" in notes,
        }
    return table


def _snap_desktop_files() -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    try:
        names = sorted(os.listdir(SNAP_DESKTOP_DIR))
    except OSError:
        return grouped
    for name in names:
        if not name.endswith(".desktop"):
            continue
        path = os.path.join(SNAP_DESKTOP_DIR, name)
        info = desktop_mod.read_desktop(path)
        instance = info.get("X-SnapInstanceName", "")
        if not instance:
            match = re.match(r"^([a-z0-9_.-]+?)_", name)
            instance = match.group(1) if match else ""
        if instance:
            grouped.setdefault(instance, []).append(path)
    return grouped


def _pick_desktop(paths: list[str]) -> str:
    best = ""
    best_score = -1
    for path in paths:
        info = desktop_mod.read_desktop(path)
        if info.get("NoDisplay") or info.get("Hidden"):
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        score = len(info.get("Categories", [])) * 2 + len(info.get("Name", ""))
        if "_" in stem and stem.split("_", 1)[0] == stem.split("_", 1)[1]:
            score += 5
        if score > best_score:
            best, best_score = path, score
    return best or (paths[0] if paths else "")


def _icon_for(info: dict, source_dir: str) -> tuple[str, str]:
    icon = info.get("Icon", "")
    if not icon:
        return "", ""
    if icon.startswith("/") or os.path.isabs(icon):
        return (icon if os.path.exists(icon) else ""), ""
    candidate = os.path.join(source_dir, icon)
    if os.path.exists(candidate):
        return candidate, ""
    return "", icon


def _candidate_keys(*values: str) -> set[str]:
    """Folder names an app may own, derived from its binary, desktop id and display name."""
    keys: set[str] = set()
    for value in values:
        if not value:
            continue
        token = value.strip()
        words = token.split()
        for variant in {token, os.path.basename(token), words[0] if words else token}:
            lowered = variant.lower()
            keys.update(
                {
                    lowered,
                    lowered.replace(" ", "-"),
                    lowered.replace("_", "-"),
                    lowered.replace(" ", "_"),
                }
            )
        stem = re.sub(r"\.desktop$", "", token, flags=re.I).lower()
        keys.add(stem)
        tail = stem.split(".")[-1]
        if tail not in {"org", "com", "io", "net", "app", "dev", "me", "xyz"}:
            keys.add(tail)
        keys.add(re.sub(r"[^a-z0-9.+-]", "-", stem))
    return {key for key in keys if len(key) > 2 and key not in DENY_SEGMENTS}


# Apps whose folders are named differently from the launcher, e.g. firefox -> ~/.mozilla.
FOLDER_ALIASES = {
    "firefox": ("mozilla",),
    "thunderbird": ("thunderbird",),
    "google-chrome": ("google-chrome", "GoogleChrome"),
    "google-chrome-stable": ("google-chrome", "GoogleChrome"),
    "chromium": ("chromium",),
    "chromium-browser": ("chromium",),
    "code": ("Code", "code-url-handler"),
    "codium": ("VSCodium",),
    "steam": ("Steam", "steam"),
    "vlc": ("vlc",),
    "spotify": ("spotify", "Spotify"),
    "discord": ("discord", "Discord"),
    "telegram": ("TelegramDesktop", "telegram-desktop"),
    "telegram-desktop": ("TelegramDesktop",),
    "zoom": ("Zoom", "zoom"),
    "obs": ("obs-studio",),
    "obs64": ("obs-studio",),
    "gimp": ("GIMP", "gimp"),
    "blender": ("blender",),
    "libreoffice": ("libreoffice",),
    "mupdf": ("mupdf",),
    "keepassxc": ("KeePassXC",),
}


def _keys_for(*values: str) -> set[str]:
    """Candidate folder names for an app, expanded through the alias table."""
    keys = _candidate_keys(*values)
    for token in list(keys):
        for alias in FOLDER_ALIASES.get(token, ()):
            keys.update(_candidate_keys(alias))
    return keys


def _matching_dirs(root: str, keys: set[str]) -> list[str]:
    found: list[str] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return found
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        name = entry.name
        if name in DENY_SEGMENTS or name.startswith("Trash"):
            continue
        normalised = {name.lower(), name.lower().replace("_", "-"), name.lower().replace(" ", "-")}
        if normalised & keys:
            found.append(entry.path)
    return found


def _user_dirs(keys: set[str]) -> list[DataDir]:
    dirs: list[DataDir] = []
    for root in CACHE_ROOTS:
        dirs.extend(DataDir(path, CACHE) for path in _matching_dirs(root, keys))
    for root in CONFIG_ROOTS:
        dirs.extend(DataDir(path, CONFIG) for path in _matching_dirs(root, keys))
    for root in DATA_ROOTS:
        dirs.extend(DataDir(path, DATA) for path in _matching_dirs(root, keys))
    return dirs


def _snap_dirs(name: str) -> list[DataDir]:
    base = os.path.join(HOME_DIR, "snap", name)
    if not os.path.isdir(base):
        return []
    cache: list[str] = []
    config: list[str] = []
    for revision in ("common", "current"):
        for sub, bucket in ((".cache", cache), (".config", config)):
            path = os.path.join(base, revision, sub)
            if os.path.lexists(path):
                real = os.path.realpath(path, strict=False)
                if real not in bucket:
                    bucket.append(real)
    dirs = [DataDir(path, CACHE) for path in cache]
    dirs += [DataDir(path, CONFIG) for path in config]
    dirs.append(DataDir(base, HOME))
    return dirs


def _snap_install_bytes(name: str, revision: str) -> int:
    total = 0
    candidates = [f"/var/lib/snapd/snaps/{name}_{revision}.snap", f"/var/lib/snapd/snaps/{name}.snap"]
    for path in candidates:
        try:
            st = os.stat(path)
        except OSError:
            continue
        total += st.st_blocks * 512
    return total


def scan_snaps(include_runtimes: bool) -> list[InstalledApp]:
    metadata = _snap_list()
    grouped = _snap_desktop_files()
    apps: list[InstalledApp] = []
    for name, info in sorted(metadata.items()):
        desktop_paths = grouped.get(name, [])
        desktop_file = _pick_desktop(desktop_paths)
        has_entry = bool(desktop_file)
        runtime = bool(RUNTIME_HINTS.match(name)) or info["base"] or not has_entry
        if runtime and not include_runtimes:
            continue
        entry = desktop_mod.read_desktop(desktop_file) if desktop_file else {}
        source_dir = os.path.dirname(desktop_file) if desktop_file else ""
        icon_path, icon_name = _icon_for(entry, source_dir)
        binary = os.path.basename(desktop_mod.exec_binary(entry.get("Exec", "")))
        keys = _keys_for(binary, name)
        dirs = _snap_dirs(name)
        if info["classic"]:
            dirs += _user_dirs(keys)
        apps.append(
            InstalledApp(
                uid=f"snap:{name}",
                name=entry.get("Name") or name.replace("-", " ").title(),
                source=SNAP,
                version=info["version"],
                publisher=info["publisher"] or "Unknown",
                summary=entry.get("Comment", ""),
                icon_path=icon_path,
                icon_name=icon_name or "package-x-generic",
                exec_line=entry.get("Exec", ""),
                desktop_file=desktop_file,
                snap_name=name,
                binary=binary or name,
                categories=entry.get("Categories", []),
                dirs=dirs,
                runtime=runtime,
                install_bytes=_snap_install_bytes(name, info["revision"]),
            )
        )
    return apps


def _appimage_version(stem: str) -> str:
    """Pick the last version-shaped token without swallowing architecture suffixes."""
    best = ""
    index = 0
    while index < len(stem):
        starts_token = index == 0 or stem[index - 1] in "-_ vV"
        if starts_token and stem[index].isdigit():
            start = index
            while index < len(stem) and (stem[index].isdigit() or stem[index] == "."):
                index += 1
            candidate = stem[start:index].rstrip(".")
            if candidate.count(".") in (1, 2, 3):
                best = candidate
            continue
        index += 1
    return best


def _appimage_files(root: str, max_depth: int):
    """Yield executable AppImages under root, without following directory symlinks."""
    try:
        base_depth = root.rstrip(os.sep).count(os.sep)
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            depth = directory.count(os.sep) - base_depth
            if depth >= max_depth:
                dirnames[:] = []
            else:
                dirnames[:] = [
                    name
                    for name in dirnames
                    if not name.startswith(".")
                    and name not in {"__pycache__", "target", "node_modules"}
                ]
            for filename in filenames:
                if not filename.lower().endswith(".appimage") or filename.endswith(".part"):
                    continue
                path = os.path.join(directory, filename)
                try:
                    stat = os.stat(path, follow_symlinks=True)
                except OSError:
                    continue
                if os.path.isfile(path) and (stat.st_mode & 0o111):
                    yield path
    except OSError:
        return


def scan_appimages() -> list[InstalledApp]:
    apps: list[InstalledApp] = []
    seen: set[str] = set()
    entries_index: list[tuple[str, dict]] = []
    for directory in SYSTEM_DESKTOP_DIRS:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".desktop"):
                continue
            path = os.path.join(directory, name)
            info = desktop_mod.read_desktop(path)
            entries_index.append((path, info))

    for root, depth in (
        (os.path.join(HOME_DIR, "Applications"), 2),
        (os.path.join(HOME_DIR, "Downloads"), 2),
        (os.path.join(HOME_DIR, "Desktop"), 1),
        (os.path.join(HOME_DIR, ".local/bin"), 0),
        ("/opt", 2),
        ("/usr/local/bin", 0),
    ):
        for path in _appimage_files(root, depth):
            try:
                st = os.stat(path)
            except OSError:
                continue
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            name = os.path.basename(path)
            stem = name[: name.lower().index(".appimage")]
            display = re.sub(r"[-_]\d.*$", "", stem).replace("-", " ").replace("_", " ").strip()
            matched_entry = ""
            for entry_path, info in entries_index:
                exec_binary = desktop_mod.exec_binary(info.get("Exec", ""))
                if exec_binary and os.path.realpath(exec_binary) == real:
                    matched_entry = entry_path
                    display = info.get("Name") or display
                    break
            keys = _keys_for(os.path.basename(stem), display)
            apps.append(
                InstalledApp(
                    uid=f"appimage:{os.path.basename(stem) or name}",
                    name=display or stem,
                    source=APPIMAGE,
                    version=_appimage_version(stem),
                    publisher="AppImage",
                    summary=f"Portable build in {os.path.dirname(path)}",
                    icon_name="application-x-executable",
                    exec_line=f'"{path}"',
                    desktop_file=matched_entry,
                    binary=os.path.basename(stem),
                    appimage_path=path,
                    dirs=_user_dirs(keys),
                    install_bytes=st.st_blocks * 512,
                    is_custom=bool(matched_entry and os.path.dirname(matched_entry) == OVERRIDE_APP_DIR),
                )
            )
    return apps


APP_CATEGORY = re.compile(
    r"^(AudioVideo|Audio|Video|Game|Graphics|Network|Office|Development|Education|Science|"
    r"WebBrowser|Email|Chat|Communication|Music|Photography|3DGraphics|VectorGraphics|"
    r"Publishing|ProjectManagement|FileTools|TerminalEmulator|TextEditor)$"
)
SYSTEM_ONLY = {
    "Settings",
    "System",
    "X-GNOME-Settings-Panel",
    "SystemSettings",
    "ConsoleOnly",
    "Screensaver",
    "TrayIcon",
    "GTK",
    "QT",
    "KDE",
    "X-FullScreen",
}


def is_main_app(categories: list[str]) -> bool:
    """True for end-user software, false for settings panels, daemons and file helpers."""
    return any(APP_CATEGORY.match(category) for category in categories)


def _package_candidates() -> list[tuple[str, dict, bool]]:
    """Desktop entries for non-snap GUI apps, deduped by filename (user entries win)."""
    chosen: dict[str, tuple[int, str, dict]] = {}
    for priority, directory in enumerate(SYSTEM_DESKTOP_DIRS):
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".desktop"):
                continue
            lowered = name.lower()
            if "ubuntuappcontrol" in lowered or "ubuntu-app-control" in lowered:
                continue
            path = os.path.join(directory, name)
            if os.path.islink(path) and not os.path.exists(path):
                continue
            info = desktop_mod.read_desktop(path)
            if not info or info.get("NoDisplay") or info.get("Hidden"):
                continue
            if info.get("X-SnapInstanceName") or info.get("X-Flatpak"):
                continue
            if info.get(desktop_mod.UAC_ORIGIN_KEY, "").startswith("/var/lib/snapd"):
                continue
            exec_line = info.get("Exec", "")
            binary = desktop_mod.exec_binary(exec_line)
            if not binary or binary.startswith("/snap/bin"):
                continue
            categories = info.get("Categories", [])
            if not categories or set(categories) <= SYSTEM_ONLY:
                continue
            if "ConsoleOnly" in categories:
                continue
            resolved = binary if os.path.isabs(binary) else ""
            if resolved and not os.path.exists(os.path.realpath(resolved, strict=False)):
                continue
            previous = chosen.get(name)
            if previous is None or priority >= previous[0]:
                chosen[name] = (priority, path, info)
    return [
        (path, info, is_main_app(info.get("Categories", [])))
        for _priority, path, info in chosen.values()
    ]


def scan_packages() -> list[InstalledApp]:
    apps: list[InstalledApp] = []
    for path, info, main_app in _package_candidates():
        stem = os.path.splitext(os.path.basename(path))[0]
        binary = desktop_mod.exec_binary(info.get("Exec", ""))
        icon_path, icon_name = _icon_for(info, os.path.dirname(path))
        display = info.get("Name") or stem
        keys = _keys_for(stem, os.path.basename(binary), display)
        apps.append(
            InstalledApp(
                uid=f"deb:{stem}",
                name=display,
                source=DEB,
                publisher="",
                summary=info.get("Comment", ""),
                icon_path=icon_path,
                icon_name=icon_name or "application-x-executable",
                exec_line=info.get("Exec", ""),
                desktop_file=path,
                binary=os.path.basename(binary),
                categories=info.get("Categories", []),
                dirs=_user_dirs(keys),
                runtime=not main_app,
            )
        )
    return apps


def process_index() -> set[str]:
    """Identifiers for everything currently running."""
    running: set[str] = set()
    try:
        pids = os.listdir("/proc")
    except OSError:
        return running
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                raw = handle.read()
        except OSError:
            continue
        if not raw:
            continue
        tokens = [token.decode("utf-8", "ignore") for token in raw.split(b"\0") if token]
        joined = " ".join(tokens)
        for token in tokens[:3]:
            if token:
                running.add(os.path.basename(token))
        match = re.search(r"/snap/([a-zA-Z0-9_-]+)/", joined)
        if match:
            running.add(f"snap:{match.group(1)}")
        if "/snap/bin/" in joined:
            running.add(f"snap-bin:{os.path.basename(tokens[0])}")
    return running


def mark_running(apps: list[InstalledApp]) -> None:
    running = process_index()
    for app in apps:
        if app.source == SNAP:
            app.running = f"snap:{app.snap_name}" in running or app.binary in running
        else:
            app.running = app.binary in running


def _outermost(paths: list[str]) -> list[str]:
    """Keep only the widest folders so a parent and its child are never counted twice."""
    roots: list[str] = []
    for path in sorted(set(paths), key=lambda item: (len(item), item)):
        real = os.path.realpath(path, strict=False)
        if any(real != kept and real.startswith(kept + os.sep) for kept in roots):
            continue
        roots.append(real)
    return roots


def cache_targets(app: InstalledApp) -> list[str]:
    return _outermost([d.path for d in app.dirs_of(CACHE)])


def data_targets(app: InstalledApp) -> list[str]:
    return _outermost([d.path for d in app.dirs_of(HOME, DATA, CONFIG)])


def measure_app(app: InstalledApp) -> None:
    """Fill in cache and user-data sizes without double counting nested folders."""
    cache_paths = cache_targets(app)
    app.cache_bytes = sum(measure(path).bytes for path in cache_paths)
    total = 0
    for root in data_targets(app):
        skip = {
            path
            for path in set(cache_paths) | set(data_targets(app))
            if path != root and path.startswith(root + os.sep)
        }
        total += measure_exclusive(root, skip).bytes if skip else measure(root).bytes
    app.data_bytes = total


def gpu_profile(app: InstalledApp) -> str:
    """The pinned GPU profile, if this app's launcher is one we rewrote."""
    if not app.desktop_file or os.path.dirname(app.desktop_file) != OVERRIDE_APP_DIR:
        return "auto"
    info = desktop_mod.read_desktop(app.desktop_file)
    return info.get(desktop_mod.UAC_GPU_KEY) or "auto"


def scan(include_runtimes: bool = False) -> list[InstalledApp]:
    apps = scan_snaps(include_runtimes)
    apps += scan_appimages()
    apps += scan_packages()
    mark_running(apps)
    for app in apps:
        app.gpu = gpu_profile(app)
    return apps
