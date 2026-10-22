use gio::prelude::*;
use gtk4::{
    prelude::*, Application, ApplicationWindow, Box as GtkBox, Button, ComboBoxText, Label,
    ListBox, ListBoxRow, Orientation, Paned, ScrolledWindow, SearchEntry, Separator,
};
use serde::{Deserialize, Serialize};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::{
    cell::RefCell,
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
    process::Command,
    rc::Rc,
    sync::{mpsc, Mutex},
    thread,
    time::Duration,
};

const APP_ID: &str = "com.local.UbuntuAppControl";
const APP_NAME: &str = "Ubuntu App Control";
const QUARANTINE_MARKER: &str = "ubuntu-app-control";

/// The only places this program will ever move or delete app data from.
/// Keep this list in sync with `safe_user_data_path`.
fn allowed_data_roots() -> Vec<PathBuf> {
    let h = home();
    let roots = vec![
        h.join(".cache"),
        h.join(".config"),
        h.join(".local/share"),
        h.join(".local/state"),
        h.join(".var/app"),
        h.join(".mozilla"),
        h.join(".thunderbird"),
        h.join(".steam"),
        h.join(".vscode"),
        h.join(".wine"),
        h.join("snap"),
        quarantine_root(),
    ];
    roots
}

/// Folder names that are too important to ever belong to one application.
const DENY_NAMES: &[&str] = &[
    "applications",
    "backgrounds",
    "certs",
    "certificates",
    "dconf",
    "desktop",
    "doc",
    "docs",
    "documents",
    "download",
    "downloads",
    "fonts",
    "gnome",
    "gtk-2.0",
    "gtk-3.0",
    "gtk-4.0",
    "icons",
    "keyrings",
    "locale",
    "mime",
    "music",
    "pictures",
    "public",
    "screenshot",
    "screenshots",
    "systemd",
    "templates",
    "themes",
    "trash",
    "ubuntu-app-control",
    "videos",
    "xdg",
];

#[derive(Clone, Debug, Serialize, Deserialize)]
struct DataDir {
    path: String,
    kind: String,
}

#[derive(Clone, Debug)]
struct App {
    id: String,
    name: String,
    source: String,
    version: String,
    publisher: String,
    icon: String,
    binary: String,
    exec: String,
    desktop: String,
    snap: String,
    image: String,
    runtime: bool,
    running: bool,
    dirs: Vec<DataDir>,
    installed: u64,
    cache: u64,
    data: u64,
}

#[derive(Clone, Default, Serialize, Deserialize)]
struct Preferences {
    #[serde(default)]
    gpu: BTreeMap<String, String>,
    #[serde(default)]
    quarantine: Vec<Quarantine>,
    #[serde(default)]
    freed: BTreeMap<String, u64>,
}

#[derive(Clone, Default, Serialize, Deserialize)]
struct Quarantine {
    id: String,
    app: String,
    #[serde(default)]
    bytes: u64,
    #[serde(default)]
    items: Vec<QuarantineItem>,
}

#[derive(Clone, Default, Serialize, Deserialize)]
struct QuarantineItem {
    from: String,
    to: String,
    #[serde(default)]
    bytes: u64,
}

struct Ui {
    window: ApplicationWindow,
    list: ListBox,
    detail: GtkBox,
    search: SearchEntry,
    filter: ComboBoxText,
    status: Label,
    apps: Vec<App>,
    selected: Option<String>,
    preferences: Preferences,
    scan_id: u64,
}

