from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field

from gi.repository import GLib

from . import desktop
from .models import APPIMAGE, InstalledApp
from .prefs import OVERRIDE_APP_DIR, ensure_dirs

VENDOR_NAMES = {
    "0x10de": "NVIDIA",
    "0x1002": "AMD",
    "0x8086": "Intel",
    "0x14e4": "Broadcom",
    "0x15ad": "VMware",
}

DISCRETE_DRIVERS = {"nvidia", "nvidia-vgpu", "nouveau"}
_PCI_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]$")


@dataclass(slots=True)
class Gpu:
    key: str
    label: str
    vendor: str
    pci: str
    driver: str
    card: str
    card_index: int
    primary: bool
    discrete: bool
    prime_index: int = 0

    @property
    def short(self) -> str:
        return f"{self.vendor} {self.label}".strip()


@dataclass(slots=True)
class Profile:
    id: str
    label: str
    detail: str
    env: dict[str, str] = field(default_factory=dict)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="ignore") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _lspci_label(pci: str) -> str:
    """Human GPU model name from lspci, preferring the bracketed marketing name."""
    try:
        out = subprocess.run(["lspci", "-s", pci], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        match = re.match(
            r"^(?:[0-9a-f]{4}:)?[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]\s+.*?:\s*(.+)$", line, re.I
        )
        if not match:
            continue
        label = match.group(1)
        branded = re.findall(r"\[([^\]]*[A-Za-z]{3,}[^\]]*)\]", label)
        label = branded[-1] if branded else label
        label = re.sub(r"\s*\[[0-9a-f]{4}:[0-9a-f]{4}\]", "", label)
        label = re.sub(r"\s*\(rev [0-9a-f]+\)", "", label, flags=re.I)
        label = re.sub(r"^\S+ (Corporation|Technologies|Inc\.|Advanced Micro Devices, Inc\. \[AMD/ATI\])\s*", "", label)
        label = label.strip(" -()")
        if len(label) > 3:
            return label
    return ""


def detect_gpus() -> list[Gpu]:
    gpus: list[Gpu] = []
    base = "/sys/bus/pci/devices"
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return gpus
    for pci in entries:
        if not _PCI_RE.match(pci):
            continue
        if not _read(f"{base}/{pci}/class").startswith("0x03"):
            continue
        vendor = VENDOR_NAMES.get(_read(f"{base}/{pci}/vendor"), "")
        try:
            driver = os.path.basename(os.readlink(f"{base}/{pci}/driver"))
        except OSError:
            driver = ""
        cards: list[str] = []
        try:
            cards = sorted(os.listdir(f"{base}/{pci}/drm"))
        except OSError:
            cards = []
        card = cards[0] if cards else ""
        index = -1
        if card:
            match = re.search(r"card(\d+)$", card)
            index = int(match.group(1)) if match else -1
        label = _lspci_label(pci) or card or driver or "GPU"
        discrete = driver in DISCRETE_DRIVERS or vendor == "NVIDIA"
        gpus.append(
            Gpu(
                key=pci,
                label=label,
                vendor=vendor or (driver.title() if driver else "GPU"),
                pci=pci,
                driver=driver,
                card=card,
                card_index=index,
                primary=_read(f"{base}/{pci}/boot_vga") == "1",
                discrete=discrete,
            )
        )
    ordered = sorted(
        gpus, key=lambda item: (not item.primary, item.card_index if item.card_index >= 0 else 99)
    )
    for position, item in enumerate(ordered):
        item.prime_index = position
    return gpus


def _nvidia_provider_index() -> int:
    path = "/proc/driver/nvidia/gpus"
    try:
        return max(0, len(os.listdir(path)) - 1)
    except OSError:
        return 0


def build_profiles(gpus: list[Gpu] | None = None) -> list[Profile]:
    gpus = gpus if gpus is not None else detect_gpus()
    profiles = [
        Profile(
            "auto",
            "System default",
            "Let Ubuntu choose, exactly as when launching from the Activities overview.",
        )
    ]
    integrated = [gpu for gpu in gpus if not gpu.discrete]
    discrete = [gpu for gpu in gpus if gpu.discrete]

    for gpu in integrated:
        env = {"DRI_PRIME": str(gpu.prime_index)}
        profiles.append(
            Profile(
                f"igpu:{gpu.key}",
                f"{gpu.short} - low power",
                f"Forces the integrated GPU ({gpu.card or gpu.pci}, driver {gpu.driver or 'unknown'}) "
                "so the discrete chip stays powered down.",
                env,
            )
        )

    for gpu in discrete:
        if gpu.vendor == "NVIDIA":
            env = {
                "__NV_PRIME_RENDER_OFFLOAD": "1",
                "__NV_PRIME_RENDER_OFFLOAD_PROVIDER": f"NVIDIA-G{_nvidia_provider_index()}",
                "__GLX_VENDOR_LIBRARY_NAME": "nvidia",
            }
        else:
            env = {"DRI_PRIME": str(gpu.prime_index)}
        profiles.append(
            Profile(
                f"dgpu:{gpu.key}",
                f"{gpu.short} - high performance",
                f"Runs this app on {gpu.label} through PRIME render offload "
                f"(driver {gpu.driver or 'unknown'}).",
                env,
            )
        )

    if len(gpus) <= 1:
        return profiles[:1]
    return profiles


def profile_by_id(profile_id: str, profiles: list[Profile]) -> Profile | None:
    for profile in profiles:
        if profile.id == profile_id:
            return profile
    return None


def nvidia_status() -> str:
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,power.draw",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    line = out.stdout.strip()
    if line:
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 5:
            return f"{parts[0]} - {parts[1]}% GPU, {parts[2]}/{parts[3]} MiB VRAM, {parts[4]} W"
        return line
    return (out.stderr or "").strip().splitlines()[0] if (out.stderr or "").strip() else ""


def _appimage_desktop_path(app: InstalledApp) -> str:
    ensure_dirs()
    slug = re.sub(r"[^a-z0-9]+", "-", app.name.lower()).strip("-") or "appimage"
    return os.path.join(OVERRIDE_APP_DIR, f"uac-appimage-{slug}.desktop")


def _write_appimage_entry(app: InstalledApp, env: dict[str, str], profile_id: str) -> str:
    target = _appimage_desktop_path(app)
    key_file = GLib.KeyFile()
    group = desktop.GROUP
    key_file.set_value(group, "Type", "Application")
    key_file.set_value(group, "Name", app.name)
    key_file.set_value(group, "Exec", desktop.with_env(f'"{app.appimage_path}" %u', env))
    key_file.set_value(group, "Terminal", "false")
    key_file.set_string_list(group, "Categories", ["Utility"])
    key_file.set_value(group, desktop.UAC_ORIGIN_KEY, app.appimage_path)
    key_file.set_value(group, desktop.UAC_GPU_KEY, profile_id)
    key_file.save_to_file(target)
    os.chmod(target, 0o755)
    return target


def apply_profile(app: InstalledApp, profile_id: str, profiles: list[Profile]) -> str:
    """Pin a GPU choice by rewriting the app's launcher entry; "auto" clears it."""
    profile = profile_by_id(profile_id, profiles)
    if profile is None:
        return f"Unknown GPU profile {profile_id!r}."

    if app.source == APPIMAGE:
        if not app.appimage_path or not os.path.exists(app.appimage_path):
            return "The AppImage file is missing, so no launcher could be written."
        app.desktop_file = _write_appimage_entry(app, profile.env, profile.id)
        app.is_custom = True
        return f"{app.name}: launcher created to run on {profile.label}."

    if not app.desktop_file:
        return "This app has no launcher entry, so the GPU choice cannot be stored."

    basename = os.path.basename(app.desktop_file)
    source = app.desktop_file
    if os.path.dirname(source) == OVERRIDE_APP_DIR:
        origin = desktop.read_desktop(source).get(desktop.UAC_ORIGIN_KEY, "")
        if origin.endswith(".desktop") and os.path.exists(origin):
            source = origin
        else:
            return (
                f"{app.name} has a launcher that you manage yourself. "
                "It was left untouched; edit it manually to pin a GPU."
            )

    if profile.id == "auto":
        desktop.remove_override(basename)
        app.desktop_file = source
        return f"{app.name}: launcher reset to the system default GPU."

    try:
        app.desktop_file = desktop.write_override(source, profile.env, profile.id)
    except OSError as error:
        return f"Could not write launcher override: {error}"
    return f"{app.name} will launch on {profile.label}."
