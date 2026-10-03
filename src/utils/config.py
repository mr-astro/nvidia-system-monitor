"""JSON configuration for the monitor.

The file is user-editable, so every value is validated: anything invalid
falls back to its default instead of crashing the application.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Optional

from src.utils.paths import config_dir

DEFAULT_CONFIG = {
    "refresh_interval_seconds": 1.0,
    "temperature_unit": "C",
    "logging_enabled": True,
    "panels": {
        "cpu": True,
        "gpu": True,
        "ram": True,
        "storage": True,
        "sensors": True,
    },
    "color_scheme": {
        "low_usage_color": "#4CAF50",
        "medium_usage_color": "#FFC107",
        "high_usage_color": "#F44336",
    },
    "thresholds": {
        "medium_percent": 60,
        "high_percent": 85,
    },
}

MIN_INTERVAL = 0.5
MAX_INTERVAL = 60.0
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _number(value, default: float, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if value != value:  # NaN
        return default
    return min(max(float(value), lo), hi)


def sanitize(data: dict) -> dict:
    """Return a complete, valid configuration built from untrusted input."""
    out = copy.deepcopy(DEFAULT_CONFIG)

    # "update_interval_seconds" was the key used by the v2 example file.
    interval = data.get("refresh_interval_seconds", data.get("update_interval_seconds"))
    if interval is not None:
        out["refresh_interval_seconds"] = _number(
            interval, DEFAULT_CONFIG["refresh_interval_seconds"], MIN_INTERVAL, MAX_INTERVAL
        )

    unit = data.get("temperature_unit")
    if isinstance(unit, str) and unit.strip().upper() in {"C", "F"}:
        out["temperature_unit"] = unit.strip().upper()

    if isinstance(data.get("logging_enabled"), bool):
        out["logging_enabled"] = data["logging_enabled"]

    panels = data.get("panels")
    if isinstance(panels, dict):
        for key in out["panels"]:
            if isinstance(panels.get(key), bool):
                out["panels"][key] = panels[key]

    colors = data.get("color_scheme")
    if isinstance(colors, dict):
        for key in out["color_scheme"]:
            value = colors.get(key)
            if isinstance(value, str) and _HEX_COLOR.match(value):
                out["color_scheme"][key] = value

    thresholds = data.get("thresholds")
    if isinstance(thresholds, dict):
        medium = _number(thresholds.get("medium_percent"), -1, 0, 100)
        high = _number(thresholds.get("high_percent"), -1, 0, 100)
        if 0 <= medium < high <= 100:
            out["thresholds"] = {"medium_percent": medium, "high_percent": high}

    return out


class Config:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else config_dir() / "config.json"
        self.values = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    def load(self) -> None:
        try:
            if not self.path.exists():
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self.values = sanitize(data)

    def get(self, key, default=None):
        return self.values.get(key, default)
