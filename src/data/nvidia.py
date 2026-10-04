"""NVIDIA GPU monitoring via nvidia-smi.

All metrics are optional. The module never raises merely because NVIDIA, the
driver or nvidia-smi is unavailable: it returns an empty list instead.
"""
from __future__ import annotations

import csv
import io
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

ND = "N/D"
_MISSING = {
    "", "N/A", "[N/A]", "NA", "NOT SUPPORTED", "[NOT SUPPORTED]",
    "[UNKNOWN ERROR]", "UNKNOWN ERROR",
}
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
# Older drivers print "CUDA Version:"; newer ones "CUDA UMD Version:".
_CUDA = re.compile(r"CUDA (?:UMD )?Version:\s*([0-9.]+)")


@dataclass
class GpuMetrics:
    index: Optional[int] = None
    name: str = ND
    utilization_percent: Optional[float] = None
    memory_total_mb: Optional[float] = None
    memory_used_mb: Optional[float] = None
    memory_free_mb: Optional[float] = None
    temperature_c: Optional[float] = None
    power_draw_w: Optional[float] = None
    power_limit_w: Optional[float] = None
    core_clock_mhz: Optional[float] = None
    memory_clock_mhz: Optional[float] = None
    fan_percent: Optional[float] = None
    driver_version: str = ND

    @property
    def memory_percent(self) -> Optional[float]:
        if self.memory_used_mb is None or not self.memory_total_mb:
            return None
        return self.memory_used_mb / self.memory_total_mb * 100.0


class NvidiaMonitor:
    FIELDS = (
        "index", "name", "utilization.gpu", "memory.total", "memory.used",
        "memory.free", "temperature.gpu", "power.draw", "power.limit",
        "clocks.gr", "clocks.mem", "fan.speed", "driver_version",
    )

    def __init__(self, timeout: float = 2.0, cuda_retry_seconds: float = 60.0):
        self.timeout = timeout
        self._binary = shutil.which("nvidia-smi")
        self._cuda: Optional[str] = None
        self._cuda_tried_at: Optional[float] = None
        self._cuda_retry = cuda_retry_seconds

    @property
    def available(self) -> bool:
        return self._binary is not None

    @staticmethod
    def _number(value: str) -> Optional[float]:
        value = value.strip()
        if value.upper() in _MISSING:
            return None
        match = _NUMBER.search(value)
        return float(match.group()) if match else None

    @staticmethod
    def _clean(value: str) -> str:
        value = value.strip()
        return ND if value.upper() in _MISSING else value

    def _run(self, args: list) -> str:
        if not self._binary:
            return ""
        try:
            result = subprocess.run(
                [self._binary, *args],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    @classmethod
    def parse_query_output(cls, text: str) -> list:
        """Parse `--query-gpu=<FIELDS> --format=csv,noheader,nounits` output."""
        n_fields = len(cls.FIELDS)
        gpus = []
        for row in csv.reader(io.StringIO(text), skipinitialspace=True):
            if len(row) < n_fields:
                continue
            gpus.append(cls._row_to_metrics(row))
        return gpus

    @classmethod
    def _row_to_metrics(cls, row: list) -> GpuMetrics:
        """Map one CSV row to a GpuMetrics object."""
        n_fields = len(cls.FIELDS)
        # A GPU name containing commas would add extra columns; fold them back.
        extra = len(row) - n_fields
        name = ", ".join(part.strip() for part in row[1:2 + extra])
        (util, total, used, free, temp, p_draw, p_limit,
         clock_gr, clock_mem, fan, driver) = row[2 + extra:]
        index = cls._number(row[0])
        return GpuMetrics(
            index=int(index) if index is not None else None,
            name=cls._clean(name),
            utilization_percent=cls._number(util),
            memory_total_mb=cls._number(total),
            memory_used_mb=cls._number(used),
            memory_free_mb=cls._number(free),
            temperature_c=cls._number(temp),
            power_draw_w=cls._number(p_draw),
            power_limit_w=cls._number(p_limit),
            core_clock_mhz=cls._number(clock_gr),
            memory_clock_mhz=cls._number(clock_mem),
            fan_percent=cls._number(fan),
            driver_version=cls._clean(driver),
        )

    def get_metrics(self) -> list:
        """One list entry per GPU; empty when nvidia-smi is missing or fails."""
        if not self.available:
            return []
        text = self._run([
            f"--query-gpu={','.join(self.FIELDS)}",
            "--format=csv,noheader,nounits",
        ])
        return self.parse_query_output(text) if text else []

    @staticmethod
    def parse_cuda_version(banner: str) -> str:
        match = _CUDA.search(banner)
        return match.group(1) if match else ND

    def get_cuda_version(self) -> str:
        """CUDA version from the nvidia-smi banner. Static, so cached once found."""
        if self._cuda is not None:
            return self._cuda
        if not self.available:
            return ND
        now = time.monotonic()
        if self._cuda_tried_at is not None and now - self._cuda_tried_at < self._cuda_retry:
            return ND
        self._cuda_tried_at = now
        version = self.parse_cuda_version(self._run([]))
        if version != ND:
            self._cuda = version
        return version
