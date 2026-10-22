from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time

from gi.repository import Gio, GLib

from . import desktop, diskspace, scanner
from .models import APPIMAGE, DEB, SNAP, InstalledApp, human_size
from .prefs import QUARANTINE_DIR, Prefs, ensure_dirs, new_quarantine_batch

HOME_DIR = os.path.expanduser("~")

# Everything this app is ever allowed to delete or move. Anything outside is refused.
ALLOWED_ROOTS = [
    os.path.join(HOME_DIR, ".cache"),
    os.path.join(HOME_DIR, ".config"),
    os.path.join(HOME_DIR, ".local/share"),
    os.path.join(HOME_DIR, ".local/state"),
    os.path.join(HOME_DIR, ".var/app"),
    os.path.join(HOME_DIR, "snap"),
    os.path.join(HOME_DIR, "Applications"),
    os.path.join(HOME_DIR, "Downloads"),
    os.path.join(HOME_DIR, "Desktop"),
    QUARANTINE_DIR,
]

PROTECTED = {
    HOME_DIR,
    os.path.realpath(HOME_DIR),
    "/",
    os.path.join(HOME_DIR, ".cache"),
    os.path.join(HOME_DIR, ".config"),
    os.path.join(HOME_DIR, ".local"),
    os.path.join(HOME_DIR, ".local/share"),
    os.path.join(HOME_DIR, "snap"),
    os.path.join(HOME_DIR, ".var"),
}

PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,199}$")
SNAP_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,78}[a-z0-9]$")


class ActionError(RuntimeError):
    pass


def _guard(path: str) -> str:
    real = diskspace.owned_root(path, ALLOWED_ROOTS)
    if not real:
        raise ActionError(f"Refusing to touch {path}: outside the managed app folders.")
    if real in PROTECTED:
        raise ActionError(f"Refusing to touch {path}: that is a shared folder, not an app folder.")
    return real


def _argv(exec_line: str, argument: str = "") -> list[str]:
    try:
        tokens = shlex.split(exec_line)
    except ValueError:
        return []
    argv: list[str] = []
    for index, token in enumerate(tokens):
        if "%" in token and index > 0:
            expanded = re.sub(r"%[fFuUdDnNickvm]", argument or "", token)
            if expanded:
                argv.append(expanded)
            continue
        argv.append(token)
    if argv and os.path.basename(argv[0]) in ("env", "sh", "bash"):
        return argv
    if argv and not os.path.isabs(argv[0]):
        found = shutil.which(argv[0])
        if not found:
            return []
        argv[0] = found
    return argv


def launch(app: InstalledApp, env: dict[str, str] | None = None, argument: str = "") -> str:
    """Start an app, optionally with GPU environment overrides."""
    exec_line = app.exec_line
    if app.source == APPIMAGE and app.appimage_path:
        exec_line = f'"{app.appimage_path}"'
    argv = _argv(exec_line, argument)
    if not argv:
        raise ActionError(f"Cannot work out how to start {app.name}.")
    child_env = dict(os.environ)
    if env:
        child_env.update(env)
    try:
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            argv,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        raise ActionError(f"Could not start {app.name}: {error}") from error
    suffix = ""
    if env:
        names = ", ".join(f"{key}={value}" for key, value in sorted(env.items()) if value)
        suffix = f" with {names}"
    return f"Started {app.name}{suffix}."


def clear_cache(app: InstalledApp) -> tuple[int, list[str], list[str]]:
    """Empty cache folders in place. Cache is safe to drop; the app rebuilds it."""
    freed = 0
    done: list[str] = []
    failed: list[str] = []
    for path in scanner.cache_targets(app):
        try:
            target = _guard(path)
            freed += diskspace.clear_directory(target)
            done.append(target)
        except (ActionError, OSError):
            failed.append(path)
    return freed, done, failed