fn home() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/tmp"))
}
fn data_home() -> PathBuf {
    std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home().join(".local/share"))
}
fn config_home() -> PathBuf {
    std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home().join(".config"))
}
fn prefs_path() -> PathBuf {
    config_home().join("ubuntu-app-control/prefs.json")
}
fn quarantine_root() -> PathBuf {
    data_home().join("ubuntu-app-control/quarantine")
}
fn read_prefs() -> Preferences {
    fs::read(prefs_path())
        .ok()
        .and_then(|v| serde_json::from_slice(&v).ok())
        .unwrap_or_default()
}
/// Serialises read-modify-write cycles for the preferences file. Without this a
/// GPU choice saved while a clear/restore is running can overwrite the
/// quarantine manifest and make the user's data unrecoverable.
static PREFS_LOCK: Mutex<()> = Mutex::new(());
fn save_prefs(p: &Preferences) -> Result<(), String> {
    let path = prefs_path();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("Could not create {}: {e}", parent.display()))?;
        let _ = fs::set_permissions(parent, fs::Permissions::from_mode(0o700));
    }
    let bytes = serde_json::to_vec_pretty(p).map_err(|e| e.to_string())?;
    let tmp = path.with_extension("tmp");
    fs::write(&tmp, bytes).map_err(|e| format!("Could not write {}: {e}", tmp.display()))?;
    let _ = fs::set_permissions(&tmp, fs::Permissions::from_mode(0o600));
    fs::rename(&tmp, &path).map_err(|e| format!("Could not save {}: {e}", path.display()))?;
    Ok(())
}
fn ensure_private_dir(path: &Path) -> Result<(), String> {
    fs::create_dir_all(path).map_err(|e| format!("Could not create {}: {e}", path.display()))?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
        .map_err(|e| format!("Could not secure {}: {e}", path.display()))
}
fn update_prefs<T>(f: impl FnOnce(&mut Preferences) -> T) -> Result<T, String> {
    let _guard = PREFS_LOCK.lock().map_err(|_| "Preferences lock is poisoned")?;
    let mut prefs = read_prefs();
    let value = f(&mut prefs);
    save_prefs(&prefs)?;
    Ok(value)
}
/// Run a command with a hard timeout using coreutils `timeout` (already a
/// declared dependency). A hung `snap list` or `du` must never freeze a scan.
fn timed_output(args: &[&str], seconds: u64) -> Option<std::process::Output> {
    let timeout = trusted_system_binary("timeout");
    let has_timeout = timeout.is_some();
    let mut command = match timeout {
        Some(path) => {
            let mut c = Command::new(path);
            c.arg(seconds.to_string());
            c
        }
        None => Command::new(args[0]),
    };
    if has_timeout {
        command.args(args);
    } else {
        command.args(&args[1..]);
    }
    command.output().ok()
}
fn output_with_timeout(args: &[&str], seconds: u64) -> String {
    timed_output(args, seconds)
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).into_owned())
        .unwrap_or_default()
}
fn output(args: &[&str]) -> String {
    output_with_timeout(args, 30)
}
fn du(path: &str) -> u64 {
    timed_output(
        &["du", "-s", "--block-size=1", "--null", "--", path],
        120,
    )
    .filter(|o| o.status.success())
    .and_then(|o| {
        String::from_utf8_lossy(&o.stdout)
            .split('\0')
            .next()?
            .split('\t')
            .next()?
            .parse()
            .ok()
    })
    .unwrap_or(0)
}
fn size_text(n: u64) -> String {
    let mut x = n as f64;
    for u in ["B", "KB", "MB", "GB", "TB"] {
        if x < 1024.0 || u == "TB" {
            return if u == "B" {
                format!("{} B", n)
            } else {
                format!("{x:.1} {u}")
            };
        }
        x /= 1024.0;
    }
    format!("{x:.1} TB")
}
fn exec_binary(exec: &str) -> String {
    let Ok(mut parts) = shell_words::split(exec) else {
        return String::new();
    };
    if parts
        .first()
        .is_some_and(|p| Path::new(p).file_name().is_some_and(|n| n == "env"))
    {
        parts.remove(0);
        while parts.first().is_some_and(|p| p.contains('=')) {
            parts.remove(0);
        }
    }
    parts.first().cloned().unwrap_or_default()
}
fn desktop(path: &Path) -> BTreeMap<String, String> {
    let mut map = BTreeMap::new();
    if let Ok(text) = fs::read_to_string(path) {
        let mut active = false;
        for line in text.lines() {
            if line.starts_with('[') {
                active = line == "[Desktop Entry]";
                continue;
            }
            if active {
                if let Some((k, v)) = line.split_once('=') {
                    map.insert(k.to_string(), v.to_string());
                }
            }
        }
    }
    map
}
fn app_dirs(name: &str, binary: &str, snap: &str) -> Vec<DataDir> {
    let mut keys = BTreeSet::new();
    for v in [name, binary, snap] {
        let v = v.to_lowercase();
        if v.len() > 2 {
            keys.insert(v.clone());
            keys.insert(v.replace(' ', "-"));
            keys.insert(v.replace('_', "-"));
        }
    }
    let normalized: Vec<_> = keys.iter().cloned().collect();
    for key in normalized {
        let aliases: &[&str] = match key.as_str() {
            "firefox" => &["mozilla"],
            "thunderbird" => &["thunderbird"],
            "google-chrome" | "google-chrome-stable" => &["google-chrome", "GoogleChrome"],
            "chromium" | "chromium-browser" => &["chromium"],
            "code" | "visual-studio-code" => &["Code", "vscode"],
            "codium" => &["VSCodium"],
            "discord" => &["discord", "Discord"],
            "steam" => &["Steam", "steam"],
            "spotify" => &["spotify", "Spotify"],
            "telegram" | "telegram-desktop" => &["TelegramDesktop", "telegram-desktop"],
            "zoom" => &["Zoom", "zoom"],
            "obs" | "obs64" => &["obs-studio"],
            "gimp" => &["GIMP", "gimp"],
            "keepassxc" => &["KeePassXC"],
            "vlc" => &["vlc"],
            "thunderbird-esr" => &["thunderbird"],
            _ => &[],
        };
        keys.extend(aliases.iter().map(|a| a.to_lowercase()));
    }
    let mut out = Vec::new();
    let h = home();
    for (root, kind) in [
        (h.join(".cache"), "cache"),
        (h.join(".config"), "config"),
        (h.join(".local/share"), "data"),
        (h.join(".local/state"), "data"),
        (h.join(".var/app"), "data"),
        (h.join(".mozilla"), "data"),
        (h.join(".thunderbird"), "data"),
        (h.join(".vscode"), "data"),
        (h.join(".steam"), "data"),
        (h.join(".wine"), "data"),
    ] {
        if let Ok(items) = fs::read_dir(&root) {
            for e in items.flatten() {
                let p = e.path();
                let n = e.file_name().to_string_lossy().to_lowercase();
                if !keys.contains(&n)
                    || DENY_NAMES.contains(&n.as_str())
                    || n.starts_with("trash")
                    || !p.is_dir()
                {
                    continue;
                }
                out.push(DataDir {
                    path: p.to_string_lossy().into_owned(),
                    kind: kind.into(),
                });
            }
        }
    }
    // Some apps own a dedicated dot-folder whose name does not match any child.
    for (root, key) in [
        (h.join(".mozilla"), "mozilla"),
        (h.join(".thunderbird"), "thunderbird"),
        (h.join(".vscode"), "vscode"),
        (h.join(".steam"), "steam"),
        (h.join(".wine"), "wine"),
    ] {
        if keys.contains(key) && root.is_dir() {
            let value = root.to_string_lossy().into_owned();
            if !out.iter().any(|d| d.path == value) {
                out.push(DataDir {
                    path: value,
                    kind: "data".into(),
                });
            }
        }
    }
    if !snap.is_empty() {
        let p = h.join("snap").join(snap);
        if p.is_dir() {
            out.push(DataDir {
                path: p.to_string_lossy().into_owned(),
                kind: "snap-home".into(),
            });
        }
    }
    out
}
fn snap_dirs(name: &str) -> Vec<DataDir> {
    let base = home().join("snap").join(name);
    if !base.is_dir() {
        return Vec::new();
    }
    let mut out = Vec::new();
    for revision in ["common", "current"] {
        for (sub, kind) in [(".cache", "cache"), (".config", "config")] {
            // Keep the path exactly as found. Canonicalising it here would let a
            // symlink make us operate on a different app's folder.
            let path = base.join(revision).join(sub);
            if path.exists() {
                let value = path.to_string_lossy().into_owned();
                if !out.iter().any(|d: &DataDir| d.path == value) {
                    out.push(DataDir {
                        path: value,
                        kind: kind.into(),
                    });
                }
            }
        }
    }
    out.push(DataDir {
        path: base.to_string_lossy().into_owned(),
        kind: "snap-home".into(),
    });
    out
}
fn pick_snap_desktop(paths: &[PathBuf]) -> String {
    let mut sorted = paths.to_vec();
    sorted.sort();
    let mut best: Option<(usize, String)> = None;
    for path in sorted {
        let d = desktop(&path);
        if d.get("NoDisplay").is_some_and(|v| v == "true")
            || d.get("Hidden").is_some_and(|v| v == "true")
        {
            continue;
        }
        let categories = d
            .get("Categories")
            .map(|s| s.split(';').filter(|x| !x.is_empty()).count())
            .unwrap_or(0);
        let name = d.get("Name").map(String::len).unwrap_or(0);
        let stem = path.file_stem().unwrap_or_default().to_string_lossy();
        let prefix_bonus = stem.split_once('_').is_some_and(|(a, b)| a == b) as usize * 5;
        let score = categories * 2 + name + prefix_bonus;
        if best.as_ref().is_none_or(|(old, _)| score > *old) {
            best = Some((score, path.to_string_lossy().into_owned()));
        }
    }
    best.map(|(_, p)| p)
        .or_else(|| paths.first().map(|p| p.to_string_lossy().into_owned()))
        .unwrap_or_default()
}
fn measure(app: &mut App) {
    let mut cache = 0u64;
    let mut data = 0u64;
    let mut dirs = app.dirs.clone();
    dirs.sort_by_key(|d| d.path.len());
    let sizes: BTreeMap<_, _> = dirs.iter().map(|d| (d.path.clone(), du(&d.path))).collect();
    for dir in &dirs {
        let nested: u64 = dirs
            .iter()
            .filter(|child| {
                child.path.starts_with(&(dir.path.clone() + "/"))
                    && !dirs.iter().any(|middle| {
                        middle.path != dir.path
                            && middle.path != child.path
                            && child.path.starts_with(&(middle.path.clone() + "/"))
                            && middle.path.starts_with(&(dir.path.clone() + "/"))
                    })
            })
            .map(|child| sizes.get(&child.path).copied().unwrap_or(0))
            .sum();
        let own = sizes
            .get(&dir.path)
            .copied()
            .unwrap_or(0)
            .saturating_sub(nested);
        if dir.kind == "cache" {
            cache += own;
        } else {
            data += own;
        }
    }
    app.cache = cache;
    app.data = data;
}
fn scan() -> Vec<App> {
    let mut apps = Vec::new();
    let snap_output = output(&["snap", "list"]);
    let mut snaps = BTreeMap::new();
    for line in snap_output.lines().skip(1) {
        let fields: Vec<_> = line.split_whitespace().collect();
        if fields.len() >= 5 {
            snaps.insert(
                fields[0].to_string(),
                (
                    fields[1].to_string(),
                    fields[4].trim_end_matches('*').to_string(),
                    fields[2].to_string(),
                    fields.get(5).is_some_and(|notes| notes.contains("base")),
                ),
            );
        }
    }
    let mut snap_desktops: BTreeMap<String, Vec<PathBuf>> = BTreeMap::new();
    if let Ok(files) = fs::read_dir("/var/lib/snapd/desktop/applications") {
        for e in files.flatten() {
            let path = e.path();
            if path.extension().and_then(|s| s.to_str()) != Some("desktop") {
                continue;
            }
            let d = desktop(&path);
            let snap = d.get("X-SnapInstanceName").cloned().unwrap_or_else(|| {
                path.file_name()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .split('_')
                    .next()
                    .unwrap_or("")
                    .to_string()
            });
            if snaps.contains_key(&snap) {
                snap_desktops.entry(snap).or_default().push(path);
            }
        }
    }
    for (snap, (version, publisher, revision, base)) in &snaps {
        let candidates = snap_desktops.remove(snap).unwrap_or_default();
        let desktop_path = pick_snap_desktop(&candidates);
        let d = if desktop_path.is_empty() {
            BTreeMap::new()
        } else {
            desktop(Path::new(&desktop_path))
        };
        let runtime_prefixes = [
            "core",
            "bare",
            "gtk-common-themes",
            "gnome-",
            "kf",
            "platform-",
            "mesa-",
            "snapd",
            "firmware-updater",
            "prompting-client",
            "desktop-security-center",
            "hotspot",
            "gtk-",
        ];
        let runtime = *base
            || desktop_path.is_empty()
            || runtime_prefixes
                .iter()
                .any(|prefix| snap.starts_with(prefix));
        let exec = d.get("Exec").cloned().unwrap_or_default();
        let binary = exec_binary(&exec);
        let binary = Path::new(&binary)
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .to_string();
        let name = d.get("Name").cloned().unwrap_or_else(|| {
            snap.replace('-', " ")
                .split_whitespace()
                .map(|w| {
                    let mut c = w.chars();
                    c.next()
                        .map(|f| f.to_uppercase().collect::<String>() + c.as_str())
                        .unwrap_or_default()
                })
                .collect::<Vec<_>>()
                .join(" ")
        });
        apps.push(App {
            id: format!("snap:{snap}"),
            name: name.clone(),
            source: "snap".into(),
            version: version.clone(),
            publisher: if publisher.is_empty() {
                "Unknown".into()
            } else {
                publisher.clone()
            },
            icon: d
                .get("Icon")
                .cloned()
                .unwrap_or_else(|| "package-x-generic".into()),
            binary: if binary.is_empty() {
                snap.clone()
            } else {
                binary
            },
            exec,
            desktop: desktop_path,
            snap: snap.clone(),
            image: String::new(),
            runtime,
            running: false,
            dirs: snap_dirs(snap),
            installed: du(&format!("/var/lib/snapd/snaps/{snap}_{revision}.snap")),
            cache: 0,
            data: 0,
        });
    }
    let mut seen = BTreeSet::new();
    for root in [
        data_home().join("applications"),
        PathBuf::from("/usr/local/share/applications"),
        PathBuf::from("/usr/share/applications"),
    ] {
        if let Ok(files) = fs::read_dir(root) {
            for e in files.flatten() {
                let path = e.path();
                if path.extension().and_then(|s| s.to_str()) != Some("desktop") {
                    continue;
                }
                let id = path
                    .file_name()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .to_string();
                if !seen.insert(id.clone()) || id.to_lowercase().contains("ubuntuappcontrol") {
                    continue;
                }
                let d = desktop(&path);
                if d.get("NoDisplay").is_some_and(|v| v == "true")
                    || d.get("Hidden").is_some_and(|v| v == "true")
                    || d.contains_key("X-SnapInstanceName")
                    || d.contains_key("X-Flatpak")
                    || d.get("X-UAC-Original")
                        .is_some_and(|v| !v.ends_with(".desktop"))
                {
                    continue;
                }
                let name = d
                    .get("Name")
                    .cloned()
                    .unwrap_or_else(|| id.trim_end_matches(".desktop").into());
                let exec = d.get("Exec").cloned().unwrap_or_default();
                let binary = exec_binary(&exec);
                if binary.is_empty() || binary.starts_with("/snap/bin") {
                    continue;
                }
                let cats = d.get("Categories").cloned().unwrap_or_default();
                if cats.is_empty() {
                    continue;
                }
                let categories: Vec<_> = cats.split(';').filter(|c| !c.is_empty()).collect();
                let system_only = [
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
                ];
                if categories.iter().all(|c| system_only.contains(c))
                    || categories.contains(&"ConsoleOnly")
                {
                    continue;
                }
                if Path::new(&binary).is_absolute() && !Path::new(&binary).exists() {
                    continue;
                }
                let main_category = [
                    "AudioVideo",
                    "Audio",
                    "Video",
                    "Game",
                    "Graphics",
                    "Network",
                    "Office",
                    "Development",
                    "Education",
                    "Science",
                    "WebBrowser",
                    "Email",
                    "Chat",
                    "Communication",
                    "Music",
                    "Photography",
                    "3DGraphics",
                    "VectorGraphics",
                    "Publishing",
                    "ProjectManagement",
                    "FileTools",
                    "TerminalEmulator",
                    "TextEditor",
                ];
                let runtime = !cats.split(';').any(|c| main_category.contains(&c));
                let dirs = app_dirs(&name, &binary, &"");
                let mut a = App {
                    id: format!("deb:{id}"),
                    name,
                    source: "deb".into(),
                    version: String::new(),
                    publisher: String::new(),
                    icon: d.get("Icon").cloned().unwrap_or_default(),
                    binary: Path::new(&binary)
                        .file_name()
                        .unwrap_or_default()
                        .to_string_lossy()
                        .into(),
                    exec,
                    desktop: path.to_string_lossy().into_owned(),
                    snap: String::new(),
                    image: String::new(),
                    runtime,
                    running: false,
                    dirs,
                    installed: 0,
                    cache: 0,
                    data: 0,
                };
                let package = find_package(&a);
                if let Some(pkg) = package {
                    let q = output(&["dpkg-query", "-W", "-f=${Installed-Size}\t${Status}", &pkg]);
                    if q.contains("install ok installed") {
                        a.installed = q
                            .split('\t')
                            .next()
                            .unwrap_or("0")
                            .parse::<u64>()
                            .unwrap_or(0)
                            * 1024;
                        a.version = output(&["dpkg-query", "-W", "-f=${Version}", &pkg])
                            .trim()
                            .to_string();
                        a.publisher = pkg;
                    }
                }
                apps.push(a);
            }
        }
    }
    let mut appimages_seen = BTreeSet::new();
    for (root, depth) in [
        (home().join("Applications"), 2),
        (home().join("Downloads"), 2),
        (home().join("Desktop"), 1),
        (home().join(".local/bin"), 0),
        (PathBuf::from("/opt"), 2),
        (PathBuf::from("/usr/local/bin"), 0),
    ] {
        walk_appimages(&root, &mut apps, &mut appimages_seen, depth);
    }
    for a in &mut apps {
        measure(a);
    }
    let running = running_programs();
    for app in &mut apps {
        let binary = Path::new(&app.binary)
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .to_lowercase();
        let image = Path::new(&app.image)
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .to_lowercase();
        app.running = (!binary.is_empty() && running.contains(&binary))
            || (!image.is_empty() && running.contains(&image));
    }
    apps.sort_by(|a, b| {
        a.runtime
            .cmp(&b.runtime)
            .then_with(|| a.name.to_lowercase().cmp(&b.name.to_lowercase()))
    });
    apps
}
fn running_programs() -> BTreeSet<String> {
    let mut names = BTreeSet::new();
    if let Ok(entries) = fs::read_dir("/proc") {
        for entry in entries.flatten() {
            if !entry
                .file_name()
                .to_string_lossy()
                .chars()
                .all(|c| c.is_ascii_digit())
            {
                continue;
            }
            if let Ok(bytes) = fs::read(entry.path().join("cmdline")) {
                if let Some(first) = bytes.split(|b| *b == 0).next() {
                    let command = Path::new(&String::from_utf8_lossy(first).into_owned())
                        .file_name()
                        .unwrap_or_default()
                        .to_string_lossy()
                        .to_lowercase();
                    if !command.is_empty() {
                        names.insert(command);
                    }
                }
            }
        }
    }
    names
}
fn version_from_name(stem: &str) -> String {
    let chars: Vec<char> = stem.chars().collect();
    let mut best = String::new();
    let mut i = 0;
    while i < chars.len() {
        let starts_token = i == 0 || matches!(chars[i - 1], '-' | '_' | ' ' | 'v' | 'V');
        if starts_token && chars[i].is_ascii_digit() {
            let start = i;
            while i < chars.len() && (chars[i].is_ascii_digit() || chars[i] == '.') {
                i += 1;
            }
            let candidate: String = chars[start..i].iter().collect();
            if candidate.contains('.') && candidate.matches('.').count() <= 3 {
                best = candidate;
            }
            continue;
        }
        i += 1;
    }
    best
}
fn walk_appimages(
    root: &Path,
    apps: &mut Vec<App>,
    seen: &mut BTreeSet<PathBuf>,
    depth: usize,
) {
    let Ok(it) = fs::read_dir(root) else { return };
    for e in it.flatten() {
        let p = e.path();
        let name = e.file_name().to_string_lossy().to_string();
        if p.is_dir() {
            // AppImages are frequently kept in a folder named after the app.
            // Recurse a couple of levels, but never into hidden or build dirs.
            if depth > 0 && !name.starts_with('.') && name != "__pycache__" && name != "target" {
                walk_appimages(&p, apps, seen, depth - 1);
            }
            continue;
        }
        let is_executable = fs::metadata(&p)
            .map(|m| m.permissions().mode() & 0o111 != 0)
            .unwrap_or(false);
        if !is_executable
            || !p
                .extension()
                .is_some_and(|x| x.eq_ignore_ascii_case("appimage"))
        {
            continue;
        }
        // A symlink in ~/.local/bin and the real file in /opt are the same app.
        let real = fs::canonicalize(&p).unwrap_or_else(|_| p.clone());
        if !seen.insert(real) {
            continue;
        }
        let stem = p
            .file_stem()
            .unwrap_or_default()
            .to_string_lossy()
            .to_string();
        let name = stem.replace('_', " ");
        let dirs = app_dirs(&name, &stem, "");
        apps.push(App {
            id: format!("appimage:{}", p.display()),
            name,
            binary: p.to_string_lossy().into_owned(),
            source: "appimage".into(),
            version: version_from_name(&stem),
            publisher: "AppImage".into(),
            icon: "application-x-executable".into(),
            exec: String::new(),
            desktop: String::new(),
            snap: String::new(),
            image: p.to_string_lossy().into_owned(),
            runtime: false,
            running: false,
            dirs,
            installed: fs::metadata(&p).map(|m| m.blocks() * 512).unwrap_or(0),
            cache: 0,
            data: 0,
        });
    }
}
fn find_packages(a: &App) -> Vec<String> {
    let mut paths = Vec::new();
    if !a.binary.is_empty() {
        if let Some(found) = which(&a.binary) {
            paths.push(found);
        }
    }
    if !a.desktop.is_empty() {
        paths.push(a.desktop.clone());
    }
    let mut found = BTreeSet::new();
    for p in paths {
        if p.is_empty() {
            continue;
        }
        let out = output(&["dpkg-query", "-S", &p]);
        for line in out.lines() {
            // dpkg-query -S prints "package[,package]: /path".
            let Some((packages, _)) = line.split_once(": ") else {
                continue;
            };
            for name in packages
                .split(',')
                .map(|name| name.trim().trim_end_matches(":amd64").trim_end_matches(":i386"))
                .filter(|name| is_valid_package_name(name))
            {
                found.insert(name.to_string());
            }
        }
    }
    found.into_iter().collect()
}
fn find_package(a: &App) -> Option<String> {
    let packages = find_packages(a);
    if packages.len() == 1 {
        Some(packages[0].clone())
    } else {
        None
    }
}
fn is_valid_package_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 200
        && name
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '+' | '-' | '.'))
        && name.chars().next().is_some_and(|c| c.is_ascii_alphanumeric())
}
fn is_valid_snap_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 80
        && name.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
        && name
            .chars()
            .next()
            .is_some_and(|c| c.is_ascii_alphanumeric())
        && name
            .chars()
            .last()
            .is_some_and(|c| c.is_ascii_alphanumeric())
}
/// Resolve a command only inside root-owned system directories. Never search
/// the user's PATH for something that pkexec will execute as root.
fn trusted_system_binary(name: &str) -> Option<PathBuf> {
    if name.is_empty() || name.contains('/') {
        return None;
    }
    for dir in ["/usr/bin", "/bin", "/usr/sbin", "/sbin"] {
        let candidate = Path::new(dir).join(name);
        let Ok(real) = fs::canonicalize(&candidate) else {
            continue;
        };
        let Ok(meta) = fs::metadata(&real) else {
            continue;
        };
        if meta.uid() == 0
            && meta.is_file()
            && meta.mode() & 0o111 != 0
            && meta.mode() & 0o022 == 0
        {
            return Some(real);
        }
    }
    None
}
fn run_privileged(program: &str, args: &[&str]) -> Result<String, String> {
    let pkexec = trusted_system_binary("pkexec")
        .ok_or_else(|| "pkexec is not installed, so this action cannot run.".to_string())?;
    let path = trusted_system_binary(program)
        .ok_or_else(|| format!("Required system command {program} was not found."))?;
    let out = Command::new(pkexec)
        .arg(path)
        .args(args)
        .output()
        .map_err(|e| e.to_string())?;
    let text = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    if out.status.success() {
        Ok(text)
    } else if out.status.code() == Some(126) {
        Err("Authorization was cancelled.".into())
    } else {
        Err(text
            .lines()
            .filter(|l| !l.trim().is_empty())
            .last()
            .unwrap_or("Removal failed.")
            .to_string())
    }
}
fn which(program: &str) -> Option<String> {
    let path = Path::new(program);
    if path.is_absolute() {
        return is_executable(path).then(|| program.to_string());
    }
    std::env::var("PATH")
        .ok()?
        .split(':')
        .map(|p| Path::new(p).join(program))
        .find(|p| is_executable(p))
        .map(|p| p.to_string_lossy().into_owned())
}
fn is_executable(path: &Path) -> bool {
    fs::metadata(path)
        .map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}
