#!/usr/bin/env python3
"""Headless checks for Ubuntu App Control: safety guards, disk accounting, GPU overrides."""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ubuntu_app_control import actions, desktop, gpu, scanner  # noqa: E402
from ubuntu_app_control.models import CACHE, CONFIG, DATA, DEB, DataDir, InstalledApp, human_size  # noqa: E402
from ubuntu_app_control.prefs import OVERRIDE_APP_DIR, Prefs, QUARANTINE_DIR, ensure_dirs  # noqa: E402

HOME = os.path.expanduser("~")
FIXTURE = "uac-fixture"
failures: list[str] = []


def check(label: str, condition, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"[{status}] {label}" + (f" - {detail}" if detail and not condition else ""))


def make_fixture() -> InstalledApp:
    targets = {
        os.path.join(HOME, ".cache", FIXTURE): 6,
        os.path.join(HOME, ".config", FIXTURE): 3,
        os.path.join(HOME, ".local/share", FIXTURE): 4,
    }
    for path, count in targets.items():
        os.makedirs(path, exist_ok=True)
        for index in range(count):
            with open(os.path.join(path, f"blob{index}.bin"), "wb") as handle:
                handle.write(os.urandom(4096 * 8))
    return InstalledApp(
        uid=f"deb:{FIXTURE}",
        name="UAC Fixture",
        source=DEB,
        binary=FIXTURE,
        dirs=[
            DataDir(os.path.join(HOME, ".cache", FIXTURE), CACHE),
            DataDir(os.path.join(HOME, ".config", FIXTURE), CONFIG),
            DataDir(os.path.join(HOME, ".local/share", FIXTURE), DATA),
        ],
    )


def test_scanner() -> None:
    apps = scanner.scan(include_runtimes=True)
    check("scan finds apps", len(apps) > 10, f"got {len(apps)}")
    snaps = [a for a in apps if a.source == "snap"]
    debs = [a for a in apps if a.source == "deb"]
    check("snap apps detected", bool(snaps), "no snaps with launchers found")
    check("package apps detected", bool(debs), "no desktop packages found")
    browser = next((a for a in debs if a.name in ("Google Chrome", "Firefox")), None)
    if browser:
        paths = " ".join(d.path for d in browser.dirs)
        check(f"{browser.name} owns its home folders", ".cache" in paths or ".config" in paths, paths)
    for app in apps:
        assert app.name, app
    util = [a for a in apps if a.runtime]
    check("utilities bucketed separately", 0 < len(util) < len(apps), f"{len(util)} of {len(apps)}")
    te = next((a for a in apps if a.name == "TeXInfo"), None)
    check("ConsoleOnly entries excluded", te is None, "TeXInfo should not be listed")


def test_safety() -> None:
    blocked = [
        os.path.join(HOME, ".ssh"),
        os.path.join(HOME, ".mozilla"),
        "/etc",
        os.path.join(HOME, ".config"),
        HOME,
        "/",
        "/var/lib/snapd",
    ]
    for path in blocked:
        try:
            actions._guard(path)
            check(f"guard blocks {path}", False, "was accepted")
        except actions.ActionError:
            check(f"guard blocks {path}", True)
    allowed = os.path.join(HOME, ".cache", FIXTURE)
    try:
        actions._guard(allowed)
        check("guard allows an app cache folder", True)
    except actions.ActionError as error:
        check("guard allows an app cache folder", False, str(error))


def test_cache_and_data(prefs: Prefs) -> None:
    app = make_fixture()
    scanner.measure_app(app)
    check("fixture cache measured", app.cache_bytes > 20000, human_size(app.cache_bytes))
    check("fixture data measured", app.data_bytes > 20000, human_size(app.data_bytes))

    freed, done_paths, failed = actions.clear_cache(app)
    check("clear cache frees space", freed > 0, f"{freed}")
    check("clear cache reports paths", len(done_paths) == 1, str(done_paths))
    check("clear cache has no failures", not failed, str(failed))
    check(
        "cache folder survives, contents gone",
        os.path.isdir(os.path.join(HOME, ".cache", FIXTURE))
        and not os.listdir(os.path.join(HOME, ".cache", FIXTURE)),
        "folder should still exist and be empty",
    )
    scanner.measure_app(app)
    check("cache emptied to folder overhead", app.cache_bytes < 8192, human_size(app.cache_bytes))

    total, qid, moved, failed = actions.clear_data(app, prefs)
    check("clear data reports a batch", bool(qid), qid)
    check("clear data moved two roots", len(moved) == 2, str(moved))
    check("clear data freed space", total > 8000, human_size(total))
    check(
        "config folder removed from home",
        not os.path.exists(os.path.join(HOME, ".config", FIXTURE)),
        "still present",
    )
    entry = next((item for item in prefs.quarantine if item["id"] == qid), None)
    check("quarantine manifest recorded", entry is not None, "missing entry")
    if entry:
        restored, rfailed = actions.restore_quarantine(entry)
        check("restore puts data back", restored > 8000 and not rfailed, f"{restored} {rfailed}")
        check(
            "restored folders exist again",
            os.path.isdir(os.path.join(HOME, ".config", FIXTURE))
            and os.path.isdir(os.path.join(HOME, ".local/share", FIXTURE)),
            "missing after restore",
        )
        prefs.pop_quarantine(qid)
        check(
            "quarantine entry cleared",
            not any(item.get("id") == qid for item in prefs.quarantine),
            str([item.get("id") for item in prefs.quarantine]),
        )