def clear_data(app: InstalledApp, prefs: Prefs) -> tuple[int, str, list[str], list[str]]:
    """Move app data and settings into the quarantine folder so it can be restored."""
    ensure_dirs()
    qid, batch_dir = new_quarantine_batch()
    moved: list[str] = []
    failed: list[str] = []
    manifest: list[dict] = []
    total = 0
    for path in scanner.data_targets(app):
        try:
            target = _guard(path)
        except ActionError:
            failed.append(path)
            continue
        usage = diskspace.measure(target)
        destination = diskspace.relocate(target, os.path.join(batch_dir, app.uid.replace(":", "_")))
        if destination is None:
            failed.append(path)
            continue
        total += usage.bytes
        moved.append(target)
        manifest.append({"from": target, "to": destination, "bytes": usage.bytes})
    if manifest:
        entry = {
            "id": qid,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "app": app.name,
            "uid": app.uid,
            "bytes": total,
            "items": manifest,
        }
        try:
            prefs.record_quarantine(entry)
        except OSError:
            # Never leave files moved without a manifest: put them back.
            for item in reversed(manifest):
                try:
                    os.makedirs(os.path.dirname(item["from"]), exist_ok=True)
                    if not os.path.lexists(item["from"]):
                        shutil.move(item["to"], item["from"])
                except (OSError, shutil.Error):
                    pass
            shutil.rmtree(batch_dir, ignore_errors=True)
            raise ActionError("Could not record the restore entry, so the move was undone.")
        prefs.remember_size(app.uid, total)
    else:
        shutil.rmtree(batch_dir, ignore_errors=True)
        qid = ""
    return total, qid, moved, failed


def _safe_quarantine_source(path: str) -> bool:
    if not path or os.path.islink(path):
        return False
    root = os.path.realpath(QUARANTINE_DIR)
    real = os.path.realpath(path)
    return real.startswith(root + os.sep) and real != root


def _safe_restore_destination(path: str) -> bool:
    if not path or not os.path.isabs(path) or os.path.lexists(path):
        return False
    if "/ubuntu-app-control/" in path or path.rstrip("/").endswith("/ubuntu-app-control"):
        return False
    parent = os.path.dirname(path)
    try:
        real_parent = os.path.realpath(parent)
    except OSError:
        return False
    if real_parent == os.path.realpath(HOME_DIR) or real_parent == "/":
        return False
    for root in ALLOWED_ROOTS:
        root_real = os.path.realpath(root)
        if real_parent == root_real or real_parent.startswith(root_real + os.sep):
            return True
    return False


def restore_quarantine(entry: dict) -> tuple[int, list[str]]:
    restored = 0
    failed: list[str] = []
    for item in entry.get("items", []):
        source = item.get("to", "")
        destination = item.get("from", "")
        if (
            not _safe_quarantine_source(source)
            or not _safe_restore_destination(destination)
        ):
            failed.append(destination or source)
            continue
        try:
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            if os.path.lexists(destination):
                failed.append(destination)
                continue
            shutil.move(source, destination)
            restored += int(item.get("bytes", 0))
        except (OSError, shutil.Error):
            failed.append(destination)
    return restored, failed


