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

from src.data.errors import SourceState, command_error, freshness

ND = "N/D"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_ATA_TEMP_ATTRIBUTES = {"Temperature_Celsius", "Airflow_Temperature_Cel", "Temperature_Internal"}
_SIMPLE_TEMP = re.compile(r"(?:Temperature|Current Drive Temperature):\s*(\d+)\b")

LSBLK_COLUMNS = "NAME,TYPE,SIZE,MODEL,TRAN,MOUNTPOINTS,ROTA,FSTYPE,MAJ:MIN"
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
        nvme_sysfs: Optional[str] = None,
        sys_dir: str = "/sys",
        clock=time.monotonic,
        mountinfo_path="/proc/self/mountinfo",
    ):
        self.timeout = timeout
        self.topology_interval = topology_interval
        self.temperature_interval = temperature_interval
        self.failure_backoff = failure_backoff
        self._sys = sys_dir
        self._nvme_sysfs = nvme_sysfs or os.path.join(sys_dir, "class", "nvme")
        self._clock = clock
        self._lsblk = shutil.which("lsblk")
        self._smartctl = shutil.which("smartctl")
        self._topology: list = []
        self._topology_at: Optional[float] = None
        # name -> (expires_at, value)
        self._temperature_cache: dict = {}
        self.errors = {}
        self.sources = {}
        self.refresh_interval = 1.0
        self._mountinfo_path = mountinfo_path
        self._mount_identities = {}
        self._usage_cache = {}
        self._valid_mounts = {}

    # ---- topology -------------------------------------------------------

    @staticmethod
    def parse_lsblk(text: str) -> Optional[list]:
        try:
            data = json.loads(text)
        except ValueError:
            return None
        devices = data.get("blockdevices") if isinstance(data, dict) else None

        def valid(nodes):
            return isinstance(nodes, list) and all(
                isinstance(node, dict) and isinstance(node.get("name"), str)
                and valid(node.get("children") or []) for node in nodes
            )
        return devices if valid(devices) else None

    def _run_lsblk(self) -> Optional[list]:
        if not self._lsblk:
            self.errors["topology"] = "lsblk: utilidad no disponible"
            return None
        for columns in (LSBLK_COLUMNS, LSBLK_COLUMNS_LEGACY):
            try:
                result = subprocess.run(
                    [self._lsblk, "-J", "-b", "-o", columns],
                    capture_output=True, text=True, timeout=self.timeout, check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                self.errors["topology"] = command_error("lsblk", exception=exc)
                return None
            if result.returncode == 0:
                devices = self.parse_lsblk(result.stdout)
                if devices is None:
                    self.errors["topology"] = "lsblk: JSON de topología malformado"
                else:
                    self.errors.pop("topology", None)
                return devices
            self.errors["topology"] = command_error("lsblk", result=result)
            if "MOUNTPOINTS" not in getattr(result, "stderr", ""):
                break
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

    @staticmethod
    def _unescape(value):
        return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)

    def _read_mountinfo(self):
        mounts = {}
        with open(self._mountinfo_path, encoding="utf-8") as stream:
            for line in stream:
                fields = line.split()
                separator = fields.index("-")
                path = self._unescape(fields[4])
                # Include mount ID and root: remounting the same device is a change.
                mounts[path] = (fields[0], fields[2], fields[3], fields[separator + 1],
                                self._unescape(fields[separator + 2]))
        return mounts

    def _bind_mounts(self, nodes, mounts):
        for node in nodes:
            name = str(node.get("name") or "")
            for path in self._mountpoints(node):
                identity = mounts.get(path)
                if identity:
                    source = os.path.realpath(identity[-1])
                    aliases = ("/dev/" + name, "/dev/mapper/" + name)
                    source_matches = any(source == os.path.realpath(alias) for alias in aliases)
                    # Btrfs exposes synthetic st_dev numbers; match its block source.
                    number_matches = identity[3] != "btrfs" and node.get("maj:min") == identity[1]
                    if source_matches or number_matches:
                        self._mount_identities[(name, path)] = identity
            self._bind_mounts(node.get("children") or [], mounts)

    def _partition(self, node: dict, depth: int) -> PartitionMetrics:
        name = str(node.get("name") or ND)
        mounts = self._mountpoints(node)
        key = "usage:" + name
        started = self._clock()
        previous = self.sources.get(key, SourceState())
        valid = [path for path in mounts if (name, path) in self._mount_identities
                 and self._valid_mounts.get(path) == self._mount_identities[(name, path)]]
        values = (None, None, None)
        error = None
        if any(path.startswith("/") for path in mounts):
            if not valid:
                error = "Montaje desaparecido, sustituido o no verificable"
                self._usage_cache.pop(name, None)
            else:
                values = self._filesystem_usage(valid)
                # Recheck after statvfs to reject a concurrent unmount/replacement.
                try:
                    after = self._read_mountinfo()
                    unchanged = all(after.get(path) == self._mount_identities[(name, path)] for path in valid)
                except (OSError, ValueError, IndexError):
                    unchanged = False
                if not unchanged:
                    valid = []
                    values = (None, None, None)
                    self._usage_cache.pop(name, None)
                    error = "La identidad del montaje cambió durante la lectura"
                elif values[0] is None:
                    error = "No se pudo leer el uso del montaje"
                    values = self._usage_cache.get(name, values)
                else:
                    self._usage_cache[name] = values
        self.sources[key] = SourceState(
            previous.acquired_at if error else started,
            previous.completed_at if error else self._clock(),
            freshness(self.refresh_interval), error,
        )
        if error:
            self.errors[key] = error
        else:
            self.errors.pop(key, None)
        return PartitionMetrics(
            name=name, size_bytes=node.get("size"), fstype=str(node.get("fstype") or ND),
            mountpoints=[path for path in mounts if not path.startswith("/") or path in valid],
            used_bytes=values[0], free_bytes=values[1],
            usage_percent=values[2], depth=depth,
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

    def _drivetemp_temperature(self, name: str) -> Optional[float]:
        """Temperature of a SATA/SCSI disk via the kernel's `drivetemp` module.

        Needs no root, but the module must be loaded (`sudo modprobe drivetemp`).
        Each hwmon device is matched to its block device through sysfs: the
        disk's real path lives under the hwmon device's SCSI device.
        """
        base = os.path.join(self._sys, "class", "hwmon")
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            return None
        block = os.path.realpath(os.path.join(self._sys, "block", name))
        for hw in entries:
            try:
                with open(os.path.join(base, hw, "name"), encoding="utf-8") as f:
                    if f.read().strip() != "drivetemp":
                        continue
                device = os.path.realpath(os.path.join(base, hw, "device"))
                if not block.startswith(device + os.sep):
                    continue
                with open(os.path.join(base, hw, "temp1_input"), encoding="utf-8") as f:
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
        key = "smart:" + name
        cached = self._temperature_cache.get(name)
        if cached and now < cached[0]:
            return cached[1]
        value = None
        error = None
        try:
            result = subprocess.run(
                [self._smartctl, "-A", f"/dev/{name}"], capture_output=True,
                text=True, timeout=self.timeout, check=False,
            )
            value = self.parse_smart_temperature(result.stdout)
            # Bits 0–2 describe command/device/checksum errors. Higher bits
            # describe disk health and do not invalidate temperature collection.
            if result.returncode & 7 or (value is None and result.returncode):
                error = command_error("smartctl " + name, result=result)
            elif value is None and (not result.stdout.strip() or "Temperature" in result.stdout):
                error = "smartctl " + name + ": respuesta de temperatura vacía o malformada"
        except (OSError, subprocess.SubprocessError) as exc:
            error = command_error("smartctl " + name, exception=exc)
        previous = self.sources.get(key, SourceState())
        measured = value is not None
        if error:
            self.errors[key] = error
            if not measured:
                value = cached[1] if cached else None
        else:
            self.errors.pop(key, None)
        self.sources[key] = SourceState(
            now if measured else previous.acquired_at,
            self._clock() if measured else previous.completed_at,
            freshness(max(self.refresh_interval, self.temperature_interval), self.timeout), error,
        )
        ttl = self.temperature_interval if measured else self.failure_backoff
        self._temperature_cache[name] = (self._clock() + ttl, value)
        return value

    def _temperature(self, name: str) -> Optional[float]:
        started = self._clock()
        if name.startswith("nvme"):
            value = self._nvme_temperature(name)
            if value is not None:
                self.sources["temperature:" + name] = SourceState(started, self._clock(), freshness(self.refresh_interval))
                self.sources.pop("smart:" + name, None)
                self.errors.pop("smart:" + name, None)
                return value
        else:
            value = self._drivetemp_temperature(name)
            if value is not None:
                self.sources["temperature:" + name] = SourceState(started, self._clock(), freshness(self.refresh_interval))
                self.sources.pop("smart:" + name, None)
                self.errors.pop("smart:" + name, None)
                return value
        self.sources.pop("temperature:" + name, None)
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
        try:
            self._valid_mounts = self._read_mountinfo()
        except (OSError, ValueError, IndexError) as exc:
            self._valid_mounts = {}
            self.errors["storage"] = f"mountinfo: {exc}"
        else:
            self.errors.pop("storage", None)
        if self._topology_at is None or now - self._topology_at >= self.topology_interval:
            self.errors.pop("topology", None)
            devices = self._run_lsblk()
            self._topology_at = self._clock()
            previous = self.sources.get("topology", SourceState())
            if devices is not None:
                self._topology = devices
                self.errors.pop("topology", None)
                old_identities = self._mount_identities
                self._mount_identities = {}
                self._bind_mounts(devices, self._valid_mounts)
                for name in list(self._usage_cache):
                    if {k: v for k, v in old_identities.items() if k[0] == name} != {
                        k: v for k, v in self._mount_identities.items() if k[0] == name
                    }:
                        self._usage_cache.pop(name, None)
            else:
                self.errors.setdefault("topology", "lsblk: no se pudo actualizar la topología")
            self.sources["topology"] = SourceState(
                now if devices is not None else previous.acquired_at,
                self._clock() if devices is not None else previous.completed_at,
                freshness(max(self.refresh_interval, self.topology_interval), self.timeout),
                self.errors.get("topology"),
            )
        disks = self.build_metrics(self._topology)
        active = {"topology"}
        for disk in disks:
            active.update(("temperature:" + disk.name, "smart:" + disk.name))
            active.update("usage:" + p.name for p in disk.partitions)
        self.sources = {k: v for k, v in self.sources.items() if k in active}
        self.errors = {k: v for k, v in self.errors.items() if k in active or k == "storage"}
        return disks
