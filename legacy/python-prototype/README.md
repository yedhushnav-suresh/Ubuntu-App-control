# Python/GTK prototype (unmaintained)

This is the original 1.x prototype of Ubuntu App Control, kept for reference.
It is **not** part of the packaged application and is not recommended for daily
use. The supported implementation is the Rust/GTK4 app in the repository root.

## What is here

- `app-control.py` — application entry point (GTK4 + libadwaita).
- `ubuntu_app_control/` — scanner, file actions, GPU detection and UI.
- `tests/` — the original headless checks. They write fixtures into the real
  home directory and remove them afterwards.

## Running the old checks

Install the Python GTK dependencies first:

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gdkpixbuf-2.0
cd legacy/python-prototype
python3 tests/verify.py
```

The UI probes additionally need a display:

```bash
python3 tests/layout_probe.py
python3 tests/ui_exercise.py
```

## Differences from the Rust app

The prototype established the safety model (allow-listed data roots, quarantine
manifest, PRIME profiles). The Rust rewrite adds atomic preference updates,
in-place cache clearing, restore/delete UI, trusted-binary resolution before
`pkexec`, symlink and nested-path guards, and proper Desktop Entry quoting.

Some Python paths remain intentionally conservative but less capable than the
Rust implementation; for example, GPU pinning refuses user-managed launchers
rather than editing them in place.