def find_package(app: InstalledApp) -> tuple[str, int]:
    """Map an installed app back to its dpkg package and size, for .deb apps."""
    if app.source != DEB:
        return "", 0
    binary = app.binary
    path = ""
    for candidate in (binary, shutil.which(binary) or ""):
        if candidate and os.path.exists(candidate):
            path = candidate
            break
    package = ""
    # A launcher's command can be a symlink, wrapper, or an alternatives link.
    # Query both the advertised path and its resolved target, then fall back to
    # the desktop file itself (which is commonly owned by the same package).
    ownership_paths = []
    if path:
        ownership_paths.extend((path, os.path.realpath(path)))
    if app.desktop_file:
        ownership_paths.append(app.desktop_file)
    for owned_path in dict.fromkeys(p for p in ownership_paths if p):
        try:
            out = subprocess.run(
                ["dpkg-query", "-S", owned_path],
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            name, _, found = line.partition(":")
            if found and owned_path in found:
                name = name.strip()
                if "/" in name:
                    name = name.split("/", 1)[0]
                package = name
                break
        if package:
            break
    if not package and app.desktop_file:
        package = desktop.read_desktop(app.desktop_file).get("X-Package", "")
    size = 0
    if package:
        try:
            out = subprocess.run(
                ["dpkg-query", "-W", "-f", "${Installed-Size}\t${Status}", package],
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            if "install ok installed" not in line:
                continue
            kb = line.split("\t")[0].strip()
            if kb.isdigit():
                size = int(kb) * 1024
    return package, size


def uninstall(app: InstalledApp, prefs: Prefs) -> tuple[bool, str]:
    """Remove an app. Snaps and packages go through polkit; AppImages land in the Trash."""
    if app.source == SNAP:
        if not SNAP_RE.fullmatch(app.snap_name or ""):
            return False, "That Snap has an invalid package name, so nothing was removed."
        ok, message = _run_privileged(["pkexec", "snap", "remove", app.snap_name])
        if ok:
            prefs.remember_size(app.uid, app.install_bytes)
            if app.desktop_file:
                desktop.remove_override(os.path.basename(app.desktop_file))
            message = f"Uninstalled {app.name}."
        return ok, message

    if app.source == APPIMAGE:
        path = app.appimage_path
        if not path or not os.path.exists(path):
            return False, "That AppImage file is already gone."
        try:
            Gio.File.new_for_path(path).trash(None)
        except GLib.Error:
            try:
                moved = diskspace.relocate(_guard(path), os.path.join(QUARANTINE_DIR, "appimages"))
            except ActionError as error:
                return False, str(error)
            if not moved:
                return False, f"Could not move {path} out of the way."
        if app.desktop_file and os.path.dirname(app.desktop_file) != "/usr/share/applications":
            # Only remove our generated launcher, never a user's own desktop file.
            info = desktop.read_desktop(app.desktop_file)
            if info.get(desktop.UAC_ORIGIN_KEY) or os.path.basename(app.desktop_file).startswith("uac-"):
                try:
                    os.remove(app.desktop_file)
                except OSError:
                    pass
        prefs.remember_size(app.uid, app.install_bytes)
        return True, f"{app.name} deleted ({human_size(app.install_bytes)} freed)."

    package, size = find_package(app)
    if not package or not PACKAGE_RE.fullmatch(package):
        return False, (
            f"Could not match {app.name} to a dpkg package, so it was left installed. "
            "Run 'sudo apt remove <package>' if you know the name."
        )
    ok, message = _run_privileged(["pkexec", "apt-get", "remove", "-y", package])
    if ok:
        prefs.remember_size(app.uid, size)
        message = f"Removed {package} ({human_size(size)} freed)."
    return ok, message


def _run_privileged(command: list[str]) -> tuple[bool, str]:
    # pkexec runs with a deliberately restricted environment. Resolve both
    # pkexec and the target from root-owned system directories, never from the
    # unprivileged user's PATH.
    if len(command) < 2:
        return False, "No privileged command was given."
    pkexec = _trusted_binary(command[0])
    executable = _trusted_binary(command[1])
    if not pkexec:
        return False, "pkexec is not installed in a trusted system directory."
    if not executable:
        return False, f"Required command '{command[1]}' was not found in a trusted system directory."
    command = [pkexec, executable, *command[2:]]
    try:
        result = subprocess.run(  # noqa: S603 - argv list, no shell interpretation
            command,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except OSError as error:
        return False, f"Could not run {' '.join(command)}: {error}"
    except subprocess.TimeoutExpired:
        return False, "The privileged command timed out."
    output = (result.stdout + "\n" + result.stderr).strip()
    tail = "\n".join(line for line in output.splitlines() if line.strip())[-1200:]
    if result.returncode == 0:
        return True, tail or "Done."
    if "user dismissed" in output.lower() or result.returncode == 126:
        return False, "Authorisation was cancelled, so nothing changed."
    return False, tail or f"Command failed with code {result.returncode}."


def _trusted_binary(name: str) -> str:
    """Resolve a command only inside root-owned, non-writable system dirs."""
    if not name or "/" in name:
        return ""
    for directory in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        candidate = os.path.join(directory, name)
        try:
            real = os.path.realpath(candidate)
            stat = os.stat(real)
        except OSError:
            continue
        if stat.st_uid == 0 and (stat.st_mode & 0o111) and not (stat.st_mode & 0o022):
            return real
    return ""
