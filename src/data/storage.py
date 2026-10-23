"""Storage monitoring: physical disks, their partitions and filesystem usage.

The block-device topology (lsblk) rarely changes, so it is cached; filesystem
usage (statvfs) and temperatures are refreshed on every call.
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.data.errors import SourceState, command_error, freshness
from src.data.udisks import UDisksTemperature
from src.data.identity import DeviceIdentity, Presence, probe, sequence
from src.data.storage_layout import PhysicalLayout, build_layout
from src.data.storage_order import BtrfsMembers, DiskOrder, device_number, disk_orders, order_disks

ND = "N/D"
NON_FILESYSTEM_TYPES = frozenset({"crypto_LUKS", "LVM2_member", "linux_raid_member", "swap"})
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_ATA_TEMP_ATTRIBUTES = {"Temperature_Celsius", "Airflow_Temperature_Cel", "Temperature_Internal"}
_SIMPLE_TEMP = re.compile(r"(?:Temperature|Current Drive Temperature):\s*(\d+)\b")

LSBLK_COLUMNS = ("NAME,TYPE,SIZE,MODEL,TRAN,MOUNTPOINTS,ROTA,FSTYPE,MAJ:MIN,LABEL,WWN,SERIAL,DISK-SEQ,"
                 "KNAME,START,LOG-SEC,PKNAME,PARTUUID,PARTTYPE,PTTYPE,PARTN,UUID,SUBSYSTEMS")
OPTIONAL_COLUMNS = frozenset({"WWN", "SERIAL", "DISK-SEQ", "KNAME", "START", "LOG-SEC",
                              "PKNAME", "PARTUUID", "PARTTYPE", "PTTYPE", "PARTN", "UUID", "SUBSYSTEMS"})
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
    label: Optional[str] = None


@dataclass
class DiskMetrics:
    name: str
    model: str = ND
    kind: str = "Desconocido"
    transport: str = ND
    size_bytes: Optional[int] = None
    temperature_c: Optional[float] = None
    partitions: list = field(default_factory=list)
    temperature_source: Optional[str] = None
    direct_filesystem: Optional[PartitionMetrics] = None
    layout: Optional[PhysicalLayout] = None
    order: DiskOrder = DiskOrder()


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
        udisks=None,
        identity_reader=None,
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
        self._udisks = UDisksTemperature(
            timeout=timeout, poll_interval=temperature_interval,
            failure_backoff=failure_backoff, clock=clock,
        ) if udisks is None else udisks
        self._selected_temperatures = {}
        self._temperature_sources = {}
        self._identity_reader = identity_reader or (lambda name, node: probe(name, node, self._sys))
        self._bindings = {}
        self._device_nodes = {}
        self._generation = 0
        self._unverified_retries = {}
        self.thermal_generations = {}
        self._lsblk_columns = None
        self._btrfs_members = {}
        self._layout_cache = {}

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
        # Correlate optional lsblk IDs with attachment tokens around the command,
        # including new names. Some util-linux versions lack a DISKSEQ column.
        def diskseq(name):
            try:
                with open(os.path.join(self._sys, 'class', 'block', name, 'diskseq'), encoding='utf-8') as stream:
                    return sequence(stream.read().strip())
            except OSError:
                return None
        try:
            names = os.listdir(os.path.join(self._sys, 'class', 'block'))
        except OSError:
            names = []
        before = {name: diskseq(name) for name in names}
        columns = self._lsblk_columns or LSBLK_COLUMNS
        # Drop only explicitly unsupported optional fields, remembering the
        # successful set. No capability subprocess or query per partition.
        for _ in range(len(OPTIONAL_COLUMNS) + 2):
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
                    self._lsblk_columns = columns
                    for device in devices:
                        if 'disk-seq' in device:
                            device['diskseq'] = device['disk-seq']
                        name = device['name']
                        value = diskseq(name)
                        if value is not None and before.get(name) == value:
                            device['_observed_diskseq'] = value
                return devices
            self.errors["topology"] = command_error("lsblk", result=result)
            stderr = getattr(result, 'stderr', '') or ''
            unsupported = {column for column in OPTIONAL_COLUMNS | {'MOUNTPOINTS'}
                           if re.search(r'(?<![A-Z0-9_-])' + re.escape(column) + r'(?![A-Z0-9_-])', stderr)}
            if not unsupported:
                break
            selected = columns.split(',')
            updated = ','.join('MOUNTPOINT' if col == 'MOUNTPOINTS' and col in unsupported else col for col in selected
                               if col not in unsupported or col == 'MOUNTPOINTS')
            if updated == columns:
                break
            columns = updated
        return None

    def _read_btrfs_members(self, devices):
        """Mounted kernel membership, sampled only with a topology refresh.

        UUID equality alone is insufficient (clones). Missing/degraded or changing
        inventories are unavailable, never a guess that the visible member is sole.
        """
        root = self._valid_mounts.get('/')
        if not root or root[3] != 'btrfs':
            return {}
        uuids = set()
        def visit(nodes):
            for node in nodes:
                if self._mount_identities.get((node['name'], '/')) == root and node.get('uuid'):
                    uuids.add(str(node['uuid']).lower())
                visit(node.get('children') or [])
        visit(devices)
        result = {}
        for uuid in uuids:
            if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', uuid):
                result[uuid] = BtrfsMembers(reason='UUID Btrfs no válido')
                continue
            base = Path(self._sys) / 'fs/btrfs' / uuid
            try:
                def inventory():
                    entries = sorted((base / 'devices').iterdir())
                    ids = sorted(p.name for p in (base / 'devinfo').iterdir())
                    if not entries or len(entries) != len(ids) or any(not value.isdigit() for value in ids):
                        raise ValueError('Inventario Btrfs incompleto o degradado')
                    numbers = [device_number((path / 'dev').read_text(encoding='utf-8').strip()) for path in entries]
                    if None in numbers or len(set(numbers)) != len(numbers):
                        raise ValueError('Dispositivos Btrfs no verificables')
                    return tuple(p.name for p in entries), tuple(ids), frozenset(numbers)
                before = inventory()
                if inventory() != before:
                    raise ValueError('El inventario Btrfs cambió durante la lectura')
                result[uuid] = BtrfsMembers(before[2])
            except (OSError, ValueError) as exc:
                result[uuid] = BtrfsMembers(reason=f'Miembros Btrfs: {exc}')
        return result

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
        return list(dict.fromkeys(m for m in (mounts or []) if m))

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

    def _partition(self, node: dict, depth: int, *, direct=False) -> PartitionMetrics:
        name = str(node.get("name") or ND)
        mounts = self._mountpoints(node)
        key = "usage:" + name
        started = self._clock()
        previous = self.sources.get(key, SourceState())
        valid = [path for path in mounts if (name, path) in self._mount_identities
                 and self._valid_mounts.get(path) == self._mount_identities[(name, path)]]
        if direct and valid:
            identities = [self._mount_identities[(name, path)] for path in valid]
            # Mount roots/IDs may differ for bind mounts and Btrfs subvolumes,
            # but a whole-device filesystem must have one unambiguous backing.
            backings = {(identity[3], os.path.realpath(identity[-1]) if identity[3] == "btrfs"
                         else identity[1]) for identity in identities}
            if (len(backings) != 1
                    or any(node.get("fstype") and node["fstype"] != identity[3] for identity in identities)
                    or any(node.get("maj:min") and identity[3] != "btrfs"
                           and node["maj:min"] != identity[1] for identity in identities)):
                valid = []
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
            label=node["label"].strip() or None if isinstance(node.get("label"), str) else None,
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

    def _read_temperature(self, name: str) -> Optional[float]:
        started = self._clock()
        if name.startswith("nvme"):
            value = self._nvme_temperature(name)
            if value is not None and math.isfinite(value):
                self.sources["temperature:" + name] = SourceState(started, self._clock(), freshness(self.refresh_interval))
                self.sources.pop("smart:" + name, None)
                self.errors.pop("smart:" + name, None)
                self._temperature_sources[name] = "temperature:" + name
                return value
        else:
            value = self._drivetemp_temperature(name)
            if value is not None and math.isfinite(value):
                key = "temperature:" + name
                self.sources[key] = SourceState(started, self._clock(), freshness(self.refresh_interval))
                self.errors.pop(key, None)
                self._selected_temperatures[name] = (value, key)
                self._temperature_sources[name] = key
                return value
        if name.startswith("nvme"):
            self.sources.pop("temperature:" + name, None)
            self._temperature_sources[name] = "smart:" + name
            return self._smart_temperature(name)

        sysfs_key = "temperature:" + name
        if sysfs_key in self.sources:
            old = self.sources[sysfs_key]
            message = "Temperatura sysfs no disponible"
            self.sources[sysfs_key] = SourceState(old.acquired_at, old.completed_at, old.max_age, message)
            self.errors[sysfs_key] = message

        candidates = {}
        previous = self._selected_temperatures.get(name)
        if previous and previous[1] in self.sources:
            candidates[previous[1]] = previous[0]
        udisks_key = "udisks:" + name
        if self._udisks:
            reading = self._udisks.read(name, context=self._bindings[name])
            self.sources[udisks_key] = reading.state
            if reading.state.error:
                self.errors[udisks_key] = reading.state.error
            else:
                self.errors.pop(udisks_key, None)
            if reading.value is not None:
                candidates[udisks_key] = reading.value
            udisks_valid = (reading.value is not None and math.isfinite(reading.value)
                            and not reading.state.stale(self._clock()))
        else:
            udisks_valid = False
        # An unavailable/expired preferred source must not suppress a fresh
        # fallback. A successful preferred source avoids unnecessary commands.
        if not udisks_valid:
            value = self._smart_temperature(name)
            if value is not None:
                candidates["smart:" + name] = value
        # Re-read states after attempts: a failure can preserve the value while
        # changing its diagnostic, and never its original acquisition time.
        candidates = [(value, key) for key, value in candidates.items()
                      if math.isfinite(value) and key in self.sources
                      and self.sources[key].acquired_at is not None]
        recent = [(value, key) for value, key in candidates
                  if self.sources[key].age(self._clock()) <= self.sources[key].max_age]
        chosen = max(recent or candidates, key=lambda item: (
            self.sources[item[1]].acquired_at, item[1].startswith("udisks:")
        ), default=None)
        if chosen:
            self._selected_temperatures[name] = chosen
            self._temperature_sources[name] = chosen[1]
            return chosen[0]
        self._selected_temperatures.pop(name, None)
        self._temperature_sources[name] = udisks_key if self._udisks else "smart:" + name
        return None

    def _clear_thermal(self, name, *, forget=True):
        binding = self._bindings.get(name)
        if forget and binding and self._udisks:
            self._udisks.invalidate(binding)
        self._temperature_cache.pop(name, None)
        self._selected_temperatures.pop(name, None)
        self._temperature_sources.pop(name, None)
        self._unverified_retries.pop(name, None)
        for prefix in ('smart:', 'temperature:', 'udisks:'):
            self.sources.pop(prefix + name, None)
            self.errors.pop(prefix + name, None)

    def _prepare_devices(self, devices):
        nodes = {d['name']: d for d in devices if d.get('type') == 'disk' and not d['name'].startswith('zram')}
        identities = {name: self._identity_reader(name, node) for name, node in nodes.items()}
        evidence = list(identities.values())
        for name, identity in list(identities.items()):
            if sum(other.diskseq == identity.diskseq for other in evidence) > 1 and identity.diskseq is not None:
                identities[name] = DeviceIdentity(error='Sesión de dispositivo ambigua')
            elif identity.physical and sum(other.physical == identity.physical for other in evidence) > 1:
                identities[name] = DeviceIdentity(error='Identidad física duplicada')
        old = self._bindings.copy()
        bindings = {}
        moved = set()
        for name, identity in identities.items():
            previous = old.get(name)
            if previous and previous.identity == identity:
                bindings[name] = previous
                continue
            if previous and previous.identity.same_attachment(identity):
                updated = Presence(previous.identity.enriched(identity), previous.generation)
                if self._udisks:
                    self._udisks.rebind(previous, updated)
                bindings[name] = updated
                continue
            if previous and identity.error:
                self._topology_at = None
            candidates = [(other, binding) for other, binding in old.items()
                          if other not in nodes and other not in moved and identity.cacheable
                          and binding.identity.same_attachment(identity)]
            if len(candidates) == 1:
                other, binding = candidates[0]
                self._clear_thermal(name)
                updated = Presence(binding.identity.enriched(identity), binding.generation)
                if self._udisks:
                    self._udisks.rebind(binding, updated)
                for cache in (self._temperature_cache, self._selected_temperatures,
                              self._temperature_sources, self._unverified_retries):
                    if other in cache:
                        value = cache.pop(other)
                        if cache is self._selected_temperatures:
                            value = (value[0], value[1].split(':', 1)[0] + ':' + name)
                        elif cache is self._temperature_sources:
                            value = value.split(':', 1)[0] + ':' + name
                        cache[name] = value
                for mapping in (self.sources, self.errors):
                    for prefix in ('smart:', 'temperature:', 'udisks:'):
                        if prefix + other in mapping:
                            mapping[prefix + name] = mapping.pop(prefix + other)
                bindings[name] = updated
                moved.add(other)
            else:
                self._clear_thermal(name)
                self._generation += 1
                bindings[name] = Presence(identity, self._generation)
        for name in old.keys() - nodes.keys() - moved:
            self._clear_thermal(name)
        self._bindings = bindings
        self._device_nodes = nodes
        self.thermal_generations = {prefix + name: binding.generation for name, binding in bindings.items()
                                    for prefix in ('smart:', 'temperature:', 'udisks:')}
        if self._udisks:
            self._udisks.prepare({binding for binding in bindings.values()
                                  if binding.identity.cacheable and binding.identity.physical})

    def _temperature(self, name):
        if name not in self._bindings:
            self._prepare_devices([{'name': name, 'type': 'disk'}])
        binding = self._bindings[name]
        if binding.identity.error in ('Sesión de dispositivo ambigua', 'Identidad física duplicada'):
            self._clear_thermal(name)
            key = 'temperature:' + name
            self.errors[key] = binding.identity.error
            self.sources[key] = SourceState(error=binding.identity.error)
            self._temperature_sources[name] = key
            return None
        before = self._identity_reader(name, self._device_nodes[name])
        if not before.usable or (before != binding.identity and not binding.identity.same_attachment(before)):
            self._clear_thermal(name)
            if before != binding.identity:
                self._bindings.pop(name, None)
                self._topology_at = None
            message = before.error or 'Identidad térmica no verificable o sesión cambiada'
            self.errors['temperature:' + name] = message
            self.sources['temperature:' + name] = SourceState(error=message)
            self._temperature_sources[name] = 'temperature:' + name
            return None
        if before.cacheable:
            value = self._read_temperature(name)
        else:
            # Without an attachment token, only freshly bracketed direct readings
            # are eligible. Do not reuse values, errors or wall-clock anchors.
            retry = self._unverified_retries.get(name, 0)
            self._clear_thermal(name)
            started = self._clock()
            value = self._nvme_temperature(name) if name.startswith('nvme') else self._drivetemp_temperature(name)
            if value is not None and math.isfinite(value):
                key = 'temperature:' + name
                self.sources[key] = SourceState(started, self._clock(), freshness(self.refresh_interval))
                self._temperature_sources[name] = key
            elif self._clock() >= retry:
                value = self._smart_temperature(name)
                self._temperature_sources[name] = 'smart:' + name
                retry = self._temperature_cache.get(name, (self._clock() + self.temperature_interval,))[0]
                self._temperature_cache.pop(name, None)
            else:
                value = None
            self._unverified_retries[name] = retry
        after = self._identity_reader(name, self._device_nodes[name])
        if not after.usable or (after != before and not before.same_attachment(after)):
            self._clear_thermal(name)
            self._bindings.pop(name, None)
            self._topology_at = None
            key = 'temperature:' + name
            message = 'La identidad o sesión cambió durante la adquisición térmica'
            self.errors[key] = message
            self.sources[key] = SourceState(error=message)
            self._temperature_sources[name] = key
            return None
        return value

    # ---- public ---------------------------------------------------------

    def presentation_bindings(self):
        """Copy immutable A2 evidence without probing or changing any session."""
        return {name: binding for name, binding in self._bindings.items()
                if binding.identity.usable}

    def build_metrics(self, devices: list) -> list:
        self._prepare_devices(devices)
        physical_counts = Counter(d['name'] for d in devices if d.get('type') == 'disk')
        disks = []
        for d in devices:
            name = str(d.get("name") or "")
            # zram is virtual swap; loop/rom devices are not physical storage.
            if str(d.get("type") or "") != "disk" or name.startswith("zram"):
                continue
            fstype = d.get("fstype")
            has_filesystem = fstype not in NON_FILESYSTEM_TYPES and (
                bool(fstype and fstype != ND)
                or any(path.startswith("/") for path in self._mountpoints(d))
            )
            presence = self._bindings.get(name)
            layout_presence = presence if physical_counts[name] == 1 else None
            topology = self.sources.get('topology', SourceState())
            layout_key = (layout_presence, topology)
            cached = self._layout_cache.get(name)
            if cached is None or cached[0] is not d or cached[1] != layout_key:
                cached = (d, layout_key, build_layout(d, layout_presence, topology))
                self._layout_cache[name] = cached
            disks.append(DiskMetrics(
                name=name or ND,
                model=str(d.get("model") or ND).strip() or ND,
                kind=self._kind(d),
                transport=str(d.get("tran") or ND).upper(),
                size_bytes=d.get("size"),
                temperature_c=self._temperature(name),
                partitions=list(self._flatten(d.get("children"))),
                temperature_source=self._temperature_sources.get(name),
                direct_filesystem=self._partition(d, 0, direct=True) if has_filesystem else None,
                layout=cached[2],
            ))
            if self._bindings.get(name) != presence:
                layout_presence = self._bindings.get(name) if physical_counts[name] == 1 else None
                disks[-1].layout = build_layout(d, layout_presence, topology)
        mounts = self._valid_mounts
        topology = self.sources.get('topology')
        if topology and topology.stale(self._clock()):
            mounts = {}  # Cached topology is retained, but cannot assert a current root role.
        orders = disk_orders(devices, mounts, self._mount_identities, self._bindings, self._btrfs_members)
        for disk in disks:
            disk.order = orders[disk.name]
        self._layout_cache = {name: entry for name, entry in self._layout_cache.items() if name in self._device_nodes}
        return order_disks(disks)

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
                self._btrfs_members = self._read_btrfs_members(devices)
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
            active.update(("temperature:" + disk.name, "smart:" + disk.name, "udisks:" + disk.name))
            active.update("usage:" + p.name for p in disk.partitions)
            if disk.direct_filesystem is not None:
                active.add("usage:" + disk.direct_filesystem.name)
        self.sources = {k: v for k, v in self.sources.items() if k in active}
        self.errors = {k: v for k, v in self.errors.items() if k in active or k == "storage"}
        names = {disk.name for disk in disks}
        self._selected_temperatures = {k: v for k, v in self._selected_temperatures.items() if k in names}
        self._temperature_sources = {k: v for k, v in self._temperature_sources.items() if k in names}
        return disks