type GpuProfile = (String, String, BTreeMap<String, String>);
fn pci_label(pci: &str, fallback: &str) -> String {
    let out = output_with_timeout(&["lspci", "-s", pci], 5);
    for line in out.lines() {
        let Some((_, rest)) = line.split_once(": ") else {
            continue;
        };
        let mut label = rest.trim().to_string();
        // Prefer the bracketed marketing name, e.g. "GA104 [GeForce RTX 3070]".
        if let Some(start) = label.rfind('[') {
            if let Some(end) = label[start..].find(']') {
                let inside = &label[start + 1..start + end];
                if inside.chars().any(|c| c.is_ascii_alphabetic()) {
                    label = inside.to_string();
                }
            }
        }
        label = label
            .replace("(rev ", "(")
            .trim_matches(|c: char| c == ' ' || c == '(' || c == ')')
            .to_string();
        if label.len() > 2 {
            return label;
        }
    }
    fallback.to_string()
}
fn gpu_profiles() -> Vec<GpuProfile> {
    let mut out = vec![("auto".into(), "System default".into(), BTreeMap::new())];
    let root = Path::new("/sys/bus/pci/devices");
    let Ok(entries) = fs::read_dir(root) else {
        return out;
    };
    let mut devices = Vec::new();
    for e in entries.flatten() {
        let p = e.path();
        let class = fs::read_to_string(p.join("class")).unwrap_or_default();
        if !class.starts_with("0x03") {
            continue;
        }
        let vendor = fs::read_to_string(p.join("vendor")).unwrap_or_default();
        let driver = fs::read_link(p.join("driver"))
            .ok()
            .and_then(|p| p.file_name().map(|x| x.to_string_lossy().to_string()))
            .unwrap_or_default();
        let card = fs::read_dir(p.join("drm"))
            .ok()
            .and_then(|entries| {
                entries
                    .flatten()
                    .filter_map(|e| {
                        let name = e.file_name().to_string_lossy().to_string();
                        name.strip_prefix("card")
                            .and_then(|n| n.parse::<u32>().ok())
                            .map(|n| (n, name))
                    })
                    .min_by_key(|(n, _)| *n)
                    .map(|(_, name)| name)
            })
            .unwrap_or_default();
        let boot = fs::read_to_string(p.join("boot_vga"))
            .map(|v| v.trim() == "1")
            .unwrap_or(false);
        let discrete =
            vendor.trim() == "0x10de"
                || ["nvidia", "nouveau"].contains(&driver.as_str())
                || (["0x1002", "0x8086"].contains(&vendor.trim()) && !boot);
        devices.push((
            p.file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .to_string(),
            vendor,
            driver,
            discrete,
            card,
            boot,
        ));
    }
    // Integrated first, then discrete; keep the boot GPU first inside each
    // group. This makes DRI_PRIME=0 the display GPU, matching Mesa's convention.
    devices.sort_by_key(|d| (!d.5, d.3, d.0.clone()));
    if devices.len() <= 1 {
        return out;
    }
    let mut prime = 0;
    for (pci, vendor, driver, discrete, card, _boot) in devices {
        let mut env = BTreeMap::new();
        let id;
        if discrete && vendor.trim() == "0x10de" {
            id = format!("dgpu:{pci}");
            env.insert("__NV_PRIME_RENDER_OFFLOAD".into(), "1".into());
            env.insert("__GLX_VENDOR_LIBRARY_NAME".into(), "nvidia".into());
            if let Ok(n) = fs::read_dir("/proc/driver/nvidia/gpus") {
                let count = n.count().saturating_sub(1);
                env.insert(
                    "__NV_PRIME_RENDER_OFFLOAD_PROVIDER".into(),
                    format!("NVIDIA-G{count}"),
                );
            }
        } else {
            id = format!("{}:{pci}", if discrete { "dgpu" } else { "igpu" });
            env.insert("DRI_PRIME".into(), prime.to_string());
        }
        let model = pci_label(&pci, if card.is_empty() { &driver } else { &card });
        let label = format!(
            "{} GPU - {}",
            if discrete {
                "High performance"
            } else {
                "Low power"
            },
            model
        );
        out.push((id, label, env));
        prime += 1;
    }
    out
}
fn launch_app(app: &App, prefs: &Preferences) -> Result<String, String> {
    let mut command = if app.source == "appimage" {
        vec![app.image.clone()]
    } else {
        shell_words::split(&app.exec).map_err(|e| e.to_string())?
    };
    if command.is_empty() {
        return Err(format!("No launch command for {}", app.name));
    }
    let mut inherited = BTreeMap::new();
    if command
        .first()
        .is_some_and(|x| Path::new(x).file_name().is_some_and(|n| n == "env"))
    {
        command.remove(0);
        while command.first().is_some_and(|x| x.contains('=')) {
            let pair = command.remove(0);
            if let Some((k, v)) = pair.split_once('=') {
                inherited.insert(k.to_string(), v.to_string());
            }
        }
    }
    for token in &mut command {
        for field in [
            "%f", "%F", "%u", "%U", "%d", "%D", "%n", "%N", "%i", "%c", "%k", "%v", "%m", "%%",
        ] {
            if token == field {
                token.clear();
                break;
            }
            *token = token.replace(field, "");
        }
    }
    command.retain(|x| !x.is_empty());
    let program = command
        .first()
        .cloned()
        .ok_or("No executable in launcher")?;
    let resolved = which(&program).ok_or_else(|| format!("Could not find {program}"))?;
    let mut process = Command::new(resolved);
    for arg in command.iter().skip(1) {
        process.arg(arg);
    }
    for (k, v) in inherited {
        process.env(k, v);
    }
    if !app.desktop.is_empty() {
        if let Some(path) = desktop(Path::new(&app.desktop)).get("Path") {
            if Path::new(path).is_dir() {
                process.current_dir(path);
            }
        }
    }
    if let Some(profile) = prefs.gpu.get(&app.id) {
        if let Some((_, _, env)) = gpu_profiles().into_iter().find(|(id, _, _)| id == profile) {
            for (k, v) in env {
                process.env(k, v);
            }
        }
    }
    process
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .map_err(|e| format!("Could not launch {}: {e}", app.name))
        .map(|_| format!("Started {}", app.name))
}
/// Quote a value for the Desktop Entry `Exec` key. This is NOT shell quoting:
/// the desktop spec only understands double quotes and backslash escapes.
fn desktop_quote(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len() + 2);
    escaped.push('"');
    for c in value.chars() {
        match c {
            '\\' => escaped.push_str("\\\\"),
            '"' => escaped.push_str("\\\""),
            '`' => escaped.push_str("\\`"),
            '$' => escaped.push_str("\\$"),
            '%' => escaped.push_str("%%"),
            '\n' => escaped.push(' '),
            _ => escaped.push(c),
        }
    }
    escaped.push('"');
    escaped
}
fn slug(value: &str) -> String {
    let mut out = String::new();
    let mut last_dash = false;
    for c in value.to_lowercase().chars() {
        if c.is_ascii_alphanumeric() {
            out.push(c);
            last_dash = false;
        } else if !last_dash {
            out.push('-');
            last_dash = true;
        }
    }
    out.trim_matches('-').chars().take(48).collect()
}
fn stable_hash(value: &str) -> u64 {
    // FNV-1a. Unlike DefaultHasher this stays stable across Rust releases, so
    // the generated launcher name can always be found again.
    let mut hash = 0xcbf2_9ce4_8422_2325u64;
    for byte in value.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    hash
}
fn appimage_launcher_path(app: &App) -> PathBuf {
    let slug = slug(&app.name);
    let slug = if slug.is_empty() { "appimage".to_string() } else { slug };
    data_home()
        .join("applications")
        .join(format!("uac-{slug}-{:016x}.desktop", stable_hash(&app.image)))
}
fn refresh_desktop_database() {
    let dir = data_home().join("applications");
    if let Some(db) = trusted_system_binary("update-desktop-database") {
        let _ = Command::new(db).arg(dir).status();
    }
}
fn safe_metadata_value(value: &str) -> String {
    value.replace(['\n', '\r'], " ")
}
/// Replace the `Exec` line (when requested) and the UAC metadata keys in the
/// `[Desktop Entry]` group, leaving every other group untouched.
fn update_desktop_text(
    text: &str,
    new_exec: Option<&str>,
    metadata: &[(&str, String)],
) -> Result<String, String> {
    let mut result = String::with_capacity(text.len() + 160);
    let mut in_entry = false;
    let mut saw_entry = false;
    let mut replaced_exec = false;
    let mut metadata_written = false;
    let push_metadata = |out: &mut String| {
        for (key, value) in metadata {
            out.push_str(key);
            out.push('=');
            out.push_str(&safe_metadata_value(value));
            out.push('\n');
        }
    };
    for line in text.lines() {
        if line.starts_with('[') {
            if in_entry && !metadata.is_empty() && !metadata_written {
                push_metadata(&mut result);
                metadata_written = true;
            }
            in_entry = line == "[Desktop Entry]";
            saw_entry |= in_entry;
            result.push_str(line);
            result.push('\n');
            continue;
        }
        if in_entry {
            if line.starts_with("Exec=") {
                if let Some(exec) = new_exec {
                    result.push_str("Exec=");
                    result.push_str(&safe_metadata_value(exec));
                    result.push('\n');
                    replaced_exec = true;
                } else {
                    result.push_str(line);
                    result.push('\n');
                }
                continue;
            }
            if line.starts_with("X-UAC-GPU=")
                || line.starts_with("X-UAC-Original=")
                || line.starts_with("X-UAC-Original-Exec=")
            {
                continue;
            }
        }
        result.push_str(line);
        result.push('\n');
    }
    if in_entry && !metadata.is_empty() && !metadata_written {
        push_metadata(&mut result);
    }
    if !saw_entry {
        return Err("The launcher has no [Desktop Entry] group.".into());
    }
    if new_exec.is_some() && !replaced_exec {
        return Err("The launcher has no Exec line, so its GPU cannot be pinned.".into());
    }
    Ok(result)
}
fn write_launcher(target: &Path, text: &str) -> Result<(), String> {
    if let Ok(meta) = fs::symlink_metadata(target) {
        if meta.file_type().is_symlink() {
            return Err(format!(
                "Refusing to overwrite the symlinked launcher {}.",
                target.display()
            ));
        }
    }
    fs::write(target, text).map_err(|e| format!("Could not save GPU launcher: {e}"))?;
    fs::set_permissions(target, fs::Permissions::from_mode(0o644))
        .map_err(|e| format!("Could not set launcher permissions: {e}"))
}
fn apply_gpu(app: &App, id: &str, profiles: &[GpuProfile]) -> Result<String, String> {
    let Some((_, label, env)) = profiles.iter().find(|(key, _, _)| key == id) else {
        return Err(format!("Unknown GPU profile: {id}"));
    };
    let dir = data_home().join("applications");
    fs::create_dir_all(&dir).map_err(|e| format!("Could not create {}: {e}", dir.display()))?;
    if app.source == "appimage" {
        let target = appimage_launcher_path(app);
        if id == "auto" {
            match fs::remove_file(&target) {
                Ok(()) => {}
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => return Err(format!("Could not remove {}: {e}", target.display())),
            }
            refresh_desktop_database();
            return Ok(format!("{} reset to the system GPU default", app.name));
        }
        let text = format!(
            "[Desktop Entry]\nType=Application\nName={}\nExec={} %u\nIcon=application-x-executable\nTerminal=false\nCategories=Utility;\nStartupNotify=true\nX-UAC-GPU={id}\n",
            app.name.replace(['\n', '\r'], " "),
            desktop_quote(&app.image)
        );
        write_launcher(&target, &text)?;
        refresh_desktop_database();
        return Ok(format!("{} will use {label}", app.name));
    }
    if app.desktop.is_empty() {
        return Err("This app has no launcher entry to update.".into());
    }
    let current = PathBuf::from(&app.desktop);
    if !current.is_absolute() {
        return Err("The app launcher path is not absolute.".into());
    }
    if fs::symlink_metadata(&current)
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false)
    {
        return Err("Refusing to edit a symlinked launcher.".into());
    }
    let current_map = desktop(&current);
    let original = current_map
        .get("X-UAC-Original")
        .filter(|path| Path::new(path) != &current)
        .cloned();
    let source = original
        .as_ref()
        .map(PathBuf::from)
        .unwrap_or_else(|| current.clone());
    let in_place = original.is_none() && current.starts_with(&dir);
    let target = if in_place || original.is_some() {
        current.clone()
    } else {
        dir.join(current.file_name().unwrap_or_default())
    };
    let text = fs::read_to_string(&source)
        .map_err(|e| format!("Could not read the app launcher {}: {e}", source.display()))?;
    let base_exec = current_map
        .get("X-UAC-Original-Exec")
        .cloned()
        .or_else(|| desktop(&source).get("Exec").cloned())
        .ok_or_else(|| "The launcher has no Exec line.".to_string())?;
    if id == "auto" {
        if target != source {
            match fs::remove_file(&target) {
                Ok(()) => {}
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => return Err(format!("Could not remove {}: {e}", target.display())),
            }
        } else if current_map.contains_key("X-UAC-Original-Exec") {
            let updated = update_desktop_text(&text, Some(&base_exec), &[])?;
            write_launcher(&target, &updated)?;
        } else if current_map.contains_key("X-UAC-GPU")
            || current_map.contains_key("X-UAC-Original")
            || current_map.contains_key("X-UAC-Original-Exec")
        {
            // Legacy in-place file: remove our metadata even though we do not
            // know the exact pre-override command.
            let updated = update_desktop_text(&text, None, &[])?;
            write_launcher(&target, &updated)?;
        }
        refresh_desktop_database();
        return Ok(format!("{} reset to the system GPU default", app.name));
    }
    let prefix = env
        .iter()
        .map(|(k, v)| desktop_quote(&format!("{k}={v}")))
        .collect::<Vec<_>>()
        .join(" ");
    let new_exec = format!("env {prefix} {base_exec}");
    let mut metadata = vec![
        ("X-UAC-GPU", id.to_string()),
        ("X-UAC-Original-Exec", base_exec.clone()),
    ];
    if target != source {
        metadata.push(("X-UAC-Original", source.to_string_lossy().into_owned()));
    }
    let result = update_desktop_text(&text, Some(&new_exec), &metadata)?;
    write_launcher(&target, &result)?;
    refresh_desktop_database();
    Ok(format!("{} will use {label}", app.name))
}
fn remove_launcher_override(app: &App) {
    let dir = data_home().join("applications");
    if app.source == "appimage" {
        let _ = fs::remove_file(appimage_launcher_path(app));
    } else if !app.desktop.is_empty() {
        let path = Path::new(&app.desktop);
        if path.starts_with(&dir) {
            let map = desktop(path);
            let original = map.get("X-UAC-Original").filter(|p| Path::new(p) != path);
            if original.is_some() {
                // This is our copy of a system launcher. Removing it reveals the
                // untouched system entry again.
                let _ = fs::remove_file(path);
            } else if let Some(original_exec) = map.get("X-UAC-Original-Exec") {
                // The user's only launcher was edited in place; put its command
                // back instead of deleting the file.
                if let Ok(text) = fs::read_to_string(path) {
                    if let Ok(updated) = update_desktop_text(&text, Some(original_exec), &[]) {
                        let _ = write_launcher(path, &updated);
                    }
                }
            }
        }
    }
    refresh_desktop_database();
}
fn remove_app(app: &App) -> Result<String, String> {
    let result = match app.source.as_str() {
        "snap" => {
            if app.snap.is_empty() || !is_valid_snap_name(&app.snap) {
                return Err("This Snap has no valid package name.".into());
            }
            run_privileged("snap", &["remove", &app.snap])?;
            Ok(format!("Uninstalled {}", app.name))
        }
        "appimage" => {
            let gio = which("gio").ok_or_else(|| "gio is not installed.".to_string())?;
            let out = Command::new(gio)
                .args(["trash", "--", &app.image])
                .output()
                .map_err(|e| e.to_string())?;
            if out.status.success() {
                Ok(format!("Moved {} to Trash", app.name))
            } else {
                Err(String::from_utf8_lossy(&out.stderr).trim().to_string())
            }
        }
        _ => {
            let packages = find_packages(app);
            let pkg = match packages.len() {
                0 => {
                    return Err(format!(
                        "Could not identify the Debian package for {} safely. Nothing was removed.",
                        app.name
                    ))
                }
                1 => packages[0].clone(),
                _ => {
                    return Err(format!(
                        "{} is shared by several packages ({}), so it was not removed automatically.",
                        app.name,
                        packages.join(", ")
                    ))
                }
            };
            run_privileged("apt-get", &["remove", "-y", &pkg])?;
            Ok(format!("Removed {pkg}"))
        }
    };
    if result.is_ok() {
        remove_launcher_override(app);
        let _ = update_prefs(|prefs| {
            prefs.gpu.remove(&app.id);
        });
    }
    result
}
fn app_is_running_now(app: &App) -> bool {
    let running = running_programs();
    [&app.binary, &app.image].iter().any(|value| {
        let name = Path::new(value)
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .to_lowercase();
        !name.is_empty() && running.contains(&name)
    })
}
fn clear_cache(app: &App) -> Result<String, String> {
    if app_is_running_now(app) {
        return Err(format!(
            "Close {} before clearing its cache, so open files cannot break.",
            app.name
        ));
    }
    let targets: Vec<_> = app
        .dirs
        .iter()
        .filter(|d| d.kind == "cache")
        .map(|d| d.path.clone())
        .collect();
    if targets.is_empty() {
        return Err(format!("{} has no cache folder.", app.name));
    }
    let target_count = targets.len();
    let mut found = 0usize;
    let mut freed = 0u64;
    let mut skipped = 0usize;
    for path in targets {
        let p = PathBuf::from(&path);
        if !p.exists() {
            continue;
        }
        found += 1;
        if !safe_user_data_path(&p)
            || fs::symlink_metadata(&p)
                .map(|m| m.file_type().is_symlink())
                .unwrap_or(true)
        {
            skipped += 1;
            continue;
        }
        let before = du(&path);
        let result = clear_directory_contents(&p);
        let after = du(&path);
        freed += before.saturating_sub(after);
        if result.is_err() {
            skipped += 1;
        }
    }
    if found == 0 {
        return Err("The cache folder is already gone.".into());
    }
    if freed == 0 && skipped == target_count {
        return Err("Nothing could be cleared; the folder may be read-only.".into());
    }
    let _ = update_prefs(|prefs| {
        let entry = prefs.freed.entry(app.id.clone()).or_insert(0);
        *entry += freed;
        let total = prefs.freed.entry("__total__".into()).or_insert(0);
        *total += freed;
    });
    let suffix = if skipped > 0 {
        format!(" ({skipped} folder(s) skipped)")
    } else {
        String::new()
    };
    Ok(format!(
        "Freed {} of cache for {}{}",
        size_text(freed),
        app.name,
        suffix
    ))
}
fn clear_directory_contents(dir: &Path) -> Result<(), String> {
    let entries = fs::read_dir(dir).map_err(|e| format!("Could not read {}: {e}", dir.display()))?;
    let mut failures = Vec::new();
    for entry in entries {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error) => {
                failures.push(error.to_string());
                continue;
            }
        };
        let path = entry.path();
        let result = match fs::symlink_metadata(&path) {
            Ok(meta) if meta.file_type().is_dir() => fs::remove_dir_all(&path),
            Ok(_) => fs::remove_file(&path),
            Err(error) => Err(error),
        };
        if let Err(error) = result {
            failures.push(format!("{}: {error}", path.display()));
        }
    }
    if failures.is_empty() {
        Ok(())
    } else {
        Err(failures.join("; "))
    }
}
/// Drop paths that live inside another selected path. Moving `~/snap/app`
/// already carries every revision/cache child with it; trying to move a child
/// afterwards would fail, and restoring children before their parent creates a
/// destination conflict. Working top-down avoids both problems.
fn sorted_top_level_targets(paths: impl IntoIterator<Item = String>) -> Vec<String> {
    let mut all: Vec<String> = paths.into_iter().collect();
    all.sort_by_key(|p| (Path::new(p).components().count(), p.len()));
    all.dedup();
    let mut out: Vec<String> = Vec::new();
    for path in all {
        if out.iter().any(|parent| Path::new(&path).starts_with(parent)) {
            continue;
        }
        out.push(path);
    }
    out
}
fn clear_dirs(app: &App, cache_only: bool) -> Result<String, String> {
    if cache_only {
        return clear_cache(app);
    }
    if app_is_running_now(app) {
        return Err(format!(
            "Close {} before clearing its data, so its files are not recreated while moving.",
            app.name
        ));
    }
    let targets = sorted_top_level_targets(
        app.dirs
            .iter()
            .filter(|d| d.kind != "cache")
            .map(|d| d.path.clone()),
    );
    if targets.is_empty() {
        return Err("No matching app data folders found.".into());
    }
    let _guard = PREFS_LOCK
        .lock()
        .map_err(|_| "Preferences lock is poisoned")?;
    let batch = format!("{}-{}", chrono_stub(), std::process::id() % 1000);
    let root = quarantine_root();
    ensure_private_dir(&root)?;
    let dest = root.join(&batch);
    ensure_private_dir(&dest)?;
    let mut entry = Quarantine {
        id: batch.clone(),
        app: app.name.clone(),
        bytes: 0,
        items: vec![],
    };
    for path in targets {
        let p = PathBuf::from(&path);
        if !p.exists() {
            continue;
        }
        if !safe_user_data_path(&p)
            || fs::symlink_metadata(&p)
                .map(|m| m.file_type().is_symlink())
                .unwrap_or(true)
        {
            let reason = format!("Refusing to move protected or symlinked path: {path}");
            return Err(abandon_batch(&dest, &entry, &reason));
        }
        let bytes = du(&path);
        let leaf = p
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .to_string();
        let to = dest.join(format!("{}-{}", entry.items.len(), leaf));
        let to_string = to.to_string_lossy().into_owned();
        if let Err(error) = move_path(&path, &to_string) {
            let reason = format!("Could not quarantine {path}: {error}");
            return Err(abandon_batch(&dest, &entry, &reason));
        }
        entry.bytes += bytes;
        entry.items.push(QuarantineItem {
            from: path,
            to: to_string,
            bytes,
        });
    }
    if entry.items.is_empty() {
        let _ = fs::remove_dir(&dest);
        return Err("Those folders are already gone.".into());
    }
    let mut prefs = read_prefs();
    let freed = prefs.freed.entry(app.id.clone()).or_insert(0);
    *freed += entry.bytes;
    let total = prefs.freed.entry("__total__".into()).or_insert(0);
    *total += entry.bytes;
    prefs.quarantine.push(entry.clone());
    if let Err(error) = save_prefs(&prefs) {
        let reason = format!("Could not record the restore entry: {error}");
        return Err(abandon_batch(&dest, &entry, &reason));
    }
    Ok(format!(
        "Moved {} folder(s) ({}) to the restore area",
        entry.items.len(),
        size_text(entry.bytes)
    ))
}
/// Move items back after a failed batch. If any item cannot be returned, keep
/// the quarantine folder and record what is still there. Deleting the folder
/// here would silently destroy the user's data.
fn abandon_batch(dest: &Path, entry: &Quarantine, reason: &str) -> String {
    let remaining = rollback_quarantine(entry);
    if remaining.is_empty() {
        let _ = fs::remove_dir(dest);
        return format!("{reason} The move was undone.");
    }
    let mut residual = entry.clone();
    residual.bytes = remaining.iter().map(|item| item.bytes).sum();
    residual.items = remaining;
    let mut prefs = read_prefs();
    prefs.quarantine.push(residual);
    let _ = save_prefs(&prefs);
    format!(
        "{reason} Some folders could not be moved back and remain in {}; use “Restore or delete data” to recover them.",
        dest.display()
    )
}
fn rollback_quarantine(entry: &Quarantine) -> Vec<QuarantineItem> {
    let mut remaining = Vec::new();
    for item in entry.items.iter().rev() {
        if move_path(&item.to, &item.from).is_err() {
            remaining.push(item.clone());
        }
    }
    remaining.reverse();
    remaining
}
fn move_path(from: &str, to: &str) -> Result<(), String> {
    let src = Path::new(from);
    let dst = Path::new(to);
    if let Some(parent) = dst.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    match fs::rename(src, dst) {
        Ok(()) => Ok(()),
        Err(_) => {
            let mv = trusted_system_binary("mv").ok_or_else(|| "mv is missing.".to_string())?;
            let status = Command::new(mv)
                .args(["-T", "--", from, to])
                .status()
                .map_err(|e| e.to_string())?;
            if status.success() {
                Ok(())
            } else {
                Err(format!("Could not move {from} to {to}"))
            }
        }
    }
}
fn valid_quarantine_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 80
        && id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
}
fn safe_quarantine_source(p: &Path) -> bool {
    let root = quarantine_root();
    let Ok(real_root) = fs::canonicalize(&root) else {
        return false;
    };
    let Ok(real) = fs::canonicalize(p) else {
        return false;
    };
    real.starts_with(&real_root) && real != real_root
}
fn safe_restore_destination(p: &Path) -> bool {
    let text = p.to_string_lossy();
    if !p.is_absolute()
        || text.contains("/ubuntu-app-control/")
        || p.components()
            .any(|part| part.as_os_str() == "ubuntu-app-control")
    {
        return false;
    }
    if is_dedicated_root(p) {
        return true;
    }
    let Some(parent) = p.parent() else {
        return false;
    };
    let roots = allowed_data_roots();
    if parent.exists() {
        let Ok(parent_real) = fs::canonicalize(parent) else {
            return false;
        };
        return roots.iter().any(|root| {
            let Ok(root_real) = fs::canonicalize(root) else {
                return false;
            };
            parent_real == root_real || parent_real.starts_with(&root_real)
        });
    }
    roots.iter().any(|root| p.starts_with(root) && p != root)
}
fn is_dedicated_root(p: &Path) -> bool {
    let h = home();
    [
        h.join(".mozilla"),
        h.join(".thunderbird"),
        h.join(".vscode"),
        h.join(".steam"),
        h.join(".wine"),
    ]
    .iter()
    .any(|root| p == root)
}
fn restore_quarantine(id: &str) -> Result<String, String> {
    if !valid_quarantine_id(id) {
        return Err("Invalid restore entry.".into());
    }
    let _guard = PREFS_LOCK
        .lock()
        .map_err(|_| "Preferences lock is poisoned")?;
    let mut prefs = read_prefs();
    let Some(index) = prefs.quarantine.iter().position(|q| q.id == id) else {
        return Err("Restore entry no longer exists.".into());
    };
    let mut entry = prefs.quarantine[index].clone();
    let mut failed = Vec::new();
    let mut restored: Vec<QuarantineItem> = Vec::new();
    for item in &entry.items {
        if !safe_quarantine_source(Path::new(&item.to))
            || !safe_restore_destination(Path::new(&item.from))
            || fs::symlink_metadata(&item.from).is_ok()
        {
            failed.push(item.clone());
            continue;
        }
        match move_path(&item.to, &item.from) {
            Ok(()) => restored.push(item.clone()),
            Err(_) => failed.push(item.clone()),
        }
    }
    if failed.is_empty() {
        prefs.quarantine.remove(index);
        if let Err(error) = save_prefs(&prefs) {
            // Put the files back rather than leave a restored tree with a
            // stale manifest that can never be restored again.
            for item in restored.iter().rev() {
                let _ = move_path(&item.from, &item.to);
            }
            return Err(format!("Could not record the restore: {error}"));
        }
        Ok(format!(
            "Restored {} folder(s) for {}",
            restored.len(),
            entry.app
        ))
    } else {
        entry.items = failed;
        entry.bytes = entry.items.iter().map(|item| item.bytes).sum();
        prefs.quarantine[index] = entry;
        let _ = save_prefs(&prefs);
        Err(format!(
            "Restored {} folder(s); {} could not be restored because a destination already exists or the path is no longer safe.",
            restored.len(),
            prefs.quarantine[index].items.len()
        ))
    }
}
fn delete_quarantine(id: &str) -> Result<String, String> {
    if !valid_quarantine_id(id) {
        return Err("Invalid restore entry.".into());
    }
    let _guard = PREFS_LOCK
        .lock()
        .map_err(|_| "Preferences lock is poisoned")?;
    let mut prefs = read_prefs();
    let Some(index) = prefs.quarantine.iter().position(|q| q.id == id) else {
        return Err("Restore entry no longer exists.".into());
    };
    let entry = prefs.quarantine.remove(index);
    // Update the manifest before deleting the files. If deletion fails, the
    // data is still on disk and can be recovered manually; if we deleted first
    // and saving failed, the entry would point at data that no longer exists.
    if let Err(error) = save_prefs(&prefs) {
        return Err(format!("Could not update the restore manifest: {error}"));
    }
    let dir = quarantine_root().join(&entry.id);
    if dir.exists() {
        if let Err(error) = fs::remove_dir_all(&dir) {
            prefs.quarantine.insert(index, entry);
            let _ = save_prefs(&prefs);
            return Err(format!("Could not delete {}: {error}", dir.display()));
        }
    }
    Ok(format!("Permanently deleted {} from the restore area", entry.app))
}
fn safe_user_data_path(p: &Path) -> bool {
    if !p.is_absolute() {
        return false;
    }
    let text = p.to_string_lossy();
    if text.contains("/ubuntu-app-control/")
        || p.components()
            .any(|part| part.as_os_str() == "ubuntu-app-control")
    {
        return false;
    }
    let Ok(meta) = fs::symlink_metadata(p) else {
        return false;
    };
    if meta.file_type().is_symlink() || !meta.is_dir() {
        return false;
    }
    if is_dedicated_root(p) {
        return true;
    }
    let Some(parent) = p.parent() else {
        return false;
    };
    let Ok(parent_real) = fs::canonicalize(parent) else {
        return false;
    };
    let roots = allowed_data_roots();
    roots.iter().any(|root| {
        let Ok(root_real) = fs::canonicalize(root) else {
            return false;
        };
        parent_real == root_real || parent_real.starts_with(&root_real)
    })
}
fn chrono_stub() -> String {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
        .to_string()
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.iter().any(|arg| arg == "--version") {
        println!("Ubuntu App Control {}", env!("CARGO_PKG_VERSION"));
        return;
    }
    if args.iter().any(|arg| arg == "--scan-report") {
        let apps = scan();
        let snap = apps.iter().filter(|a| a.source == "snap").count();
        let appimage = apps.iter().filter(|a| a.source == "appimage").count();
        let packages = apps.iter().filter(|a| a.source == "deb").count();
        let unmatched: Vec<_> = apps
            .iter()
            .filter(|a| a.source == "deb" && find_package(a).is_none())
            .collect();
        let visible = apps.iter().filter(|a| !a.runtime).count();
        let utilities = apps.iter().filter(|a| a.runtime).count();
        println!(
            "scan complete: {visible} apps, {utilities} utilities; {snap} Snap, {appimage} AppImage, {packages} package entries; {} package entries have no dpkg owner",
            unmatched.len()
        );
        for app in unmatched {
            println!("unmatched package entry: {} ({})", app.name, app.binary);
        }
        if args.iter().any(|arg| arg == "--scan-details") {
            for app in apps {
                println!(
                    "{}\t{}\t{}\t{}",
                    if app.runtime { "utility" } else { "app" },
                    app.source,
                    app.name,
                    app.desktop
                );
            }
        }
        return;
    }
    let app = Application::builder().application_id(APP_ID).build();
    app.connect_activate(build);
    app.run();
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn desktop_exec_parser_handles_env_and_quoted_command() {
        assert_eq!(
            exec_binary("env GTK_USE_PORTAL=1 /usr/bin/example %U"),
            "/usr/bin/example"
        );
        assert_eq!(
            exec_binary("\"/opt/Example App/bin/run\" %F"),
            "/opt/Example App/bin/run"
        );
    }

    #[test]
    fn native_du_counts_allocated_file_space() {
        let id = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("uac-rust-du-{id}"));
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("payload"), vec![0u8; 8192]).unwrap();
        let used = du(dir.to_str().unwrap());
        assert!(used >= 8192, "du returned {used} bytes");
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn arbitrary_system_paths_are_not_deletable_app_data() {
        assert!(!safe_user_data_path(Path::new("/usr/share")));
        assert!(!safe_user_data_path(Path::new("/tmp")));
    }

    #[test]
    fn preferences_read_the_previous_python_quarantine_format() {
        let old = r#"{
          "gpu": {"deb:sample.desktop": "dgpu:0000:01:00.0"},
          "quarantine": [{
            "id": "batch-1", "created": "2026-10-20 10:00:00", "app": "Sample",
            "uid": "deb:sample.desktop", "bytes": 4096,
            "items": [{"from": "/home/user/.config/sample", "to": "/home/user/.local/share/ubuntu-app-control/quarantine/batch-1/sample", "bytes": 4096}]
          }],
          "freed": {"deb:sample.desktop": 4096, "__total__": 4096}
        }"#;
        let prefs: Preferences = serde_json::from_str(old).unwrap();
        assert_eq!(prefs.quarantine[0].items[0].bytes, 4096);
        assert_eq!(prefs.gpu["deb:sample.desktop"], "dgpu:0000:01:00.0");
        assert_eq!(prefs.freed["__total__"], 4096);
    }

    #[test]
    fn desktop_quote_uses_desktop_entry_rules_not_shell_rules() {
        assert_eq!(desktop_quote("/opt/My App/run"), "\"/opt/My App/run\"");
        assert_eq!(
            desktop_quote("/opt/a\"b\\c$d`e"),
            "\"/opt/a\\\"b\\\\c\\$d\\`e\""
        );
        assert_eq!(desktop_quote("100% ready"), "\"100%% ready\"");
        assert_eq!(desktop_quote("line\nbreak"), "\"line break\"");
    }

    #[test]
    fn appimage_versions_are_extracted_from_the_filename() {
        assert_eq!(version_from_name("MyApp-1.4.2-x86_64"), "1.4.2");
        assert_eq!(version_from_name("Tool_v2.0.0"), "2.0.0");
        assert_eq!(version_from_name("NoVersionHere"), "");
    }

    #[test]
    fn package_and_snap_names_are_strictly_validated() {
        assert!(is_valid_package_name("firefox"));
        assert!(is_valid_package_name("libgtk-4-1"));
        assert!(!is_valid_package_name("-oops"));
        assert!(!is_valid_package_name("Firefox"));
        assert!(!is_valid_package_name("pkg;rm -rf /"));
        assert!(is_valid_snap_name("firefox"));
        assert!(!is_valid_snap_name("--danger"));
        assert!(!is_valid_snap_name("Bad Name"));
    }

    #[test]
    fn quarantine_ids_reject_path_traversal() {
        assert!(valid_quarantine_id("1792548302412177908-467"));
        assert!(!valid_quarantine_id("../outside"));
        assert!(!valid_quarantine_id("a/b"));
        assert!(!valid_quarantine_id(""));
    }

    #[test]
    fn nested_clear_targets_keep_only_the_parent() {
        let targets = sorted_top_level_targets([
            "/home/u/snap/app/common/.cache".to_string(),
            "/home/u/snap/app".to_string(),
            "/home/u/.config/app".to_string(),
            "/home/u/snap/app/current/.config".to_string(),
        ]);
        assert_eq!(
            targets,
            vec!["/home/u/snap/app".to_string(), "/home/u/.config/app".to_string()]
        );
    }

    #[test]
    fn desktop_rewrite_preserves_other_groups_and_removes_stale_metadata() {
        let original = "[Desktop Entry]\nType=Application\nName=Sample\nExec=/usr/bin/sample %U\nX-UAC-GPU=old\nX-UAC-Original=/usr/share/applications/sample.desktop\n\n[Desktop Action New]\nName=New\nExec=/usr/bin/sample --new\n";
        let rewritten = update_desktop_text(
            original,
            Some("env \"DRI_PRIME=1\" /usr/bin/sample %U"),
            &[
                ("X-UAC-GPU", "igpu:0000:00:02.0".to_string()),
                (
                    "X-UAC-Original",
                    "/usr/share/applications/sample.desktop".to_string(),
                ),
            ],
        )
        .unwrap();
        assert!(rewritten.contains("Exec=env \"DRI_PRIME=1\" /usr/bin/sample %U"));
        assert!(rewritten.contains("X-UAC-GPU=igpu:0000:00:02.0"));
        assert!(rewritten.contains("[Desktop Action New]\nName=New"));
        assert_eq!(rewritten.matches("X-UAC-GPU=").count(), 1);

        let restored =
            update_desktop_text(&rewritten, Some("/usr/bin/sample %U"), &[]).unwrap();
        assert!(restored.contains("Exec=/usr/bin/sample %U"));
        assert!(!restored.contains("X-UAC-"));
        assert!(restored.contains("[Desktop Action New]"));
    }

    #[test]
    fn stable_launcher_hash_is_repeatable() {
        assert_eq!(stable_hash("/opt/App.AppImage"), stable_hash("/opt/App.AppImage"));
        assert_ne!(stable_hash("/opt/App.AppImage"), stable_hash("/opt/Other.AppImage"));
    }
}
const CSS: &str = "
.app-row-gpu {
  font-size: 0.8em;
  font-weight: bold;
  padding: 2px 8px;
  border-radius: 999px;
  background-color: alpha(currentColor, 0.12);
}
.detail-title {
  font-size: 1.45em;
  font-weight: bold;
}
.detail-path {
  font-family: monospace;
  font-size: 0.85em;
}
.section-title {
  font-weight: bold;
  margin-top: 4px;
}
";

