"""XDG-aware locations for configuration and state."""
from __future__ import annotations

import os
from pathlib import Path

APP_DIRNAME = "nvidia-system-monitor"


def _xdg(env_name: str, fallback: Path) -> Path:
    value = os.environ.get(env_name)
    # The XDG spec requires absolute paths; ignore anything else.
    if value and os.path.isabs(value):
        return Path(value)
    return fallback


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / APP_DIRNAME


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state") / APP_DIRNAME
