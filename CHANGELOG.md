# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [2.0.0] - 2026-10-22

### Added

- GTK4/Rust rewrite of the application (the Python/GTK 1.x prototype is kept
  under `legacy/` for reference).
- Search across app name, source, binary, version, publisher and paths.
- Per-row GPU badge and cache/data sizes, plus a summary of visible apps and
  total personal data.
- “Launch once” with a selected GPU without changing the saved launcher.
- “Restore or delete data” window for quarantined app data.
- About dialog, open-data-folder button and application styling.
- Recursive AppImage discovery with duplicate suppression and version parsing.
- GPU model labels from `lspci`, PCI class filtering, boot-GPU ordering and
  NVIDIA PRIME offload profiles.
- Stable, hashed filenames for generated AppImage launchers.
- Root-owned trusted-binary resolution before `pkexec`.

### Changed

- **Cache clearing now deletes cache contents in place** instead of moving the
  cache to quarantine. This actually reclaims disk space and avoids leaving a
  stale multi-gigabyte quarantine batch.
- App data clearing still moves folders to a restorable quarantine, but now
  records a manifest atomically and rolls the move back if the manifest cannot
  be written.
- Preferences use a read-modify-write lock so a GPU choice cannot overwrite a
  quarantine entry.
- Data clearing refuses running apps, symlinked targets and shared/denied
  folders.
- Package removal is refused when the desktop entry and binary resolve to more
  than one package owner.
- Desktop Entry `Exec` values use Desktop Entry quoting rather than shell
  quoting.
- GPU reset restores an in-place user launcher instead of deleting it, and
  preserves other desktop groups.
- Repository layout is a standard root-level Cargo project; packaging and docs
  live in `packaging/`, the prototype in `legacy/`.
- Uninstall script now removes the installed binary and app-created GPU
  launchers.

### Fixed

- `pkexec` could previously execute a command from the user's `PATH`, allowing
  local privilege escalation.
- Snap data could be moved in an order that made restoration impossible because
  a child path was restored before its parent.
- Applying a GPU override twice added duplicate `env` prefixes, and resetting a
  user-owned launcher could delete that launcher.
- Restoring partially failed batches left inflated size metadata.
- The legacy test suite hard-coded a developer's home path and asserted that the
  entire quarantine was empty.
- Uninstall could remove an unrelated package when ownership was ambiguous.

### Security

- Validate package names, Snap names and quarantine IDs.
- Restrict destructive paths to a fixed allow-list, reject symlinks, and verify
  restore sources/destinations.
- Resolve privileged binaries only from root-owned system directories.
- Refuse to overwrite symlinked desktop launchers.

## [1.0.0] - 2026-10-20

- Initial Python/GTK prototype with app scanning, cache/data clearing, package
  removal and GPU launcher overrides.
