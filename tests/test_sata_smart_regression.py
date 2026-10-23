"""3.1C: Fedora A400 SCSI model truncation, identity, and source recovery.

Identity strings/times and the 24 unreported ATA rows are synthetic. The long
and short models, 8298000 seconds, 300 K, row count, CRC and ambiguous 231
reproduce the observed interface shape without retaining physical identifiers.
"""
import copy
from dataclasses import replace
import unittest
from unittest import mock

from src.data.collector import Collector
from src.data.identity import DeviceIdentity, Presence
from src.data.storage import StorageMonitor
from src.data.udisks import ATA, BLOCK, DRIVE, UDisksTemperature
from src.data.udisks_smart import UDisksSMART


def kingston_fedora(updated=1000):
    path = '/org/freedesktop/UDisks2/drives/synthetic_kingston'
    objects = {
        '/org/freedesktop/UDisks2/block_devices/sda': {BLOCK: {
            'Device': b'/dev/sda\0', 'Drive': path, 'Id': 'by-id-wwn-0x500a'}},
        path: {DRIVE: {'Model': 'KINGSTON SA400S37480G', 'Serial': 'SYNTHETIC-A400', 'WWN': '0x500a'},
               ATA: {'SmartSupported': True, 'SmartEnabled': True, 'SmartFailing': False,
                     'SmartUpdated': updated, 'SmartPowerOnSeconds': 8298000,
                     'SmartTemperature': 300.0, 'SmartNumAttributesFailing': 0,
                     'SmartNumAttributesFailedInThePast': 0, 'SmartNumBadSectors': 0}},
    }
    rows = [(index, f'attribute-{index}', 0, 100, 100, 0, 0, 0, {}) for index in range(1, 25)]
    rows += [(199, 'udma-crc-error-count', 50, 100, 100, 0, 131109, 1, {}),
             (231, 'temperature-celsius', 0, 95, 95, 0, 368150, 4, {})]
    identity = DeviceIdentity(diskseq=1, wwn='500a', serial='SYNTHETIC-A400', model='KINGSTON SA400S3')
    return objects, rows, Presence(identity, 1), path


