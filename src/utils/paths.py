"""XDG-aware locations for configuration and state."""
from __future__ import annotations

import os
from pathlib import Path

from src.utils.channel import DEFAULT_CHANNEL, STABLE, get_channel

APP_DIRNAME = STABLE.directory_name


def _xdg(env_name: str, fallback: Path) -> Path:
    value = os.environ.get(env_name)
    # The XDG spec requires absolute paths; ignore anything else.
    if value and os.path.isabs(value):
        return Path(value)
    return fallback


def config_dir(channel: str = DEFAULT_CHANNEL) -> Path:
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / get_channel(channel).directory_name


def state_dir(channel: str = DEFAULT_CHANNEL) -> Path:
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state") / get_channel(channel).directory_name


def dev_config_dir() -> Path:
    """Development preferences never overwrite the stable installation."""
    return config_dir("dev")