fn load_css() {
    let provider = gtk4::CssProvider::new();
    provider.load_from_data(CSS);
    if let Some(display) = gtk4::gdk::Display::default() {
        gtk4::style_context_add_provider_for_display(
            &display,
            &provider,
            gtk4::STYLE_PROVIDER_PRIORITY_APPLICATION,
        );
    }
}

fn open_path(path: &Path) {
    let file = gio::File::for_path(path);
    let uri = file.uri();
    let _ = gio::AppInfo::launch_default_for_uri(&uri, None::<&gio::AppLaunchContext>);
}

fn build(application: &Application) {
    load_css();
    let window = ApplicationWindow::builder()
        .application(application)
        .title(APP_NAME)
        .default_width(1180)
        .default_height(760)
        .build();
    let root = GtkBox::new(Orientation::Vertical, 8);
    root.set_margin_top(10);
    root.set_margin_bottom(10);
    root.set_margin_start(10);
    root.set_margin_end(10);

    let toolbar = GtkBox::new(Orientation::Horizontal, 8);
    let restore = Button::with_label("Restore or delete data");
    restore.set_tooltip_text(Some("Review folders this app moved to its restore area."));
    toolbar.append(&restore);
    let open_data = Button::with_label("Open data folder");
    open_data.set_tooltip_text(Some("Open ~/.local/share/ubuntu-app-control."));
    toolbar.append(&open_data);
    let rescan = Button::with_label("Rescan");
    toolbar.append(&rescan);
    let about = Button::with_label("About");
    about.set_hexpand(true);
    about.set_halign(gtk4::Align::End);
    toolbar.append(&about);
    root.append(&toolbar);

    let paned = Paned::new(Orientation::Horizontal);
    paned.set_wide_handle(true);
    root.append(&paned);
    window.set_child(Some(&root));

    let sidebar = GtkBox::new(Orientation::Vertical, 8);
    sidebar.set_size_request(330, -1);
    let search = SearchEntry::new();
    search.set_placeholder_text(Some("Search name, publisher, path…"));
    sidebar.append(&search);
    let filter = ComboBoxText::new();
    for x in ["All apps", "Snap", "AppImage", "Package", "Utilities"] {
        filter.append_text(x);
    }
    filter.set_active(Some(0));
    sidebar.append(&filter);
    let list = ListBox::new();
    list.set_selection_mode(gtk4::SelectionMode::Single);
    let sc = ScrolledWindow::new();
    sc.set_child(Some(&list));
    sc.set_vexpand(true);
    sidebar.append(&sc);
    paned.set_start_child(Some(&sidebar));

    let right = GtkBox::new(Orientation::Vertical, 10);
    let header = GtkBox::new(Orientation::Vertical, 3);
    let title = Label::new(Some(APP_NAME));
    title.add_css_class("title-2");
    let status = Label::new(Some("Scanning installed apps…"));
    status.add_css_class("dim-label");
    status.set_xalign(0.0);
    status.set_wrap(true);
    header.append(&title);
    header.append(&status);
    right.append(&header);
    let detail = GtkBox::new(Orientation::Vertical, 12);
    detail.set_margin_top(12);
    detail.set_margin_bottom(12);
    detail.set_margin_start(14);
    detail.set_margin_end(14);
    let right_scroll = ScrolledWindow::new();
    right_scroll.set_child(Some(&detail));
    right_scroll.set_vexpand(true);
    right.append(&right_scroll);
    paned.set_end_child(Some(&right));

    let ui = Rc::new(RefCell::new(Ui {
        window: window.clone(),
        list: list.clone(),
        detail: detail.clone(),
        search: search.clone(),
        filter: filter.clone(),
        status: status.clone(),
        apps: vec![],
        selected: None,
        preferences: read_prefs(),
        scan_id: 0,
    }));

    {
        let u = ui.clone();
        restore.connect_clicked(move |_| show_quarantine(&u));
    }
    {
        let u = ui.clone();
        open_data.connect_clicked(move |_| {
            let dir = data_home().join(QUARANTINE_MARKER);
            let _ = fs::create_dir_all(&dir);
            open_path(&dir);
            u.borrow().status.set_text("Opened the app data folder.");
        });
    }
    {
        let u = ui.clone();
        rescan.connect_clicked(move |_| spawn_scan(&u));
    }
    {
        let u = ui.clone();
        about.connect_clicked(move |_| show_about(&u));
    }
    {
        let u = ui.clone();
        search.connect_search_changed(move |_| refresh_rows(&u));
    }
    {
        let u = ui.clone();
        filter.connect_changed(move |_| refresh_rows(&u));
    }
    {
        let u = ui.clone();
        list.connect_row_selected(move |_, r| {
            if let Some(row) = r {
                if let Some(id) = row.widget_name().strip_prefix("app:") {
                    show_detail(&u, id);
                }
            }
        });
    }

    show_empty_detail(&ui);
    window.present();
    spawn_scan(&ui);
}

