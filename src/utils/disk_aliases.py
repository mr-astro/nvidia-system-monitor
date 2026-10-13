"""User-local visual names resolved through existing udev symlinks.

No block devices are opened. The resolver reads only configuration and symlinks,
and callers cache its result until a topology update.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from src.utils.paths import dev_config_dir

_PARTITION = re.compile(r"-part\d+$")
_PREFIXES = ("wwn-", "nvme-eui.", "nvme-uuid.", "ata-", "nvme-", "scsi-", "usb-")


@dataclass(frozen=True)
class DiskIdentity:
    key: str
    alias: str | None = None


def valid_id(value):
    return (isinstance(value, str) and value.startswith(_PREFIXES)
            and '/' not in value and '\\' not in value
            and not _PARTITION.search(value))


class DiskAliasResolver:
    def __init__(self, config_path=None, by_id_dir="/dev/disk/by-id", device_dir="/dev"):
        self.config_path = Path(config_path) if config_path is not None else dev_config_dir() / "storage-aliases.json"
        self.by_id_dir = Path(by_id_dir)
        self.device_dir = Path(device_dir).resolve()
        self._generation = object()
        self._identities = {}
        self.error = None

    def _aliases(self):
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            aliases = data.get("aliases") if isinstance(data, dict) else None
            if not isinstance(aliases, dict):
                raise ValueError("aliases debe ser un objeto")
            if any(not valid_id(key) or not isinstance(value, str) or not value.strip()
                   or len(value) > 120 for key, value in aliases.items()):
                raise ValueError("alias o identificador no válido")
            return {key: value.strip() for key, value in aliases.items()}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            self.error = f"Configuración de alias: {exc}"
            return {}

    def refresh(self, disks, generation):
        """A generation is the existing topology acquisition time plus disk names."""
        if generation == self._generation:
            return self._identities
        self._generation = generation
        self.error = None
        aliases = self._aliases()
        by_device = {}
        try:
            entries = list(self.by_id_dir.iterdir())
        except FileNotFoundError:
            entries = []
        except OSError as exc:
            self.error = f"Identidad de discos: {exc}"
            entries = []
        for entry in entries:
            if not valid_id(entry.name) or not entry.is_symlink():
                continue
            try:
                target = entry.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if target.parent == self.device_dir:
                by_device.setdefault(target.name, []).append(entry.name)
        names = [disk.name for disk in disks]
        identities = {}
        for disk in disks:
            identifiers = sorted(by_device.get(disk.name, []), key=lambda value: (
                next(i for i, prefix in enumerate(_PREFIXES) if value.startswith(prefix)), value
            ))
            unique = names.count(disk.name) == 1
            configured = {aliases[value] for value in identifiers if value in aliases}
            if len(configured) > 1:
                self.error = "Alias ambiguos: varios nombres configurados para una unidad"
            alias = next(iter(configured)) if unique and len(configured) == 1 else None
            key = "id:" + identifiers[0] if identifiers and unique else "device:" + disk.name
            identities[disk.name] = DiskIdentity(key, alias)
        self._identities = identities
        return identities
