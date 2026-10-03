"""Storage monitoring: physical disks, their partitions and filesystem usage.

The block-device topology (lsblk) rarely changes, so it is cached; filesystem
usage (statvfs) and temperatures are refreshed on every call.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

ND = "N/D"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_ATA_TEMP_ATTRIBUTES = {"Temperature_Celsius", "Airflow_Temperature_Cel", "Temperature_Internal"}
_SIMPLE_TEMP = re.compile(r"(?:Temperature|Current Drive Temperature):\s*(\d+)\b")

LSBLK_COLUMNS = "NAME,TYPE,SIZE,MODEL,TRAN,MOUNTPOINTS,ROTA,FSTYPE"
# util-linux < 2.37 has no MOUNTPOINTS column.
LSBLK_COLUMNS_LEGACY = LSBLK_COLUMNS.replace("MOUNTPOINTS", "MOUNTPOINT")


@dataclass
class PartitionMetrics:
    name: str
    size_bytes: Optional[int] = None
    fstype: str = ND
    mountpoints: list = field(default_factory=list)
    used_bytes: Optional[int] = None
    free_bytes: Optional[int] = None
    usage_percent: Optional[float] = None
    depth: int = 1


@dataclass
class DiskMetrics:
    name: str
    model: str = ND
    kind: str = "Desconocido"
    transport: str = ND
    size_bytes: Optional[int] = None
    temperature_c: Optional[float] = None
    partitions: list = field(default_factory=list)


def _truthy(value) -> bool:
    # lsblk emits booleans on recent util-linux and "1"/"0" strings on older ones.
    return value is True or value in (1, "1", "true", "True")


class StorageMonitor:
    def __init__(
        self,
        timeout: float = 2.0,
        topology_interval: float = 15.0,
        temperature_interval: float = 10.0,
        failure_backoff: float = 300.0,
        nvme_sysfs: str = "/sys/class/nvme",
        clock=time.monotonic,
    ):
        self.timeout = timeout
        self.topology_interval = topology_interval
        self.temperature_interval = temperature_interval
        self.failure_backoff = failure_backoff
        self._nvme_sysfs = nvme_sysfs
        self._clock = clock
        self._lsblk = shutil.which("lsblk")
        self._smartctl = shutil.which("smartctl")
        self._topology: list = []
        self._topology_at: Optional[float] = None
        # name -> (expires_at, value)
        self._temperature_cache: dict = {}

    # ---- topology -------------------------------------------------------

    @staticmethod
    def parse_lsblk(text: str) -> Optional[list]:
        try:
            data = json.loads(text)
        except ValueError:
            return None
        devices = data.get("blockdevices") if isinstance(data, dict) else None
        return devices if isinstance(devices, list) else None

    def _run_lsblk(self) -> Optional[list]:
        if not self._lsblk:
            return None
        for columns in (LSBLK_COLUMNS, LSBLK_COLUMNS_LEGACY):
            try:
                result = subprocess.run(
                    [self._lsblk, "-J", "-b", "-o", columns],
                    capture_output=True, text=True, timeout=self.timeout, check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            if result.returncode == 0:
                return self.parse_lsblk(result.stdout)
        return None

    @staticmethod
    def _kind(device: dict) -> str:
        name = str(device.get("name") or "").lower()
        tran = str(device.get("tran") or "").lower()
        rotational = _truthy(device.get("rota"))
        if name.startswith("nvme") or tran == "nvme":
            return "NVMe"
        if name.startswith("mmcblk"):
            return "eMMC/SD"
        if tran == "usb":
            return "USB"
        if tran in {"sata", "ata"}:
            return "HDD" if rotational else "SSD SATA"
        if rotational:
            return "HDD"
        return "Almacenamiento"

    # ---- filesystem usage ----------------------------------------------

    @staticmethod
    def _filesystem_usage(mountpoints: list) -> tuple:
        """(used, available, percent) using df semantics.

        A device holds one filesystem, so every mountpoint reports the same
        figures (e.g. btrfs subvolumes mounted at / and /home). The first
        readable real path is used.
        """
        for mountpoint in mountpoints:
            if not mountpoint.startswith("/"):  # e.g. "[SWAP]"
                continue
            try:
                st = os.statvfs(mountpoint)
            except OSError:
                continue
            if not st.f_blocks:
                continue
            used = (st.f_blocks - st.f_bfree) * st.f_frsize
            avail = st.f_bavail * st.f_frsize
            denominator = used + avail
            percent = used / denominator * 100.0 if denominator else None
            return used, avail, percent
        return None, None, None

    @staticmethod
    def _mountpoints(node: dict) -> list:
        mounts = node.get("mountpoints")
        if mounts is None and node.get("mountpoint"):
            mounts = [node["mountpoint"]]
        return [m for m in (mounts or []) if m]

    def _partition(self, node: dict, depth: int) -> PartitionMetrics:
        mounts = self._mountpoints(node)
        used, free, percent = self._filesystem_usage(mounts)
        return PartitionMetrics(
            name=str(node.get("name") or ND),
            size_bytes=node.get("size"),
            fstype=str(node.get("fstype") or ND),
            mountpoints=mounts,
            used_bytes=used,
            free_bytes=free,
            usage_percent=percent,
            depth=depth,
        )

    def _flatten(self, nodes, depth: int = 1):
        """All descendants of a disk (partitions, LUKS mappings, LVM volumes...)."""
        for node in nodes or []:
            yield self._partition(node, depth)
            yield from self._flatten(node.get("children"), depth + 1)

    # ---- temperature ----------------------------------------------------

    def _nvme_temperature(self, name: str) -> Optional[float]:
        match = re.match(r"(nvme\d+)", name)
        if not match:
            return None
        controller = match.group(1)
        patterns = (
            f"{self._nvme_sysfs}/{controller}/hwmon*/temp1_input",
            f"{self._nvme_sysfs}/{controller}/device/hwmon/hwmon*/temp1_input",
        )
        for pattern in patterns:
            for path in sorted(glob.glob(pattern)):
                try:
                    with open(path, encoding="utf-8") as f:
                        return float(f.read().strip()) / 1000.0
                except (OSError, ValueError):
                    continue
        return None

    @staticmethod
    def parse_smart_temperature(text: str) -> Optional[float]:
        """Temperature from `smartctl -A` output (ATA, NVMe and SCSI layouts)."""
        for line in text.splitlines():
            stripped = line.strip()
            parts = stripped.split()
            # ATA table: ID# NAME FLAG VALUE WORST THRESH TYPE UPDATED WHEN_FAILED RAW_VALUE
            if len(parts) >= 10 and parts[0].isdigit() and parts[1] in _ATA_TEMP_ATTRIBUTES:
                raw = re.match(r"\d+", parts[9])
                if raw:
                    return float(raw.group())
            simple = _SIMPLE_TEMP.match(stripped)
            if simple:
                return float(simple.group(1))
        return None

    def _smart_temperature(self, name: str) -> Optional[float]:
        if not self._smartctl or not _SAFE_NAME.match(name):
            return None
        now = self._clock()
        cached = self._temperature_cache.get(name)
        if cached and now < cached[0]:
            return cached[1]

        value = None
        try:
            # smartctl's exit status is a bit mask, so do not rely on it:
            # parse the output whenever there is any.
            result = subprocess.run(
                [self._smartctl, "-A", f"/dev/{name}"],
                capture_output=True, text=True, timeout=self.timeout, check=False,
            )
            value = self.parse_smart_temperature(result.stdout)
        except (OSError, subprocess.SubprocessError):
            value = None

        # Typically fails without root: back off instead of spawning it constantly.
        ttl = self.temperature_interval if value is not None else self.failure_backoff
        self._temperature_cache[name] = (now + ttl, value)
        return value

    def _temperature(self, name: str) -> Optional[float]:
        if name.startswith("nvme"):
            value = self._nvme_temperature(name)
            if value is not None:
                return value
        return self._smart_temperature(name)

    # ---- public ---------------------------------------------------------

    def build_metrics(self, devices: list) -> list:
        disks = []
        for d in devices:
            name = str(d.get("name") or "")
            # zram is virtual swap; loop/rom devices are not physical storage.
            if str(d.get("type") or "") != "disk" or name.startswith("zram"):
                continue
            disks.append(DiskMetrics(
                name=name or ND,
                model=str(d.get("model") or ND).strip() or ND,
                kind=self._kind(d),
                transport=str(d.get("tran") or ND).upper(),
                size_bytes=d.get("size"),
                temperature_c=self._temperature(name),
                partitions=list(self._flatten(d.get("children"))),
            ))
        return disks

    def get_metrics(self) -> list:
        now = self._clock()
        if self._topology_at is None or now - self._topology_at >= self.topology_interval:
            devices = self._run_lsblk()
            self._topology_at = now  # also on failure, to avoid hammering lsblk
            if devices is not None:
                self._topology = devices
        return self.build_metrics(self._topology)