fn summary_text(apps: &[App]) -> String {
    let visible = apps.iter().filter(|a| !a.runtime).count();
    let utilities = apps.len().saturating_sub(visible);
    let user_bytes: u64 = apps
        .iter()
        .filter(|a| !a.runtime)
        .map(|a| a.cache + a.data)
        .sum();
    format!(
        "{visible} apps · {utilities} utilities · {} in cache and app data",
        size_text(user_bytes)
    )
}

fn show_empty_detail(ui: &Rc<RefCell<Ui>>) {
    let detail = { ui.borrow().detail.clone() };
    while let Some(child) = detail.first_child() {
        detail.remove(&child);
    }
    let heading = Label::new(Some("Choose an app"));
    heading.add_css_class("detail-title");
    heading.set_xalign(0.0);
    detail.append(&heading);
    let hint = Label::new(Some(
        "Select an application on the left to see its launcher, data folders, GPU choice and removal options.",
    ));
    hint.set_wrap(true);
    hint.set_xalign(0.0);
    hint.add_css_class("dim-label");
    detail.append(&hint);
}

fn spawn_scan(ui: &Rc<RefCell<Ui>>) {
    let scan_id = {
        let mut s = ui.borrow_mut();
        s.scan_id = s.scan_id.wrapping_add(1);
        s.status.set_text("Scanning installed apps…");
        s.scan_id
    };
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        let _ = tx.send(scan());
    });
    let u = ui.clone();
    glib::timeout_add_local(Duration::from_millis(100), move || {
        match rx.try_recv() {
            Ok(apps) => {
                let stale = {
                    let mut s = u.borrow_mut();
                    if s.scan_id != scan_id {
                        true
                    } else {
                        s.apps = apps;
                        s.status.set_text(&summary_text(&s.apps));
                        false
                    }
                };
                if stale {
                    return glib::ControlFlow::Break;
                }
                refresh_rows(&u);
                glib::ControlFlow::Break
            }
            Err(mpsc::TryRecvError::Empty) => glib::ControlFlow::Continue,
            Err(_) => {
                let s = u.borrow_mut();
                if s.scan_id == scan_id {
                    s.status.set_text("App scan failed. Try Rescan.");
                }
                glib::ControlFlow::Break
            }
        }
    });
}

