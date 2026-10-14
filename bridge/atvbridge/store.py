"""Persistent JSON settings stored in the data directory."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("ATVB_DATA") or Path.cwd() / "data").resolve()

DEFAULTS: dict[str, Any] = {
    # {"address", "identifier", "name", "model", "mac", "credentials": {"companion", "airplay"}}
    "appletv": None,
    # Present a per-phone key so the pairing code is always 000000.
    "easy_pairing": True,
    # Name shown on phones; defaults to the Apple TV's name.
    "display_name": None,
    # fingerprint -> {"name", "cert_pem", "server_e", "last_ip", "paired_at", "last_seen"}
    "phones": {},
}


class Store:
    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._path = data_dir / "config.json"
        self.data: dict[str, Any] = json.loads(json.dumps(DEFAULTS))
        if self._path.exists():
            self.data.update(json.loads(self._path.read_text()))

    def save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        os.replace(tmp, self._path)

    # --- convenience -------------------------------------------------------------
    @property
    def appletv(self) -> dict[str, Any] | None:
        return self.data.get("appletv")

    @property
    def phones(self) -> dict[str, dict[str, Any]]:
        return self.data.setdefault("phones", {})

    def display_name(self) -> str:
        if self.data.get("display_name"):
            return self.data["display_name"]
        if self.appletv and self.appletv.get("name"):
            return self.appletv["name"]
        return "Apple TV"

    def add_phone(self, fp: str, name: str, cert_pem: str, server_e: int, ip: str) -> None:
        now = time.time()
        self.phones[fp] = {
            "name": name or "Android device",
            "cert_pem": cert_pem,
            "server_e": server_e,
            "last_ip": ip,
            "paired_at": now,
            "last_seen": now,
        }
        self.save()

    def touch_phone(self, fp: str, ip: str) -> None:
        phone = self.phones.get(fp)
        if phone:
            phone["last_ip"] = ip
            phone["last_seen"] = time.time()
            self.save()

    def phone_for_ip(self, ip: str) -> tuple[str, dict[str, Any]] | None:
        best = None
        for fp, phone in self.phones.items():
            if phone.get("last_ip") == ip and (best is None or phone["last_seen"] > best[1]["last_seen"]):
                best = (fp, phone)
        return best
