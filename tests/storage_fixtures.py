"""Synthetic topology: no hardware identifiers or machine-specific paths."""
from src.data.identity import DeviceIdentity, Presence


def disk(name='sda', number='8:0', transport='sata', children=None, **overrides):
    node = dict(name=name, kname=name, type='disk', size=512 * 1000, tran=transport,
                diskseq=sum(map(ord, name)) + 1, **{'maj:min': number, 'log-sec': 512},
                fstype=None, pttype='gpt', mountpoints=[], children=children or [])
    node.update(overrides)
    return node


def part(name='sda1', start=2, sectors=998, parent='sda', number='8:1', **overrides):
    node = dict(name=name, kname=name, type='part', start=start, size=512 * sectors,
                pkname=parent, partuuid='synthetic-' + name, parttype='synthetic-gpt-type',
                partn=1 if name == 'sda1' else None, fstype='ext4', mountpoints=[], **{'maj:min': number})
    node.update(overrides)
    return node


def presence(node, generation=1):
    return Presence(DeviceIdentity(diskseq=node['diskseq'], wwn='synthetic-' + node['name']), generation)


def root_mount(source='/dev/sda1', number='8:1', fstype='ext4', mount_id='10'):
    return (mount_id, number, '/', fstype, source)