fn refresh_rows(ui: &Rc<RefCell<Ui>>) {
    let (ids_len, selected_index) = {
        let s = ui.borrow();
        while let Some(child) = s.list.first_child() {
            s.list.remove(&child);
        }
        let query = s.search.text().to_string().to_lowercase();
        let f = s.filter.active().unwrap_or(0);
        let mut ids = Vec::new();
        for app in &s.apps {
            if f == 1 && app.source != "snap"
                || f == 2 && app.source != "appimage"
                || f == 3 && app.source != "deb"
                || f == 4 && !app.runtime
            {
                continue;
            }
            if f != 4 && app.runtime {
                continue;
            }
            if !query.is_empty() {
                let haystack = format!(
                    "{} {} {} {} {} {}",
                    app.name,
                    app.source,
                    app.binary,
                    app.version,
                    app.publisher,
                    app.dirs.iter().map(|d| d.path.as_str()).collect::<Vec<_>>().join(" ")
                )
                .to_lowercase();
                if !haystack.contains(&query) {
                    continue;
                }
            }
            ids.push(app.id.clone());
            let row = ListBoxRow::new();
            row.set_widget_name(&format!("app:{}", app.id));
            row.set_tooltip_text(Some(if app.desktop.is_empty() {
                app.name.as_str()
            } else {
                app.desktop.as_str()
            }));
            let line = GtkBox::new(Orientation::Horizontal, 8);
            let icon = if Path::new(&app.icon).is_absolute() {
                gtk4::Image::from_file(&app.icon)
            } else {
                gtk4::Image::from_icon_name(&app.icon)
            };
            icon.set_pixel_size(30);
            line.append(&icon);
            let box_ = GtkBox::new(Orientation::Vertical, 2);
            let name = Label::new(Some(&app.name));
            name.set_xalign(0.0);
            name.add_css_class("heading");
            let mut bits = vec![app.source.clone()];
            if !app.version.is_empty() {
                bits.push(app.version.clone());
            }
            if app.running {
                bits.push("Running".into());
            }
            let sub = Label::new(Some(&bits.join(" · ")));
            sub.set_xalign(0.0);
            sub.add_css_class("dim-label");
            box_.append(&name);
            box_.append(&sub);
            box_.set_margin_top(6);
            box_.set_margin_bottom(6);
            box_.set_margin_start(8);
            line.append(&box_);
            if let Some(profile) = s.preferences.gpu.get(&app.id) {
                let badge = Label::new(Some(if profile.starts_with("igpu") {
                    "iGPU"
                } else {
                    "GPU"
                }));
                badge.add_css_class("app-row-gpu");
                badge.set_valign(gtk4::Align::Center);
                line.append(&badge);
            }
            let usage = app.cache + app.data;
            if usage > 0 {
                let size = Label::new(Some(&size_text(usage)));
                size.add_css_class("dim-label");
                size.set_valign(gtk4::Align::Center);
                line.append(&size);
            }
            row.set_child(Some(&line));
            s.list.append(&row);
        }
        let selected = s.selected.clone();
        let index = selected
            .as_ref()
            .and_then(|id| ids.iter().position(|candidate| candidate == id));
        (ids.len(), index)
    };
    if ids_len == 0 {
        show_empty_detail(ui);
        ui.borrow_mut().selected = None;
        ui.borrow().status.set_text("No matching apps");
        return;
    }
    let index = selected_index.unwrap_or(0);
    let list = { ui.borrow().list.clone() };
    if let Some(row) = list.row_at_index(index as i32) {
        list.select_row(Some(&row));
    }
}