def test_gpu(prefs: Prefs) -> None:
    gpus = gpu.detect_gpus()
    check("GPUs detected", len(gpus) >= 1, f"{len(gpus)}")
    profiles = gpu.build_profiles(gpus)
    check("auto profile exists", profiles[0].id == "auto", profiles[0].id)
    if len(gpus) > 1:
        check("one profile per GPU plus auto", len(profiles) == len(gpus) + 1, f"{profiles}")
        nvidia = [p for p in profiles if p.id.startswith("dgpu") and "NVIDIA" in p.label]
        check("NVIDIA offload profile", bool(nvidia), "no nvidia profile")
        if nvidia:
            env = nvidia[0].env
            check(
                "offload env vars set",
                env.get("__NV_PRIME_RENDER_OFFLOAD") == "1"
                and env.get("__GLX_VENDOR_LIBRARY_NAME") == "nvidia",
                str(env),
            )
        igpu = [p for p in profiles if p.id.startswith("igpu")]
        check("integrated profile uses DRI_PRIME=0", igpu and igpu[0].env.get("DRI_PRIME") == "0", str(igpu))

    target = next((a for a in scanner.scan(False) if a.source == "snap" and a.desktop_file), None)
    if target is None:
        check("snap launcher available", False, "no snap with a desktop entry")
        return
    basename = os.path.basename(target.desktop_file)
    original_dir = os.path.dirname(target.desktop_file)
    check("snap launcher is system-owned", original_dir == "/var/lib/snapd/desktop/applications", original_dir)
    before = os.listdir(OVERRIDE_APP_DIR)

    message = gpu.apply_profile(target, profiles[-1].id, profiles)
    override_path = os.path.join(OVERRIDE_APP_DIR, basename)
    check("override written", os.path.exists(override_path), message)
    info = desktop.read_desktop(override_path)
    check("override keeps app name", info.get("Name") == target.name, info.get("Name", ""))
    exec_line = info.get("Exec", "")
    check("override injects env", exec_line.startswith("env "), exec_line)
    check("override records profile", info.get(desktop.UAC_GPU_KEY) == profiles[-1].id, str(info))
    check("launcher resolves to a real file", os.path.exists(desktop.exec_binary(exec_line)), exec_line)
    parsed = desktop.env_prefix_of(exec_line)
    check("env round-trips", parsed == profiles[-1].env, f"{parsed} != {profiles[-1].env}")
    pinned = InstalledApp(
        uid=target.uid, name=target.name, source=target.source,
        desktop_file=override_path, snap_name=target.snap_name,
    )
    check(
        "scanner reads the pinned profile back",
        scanner.gpu_profile(pinned) == profiles[-1].id,
        scanner.gpu_profile(pinned),
    )

    target.desktop_file = override_path
    reset = gpu.apply_profile(target, "auto", profiles)
    check("reset removes override", not os.path.exists(override_path), reset)
    check("override folder unchanged", os.listdir(OVERRIDE_APP_DIR) == before, str(os.listdir(OVERRIDE_APP_DIR)))

    rebuilt = desktop.with_env("/snap/bin/vlc --started-from-file %U", profiles[-1].env)
    check("env prefix replaces old one", rebuilt.count("env ") == 1, rebuilt)
    check("no double env", "env env" not in rebuilt, rebuilt)


def test_launch_argv() -> None:
    app = InstalledApp(
        uid="deb:nautilus", name="Files", source=DEB, binary="nautilus",
        exec_line="nautilus --no-desktop %U",
    )
    argv = actions._argv(app.exec_line)
    check("relative binary resolved", argv and os.path.isabs(argv[0]), str(argv))
    check("field codes dropped", "%U" not in argv and "--no-desktop" in argv, str(argv))
    quoted = actions._argv(f'"{os.path.join(HOME, "Downloads", "My App.AppImage")}" %u')
    check(
        "quoted AppImage path kept",
        quoted and quoted[0] == os.path.join(HOME, "Downloads", "My App.AppImage"),
        str(quoted),
    )


def purge_stats(uid: str) -> None:
    """Keep the app's 'space freed' counter honest - it must not remember test data."""
    import json

    from ubuntu_app_control.prefs import PREFS_PATH

    try:
        with open(PREFS_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return
    freed = data.get("freed", {})
    if uid not in freed:
        return
    freed.pop(uid)
    freed["__total__"] = sum(value for key, value in freed.items() if key != "__total__")
    data["freed"] = freed
    with open(PREFS_PATH + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    os.replace(PREFS_PATH + ".tmp", PREFS_PATH)


def purge_fixture(fixture: str) -> None:
    """Undo everything a test run wrote into the real home directories."""
    uid = f"deb:{fixture}"
    prefs = Prefs()
    for entry in list(prefs.quarantine):
        if entry.get("uid") == uid:
            actions.restore_quarantine(entry)
            prefs.pop_quarantine(entry["id"])
    for path in (
        os.path.join(HOME, ".cache", fixture),
        os.path.join(HOME, ".config", fixture),
        os.path.join(HOME, ".local/share", fixture),
    ):
        shutil.rmtree(path, ignore_errors=True)
    for batch in sorted(glob.glob(os.path.join(QUARANTINE_DIR, "*"))):
        children = os.listdir(batch) if os.path.isdir(batch) else []
        if children and all(child.startswith("deb_") and fixture in child for child in children):
            shutil.rmtree(batch, ignore_errors=True)
    purge_stats(uid)


def cleanup() -> None:
    shutil.rmtree(os.path.join(QUARANTINE_DIR, FIXTURE), ignore_errors=True)
    purge_fixture(FIXTURE)


def main() -> int:
    ensure_dirs()
    cleanup()
    prefs = Prefs()
    try:
        make_fixture()
        test_scanner()
        test_safety()
        test_cache_and_data(prefs)
        test_gpu(prefs)
        test_launch_argv()
    finally:
        cleanup()
    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for line in failures:
            print("  -", line)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