class SataSmartRegression(unittest.TestCase):
    def setUp(self):
        self.now, self.wall = [100.0], [1000.0]
        self.objects, self.rows, self.context, self.path = kingston_fedora()
        self.fetch = mock.Mock(side_effect=lambda: copy.deepcopy(self.objects))
        self.attributes = mock.Mock(side_effect=lambda path, interface: copy.deepcopy(self.rows))
        self.provider = UDisksSMART(fetch=self.fetch, attributes=self.attributes,
                                    clock=lambda: self.now[0], wall_clock=lambda: self.wall[0])
        self.access = mock.Mock(return_value=False)
        with mock.patch('src.data.storage.shutil.which', return_value=None):
            self.monitor = StorageMonitor(udisks=self.provider, clock=lambda: self.now[0],
                                          identity_reader=lambda name, node: self.context.identity,
                                          smart_access=self.access)
        self.monitor._smartctl = '/synthetic/smartctl'
        self.monitor._run_lsblk = mock.Mock(return_value=[{'name': 'sda', 'type': 'disk', 'tran': 'sata'}])
        self.monitor._read_mountinfo = mock.Mock(return_value={})
        self.monitor._drivetemp_temperature = mock.Mock(return_value=None)
        patcher = mock.patch('src.data.storage.subprocess.run', side_effect=AssertionError('smartctl must not run'))
        self.run = patcher.start()
        self.addCleanup(patcher.stop)

    def advance(self, seconds):
        self.now[0] += seconds
        self.wall[0] += seconds

    def test_sda_truncated_scsi_model_accepts_matching_wwn_and_serial(self):
        block = self.objects['/org/freedesktop/UDisks2/block_devices/sda'][BLOCK]
        drive = self.objects[self.path][DRIVE]
        self.assertTrue(self.provider._matches(block, drive, self.context))
        self.assertTrue(self.context.identity.cacheable)
        self.assertEqual(self.context.identity.diskseq, 1)

    def test_26_ata_attributes_and_temperature_are_available_without_direct_access(self):
        collector = Collector(storage=self.monitor, clock=lambda: self.now[0])
        snapshot = collector.collect_once()
        disk, = snapshot.disks
        self.assertEqual(disk.smart.source, 'udisks2-ata')
        self.assertEqual(disk.smart.health, 'passed')
        self.assertEqual(disk.smart.power_on_hours, 2305)
        self.assertEqual(disk.smart.interface_crc_errors, 131109)
        self.assertEqual(len(disk.smart.ata_attributes), 26)
        self.assertIsNone(disk.smart.percentage_used)
        self.assertAlmostEqual(disk.temperature_c, 26.85)
        self.assertEqual(disk.temperature_source, 'udisks:sda')
        self.assertEqual(snapshot.errors, {})
        self.assertTrue(disk.smart.state.current(100))
        self.attributes.assert_called_once_with(self.path, ATA)
        self.access.assert_not_called()
        self.run.assert_not_called()

    def test_valid_primary_retires_unused_smartctl_permission_error(self):
        # Reproduce a previous failed attempt, then a working UDisks primary.
        self.monitor._read_smartctl('sda')
        self.assertIn('smart:sda', self.monitor.errors)
        self.access.reset_mock()
        disk, = self.monitor.get_metrics()
        self.assertTrue(disk.smart.state.current(100))
        self.assertNotIn('smart:sda', self.monitor.errors)
        self.assertNotIn('smart:sda', self.monitor.sources)
        self.assertAlmostEqual(disk.temperature_c, 26.85)
        self.access.assert_not_called()
        self.run.assert_not_called()

    def test_unused_permission_cache_restores_diagnostics_only_when_fallback_is_needed(self):
        self.monitor._read_smartctl('sda')
        self.monitor.get_metrics()
        self.advance(60)
        self.attributes.side_effect = TimeoutError('attributes timeout')
        disk, = self.monitor.get_metrics()
        self.assertFalse(disk.smart.state.current(160))
        self.assertIn('sin acceso', self.monitor.errors['smart:sda'])
        self.assertIn('attributes timeout', self.monitor.errors['udisks:smart:sda'])
        self.assertEqual(self.monitor.sources['smart:sda'].error, self.monitor.errors['smart:sda'])
        self.assertAlmostEqual(disk.temperature_c, 26.85)
        self.assertTrue(self.monitor.sources['udisks:sda'].current(160))
        self.run.assert_not_called()

    def test_attribute_failure_does_not_remove_independent_valid_temperature(self):
        self.attributes.side_effect = ValueError('malformed attribute reply')
        disk, = self.monitor.get_metrics()
        self.assertFalse(disk.smart.state.current(100))
        self.assertAlmostEqual(disk.temperature_c, 26.85)
        self.assertEqual(disk.temperature_source, 'udisks:sda')
        self.assertIsNone(self.monitor.sources['udisks:sda'].error)
        self.assertIn('malformed attribute reply', self.monitor.errors['udisks:smart:sda'])
        self.run.assert_not_called()

    def test_age_never_reanchors_and_expired_sata_is_not_current(self):
        self.objects[self.path][ATA]['SmartUpdated'] = 628  # Observed ~372 s old, within 752 s.
        disk, = self.monitor.get_metrics()
        state = disk.smart.state
        self.assertEqual(state.age(100), 372)
        self.assertTrue(state.current(100))
        self.advance(60)
        again, = self.monitor.get_metrics()
        self.assertEqual(again.smart.state.acquired_at, state.acquired_at)
        self.advance(321)
        expired, = self.monitor.get_metrics()
        self.assertFalse(expired.smart.state.current(self.now[0]))
        self.assertTrue(self.monitor.sources['udisks:sda'].stale(self.now[0]))
        self.assertEqual(expired.smart.state.acquired_at, state.acquired_at)
        self.run.assert_not_called()

    def test_truncation_does_not_accept_conflicting_ids_or_an_arbitrary_prefix(self):
        block = self.objects['/org/freedesktop/UDisks2/block_devices/sda'][BLOCK]
        drive = self.objects[self.path][DRIVE]
        for change in ({'Serial': 'OTHER'}, {'WWN': '0x500b'}, {'Model': 'KINGSTON OTHER MODEL'}):
            with self.subTest(change=change):
                self.assertFalse(self.provider._matches(block, {**drive, **change}, self.context))
        for model in ('KINGSTON', 'KINGSTON SA400S', 'KINGSTON SA400S3748X'):
            context = replace(self.context, identity=replace(self.context.identity, model=model))
            self.assertFalse(self.provider._matches(block, drive, context))
        nvme = {**block, 'Device': b'/dev/nvme0n1\0'}
        self.assertFalse(self.provider._matches(nvme, drive, self.context))

    def test_model_alone_never_establishes_physical_identity(self):
        block = {**self.objects['/org/freedesktop/UDisks2/block_devices/sda'][BLOCK], 'Id': ''}
        drive = {'Model': 'KINGSTON SA400S37480G'}
        self.assertFalse(self.provider._matches(block, drive, self.context))

    def test_diskseq_change_during_attributes_discards_health_and_temperature(self):
        original = self.attributes.side_effect
        def replace_attachment(path, interface):
            self.context = replace(self.context, identity=replace(self.context.identity, diskseq=2), generation=2)
            return original(path, interface)
        self.attributes.side_effect = replace_attachment
        disk, = self.monitor.get_metrics()
        self.assertIsNone(disk.smart.state.acquired_at)
        self.assertIsNone(disk.temperature_c)
        self.assertNotIn('sda', self.monitor._bindings)
        self.assertFalse(self.provider._health_samples)

    def test_scsi_long_model_is_not_rejected_by_temperature_provider(self):
        provider = UDisksTemperature(fetch=self.fetch, clock=lambda: self.now[0], wall_clock=lambda: self.wall[0])
        reading = provider.read('sda', self.context)
        self.assertAlmostEqual(reading.value, 26.85)
        self.assertTrue(reading.state.current(100))
