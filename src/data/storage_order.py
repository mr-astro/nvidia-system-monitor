"""Pure ordering policy over acquired topology and verified mount evidence.

No device names, model names, labels or equal filesystem UUIDs prove root
ownership. Btrfs membership must be corroborated by its mounted sysfs inventory.
"""
from dataclasses import dataclass
import re

from src.data.identity import Presence, sequence


def device_number(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+:\d+", value):
        return None
    return ':'.join(str(int(part)) for part in value.split(':'))


def attachment_matches(node, presence):
    token = sequence(node.get('diskseq')) or sequence(node.get('_observed_diskseq'))
    return bool(presence and presence.identity.cacheable and token == presence.identity.diskseq)


@dataclass(frozen=True)
class BtrfsMembers:
    devices: frozenset[str] = frozenset()
    reason: str | None = None


@dataclass(frozen=True)
class RootBacking:
    disks: tuple[str, ...] = ()
    reason: str | None = 'Raíz sin verificar'

    @property
    def available(self):
        return self.reason is None

    @property
    def primary(self):
        return self.disks[0] if self.available and len(self.disks) == 1 else None


@dataclass(frozen=True)
class DiskOrder:
    priority: int = 4
    stable_key: tuple[str, ...] = ()
    root: RootBacking = RootBacking()


def transport_priority(node):
    transport = str(node.get('tran') or '').lower()
    subsystems = str(node.get('subsystems') or '').lower().split(':')
    if transport == 'usb' or 'usb' in subsystems:
        return 3
    if transport == 'nvme':
        return 1
    if transport in {'sata', 'ata'}:
        return 2
    return 4


def resolve_root(devices, mounts, mount_bindings, presences, btrfs_members=None):
    root = mounts.get('/')
    if not root:
        return RootBacking(reason='No se pudo verificar el montaje raíz')
    nodes, owners, signatures, conflicts = {}, {}, {}, set()
    disks = {d['name']: d for d in devices if d.get('type') == 'disk' and not d['name'].startswith('zram')}
    if len(disks) != sum(d.get('type') == 'disk' and not d['name'].startswith('zram') for d in devices):
        return RootBacking(reason='Discos físicos repetidos en la topología')

    def visit(node, owner=None):
        if node.get('type') == 'disk':
            owner = node['name'] if node['name'] in disks else None
        number = device_number(node.get('maj:min'))
        if number:
            signature = (node.get('kname') or node['name'],) + tuple(
                node.get(field) for field in ('type', 'size', 'fstype', 'uuid'))
            if number in signatures and signatures[number] != signature:
                conflicts.add(number)
            signatures[number] = signature
            nodes.setdefault(number, []).append(node)
            if owner:
                owners.setdefault(number, set()).add(owner)
        for child in node.get('children') or []:
            visit(child, owner)
    for node in devices:
        visit(node)

    candidates = {number for number, records in nodes.items() for node in records
                  if mount_bindings.get((node['name'], '/')) == root
                  and node.get('fstype') == root[3]}
    if not candidates or candidates & conflicts:
        return RootBacking(reason='La raíz no corresponde a una topología inequívoca y verificada')
    if root[3] == 'btrfs':
        uuids = {str(node.get('uuid') or '').lower() for number in candidates for node in nodes[number]}
        if len(uuids) != 1 or not next(iter(uuids)):
            return RootBacking(reason='UUID Btrfs raíz ausente o ambiguo')
        uuid = next(iter(uuids))
        membership = (btrfs_members or {}).get(uuid)
        if not membership or membership.reason or not membership.devices:
            return RootBacking(reason=(membership.reason if membership else None) or 'Miembros Btrfs no verificables')
        numbers = membership.devices
        if not candidates <= numbers or numbers & conflicts:
            return RootBacking(reason='La raíz Btrfs contradice el inventario montado')
        if any(number not in nodes or any(n.get('fstype') != 'btrfs' or
               str(n.get('uuid') or '').lower() != uuid for n in nodes[number]) for number in numbers):
            return RootBacking(reason='Miembro Btrfs ausente o no corroborado por lsblk')
    else:
        if len(candidates) != 1 or device_number(root[1]) not in candidates:
            return RootBacking(reason='Dispositivo raíz ambiguo o major:minor contradictorio')
        numbers = candidates
    if any(not owners.get(number) for number in numbers):
        return RootBacking(reason='Falta un respaldo físico de la raíz')
    backing = set().union(*(owners[number] for number in numbers))
    if any(not attachment_matches(disks[name], presences.get(name)) for name in backing):
        return RootBacking(reason='La identidad o sesión de un respaldo raíz no está verificada')
    evidence = [p.identity for name, p in presences.items() if name in disks and p.identity.usable]
    for name in backing:
        identity = presences[name].identity
        if (sum(other.diskseq == identity.diskseq for other in evidence) != 1 or
                (identity.physical and sum(other.physical == identity.physical for other in evidence) != 1)):
            return RootBacking(reason='Identidad física o sesión raíz ambigua')
    return RootBacking(tuple(sorted(backing)), None)


def disk_orders(devices, mounts, mount_bindings, presences, btrfs_members=None):
    root = resolve_root(devices, mounts, mount_bindings, presences, btrfs_members)
    result = {}
    for node in devices:
        name = node['name']
        if node.get('type') != 'disk' or name.startswith('zram'):
            continue
        presence: Presence | None = presences.get(name)
        identity = presence.identity if presence else None
        if identity and identity.usable and identity.physical:
            stable = tuple(str(value) for value in identity.physical)
        elif identity and identity.cacheable:
            stable = ('session', str(identity.diskseq).zfill(20))
        else:
            stable = ('name', name)
        priority = 0 if root.available and name in root.disks else transport_priority(node)
        result[name] = DiskOrder(priority, stable, root)
    return result


def order_disks(disks):
    """New list, same metric objects. Reconciliation keys are never changed."""
    return sorted(disks, key=lambda disk: (disk.order.priority,
                  disk.order.stable_key or ('name', disk.name), disk.name))
