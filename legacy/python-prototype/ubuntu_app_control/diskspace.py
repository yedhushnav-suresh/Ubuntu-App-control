from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

BLOCK = 512


@dataclass(slots=True)
class Usage:
    bytes: int = 0
    files: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.bytes + other.bytes, self.files + other.files)


def _inode_usage(st) -> int:
    return max(st.st_blocks * BLOCK, st.st_size if st.st_blocks == 0 else 0)


def measure(path: str) -> Usage:
    """Measure an app tree with coreutils instead of a Python per-file walk.

    A Python walker can hold the interpreter lock while visiting a large game
    library, starving GTK's event loop even when the scan itself is in a worker
    thread. `du` does the same allocated-space traversal in native code and
    does not follow symlinks by default.
    """
    if not os.path.lexists(path):
        return Usage()
    try:
        result = subprocess.run(
            ["du", "-s", "--block-size=1", "--null", "--", path],
            capture_output=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return Usage()
    if result.returncode not in (0, 1):
        return Usage()
    first = result.stdout.split(b"\0", 1)[0]
    amount = first.split(b"\t", 1)[0]
    try:
        return Usage(bytes=int(amount))
    except ValueError:
        return Usage()


def measure_exclusive(path: str, skip: set[str]) -> Usage:
    """Measure a tree while excluding child trees, without walking in Python."""
    total = measure(path)
    root = os.path.realpath(path, strict=False)
    descendants = {
        os.path.realpath(item, strict=False)
        for item in skip
        if os.path.realpath(item, strict=False).startswith(root + os.sep)
    }
    # Exclude only outermost skipped directories so overlapping entries are
    # not subtracted more than once.
    outermost = sorted(descendants, key=len)
    selected: list[str] = []
    for item in outermost:
        if not any(item.startswith(parent + os.sep) for parent in selected):
            selected.append(item)
    total.bytes = max(0, total.bytes - sum(measure(item).bytes for item in selected))
    return total


def owned_root(path: str, allowed_roots: list[str]) -> str | None:
    """Return the real path only when it is a protected leaf inside an allowed root."""
    if not path:
        return None
    try:
        real = os.path.realpath(path, strict=False)
    except OSError:
        return None
    if not real or real == os.path.realpath(os.path.expanduser("~")) or real == "/":
        return None
    for root in allowed_roots:
        root_real = os.path.realpath(root, strict=False)
        if real == root_real or not real.startswith(root_real + os.sep):
            continue
        if not os.path.lexists(real):
            return None
        return real
    return None


def contents(path: str) -> list[os.DirEntry]:
    try:
        return list(os.scandir(path))
    except OSError:
        return []


def clear_directory(path: str) -> int:
    """Delete everything inside `path` but keep the directory itself. Returns bytes freed."""
    freed = 0
    for entry in contents(path):
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        freed += _inode_usage(info)
        try:
            if entry.is_dir(follow_symlinks=False) and not os.path.islink(entry.path):
                shutil.rmtree(entry.path, ignore_errors=False)
            else:
                os.unlink(entry.path)
        except OSError:
            continue
    return freed


def relocate(path: str, destination_dir: str) -> str | None:
    """Move a tree into the quarantine area, preferring an atomic rename."""
    target = os.path.join(destination_dir, os.path.basename(path.rstrip("/") or path))
    stem, extension = os.path.splitext(target)
    suffix = 1
    while os.path.exists(target):
        target = f"{stem}-{suffix}{extension}"
        suffix += 1
    try:
        os.makedirs(destination_dir, exist_ok=True)
        shutil.move(path, target)
        return target
    except (OSError, shutil.Error):
        return None
