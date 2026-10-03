"""Optional lm-sensors integration.

Parses the output of `sensors -u`. Values in that format are already in
degrees Celsius (not millidegrees), and chip/label headers are *not* indented.
It never requires root.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

_TEMP_INPUT = re.compile(r"^(temp\d+)_input:\s*(-?\d+(?:\.\d+)?)")


@dataclass
class SensorReading:
    chip: str
    label: str
    value_c: Optional[float]


def parse_sensors_output(text: str) -> list:
    """Parse `sensors -u` into temperature readings.

    Layout:
        chip-name              <- first line of a block (blocks split by blank lines)
        Adapter: ...
        Label:                 <- unindented, ends with ':'
          temp1_input: 45.250  <- indented, value in °C
    """
    readings = []
    chip: Optional[str] = None
    label: Optional[str] = None

    for raw in text.splitlines():
        if not raw.strip():
            chip = None
            label = None
            continue

        if raw[0] not in " \t":
            stripped = raw.strip()
            if chip is None:
                chip = stripped
                label = None
            elif stripped.startswith("Adapter:"):
                continue
            elif stripped.endswith(":"):
                label = stripped[:-1]
            continue

        match = _TEMP_INPUT.match(raw.strip())
        if match and chip:
            readings.append(SensorReading(chip, label or match.group(1), float(match.group(2))))

    return readings


class SensorMonitor:
    def __init__(self, timeout: float = 2.0):
        self.timeout = timeout
        self._binary = shutil.which("sensors")

    @property
    def available(self) -> bool:
        return self._binary is not None

    def get_readings(self) -> list:
        if not self._binary:
            return []
        try:
            result = subprocess.run(
                [self._binary, "-u"],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode != 0:
            return []
        return parse_sensors_output(result.stdout)