fn show_detail(ui: &Rc<RefCell<Ui>>, id: &str) {
    let (app, detail) = {
        let mut s = ui.borrow_mut();
        s.selected = Some(id.into());
        let Some(app) = s.apps.iter().find(|a| a.id == id).cloned() else {
            return;
        };
        while let Some(c) = s.detail.first_child() {
            s.detail.remove(&c);
        }
        (app, s.detail.clone())
    };

    let heading = Label::new(Some(&app.name));
    heading.set_xalign(0.0);
    heading.add_css_class("detail-title");
    heading.set_wrap(true);
    detail.append(&heading);

    let mut meta = vec![app.source.clone()];
    if !app.version.is_empty() {
        meta.push(app.version.clone());
    }
    if !app.publisher.is_empty() {
        meta.push(app.publisher.clone());
    }
    let meta_label = Label::new(Some(&meta.join(" · ")));
    meta_label.set_xalign(0.0);
    meta_label.set_wrap(true);
    detail.append(&meta_label);
    let running = Label::new(Some(if app.running {
        "● Running now"
    } else {
        "Not running"
    }));
    running.set_xalign(0.0);
    if app.running {
        running.add_css_class("accent");
    } else {
        running.add_css_class("dim-label");
    }
    detail.append(&running);

    let launch_row = GtkBox::new(Orientation::Horizontal, 8);
    let launch = Button::with_label("Launch");
    launch.add_css_class("suggested-action");
    {
        let a = app.clone();
        let u = ui.clone();
        launch.connect_clicked(move |_| {
            let prefs = u.borrow().preferences.clone();
            let result = launch_app(&a, &prefs);
            u.borrow().status.set_text(&result.unwrap_or_else(|e| e));
        });
    }
    launch_row.append(&launch);
    detail.append(&launch_row);

    let gpu = ComboBoxText::new();
    let profiles = gpu_profiles();
    let current = ui
        .borrow()
        .preferences
        .gpu
        .get(&app.id)
        .cloned()
        .unwrap_or_else(|| "auto".into());
    for (profile_id, label, _) in &profiles {
        gpu.append(Some(profile_id), label);
    }
    gpu.set_active_id(Some(&current));
    gpu.set_hexpand(true);
    let save_gpu = Button::with_label("Save GPU choice");
    {
        let a = app.clone();
        let u = ui.clone();
        let profiles_for_save = profiles.clone();
        let gpu_for_save = gpu.clone();
        save_gpu.connect_clicked(move |_| {
            let profile_id = gpu_for_save
                .active_id()
                .map(|x| x.to_string())
                .unwrap_or_else(|| "auto".into());
            match apply_gpu(&a, &profile_id, &profiles_for_save) {
                Ok(message) => {
                    let chosen = profile_id.clone();
                    let _ = update_prefs(|prefs| {
                        if chosen == "auto" {
                            prefs.gpu.remove(&a.id);
                        } else {
                            prefs.gpu.insert(a.id.clone(), chosen.clone());
                        }
                    });
                    {
                        let mut s = u.borrow_mut();
                        s.preferences = read_prefs();
                        s.status.set_text(&message);
                    }
                    refresh_rows(&u);
                    show_detail(&u, &a.id);
                }
                Err(error) => {
                    u.borrow().status.set_text(&error);
                }
            }
        });
    }
    let launch_once = Button::with_label("Launch once");
    launch_once.set_tooltip_text(Some("Launch with this GPU without saving the choice."));
    {
        let a = app.clone();
        let u = ui.clone();
        let gpu_for_once = gpu.clone();
        launch_once.connect_clicked(move |_| {
            let profile_id = gpu_for_once
                .active_id()
                .map(|x| x.to_string())
                .unwrap_or_else(|| "auto".into());
            let mut prefs = u.borrow().preferences.clone();
            if profile_id == "auto" {
                prefs.gpu.remove(&a.id);
            } else {
                prefs.gpu.insert(a.id.clone(), profile_id);
            }
            let result = launch_app(&a, &prefs);
            u.borrow().status.set_text(&result.unwrap_or_else(|e| e));
        });
    }
    let gpu_row = GtkBox::new(Orientation::Horizontal, 8);
    gpu_row.append(&Label::new(Some("GPU to launch with")));
    gpu_row.append(&gpu);
    gpu_row.append(&save_gpu);
    gpu_row.append(&launch_once);
    detail.append(&gpu_row);

    detail.append(&Separator::new(Orientation::Horizontal));
    let storage = Label::new(Some(&format!(
        "Installed: {}    Cache: {}    App data: {}",
        size_text(app.installed),
        size_text(app.cache),
        size_text(app.data)
    )));
    storage.set_xalign(0.0);
    storage.set_wrap(true);
    detail.append(&storage);

    let folders_title = Label::new(Some("Folders this app owns"));
    folders_title.add_css_class("section-title");
    folders_title.set_xalign(0.0);
    detail.append(&folders_title);
    if app.dirs.is_empty() {
        let none = Label::new(Some("No personal folders were found for this app."));
        none.add_css_class("dim-label");
        none.set_xalign(0.0);
        detail.append(&none);
    }
    for directory in &app.dirs {
        let row = GtkBox::new(Orientation::Horizontal, 8);
        let path_label = Label::new(Some(&directory.path));
        path_label.add_css_class("detail-path");
        path_label.set_xalign(0.0);
        path_label.set_wrap(true);
        path_label.set_hexpand(true);
        row.append(&path_label);
        let kind = Label::new(Some(&directory.kind));
        kind.add_css_class("dim-label");
        row.append(&kind);
        let open = Button::with_label("Open");
        let p = directory.path.clone();
        open.connect_clicked(move |_| open_path(Path::new(&p)));
        row.append(&open);
        detail.append(&row);
    }

    detail.append(&Separator::new(Orientation::Horizontal));
    let controls = GtkBox::new(Orientation::Horizontal, 8);
    let clear_cache = Button::with_label("Clear cache");
    let clear_data = Button::with_label("Clear data");
    let uninstall = Button::with_label(if app.source == "appimage" {
        "Move to Trash"
    } else {
        "Uninstall"
    });
    uninstall.add_css_class("destructive-action");

    {
        let a = app.clone();
        let u = ui.clone();
        clear_cache.connect_clicked(move |_| {
            let targets: Vec<_> = a
                .dirs
                .iter()
                .filter(|d| d.kind == "cache")
                .map(|d| d.path.clone())
                .collect();
            let body = if targets.is_empty() {
                format!("{} has no cache folder.", a.name)
            } else {
                format!(
                    "Deletes the contents of:\n{}\n\n{} will be freed. The app rebuilds its cache next time it starts.",
                    targets.join("\n"),
                    size_text(a.cache)
                )
            };
            let action_app = a.clone();
            confirm_action(
                &u,
                &format!("Clear cache for {}?", a.name),
                &body,
                "Clear cache",
                move || clear_dirs(&action_app, true),
            );
        });
    }
    {
        let a = app.clone();
        let u = ui.clone();
        clear_data.connect_clicked(move |_| {
            let targets = sorted_top_level_targets(
                a.dirs
                    .iter()
                    .filter(|d| d.kind != "cache")
                    .map(|d| d.path.clone()),
            );
            if targets.is_empty() {
                u.borrow()
                    .status
                    .set_text(&format!("{} has no app data folders.", a.name));
                return;
            }
            let body = format!(
                "These folders will be moved to the restorable area:\n{}\n\nYou can put them back from “Restore or delete data”. Settings and saved state are included.",
                targets.join("\n"),
            );
            let action_app = a.clone();
            confirm_action(
                &u,
                &format!("Clear data for {}?", a.name),
                &body,
                "Move to restore area",
                move || clear_dirs(&action_app, false),
            );
        });
    }
    {
        let a = app.clone();
        let u = ui.clone();
        uninstall.connect_clicked(move |_| {
            let (body, enabled) = match a.source.as_str() {
                "snap" => (format!("Runs: pkexec snap remove {}\n\nApp data in your home folder is kept.", a.snap), true),
                "appimage" => (format!("Moves this file to the Trash:\n{}\n\nApp data in your home folder is kept.", a.image), true),
                _ => {
                    let packages = find_packages(&a);
                    match packages.len() {
                        1 => (format!("Package: {}\nRuns: pkexec apt-get remove {}\n\nApp data in your home folder is kept.", packages[0], packages[0]), true),
                        0 => (format!("Could not identify the Debian package that owns {}. Nothing will be removed.", a.name), false),
                        _ => (format!("This app is shared by several packages:\n{}\n\nIt was not removed automatically for safety.", packages.join("\n")), false),
                    }
                }
            };
            if !enabled {
                u.borrow().status.set_text(&body);
                return;
            }
            let action_app = a.clone();
            confirm_action(
                &u,
                &format!("Uninstall {}?", a.name),
                &body,
                "Uninstall",
                move || remove_app(&action_app),
            );
        });
    }
    controls.append(&clear_cache);
    controls.append(&clear_data);
    controls.append(&uninstall);
    detail.append(&controls);
}

