# Ubuntu App Control

A small GTK4 application for Ubuntu and other Debian-based desktops that puts
**you** in control of the apps you installed: see how much cache and personal
data each app owns, clear it safely, uninstall the app, and choose which GPU it
uses.

It is aimed at people coming from Windows who expect one obvious place to
manage installed programs and free up disk space, without learning `du`,
`dpkg-query`, Snap revisions, AppImage folders, and PRIME environment
variables first.

> Ubuntu App Control is an independent project and is not affiliated with
> Canonical.

Ubuntu App Control is created and maintained by
[Yedhushnav Suresh](https://github.com/yedhushnav-suresh).

## Features

- **A user-focused app list.** Shows Snap, AppImage and desktop-package entries
  with their version, publisher, launcher, data folders and size. System
  utilities and settings panels are separated from real apps.
- **Safe cache clearing.** Empties cache folders in place, so the disk space is
  reclaimed immediately and the app rebuilds its cache on the next start.
- **Restorable data clearing.** Settings and app data are moved to a quarantine
  area instead of being deleted. A dedicated window lets you **Restore** them or
  **Delete permanently**.
- **Uninstall.** Removes Snaps and Debian packages through `pkexec`; AppImages
  go to the desktop Trash. If a desktop entry is shared by several packages, the
  app refuses to guess.
- **GPU pinning.** Detects AMD/Intel/NVIDIA GPUs and writes a per-user launcher
  with the correct `DRI_PRIME` or NVIDIA PRIME offload variables. You can save a
  choice or **Launch once** without changing the launcher.
- **Search and status.** Search by name, publisher, binary or path; rows show a
  GPU badge and the cache/data size. The toolbar summarises visible apps and
  total cache/data.
- **Defensive file handling.** Operations are restricted to a fixed allow-list
  of per-user data directories. Symlinks, shared folders, running apps and
  ambiguous package ownership are refused rather than guessed at.
- **Trusted privilege boundary.** Privileged commands are resolved from
  root-owned system directories before `pkexec` is invoked, instead of trusting
  the user's `PATH`.

## Requirements

Runtime:

- Ubuntu 24.04 or newer (GTK 4.10+)
- `pkexec` for Snap/package removal
- `coreutils`, `dpkg`
- `snapd` if you want Snap management
- `desktop-file-utils` recommended, for launcher database refreshes

Build:

- Rust 1.85+ (`rustc`, `cargo`)
- GTK4 development headers (`libgtk-4-dev`, `pkg-config`, `build-essential`)
- Python 3 + PyGObject + GdkPixbuf + librsvg for icon rendering
  (`python3-gi`, `gir1.2-gdkpixbuf-2.0`, `librsvg2-common`)

Python is only used by the packaging/icon-rendering script; the application
itself is Rust.

On Ubuntu:

```bash
sudo apt install build-essential pkg-config libgtk-4-dev \
  python3-gi gir1.2-gdkpixbuf-2.0 librsvg2-common
```

## Download

Download the latest `.deb` from the
[Releases page](https://github.com/yedhushnav-suresh/Ubuntu-App-control/releases/latest),
then install it:

```bash
sudo apt install ./ubuntu-app-control_*_amd64.deb
```

## Install for the current user

```bash
git clone https://github.com/yedhushnav-suresh/Ubuntu-App-control.git
cd Ubuntu-App-control
./install.sh
```

The program is installed to `~/.local/bin/ubuntu-app-control` and a launcher to
`~/.local/share/applications/`. Make sure `~/.local/bin` is on your `PATH` if
you want to start it from a terminal.

## Build a .deb

```bash
./build-deb.sh
sudo apt install ./dist/ubuntu-app-control_*_amd64.deb
```

The package version is read from `Cargo.toml` (currently `2.0.0`). The
maintainer is set in `build-deb.sh`. Pushing a `v*` tag makes GitHub Actions
build the `.deb` and attach it to the release.

## Usage

Start **Ubuntu App Control** from the app grid or run:

```bash
ubuntu-app-control
```

Useful non-GUI options:

```bash
ubuntu-app-control --version
ubuntu-app-control --scan-report
ubuntu-app-control --scan-report --scan-details
```

### What “Clear cache” does

Deletes the contents of the app's cache folders but keeps the folders. Close the
app first; the button refuses to run while the app is detected as running.

### What “Clear data” does

Moves the app's settings and personal data into:

```
~/.local/share/ubuntu-app-control/quarantine/<batch-id>/
```

Open **Restore or delete data** in the toolbar to restore the original paths or
delete the batch permanently. Data is never silently deleted by “Clear data”.

### GPU pinning

The GPU list is built from `/sys/bus/pci/devices` and `lspci`. Saving a choice
writes a per-user desktop entry in `~/.local/share/applications/` and leaves the
system launcher untouched. Choosing **System default** removes the override (or
restores a user-owned launcher that was edited in place).

The launcher menu may need a moment to refresh. For AppImages, the generated
launcher is named with a stable hash so it can always be found again.

## Safety model

- Destructive operations are limited to `~/.cache`, `~/.config`,
  `~/.local/share`, `~/.local/state`, `~/.var/app`, `~/.mozilla`,
  `~/.thunderbird`, `~/.steam`, `~/.vscode`, `~/.wine` and `~/snap`.
- Shared folders such as `~/.config`, `~/.cache`, `~/snap` and the home
  directory itself are never treated as an app's own folder.
- Symlinked targets are refused. Running apps cannot have cache or data cleared.
- “Clear data” records a manifest before returning. If writing the manifest
  fails, the move is rolled back.
- Restore validates both the quarantine source and the original destination.
- Snap and Debian package removal only proceeds when exactly one package owner
  can be identified. Package and Snap names are validated before use.
- Privileged commands are resolved from `/usr/bin`, `/bin`, `/usr/sbin` or
  `/sbin` and must be root-owned and not group/world-writable.

### Existing quarantine from older builds

Older 2.0 development builds moved Chrome's cache into quarantine. If you have
a large old batch under `~/.local/share/ubuntu-app-control/quarantine/`, review
it in **Restore or delete data**. The current version clears cache in place and
uses quarantine only for app data.

## Project layout

```
src/main.rs                     GTK4 application and safety logic
Cargo.toml / Cargo.lock         Rust package
packaging/                      desktop entry and man page used by the .deb
data/                           application icon (SVG)
tools/render_icons.py           renders hicolor PNG/SVG icon sizes
install.sh / uninstall.sh       per-user install and removal
build-deb.sh                    Debian package builder
.github/                        CI and release automation
```

The application is implemented entirely in Rust under `src/`.

## Development

```bash
cargo test --locked
cargo build --locked --release
./target/release/ubuntu-app-control --scan-report
```

The tests cover desktop-entry quoting, version parsing, package/Snap name
validation, quarantine ID validation, nested-target selection, and preference
compatibility.

GitHub Actions runs the same tests and builds the `.deb` on every push.

## Uninstall

```bash
./uninstall.sh
```

This removes the program, launcher and app-created GPU launchers, but keeps
`~/.config/ubuntu-app-control` and the quarantine. Use `--purge` to remove them
as well; purge refuses while restorable data still exists:

```bash
./uninstall.sh --purge
```

## Contributing

Bug reports and pull requests are welcome. Please include your Ubuntu version,
desktop environment, whether the app came from Snap/AppImage/deb, and the output
of `ubuntu-app-control --scan-report`. Do not include secrets or personal paths
in reports.

## Maintainer

Yedhushnav Suresh · [GitHub](https://github.com/yedhushnav-suresh)

## License

MIT. See [LICENSE](LICENSE).
