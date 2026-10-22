from __future__ import annotations

import json
import os
import time

APP_ID = "com.local.UbuntuAppControl"


def _config_home() -> str:
    return os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")


def _data_home() -> str:
    return os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")


CONFIG_DIR = os.path.join(_config_home(), "ubuntu-app-control")
DATA_DIR = os.path.join(_data_home(), "ubuntu-app-control")
QUARANTINE_DIR = os.path.join(DATA_DIR, "quarantine")
PREFS_PATH = os.path.join(CONFIG_DIR, "prefs.json")
OVERRIDE_APP_DIR = os.path.join(_data_home(), "applications")


def ensure_dirs() -> None:
    for path in (CONFIG_DIR, DATA_DIR, QUARANTINE_DIR, OVERRIDE_APP_DIR):
        os.makedirs(path, exist_ok=True)


class Prefs:
    def __init__(self) -> None:
        self._raw: dict = {}
        self.load()

    def load(self) -> None:
        try:
            with open(PREFS_PATH, encoding="utf-8") as handle:
                data = json.load(handle)
            self._raw = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self._raw = {}

    def save(self) -> None:
        ensure_dirs()
        tmp = PREFS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self._raw, handle, indent=2, sort_keys=True)
        os.replace(tmp, PREFS_PATH)

    def set_gpu(self, uid: str, profile: str) -> None:
        table = self._raw.setdefault("gpu", {})
        if profile == "auto":
            table.pop(uid, None)
        else:
            table[uid] = profile
        self.save()

    def get_gpu(self, uid: str) -> str:
        return self._raw.get("gpu", {}).get(uid, "auto")

    def record_quarantine(self, entry: dict) -> None:
        self._raw.setdefault("quarantine", []).append(entry)
        self.save()

    def pop_quarantine(self, qid: str) -> dict | None:
        items = self._raw.get("quarantine", [])
        for index, item in enumerate(items):
            if item.get("id") == qid:
                items.pop(index)
                self.save()
                return item
        return None

    @property
    def quarantine(self) -> list[dict]:
        return list(self._raw.get("quarantine", []))

    def remember_size(self, uid: str, freed: int) -> None:
        table = self._raw.setdefault("freed", {})
        table[uid] = int(table.get(uid, 0)) + int(freed)
        table["__total__"] = int(table.get("__total__", 0)) + int(freed)
        self.save()

    def freed_bytes(self, uid: str | None = None) -> int:
        table = self._raw.get("freed", {})
        if uid is None:
            return int(table.get("__total__", 0))
        return int(table.get(uid, 0))


def new_quarantine_batch() -> tuple[str, str]:
    qid = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid() % 1000:03d}"
    return qid, os.path.join(QUARANTINE_DIR, qid)
