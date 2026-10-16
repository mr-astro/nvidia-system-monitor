"""A2 regressions: synthetic devices only, no live SMART, sysfs or D-Bus."""
import copy
from dataclasses import replace
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from src.data.identity import DeviceIdentity, probe, sequence
from src.data.storage import StorageMonitor
from src.data.udisks import ATA, BLOCK, UDisksTemperature
from src.data.collector import Collector

DRIVE = 'org.freedesktop.UDisks2.Drive'


def identity(letter='A', seq=1):
    return DeviceIdentity(diskseq=seq, wwn='500' + letter.lower(), serial='synthetic-' + letter, model='Synthetic')


class ThermalIdentity(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.wall = [1000.0]
        self.live = {'sda': identity()}
        self.values = {'sda': 38}
        self.nodes = []
        self.topology()
        with mock.patch('src.data.storage.shutil.which', return_value=None):
            self.monitor = StorageMonitor(clock=lambda: self.now[0], topology_interval=0,
                                          identity_reader=self.identify, udisks=False)
        self.monitor._smartctl = '/synthetic/smartctl'
        self.monitor._read_mountinfo = mock.Mock(return_value={})
        self.monitor._run_lsblk = mock.Mock(side_effect=lambda: copy.deepcopy(self.nodes))
        self.monitor._drivetemp_temperature = mock.Mock(return_value=None)
        self.monitor._nvme_temperature = mock.Mock(return_value=None)
        patch = mock.patch('src.data.storage.subprocess.run', side_effect=self.smart)
        self.run = patch.start()
        self.addCleanup(patch.stop)

    def identify(self, name, node):
        current = self.live.get(name, DeviceIdentity(error='Dispositivo ausente'))
        if node.get('diskseq') is not None and current.diskseq != node['diskseq']:
            return DeviceIdentity(error='Topología de otra sesión')
        return current

    def topology(self):
        self.nodes = [{'name': name, 'type': 'disk', 'tran': 'sata', 'model': value.model,
                       'diskseq': value.diskseq, 'serial': value.serial, 'wwn': value.wwn}
                      for name, value in self.live.items()]

    def smart(self, args, **kwargs):
        name = args[-1].rsplit('/', 1)[-1]
        return SimpleNamespace(returncode=0, stdout=f'Temperature: {self.values[name]} Celsius\n', stderr='')

    def metrics(self):
        return {disk.name: disk for disk in self.monitor.get_metrics()}

    def replace_disk(self, new=None, name='sda', value=65):
        self.now[0] += 1
        self.wall[0] += 1
        self.live[name] = new or identity('B', 2)
        self.values[name] = value
        self.topology()

    def udisks(self, updated=704):
        def fetch():
            objects = {}
            for name, value in self.live.items():
                if not value.physical:
                    continue
                drive = '/drive/' + name
                objects['/block/' + name] = {BLOCK: {'Device': list(('/dev/' + name).encode()) + [0],
                                                   'Id': 'by-id-wwn-' + value.wwn, 'Drive': drive}}
                objects[drive] = {DRIVE: {'WWN': value.wwn, 'Serial': value.serial}, ATA: {
                    'SmartSupported': True, 'SmartEnabled': True,
                    'SmartUpdated': updated, 'SmartTemperature': self.values[name] + 273.15}}
            return objects
        self.fetch = mock.Mock(side_effect=fetch)
        provider = UDisksTemperature(fetch=self.fetch, clock=lambda: self.now[0], wall_clock=lambda: self.wall[0])
        self.monitor._udisks = provider
        return provider

    def test_original_38_65_reproducer_queries_new_disk_and_preserves_old_snapshot(self):
        collector = Collector(storage=self.monitor, clock=lambda: self.now[0])
        first = collector.collect_once()
        self.replace_disk()
        second = collector.collect_once()
        self.assertEqual(second.disks[0].temperature_c, 65)
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(second.sources['smart:sda'].acquired_at, 101)
        self.assertEqual(second.errors, {})
        self.assertEqual(first.disks[0].temperature_c, 38)
        self.assertEqual(first.sources['smart:sda'].acquired_at, 100)

    def test_direct_filesystem_mount_changes_preserve_a2_temperature_and_identity(self):
        self.nodes[0].update(fstype='ext4', mountpoints=['/mnt/datos'], label='Datos', size=10**12)
        mounts = {'/mnt/datos': ('10', '8:0', '/', 'ext4', '/dev/sda')}
        self.monitor._read_mountinfo.side_effect = lambda: dict(mounts)
        stat = SimpleNamespace(f_blocks=1000, f_bfree=400, f_bavail=300, f_frsize=4096)
        with mock.patch('src.data.storage.os.statvfs', return_value=stat) as statvfs:
            first = self.metrics()['sda']
            state = self.monitor.sources['smart:sda']
            binding = self.monitor._bindings['sda']
            generations = dict(self.monitor.thermal_generations)
            self.assertEqual(first.direct_filesystem.used_bytes, 600 * 4096)
            for moment, mounted, failing in ((101, True, True), (102, False, False), (103, True, False)):
                self.now[0] = moment
                statvfs.side_effect = OSError('synthetic failure') if failing else None
                if mounted:
                    mounts['/mnt/datos'] = ('10' if moment == 101 else '11', '8:0', '/', 'ext4', '/dev/sda')
                else:
                    mounts.clear()
                disk = self.metrics()['sda']
                self.assertEqual(disk.temperature_c, 38)
                self.assertEqual(disk.temperature_source, 'smart:sda')
                self.assertIs(self.monitor.sources['smart:sda'], state)
                self.assertEqual(state.age(moment), moment - 100)
                self.assertEqual(self.monitor._bindings['sda'], binding)
                self.assertEqual(self.monitor._bindings['sda'].identity, self.live['sda'])
                self.assertEqual(self.monitor.thermal_generations, generations)
                self.assertEqual(disk.model, self.live['sda'].model)
                if mounted:
                    self.assertEqual(disk.direct_filesystem.used_bytes, 600 * 4096)
                else:
                    self.assertIsNone(disk.direct_filesystem.used_bytes)
            self.assertEqual(self.run.call_count, 1)
            self.assertNotIn('usage:sda', self.monitor.errors)
            self.assertEqual(self.monitor.sources['usage:sda'].acquired_at, 103)
            self.assertEqual(first.direct_filesystem.used_bytes, 600 * 4096)

    def test_cached_topology_cannot_authorize_new_attachment(self):
        self.monitor.topology_interval = 15
        self.metrics()
        self.replace_disk()
        pending = self.metrics()['sda']
        self.assertIsNone(pending.temperature_c)
        self.assertNotIn('sda', self.monitor._temperature_cache)
        self.assertIsNone(self.monitor.sources['temperature:sda'].acquired_at)
        self.now[0] += 1
        self.assertEqual(self.metrics()['sda'].temperature_c, 65)
        self.assertEqual(self.monitor._run_lsblk.call_count, 2)

    def test_changes_of_wwn_or_serial_invalidate_even_with_same_diskseq(self):
        for change in ({'wwn': '500b'}, {'serial': 'synthetic-B'}):
            with self.subTest(change=change):
                self.metrics()
                self.replace_disk(replace(self.live['sda'], **change))
                self.assertEqual(self.metrics()['sda'].temperature_c, 65)
                self.assertEqual(self.monitor.sources['smart:sda'].acquired_at, self.now[0])

    def test_rename_continuous_attachment_preserves_smart_age_and_cache(self):
        self.metrics()
        before = self.monitor.sources['smart:sda']
        self.live = {'sdb': self.live['sda']}
        self.values = {'sdb': 38}
        self.topology()
        self.now[0] += 1
        disk = self.metrics()['sdb']
        self.assertEqual(disk.temperature_c, 38)
        self.assertIs(self.monitor.sources['smart:sdb'], before)
        self.assertEqual(self.run.call_count, 1)
        self.assertNotIn('sda', self.monitor._temperature_cache)
        self.assertNotIn('smart:sda', self.monitor.sources)

    def test_disappearance_and_reappearance_start_new_session(self):
        self.metrics()
        old = self.monitor._bindings['sda']
        self.nodes = []; self.live = {}; self.now[0] += 1
        self.assertEqual(self.metrics(), {})
        self.assertFalse(self.monitor._temperature_cache)
        self.assertNotIn('smart:sda', self.monitor.sources)
        self.replace_disk(identity('A', 1))  # Even identical IDs cannot resurrect an observed absent session.
        self.assertEqual(self.metrics()['sda'].temperature_c, 65)
        self.assertNotEqual(self.monitor._bindings['sda'], old)
        self.assertEqual(self.run.call_count, 2)

    def test_same_physical_disk_reconnected_with_new_diskseq_clears_state(self):
        self.metrics()
        self.replace_disk(identity('A', 2))
        self.assertEqual(self.metrics()['sda'].temperature_c, 65)
        self.assertEqual(self.run.call_count, 2)

    def test_nvme_smart_fallback_does_not_survive_disappearance(self):
        self.live = {'nvme0n1': identity()}; self.values = {'nvme0n1': 38}; self.topology()
        self.metrics(); self.nodes = []; self.live = {}; self.now[0] += 1
        self.metrics(); self.replace_disk(name='nvme0n1')
        self.assertEqual(self.metrics()['nvme0n1'].temperature_c, 65)
        self.assertEqual(self.monitor.sources['smart:nvme0n1'].acquired_at, self.now[0])

    def test_no_physical_ids_with_diskseq_can_validate_current_direct_smart(self):
        self.live['sda'] = DeviceIdentity(diskseq=1); self.topology()
        self.assertEqual(self.metrics()['sda'].temperature_c, 38)
        self.replace_disk(DeviceIdentity(diskseq=2))
        self.assertEqual(self.metrics()['sda'].temperature_c, 65)

    def test_unknown_and_contradictory_identity_publish_no_temperature(self):
        for value in (DeviceIdentity(), DeviceIdentity(error='contradictory', diskseq=1)):
            with self.subTest(identity=value):
                self.live['sda'] = value; self.topology()
                self.assertIsNone(self.metrics()['sda'].temperature_c)
                self.assertIsNone(self.monitor.sources['temperature:sda'].acquired_at)
        self.run.assert_not_called()

    def test_duplicate_physical_ids_make_both_devices_unavailable(self):
        self.live['sdb'] = identity('A', 2); self.topology()
        self.assertTrue(all(d.temperature_c is None for d in self.metrics().values()))
        self.run.assert_not_called()

    def test_no_diskseq_allows_only_fresh_direct_reads_without_expensive_polling(self):
        self.live['sda'] = replace(identity(), diskseq=None); self.topology()
        self.assertEqual(self.metrics()['sda'].temperature_c, 38)
        self.now[0] += 1
        self.assertIsNone(self.metrics()['sda'].temperature_c)
        self.assertNotIn('smart:sda', self.monitor.sources)
        self.assertEqual(self.run.call_count, 1)
        self.now[0] += 10; self.values['sda'] = 65
        self.assertEqual(self.metrics()['sda'].temperature_c, 65)
        self.assertEqual(self.run.call_count, 2)

    def test_changed_identity_during_smart_discards_value_and_all_old_state(self):
        def racing(args, **kwargs):
            self.live['sda'] = identity('B', 2)
            return SimpleNamespace(returncode=0, stdout='Temperature: 38 Celsius\n', stderr='')
        self.run.side_effect = racing
        self.assertIsNone(self.metrics()['sda'].temperature_c)
        self.assertNotIn('sda', self.monitor._temperature_cache)
        self.assertNotIn('smart:sda', self.monitor.sources)
        self.assertNotIn('sda', self.monitor._selected_temperatures)
        self.assertIsNone(self.monitor.sources['temperature:sda'].acquired_at)
        self.run.side_effect = self.smart
        self.topology(); self.values['sda'] = 65
        self.assertEqual(self.metrics()['sda'].temperature_c, 65)

    def test_expired_old_smart_and_new_read_failure_never_retain_38(self):
        self.metrics(); self.now[0] += 20; self.replace_disk()
        self.run.return_value = SimpleNamespace(returncode=2, stdout='', stderr='synthetic permission failure')
        self.run.side_effect = None
        self.assertIsNone(self.metrics()['sda'].temperature_c)
        self.assertIsNone(self.monitor.sources['smart:sda'].acquired_at)
        self.assertIn('permission', self.monitor.errors['smart:sda'])

    def test_new_device_restarts_error_logging_without_inheriting_age(self):
        self.run.side_effect = None
        self.run.return_value = SimpleNamespace(returncode=2, stdout='', stderr='synthetic denied')
        logger = mock.Mock(); collector = Collector(storage=self.monitor, logger=logger, clock=lambda: self.now[0])
        collector.collect_once(); collector.collect_once()
        self.assertEqual(logger.error.call_count, 1)
        self.replace_disk(); snapshot = collector.collect_once()
        self.assertEqual(logger.error.call_count, 2)
        self.assertIsNone(snapshot.sources['smart:sda'].acquired_at)

    def test_replacing_one_disk_preserves_other_temperature_times_and_error(self):
        self.live['sdb'] = identity('C', 3); self.values['sdb'] = 42; self.topology()
        def read(args, **kwargs):
            result = self.smart(args, **kwargs)
            if args[-1].endswith('sdb'):
                result.returncode = 4; result.stderr = 'synthetic checksum'
            return result
        self.run.side_effect = read
        self.metrics()
        state = self.monitor.sources['smart:sdb']; error = self.monitor.errors['smart:sdb']
        self.replace_disk()
        current = self.metrics()
        self.assertEqual((current['sda'].temperature_c, current['sdb'].temperature_c), (65, 42))
        self.assertIs(self.monitor.sources['smart:sdb'], state)
        self.assertEqual(self.monitor.errors['smart:sdb'], error)
        self.assertEqual(self.run.call_count, 3)

    def test_udisks_replacement_reads_new_snapshot_not_cached_old_objectmanager(self):
        provider = self.udisks()
        self.assertEqual(round(self.metrics()['sda'].temperature_c), 38)
        old = self.monitor._bindings['sda']
        self.replace_disk()
        self.assertEqual(round(self.metrics()['sda'].temperature_c), 65)
        self.assertNotIn(old, provider._samples)
        self.assertEqual(self.fetch.call_count, 2)
        self.run.assert_not_called()

    def test_udisks_daemon_still_reporting_old_association_is_rejected(self):
        provider = self.udisks()
        self.metrics(); old_objects = copy.deepcopy(provider._objects)
        self.replace_disk(); self.fetch.side_effect = None; self.fetch.return_value = old_objects
        self.assertEqual(self.metrics()['sda'].temperature_c, 65)  # Validated smartctl fallback.
        self.assertIsNone(self.monitor.sources['udisks:sda'].acquired_at)
        self.assertIn('ObjectManager', self.monitor.errors['udisks:sda'])

    def test_kingston_rename_and_wall_jump_preserve_original_smartupdated_anchor(self):
        self.values['sda'] = 40; provider = self.udisks()
        self.metrics(); state = self.monitor.sources['udisks:sda']; context = self.monitor._bindings['sda']
        self.assertEqual(state.age(100), 296)
        self.live = {'sdb': self.live['sda']}; self.values = {'sdb': 40}; self.topology()
        self.now[0] = 111; self.wall[0] = 511
        self.assertEqual(round(self.metrics()['sdb'].temperature_c), 40)
        self.assertEqual(self.monitor.sources['udisks:sdb'].acquired_at, state.acquired_at)
        self.assertEqual(self.monitor.sources['udisks:sdb'].age(111), 307)
        self.assertEqual(self.monitor._bindings['sdb'], context)
        self.assertEqual(len(provider._samples), 1)
        self.run.assert_not_called()

    def test_udisks_changed_identity_during_fetch_cannot_publish_old_value(self):
        self.udisks(); fetch = self.fetch.side_effect
        def race():
            result = fetch()
            self.live['sda'] = identity('B', 2)
            return result
        self.fetch.side_effect = race
        self.assertIsNone(self.metrics()['sda'].temperature_c)
        self.assertFalse(self.monitor._udisks._samples)
        self.assertNotIn('udisks:sda', self.monitor.sources)

    def test_many_stable_cycles_keep_smart_cadence_and_measurement_age(self):
        for offset in range(10):
            self.now[0] = 100 + offset
            self.assertEqual(self.metrics()['sda'].temperature_c, 38)
            self.assertEqual(self.monitor.sources['smart:sda'].acquired_at, 100)
        self.assertEqual(self.run.call_count, 1)

    def test_multiple_udisks_devices_share_initial_query_and_are_isolated_on_change(self):
        self.live['sdb'] = identity('C', 3); self.values['sdb'] = 42; self.topology(); provider = self.udisks()
        self.metrics(); context = self.monitor._bindings['sdb']; state = self.monitor.sources['udisks:sdb']
        self.assertEqual(self.fetch.call_count, 1)
        self.replace_disk(); disks = self.metrics()
        self.assertEqual(round(disks['sdb'].temperature_c), 42)
        self.assertEqual(self.monitor.sources['udisks:sdb'].acquired_at, state.acquired_at)
        self.assertEqual(self.monitor._bindings['sdb'], context)
        self.assertEqual(self.fetch.call_count, 2)
        self.assertIn(context, provider._samples)

    def test_enriched_identity_keeps_smart_measurement_and_presence_generation(self):
        self.live['sda'] = DeviceIdentity(diskseq=1); self.topology()
        self.metrics(); state = self.monitor.sources['smart:sda']; generation = self.monitor._bindings['sda'].generation
        self.live['sda'] = identity(); self.topology(); self.now[0] += 1
        self.assertEqual(self.metrics()['sda'].temperature_c, 38)
        self.assertIs(self.monitor.sources['smart:sda'], state)
        self.assertEqual(self.monitor._bindings['sda'].generation, generation)
        self.assertEqual(self.run.call_count, 1)

    def test_enriched_identity_keeps_udisks_anchor_across_wall_jump(self):
        self.live['sda'] = replace(identity(), serial=None); self.topology(); provider = self.udisks()
        self.metrics(); state = self.monitor.sources['udisks:sda']
        self.live['sda'] = identity(); self.topology(); self.now[0] += 1; self.wall[0] += 1000
        self.assertEqual(round(self.metrics()['sda'].temperature_c), 38)
        self.assertEqual(self.monitor.sources['udisks:sda'].acquired_at, state.acquired_at)
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(len(provider._samples), 1)

    def test_unknown_device_does_not_force_udisks_or_lsblk_on_every_cycle(self):
        self.live['sdb'] = DeviceIdentity(); self.topology(); self.udisks()
        self.monitor.topology_interval = 15
        logger = mock.Mock(); collector = Collector(storage=self.monitor, logger=logger, clock=lambda: self.now[0])
        for offset in range(10):
            self.now[0] = 100 + offset
            snapshot = collector.collect_once()
            self.assertEqual(round(snapshot.disks[0].temperature_c), 38)
            self.assertIsNone(snapshot.disks[1].temperature_c)
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(self.monitor._run_lsblk.call_count, 1)
        self.assertEqual(logger.error.call_count, 1)
        self.run.assert_not_called()

    def test_udisks_bus_backoff_on_replacement_never_inherits_previous_sample(self):
        self.udisks(); self.metrics()
        self.now[0] = 111; self.wall[0] = 1011
        self.fetch.side_effect = OSError('synthetic bus failure')
        self.metrics(); self.replace_disk()
        self.assertEqual(self.metrics()['sda'].temperature_c, 65)
        self.assertIsNone(self.monitor.sources['udisks:sda'].acquired_at)
        self.assertEqual(self.fetch.call_count, 2)

    def test_sysfs_replacement_discards_retained_old_candidate(self):
        self.monitor._drivetemp_temperature.side_effect = [38, None]
        self.metrics(); self.replace_disk()
        self.assertEqual(self.metrics()['sda'].temperature_c, 65)
        self.assertNotIn('temperature:sda', self.monitor.sources)

    def test_smart_measurement_is_rejected_if_device_disappears_during_call(self):
        def race(args, **kwargs):
            self.live.clear()
            return SimpleNamespace(returncode=0, stdout='Temperature: 38 Celsius\n', stderr='')
        self.run.side_effect = race
        self.assertIsNone(self.metrics()['sda'].temperature_c)
        self.assertFalse(self.monitor._temperature_cache)
        self.assertNotIn('smart:sda', self.monitor.sources)

    def test_duplicate_identity_does_not_trigger_repeated_topology_queries(self):
        self.live['sdb'] = identity('A', 2); self.values['sdb'] = 42; self.topology()
        self.monitor.topology_interval = 15
        for offset in range(10):
            self.now[0] = 100 + offset
            self.assertTrue(all(d.temperature_c is None for d in self.metrics().values()))
        self.assertEqual(self.monitor._run_lsblk.call_count, 1)


class KernelIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.device = self.root / 'class/block/sda'
        (self.device / 'device').mkdir(parents=True)
        (self.device / 'diskseq').write_text('1\n')

    def test_diskseq_is_attachment_not_permanent_physical_identity(self):
        value = probe('sda', {}, self.root)
        self.assertEqual(value.diskseq, 1)
        self.assertIsNone(value.physical)
        self.assertTrue(value.cacheable)

    def test_correlated_lsblk_metadata_is_used_without_diskseq_column(self):
        value = probe('sda', {'_observed_diskseq': 1, 'wwn': '0x500a', 'serial': 'Synthetic-A', 'model': 'Synthetic'}, self.root)
        self.assertEqual(value.wwn, '500a')
        self.assertEqual(value.physical, ('wwn', '500a'))

    def test_uncorrelated_lsblk_ids_are_not_proof(self):
        value = probe('sda', {'wwn': '0x500a', 'serial': 'Synthetic-A'}, self.root)
        self.assertIsNone(value.physical)

    def test_old_topology_diskseq_is_rejected(self):
        self.assertFalse(probe('sda', {'diskseq': 2}, self.root).usable)

    def test_live_and_topology_wwn_conflict_is_rejected(self):
        (self.device / 'device/wwid').write_text('naa.500b')
        self.assertFalse(probe('sda', {'diskseq': 1, 'wwn': '500a'}, self.root).usable)

    def test_live_physical_evidence_without_diskseq_is_fresh_only(self):
        (self.device / 'diskseq').unlink()
        (self.device / 'device/wwid').write_text('naa.500a')
        value = probe('sda', {}, self.root)
        self.assertTrue(value.usable)
        self.assertFalse(value.cacheable)

    def test_missing_and_malformed_evidence_is_unusable(self):
        (self.device / 'diskseq').write_text('invalid')
        self.assertFalse(probe('sda', {}, self.root).usable)
        for invalid in (True, 1.5, '0', '-1', 'invalid', str(2**64)):
            self.assertIsNone(sequence(invalid))

    def test_diskseq_change_during_probe_rejects_mixed_identity(self):
        original = Path.read_text
        values = iter(('1', '2'))
        def racing(path, *args, **kwargs):
            if path.name == 'diskseq':
                return next(values)
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'read_text', racing):
            self.assertFalse(probe('sda', {}, self.root).usable)

    def test_lsblk_fallback_keeps_physical_fields_and_remembers_supported_columns(self):
        import json
        with mock.patch('src.data.storage.shutil.which', return_value=None):
            monitor = StorageMonitor(sys_dir=str(self.root), udisks=False)
        monitor._lsblk = '/synthetic/lsblk'
        output = json.dumps({'blockdevices': [{'name': 'sda', 'type': 'disk', 'wwn': '0x500a',
                                               'serial': 'synthetic-A', 'model': 'Synthetic'}]})
        unsupported = SimpleNamespace(returncode=1, stdout='', stderr='unknown column DISKSEQ')
        success = SimpleNamespace(returncode=0, stdout=output, stderr='')
        with mock.patch('src.data.storage.subprocess.run', side_effect=[unsupported, success]) as run:
            node, = monitor._run_lsblk()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(probe('sda', node, self.root).wwn, '500a')
        self.assertNotIn('DISKSEQ', monitor._lsblk_columns)
        self.assertIn('WWN', monitor._lsblk_columns)
        with mock.patch('src.data.storage.subprocess.run', return_value=success) as run:
            monitor._run_lsblk()
        self.assertEqual(run.call_count, 1)
