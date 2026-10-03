"""CPU monitoring using Linux proc/sysfs.

Parsing is done by pure functions so it can be tested with simulated input.
Static information (model, topology) is read once at start-up.
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from typing import Optional

ND = "N/D"


@dataclass
class CpuMetrics:
    model: str = ND
    usage_percent: Optional[float] = None
    cores: int = 0
    threads: int = 0
    frequency_mhz: Optional[float] = None
    max_frequency_mhz: Optional[float] = None
    temperature_c: Optional[float] = None
    load_1m: Optional[float] = None
    load_5m: Optional[float] = None
    load_15m: Optional[float] = None


def parse_cpuinfo(text: str) -> dict:
    """Extract model, logical CPU count and physical core count."""
    blocks = []
    current: dict = {}
    processors = 0
    for line in text.splitlines():
        if line.partition(":")[0].strip() == "processor":
            processors += 1
        if not line.strip():
            if current:
                blocks.append(current)
                current = {}
            continue
        key, sep, value = line.partition(":")
        if sep:
            current.setdefault(key.strip(), value.strip())
    if current:
        blocks.append(current)

    model = ND
    for block in blocks:
        for key in ("model name", "Model", "Hardware"):
            if block.get(key):
                model = block[key]
                break
        if model != ND:
            break

    threads = processors
    cores_seen = {
        (b.get("physical id", "0"), b["core id"]) for b in blocks if "core id" in b
    }
    # Without topology information, do not invent SMT: report logical CPUs.
    cores = len(cores_seen) if cores_seen else threads
    return {"model": model, "threads": threads, "cores": cores}


def parse_stat_cpu_line(text: str) -> Optional[tuple]:
    """Return (total, idle) jiffies from the aggregate 'cpu ' line of /proc/stat.

    Only the first eight fields are summed: guest and guest_nice are already
    accounted for inside user and nice, so including them double-counts.
    """
    for line in text.splitlines():
        if line.startswith("cpu "):
            try:
                values = [int(x) for x in line.split()[1:]]
            except ValueError:
                return None
            if len(values) < 4:
                return None
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            return sum(values[:8]), idle
    return None


def usage_between(prev: Optional[tuple], now: Optional[tuple]) -> Optional[float]:
    if prev is None or now is None:
        return None
    total_delta = now[0] - prev[0]
    idle_delta = now[1] - prev[1]
    if total_delta <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (1.0 - idle_delta / total_delta)))


class CpuMonitor:
    TEMP_CHIPS = {"k10temp", "zenpower", "coretemp"}

    def __init__(self, proc_dir: str = "/proc", sys_dir: str = "/sys"):
        self._proc = proc_dir
        self._sys = sys_dir
        info = parse_cpuinfo(self._read_text(os.path.join(proc_dir, "cpuinfo")))
        self._model = info["model"]
        self._threads = info["threads"] or (os.cpu_count() or 0)
        self._cores = info["cores"] or self._threads
        # Prime the usage counter so the first real sample has a delta.
        self._prev = parse_stat_cpu_line(self._read_text(os.path.join(proc_dir, "stat")))

    @staticmethod
    def _read_text(path: str) -> str:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    @staticmethod
    def _read_int(path: str) -> Optional[int]:
        try:
            with open(path, encoding="utf-8") as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def _temperature(self) -> Optional[float]:
        base = os.path.join(self._sys, "class", "hwmon")
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            return None
        for hw in entries:
            name = self._read_text(os.path.join(base, hw, "name")).strip().lower()
            if name not in self.TEMP_CHIPS:
                continue
            for sensor in ("temp1_input", "temp2_input", "temp3_input"):
                value = self._read_int(os.path.join(base, hw, sensor))
                if value is not None:
                    return value / 1000.0
        return None

    def _frequencies(self) -> tuple:
        pattern = os.path.join(
            self._sys, "devices", "system", "cpu", "cpu[0-9]*", "cpufreq", "scaling_cur_freq"
        )
        values = [self._read_int(p) for p in glob.glob(pattern)]
        values = [v / 1000.0 for v in values if v]
        current = sum(values) / len(values) if values else None
        max_raw = self._read_int(os.path.join(
            self._sys, "devices", "system", "cpu", "cpu0", "cpufreq", "cpuinfo_max_freq"
        ))
        return current, (max_raw / 1000.0 if max_raw else None)

    def _usage(self) -> Optional[float]:
        now = parse_stat_cpu_line(self._read_text(os.path.join(self._proc, "stat")))
        if now is None:
            return None
        prev, self._prev = self._prev, now
        return usage_between(prev, now)

    def get_metrics(self) -> CpuMetrics:
        m = CpuMetrics(model=self._model, cores=self._cores, threads=self._threads)
        m.frequency_mhz, m.max_frequency_mhz = self._frequencies()
        m.usage_percent = self._usage()
        m.temperature_c = self._temperature()
        try:
            parts = self._read_text(os.path.join(self._proc, "loadavg")).split()
            m.load_1m, m.load_5m, m.load_15m = map(float, parts[:3])
        except ValueError:
            pass
        return m
