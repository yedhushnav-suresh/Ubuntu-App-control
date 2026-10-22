from __future__ import annotations

import os
from dataclasses import dataclass, field

SNAP = "snap"
APPIMAGE = "appimage"
DEB = "deb"

CACHE = "cache"
CONFIG = "config"
DATA = "data"
HOME = "snap-home"

SOURCES = {
    SNAP: "Snap",
    APPIMAGE: "AppImage",
    DEB: "Package",
}


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(num)} {unit}"
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


@dataclass(slots=True)
class DataDir:
    path: str
    kind: str

    @property
    def label(self) -> str:
        return {
            CACHE: "Cache",
            CONFIG: "Settings",
            DATA: "App data",
            HOME: "Snap home",
        }[self.kind]


@dataclass(slots=True)
class InstalledApp:
    uid: str
    name: str
    source: str
    version: str = ""
    publisher: str = ""
    summary: str = ""
    icon_path: str = ""
    icon_name: str = ""
    exec_line: str = ""
    desktop_file: str = ""
    snap_name: str = ""
    binary: str = ""
    categories: list[str] = field(default_factory=list)
    dirs: list[DataDir] = field(default_factory=list)
    appimage_path: str = ""
    runtime: bool = False
    running: bool = False
    is_custom: bool = False
    gpu: str = "auto"
    cache_bytes: int = 0
    data_bytes: int = 0
    install_bytes: int = 0

    def dirs_of(self, *kinds: str) -> list[DataDir]:
        return [d for d in self.dirs if d.kind in kinds]

    @property
    def source_label(self) -> str:
        return SOURCES.get(self.source, self.source)

    @property
    def runtime_label(self) -> str:
        if not self.runtime:
            return ""
        return "System utility" if self.source == DEB else "Runtime / daemon"

    @property
    def total_user_bytes(self) -> int:
        return self.cache_bytes + self.data_bytes

    def size_detail(self) -> str:
        bits = []
        if self.install_bytes:
            bits.append(f"{human_size(self.install_bytes)} installed")
        if self.cache_bytes:
            bits.append(f"{human_size(self.cache_bytes)} cache")
        if self.data_bytes:
            bits.append(f"{human_size(self.data_bytes)} data")
        return " - ".join(bits) if bits else "unknown size"
