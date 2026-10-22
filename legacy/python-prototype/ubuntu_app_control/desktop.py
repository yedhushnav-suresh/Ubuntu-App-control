from __future__ import annotations

import os
import shlex

from gi.repository import GLib

from .prefs import OVERRIDE_APP_DIR, ensure_dirs

GROUP = "Desktop Entry"
UAC_GPU_KEY = "X-UAC-GPU"
UAC_ORIGIN_KEY = "X-UAC-Origin"

_TEXT_KEYS = (
    "Name",
    "GenericName",
    "Comment",
    "Icon",
    "Exec",
    "Path",
    "StartupWMClass",
    "TryExec",
    "Terminal",
    "Type",
    "X-SnapInstanceName",
    "X-Flatpak",
    UAC_GPU_KEY,
    UAC_ORIGIN_KEY,
)


def _get_str(key_file: GLib.KeyFile, key: str) -> str:
    try:
        return key_file.get_string(GROUP, key)
    except (GLib.Error, ValueError):
        return ""


def _get_list(key_file: GLib.KeyFile, key: str) -> list[str]:
    try:
        result = key_file.get_string_list(GROUP, key)
    except (GLib.Error, ValueError):
        raw = _get_str(key_file, key)
        return [part for part in raw.split(";") if part]
    values = result[0] if isinstance(result, tuple) else result
    return [str(value) for value in values or ()]


def read_desktop(path: str) -> dict:
    """Parse a .desktop file into a plain dict. Empty dict when unreadable."""
    key_file = GLib.KeyFile()
    try:
        key_file.load_from_file(path, GLib.KeyFileFlags.NONE)
    except GLib.Error:
        return {}
    info = {key: _get_str(key_file, key) for key in _TEXT_KEYS}
    info["Categories"] = _get_list(key_file, "Categories")
    info["Keywords"] = _get_list(key_file, "Keywords")
    try:
        info["NoDisplay"] = key_file.get_boolean(GROUP, "NoDisplay")
    except GLib.Error:
        info["NoDisplay"] = False
    try:
        info["Hidden"] = key_file.get_boolean(GROUP, "Hidden")
    except GLib.Error:
        info["Hidden"] = False
    info["path"] = path
    info["basename"] = os.path.basename(path)
    return info


def exec_binary(exec_line: str) -> str:
    """First real executable token of an Exec line, ignoring env prefixes and %f codes."""
    try:
        tokens = shlex.split(exec_line)
    except ValueError:
        tokens = exec_line.split()
    index = 0
    env_prefix: dict[str, str] = {}
    while index < len(tokens) and tokens[index] in ("env", "command-not-found"):
        index += 1
        while index < len(tokens) and "=" in tokens[index] and not tokens[index].startswith("-"):
            name, _, value = tokens[index].partition("=")
            env_prefix[name] = value
            index += 1
    if index >= len(tokens):
        return ""
    program = tokens[index]
    if os.path.basename(program) == "env":
        return exec_binary(" ".join(tokens[index + 1 :]))
    return program


def env_prefix_of(exec_line: str) -> dict[str, str]:
    """Environment variables already encoded in an Exec line as `env A=B ...`."""
    try:
        tokens = shlex.split(exec_line)
    except ValueError:
        return {}
    if not tokens or os.path.basename(tokens[0]) != "env":
        return {}
    prefix: dict[str, str] = {}
    for token in tokens[1:]:
        if "=" in token and not token.startswith("-") and not token.startswith("%"):
            name, _, value = token.partition("=")
            if name.isidentifier() or name.startswith("__"):
                prefix[name] = value
        else:
            break
    return prefix


def strip_env_prefix(exec_line: str) -> str:
    prefix = env_prefix_of(exec_line)
    if not prefix:
        return exec_line
    try:
        tokens = shlex.split(exec_line)
    except ValueError:
        return exec_line
    index = 0
    if tokens and os.path.basename(tokens[0]) == "env":
        index = 1
        while index < len(tokens):
            name, separator, _ = tokens[index].partition("=")
            if separator and name in prefix:
                index += 1
            else:
                break
    return _join_exec(tokens[index:])


def _desktop_quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
        .replace("\n", " ")
    )
    return f'"{escaped}"'


def _join_exec(tokens: list[str]) -> str:
    quoted: list[str] = []
    for token in tokens:
        if not token:
            continue
        if any(character.isspace() for character in token) or any(
            character in token for character in '"\\`$'
        ):
            quoted.append(_desktop_quote(token))
        else:
            quoted.append(token)
    return " ".join(quoted)


def with_env(exec_line: str, env: dict[str, str]) -> str:
    base = strip_env_prefix(exec_line)
    if not env:
        return base
    pairs = " ".join(
        f"{key}={_desktop_quote(str(value))}" for key, value in sorted(env.items())
    )
    return f"env {pairs} {base}"


def override_path(basename: str) -> str:
    return os.path.join(OVERRIDE_APP_DIR, basename)


def write_override(source_desktop: str, env: dict[str, str], profile: str) -> str:
    """Copy a desktop entry into ~/.local/share/applications with a pinned GPU env."""
    ensure_dirs()
    key_file = GLib.KeyFile()
    try:
        key_file.load_from_file(source_desktop, GLib.KeyFileFlags.NONE)
    except GLib.Error as error:
        raise OSError(f"Cannot read {source_desktop}: {error}") from error
    try:
        original_exec = key_file.get_string(GROUP, "Exec")
    except GLib.Error as error:
        raise OSError(f"{source_desktop} has no Exec line") from error
    # Reuse the loaded key file so Actions, localized keys and unknown vendor
    # keys survive the override instead of being silently dropped.
    key_file.set_value(GROUP, "Exec", with_env(original_exec, env))
    key_file.set_value(GROUP, UAC_GPU_KEY, profile)
    key_file.set_value(GROUP, UAC_ORIGIN_KEY, source_desktop)
    key_file.set_boolean(GROUP, "NoDisplay", False)
    target = override_path(os.path.basename(source_desktop))
    try:
        saved = key_file.save_to_file(target)
    except GLib.Error as error:
        raise OSError(f"Cannot write {target}: {error}") from error
    if not saved:
        raise OSError(f"Cannot write {target}")
    os.chmod(target, 0o644)
    return target


def remove_override(basename: str) -> bool:
    target = override_path(basename)
    if not os.path.exists(target):
        return False
    info = read_desktop(target)
    if not (info.get(UAC_ORIGIN_KEY) or info.get(UAC_GPU_KEY)):
        # A user-managed desktop file that happens to live in the same folder.
        return False
    try:
        os.remove(target)
        return True
    except OSError:
        return False


def read_override_profile(basename: str) -> str:
    target = override_path(basename)
    if not os.path.exists(target):
        return ""
    return _get_str(_load(target), UAC_GPU_KEY)


def _load(path: str) -> GLib.KeyFile:
    key_file = GLib.KeyFile()
    try:
        key_file.load_from_file(path, GLib.KeyFileFlags.NONE)
    except GLib.Error:
        pass
    return key_file
