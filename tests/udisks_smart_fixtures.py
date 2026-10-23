"""Confirmed counters in synthetic D-Bus envelopes, not live hardware dumps.

Models/counters supplied by the user; serials, paths, times, temperatures and
operating hours below are fabricated solely to exercise identity and freshness.
"""
from src.data.identity import DeviceIdentity, Presence
from src.data.udisks import ATA, BLOCK, DRIVE, NVME
from tests.smart_fixtures import smart_json


KINGSTON_SMARTCTL = smart_json(device={'protocol': 'ATA'}, ata_smart_attributes={'table': [
    {'id': 199, 'name': 'UDMA_CRC_Error_Count', 'raw': {'value': 131109}},
    {'id': 231, 'name': 'SSD_Life_Left', 'value': 92, 'raw': {'value': 92}}]})


def three_disks(updated=1000):
    objects, attributes, contexts = {}, {}, {}
    models = [('nvme0n1', 'Dahua C970'), ('nvme1n1', 'Samsung PM9C1b'), ('sda', 'Kingston A400')]
    for number, (name, model) in enumerate(models, 1):
        serial = 'SYNTHETIC-' + name
        path = '/org/freedesktop/UDisks2/drives/synthetic_' + name
        interface = ATA if name == 'sda' else NVME
        props = {'SmartUpdated': updated, 'SmartTemperature': 310, **(
            {'SmartSupported': True, 'SmartEnabled': True, 'SmartFailing': False,
             'SmartPowerOnSeconds': 720000, 'SmartNumAttributesFailing': 0,
             'SmartNumAttributesFailedInThePast': 0, 'SmartNumBadSectors': 0} if interface == ATA else
            {'State': 'live', 'SmartCriticalWarning': [], 'SmartPowerOnHours': 200})}
        objects['/org/freedesktop/UDisks2/block_devices/' + name] = {BLOCK: {
            'Device': list(('/dev/' + name).encode()) + [0], 'Id': 'synthetic-' + name, 'Drive': path}}
        objects[path] = {DRIVE: {'Model': model, 'Serial': serial}, interface: props}
        contexts[name] = Presence(DeviceIdentity(diskseq=number, serial=serial, model=model), number)
        if interface == NVME:
            attributes[(path, interface)] = {'percent_used': 7 if name == 'nvme0n1' else 0,
                                             'avail_spare': 100, 'media_errors': 0}
            if name == 'nvme0n1':
                attributes[(path, interface)]['unsafe_shutdowns'] = 104
        else:
            attributes[(path, interface)] = [
                (199, 'udma-crc-error-count', 50, 100, 100, 0, 131109, 1, {}),
                # Deliberately conflicting interpretation vs smartctl. Do not
                # treat this name/value as an endurance estimate.
                (231, 'temperature-celsius', 50, 92, 92, 0, 92000, 4, {})]
    return objects, attributes, contexts
