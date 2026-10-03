"""RAM and swap statistics from /proc/meminfo."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class RamMetrics:
    total_mb: Optional[float] = None
    used_mb: Optional[float] = None
    available_mb: Optional[float] = None
    free_mb: Optional[float] = None
    cached_mb: Optional[float] = None
    usage_percent: Optional[float] = None
    swap_total_mb: Optional[float] = None
    swap_used_mb: Optional[float] = None
    swap_free_mb: Optional[float] = None
    swap_usage_percent: Optional[float] = None


def parse_meminfo(text: str) -> dict:
    """Parse /proc/meminfo into {key: kB}. Malformed lines are skipped."""
    data = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        parts = value.split()
        if not parts:
            continue
        try:
            data[key.strip()] = int(parts[0])
        except ValueError:
            continue
    return data


class RamMonitor:
    def __init__(self, path: str = "/proc/meminfo"):
        self._path = path

    def _read(self) -> dict:
        try:
            with open(self._path, encoding="utf-8") as f:
                return parse_meminfo(f.read())
        except OSError:
            return {}

    def get_metrics(self) -> RamMetrics:
        d = self._read()
        m = RamMetrics()
        if "MemTotal" not in d:
            return m

        def mb(key):
            return d.get(key, 0) / 1024.0

        m.total_mb = mb("MemTotal")
        m.free_mb = mb("MemFree")
        # Same definition as free(1): buffers + page cache + reclaimable slab.
        m.cached_mb = mb("Buffers") + mb("Cached") + mb("SReclaimable")
        if "MemAvailable" in d:
            m.available_mb = mb("MemAvailable")
        else:  # kernels older than 3.14
            m.available_mb = min(m.total_mb, m.free_mb + m.cached_mb)
        m.used_mb = max(0.0, m.total_mb - m.available_mb)
        if m.total_mb:
            m.usage_percent = m.used_mb / m.total_mb * 100.0

        m.swap_total_mb = mb("SwapTotal")
        m.swap_free_mb = mb("SwapFree")
        m.swap_used_mb = max(0.0, m.swap_total_mb - m.swap_free_mb)
        if m.swap_total_mb:
            m.swap_usage_percent = m.swap_used_mb / m.swap_total_mb * 100.0
        return m