fn start_work(
    ui: &Rc<RefCell<Ui>>,
    working: &str,
    work: impl FnOnce() -> Result<String, String> + Send + 'static,
) {
    ui.borrow().status.set_text(working);
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        let _ = tx.send(work());
    });
    let u = ui.clone();
    glib::timeout_add_local(Duration::from_millis(100), move || match rx.try_recv() {
        Ok(result) => {
            let success = result.is_ok();
            let message = result.unwrap_or_else(|e| e);
            {
                let mut s = u.borrow_mut();
                s.preferences = read_prefs();
                s.status.set_text(&message);
            }
            if success {
                spawn_scan(&u);
            }
            glib::ControlFlow::Break
        }
        Err(mpsc::TryRecvError::Empty) => glib::ControlFlow::Continue,
        Err(_) => {
            u.borrow().status.set_text("Operation failed. Try Rescan.");
            glib::ControlFlow::Break
        }
    });
}

fn confirm_action(
    ui: &Rc<RefCell<Ui>>,
    question: &str,
    body: &str,
    accept_label: &str,
    action: impl FnOnce() -> Result<String, String> + Send + 'static,
) {
    let window = ui.borrow().window.clone();
    let dialog = gtk4::MessageDialog::builder()
        .transient_for(&window)
        .modal(true)
        .message_type(gtk4::MessageType::Warning)
        .buttons(gtk4::ButtonsType::Cancel)
        .text(question)
        .secondary_text(body)
        .build();
    dialog.add_button(accept_label, gtk4::ResponseType::Accept);
    dialog.set_default_response(gtk4::ResponseType::Cancel);
    let u = ui.clone();
    let action: Rc<RefCell<Option<Box<dyn FnOnce() -> Result<String, String> + Send>>>> =
        Rc::new(RefCell::new(Some(Box::new(action))));
    dialog.connect_response(move |d, r| {
        d.close();
        if r != gtk4::ResponseType::Accept {
            return;
        }
        let Some(work) = action.borrow_mut().take() else {
            return;
        };
        start_work(&u, "Working in background…", work);
    });
    dialog.present();
}

fn show_about(ui: &Rc<RefCell<Ui>>) {
    let window = ui.borrow().window.clone();
    let about = gtk4::AboutDialog::builder()
        .transient_for(&window)
        .modal(true)
        .program_name(APP_NAME)
        .version(env!("CARGO_PKG_VERSION"))
        .comments(
            "Manage the apps you installed: clear cache safely, move app data to a restorable area, uninstall, and pin the GPU used by each launcher.",
        )
        .license_type(gtk4::License::MitX11)
        .build();
    about.present();
}

fn show_quarantine(ui: &Rc<RefCell<Ui>>) {
    let entries = read_prefs().quarantine;
    let owner = ui.borrow().window.clone();
    let dialog = gtk4::Window::builder()
        .title("Restore or delete cleared data")
        .transient_for(&owner)
        .modal(true)
        .default_width(680)
        .default_height(480)
        .build();
    let outer = GtkBox::new(Orientation::Vertical, 8);
    outer.set_margin_top(12);
    outer.set_margin_bottom(12);
    outer.set_margin_start(12);
    outer.set_margin_end(12);
    let intro = Label::new(Some(
        "Data cleared with “Clear data” is moved here instead of being deleted. Restore it, or delete it permanently.",
    ));
    intro.set_wrap(true);
    intro.set_xalign(0.0);
    intro.add_css_class("dim-label");
    outer.append(&intro);
    let scrolled = ScrolledWindow::new();
    scrolled.set_vexpand(true);
    let list = GtkBox::new(Orientation::Vertical, 10);
    if entries.is_empty() {
        let empty = Label::new(Some("Nothing is stored. Cleared app data will appear here."));
        empty.set_xalign(0.0);
        empty.add_css_class("dim-label");
        list.append(&empty);
    }
    for entry in entries {
        let row = GtkBox::new(Orientation::Vertical, 4);
        let title = Label::new(Some(&format!(
            "{} · {} · {}",
            entry.app,
            size_text(entry.bytes),
            entry.id
        )));
        title.set_xalign(0.0);
        title.add_css_class("section-title");
        row.append(&title);
        let paths = entry
            .items
            .iter()
            .map(|item| item.from.as_str())
            .collect::<Vec<_>>()
            .join("\n");
        let subtitle = Label::new(Some(&paths));
        subtitle.set_xalign(0.0);
        subtitle.set_wrap(true);
        subtitle.add_css_class("detail-path");
        row.append(&subtitle);
        let buttons = GtkBox::new(Orientation::Horizontal, 8);
        let restore = Button::with_label("Restore");
        let id = entry.id.clone();
        let u = ui.clone();
        let d = dialog.clone();
        restore.connect_clicked(move |_| {
            d.close();
            let qid = id.clone();
            start_work(&u, "Restoring…", move || restore_quarantine(&qid));
        });
        buttons.append(&restore);
        let delete = Button::with_label("Delete permanently");
        delete.add_css_class("destructive-action");
        let id = entry.id.clone();
        let u = ui.clone();
        let d = dialog.clone();
        delete.connect_clicked(move |_| {
            d.close();
            let qid = id.clone();
            confirm_action(
                &u,
                "Delete this data permanently?",
                "This cannot be undone. The app’s settings and saved state in this batch will be gone.",
                "Delete permanently",
                move || delete_quarantine(&qid),
            );
        });
        buttons.append(&delete);
        row.append(&buttons);
        row.append(&Separator::new(Orientation::Horizontal));
        list.append(&row);
    }
    scrolled.set_child(Some(&list));
    outer.append(&scrolled);
    dialog.set_child(Some(&outer));
    dialog.present();
}
