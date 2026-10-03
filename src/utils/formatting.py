"""Formatting helpers. No GTK dependency so they can be unit-tested."""
from __future__ import annotations

from typing import Optional

ND = "N/D"


def fmt(value, suffix: str = "", decimals: int = 1) -> str:
    if value is None:
        return ND
    if isinstance(value, float):
        return f"{value:.{decimals}f}{suffix}"
    return f"{value}{suffix}"


def fmt_mb(value) -> str:
    """Format a value expressed in MiB."""
    if value is None:
        return ND
    if value >= 1024:
        return f"{value / 1024:.1f} GiB"
    return f"{value:.0f} MiB"


def fmt_bytes(value) -> str:
    if value is None:
        return ND
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    x = float(value)
    for unit in units:
        if x < 1024 or unit == units[-1]:
            return f"{x:.1f} {unit}"
        x /= 1024
    return ND


def fmt_temp(celsius: Optional[float], unit: str = "C", decimals: int = 1) -> str:
    if celsius is None:
        return ND
    if unit == "F":
        return f"{celsius * 9 / 5 + 32:.{decimals}f} °F"
    return f"{celsius:.{decimals}f} °C"


def usage_level(percent: Optional[float], medium: float = 60.0,
                high: float = 85.0) -> Optional[str]:
    """Map a usage percentage to 'low' / 'medium' / 'high' (None if unknown)."""
    if percent is None:
        return None
    if percent >= high:
        return "high"
    if percent >= medium:
        return "medium"
    return "low"
