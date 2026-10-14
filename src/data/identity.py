"""Read-only physical evidence and kernel attachment identity, independent of GTK."""
from dataclasses import dataclass, replace
from pathlib import Path
import re


def identifier(value):
    return value.strip() or None if isinstance(value, str) else None


def wwn(value):
    value = identifier(value)
    if value is None:
        return None
    return re.sub(r'^(?:wwn-|naa\.|eui\.|0x)', '', value.lower()).removeprefix('0x')


def sequence(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number < 2**64 else None


@dataclass(frozen=True)
class DeviceIdentity:
    diskseq: int | None = None
    wwn: str | None = None
    serial: str | None = None
    model: str | None = None
    wwid: str | None = None
    error: str | None = None

    @property
    def physical(self):
        if self.wwn:
            return ('wwn', self.wwn)
        if self.serial and self.model:
            return ('serial', self.model, self.serial)
        if self.wwid:
            return ('wwid', self.wwid)
        return None

    @property
    def usable(self):
        return not self.error and (self.diskseq is not None or self.physical is not None)

    @property
    def cacheable(self):
        # A physical identifier is not proof that an attachment never changed.
        return self.usable and self.diskseq is not None

    def same_attachment(self, other):
        if not self.cacheable or not other.cacheable or self.diskseq != other.diskseq:
            return False
        return all(not first or not second or first == second for first, second in (
            (self.wwn, other.wwn), (self.serial, other.serial), (self.wwid, other.wwid),
            (self.model if self.serial else None, other.model if other.serial else None)))

    def enriched(self, other):
        return replace(self, **{field: getattr(other, field) or getattr(self, field)
                                for field in ('wwn', 'serial', 'model', 'wwid')})


@dataclass(frozen=True)
class Presence:
    identity: DeviceIdentity
    generation: int


def probe(name, node, sys_dir='/sys'):
    if not isinstance(name, str) or name in ('.', '..') or not re.fullmatch(r'[A-Za-z0-9._-]+', name):
        return DeviceIdentity(error='Nombre de dispositivo no válido')
    root = Path(sys_dir) / 'class/block' / name

    def read(relative):
        try:
            return identifier((root / relative).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

    before = sequence(read('diskseq'))
    raw_wwid = read('device/wwid') or read('wwid')
    live_wwn = wwn(raw_wwid) if raw_wwid and raw_wwid.lower().startswith(('naa.', 'eui.', '0x')) else None
    live_serial = read('device/serial')
    live_model = read('device/model')
    after = sequence(read('diskseq'))
    if before != after:
        return DeviceIdentity(error='La sesión cambió durante la identificación')
    topology_seq = sequence(node.get('diskseq')) or sequence(node.get('_observed_diskseq'))
    if topology_seq is not None and topology_seq != before:
        return DeviceIdentity(error='La topología corresponde a otra sesión del dispositivo')
    # lsblk evidence is usable only when its own DISKSEQ matches the live kernel.
    correlated = before is not None and topology_seq == before
    topology_wwn = wwn(node.get('wwn')) if correlated else None
    topology_serial = identifier(node.get('serial')) if correlated else None
    if ((live_wwn and topology_wwn and live_wwn != topology_wwn)
            or (live_serial and topology_serial and live_serial != topology_serial)):
        return DeviceIdentity(error='Identificadores físicos contradictorios')
    return DeviceIdentity(before, live_wwn or topology_wwn, live_serial or topology_serial,
                          live_model or (identifier(node.get('model')) if correlated else None), raw_wwid)
