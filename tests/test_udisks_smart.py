"""ATA/NVMe SMART with the three confirmed disks, entirely simulated."""
import copy
from dataclasses import replace
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from src.data.collector import Collector
from src.data.errors import SourceState
from src.data.smart import parse_smart_json
from src.data.storage import StorageMonitor
from src.data.udisks import ATA, BLOCK, DRIVE, NVME
from src.data.udisks_smart import UDisksSMART, parse_properties
from tests.smart_fixtures import smart_json
from tests.udisks_smart_fixtures import KINGSTON_SMARTCTL, three_disks


class SmartFixture:
    def setUp(self):
        self.now, self.wall = [100.0], [1000.0]
        self.objects, self.attributes, self.contexts = three_disks()
        self.fetch = mock.Mock(side_effect=lambda: copy.deepcopy(self.objects))
        self.get_attributes = mock.Mock(side_effect=lambda path, interface: copy.deepcopy(self.attributes[(path, interface)]))
        self.provider = UDisksSMART(fetch=self.fetch, attributes=self.get_attributes,
                                    clock=lambda: self.now[0], wall_clock=lambda: self.wall[0])
        self.provider.prepare(set(self.contexts.values()))

    def reading(self, name='nvme0n1'):
        return self.provider.read_smart(name, self.contexts[name])

    def path(self, name='nvme0n1'):
        return '/org/freedesktop/UDisks2/drives/synthetic_' + name

    def props(self, name='nvme0n1'):
        return self.objects[self.path(name)][ATA if name == 'sda' else NVME]

    def advance(self, amount):
        self.now[0] += amount
        self.wall[0] += amount


class UDisksSmartReadings(SmartFixture, unittest.TestCase):
    def test_confirmed_dahua_samsung_and_kingston_counters(self):
        dahua = self.reading()
        samsung = self.reading('nvme1n1')
        kingston = self.reading('sda')
        self.assertEqual((dahua.percentage_used, dahua.available_spare, dahua.media_errors, dahua.unsafe_shutdowns),
                         (7, 100, 0, 104))
        self.assertEqual((samsung.percentage_used, samsung.available_spare, samsung.media_errors), (0, 100, 0))
        self.assertIsNone(samsung.unsafe_shutdowns)  # Not supplied; never fabricate zero.
        self.assertEqual(kingston.interface_crc_errors, 131109)
        self.assertEqual([row.id for row in kingston.ata_attributes], [199, 231])
        self.assertIsNone(kingston.percentage_used)
        self.assertEqual([row.name for row in kingston.ata_attributes if row.id == 231], ['temperature-celsius'])
        self.assertAlmostEqual(kingston.temperature_c, 36.85)  # Property, never attribute 231.
        self.assertEqual(kingston.power_on_hours, 200)
        self.assertEqual(kingston.health, 'passed')  # CRC count does not diagnose disk failure.
        self.assertTrue(kingston.warnings)
        self.assertTrue(all(sample.state.current(100) for sample in (dahua, samsung, kingston)))
        self.assertEqual(self.get_attributes.call_count, 3)
        alternative = parse_smart_json(KINGSTON_SMARTCTL, 0)
        self.assertIsNone(alternative.percentage_used)

    def test_nvme_temperature_uses_controller_properties(self):
        reading = self.provider.read('nvme0n1', self.contexts['nvme0n1'])
        self.assertAlmostEqual(reading.value, 36.85)
        self.assertEqual(reading.state.acquired_at, 100)
        self.get_attributes.assert_not_called()

    def test_disabling_smart_or_losing_controller_state_invalidates_cached_health(self):
        for name, change in (('sda', {'SmartEnabled': False}), ('nvme0n1', {'State': 'dead'}),
                             ('nvme0n1', {'SmartCriticalWarning': ['readonly']})):
            with self.subTest(name=name, change=change):
                self.setUp()
                self.assertTrue(self.reading(name).state.current(100))
                self.props(name).update(change)
                self.advance(11)
                self.assertFalse(self.reading(name).state.current(111))
                self.get_attributes.assert_called_once()

    def test_attribute_cache_does_not_refresh_every_second(self):
        first = self.reading()
        for moment in range(101, 160):
            self.now[0] = moment
            self.assertIs(self.reading(), first)
        self.get_attributes.assert_called_once()
        self.now[0] = 160
        self.assertEqual(self.reading().state.acquired_at, 100)
        self.assertEqual(self.get_attributes.call_count, 2)

    def test_attribute_cache_never_combines_new_properties_with_old_attributes(self):
        first = self.reading()
        self.advance(11)
        self.props().update(SmartUpdated=1011, SmartPowerOnHours=201)
        self.attributes[(self.path(), NVME)]['percent_used'] = 8
        second = self.reading()
        self.assertEqual(second.percentage_used, first.percentage_used)
        self.assertEqual(second.state.acquired_at, first.state.acquired_at)
        self.assertIn('renovación pendiente', second.state.error)
        self.assertEqual(second.power_on_hours, 200)
        self.advance(49)
        third = self.reading()
        self.assertEqual((third.percentage_used, third.power_on_hours), (8, 201))
        self.assertEqual(third.state.acquired_at, 111)

    def test_wall_clock_jumps_do_not_reanchor_existing_smartupdated(self):
        first = self.reading()
        for shift in (-500, 1500):
            self.wall[0] += shift
            self.advance(61)
            reading = self.reading()
            self.assertEqual(reading.state.acquired_at, first.state.acquired_at)
            self.assertFalse(reading.state.error)

    def test_stopped_updates_expire_and_regressing_timestamp_is_rejected(self):
        first = self.reading()
        self.advance(753)
        expired = self.reading()
        self.assertFalse(expired.state.current(self.now[0]))
        self.assertEqual(expired.state.acquired_at, first.state.acquired_at)
        self.advance(61)
        self.props()['SmartUpdated'] = 999
        self.assertIn('retrocedió', self.reading().state.error)

    def test_getattributes_errors_back_off_and_preserve_original_age(self):
        for error in (PermissionError('denied'), TimeoutError('timeout'), OSError('disconnected'), ValueError('bad reply')):
            with self.subTest(error=error):
                self.setUp()
                first = self.reading()
                self.advance(60)
                self.get_attributes.side_effect = error
                failed = self.reading()
                self.assertEqual(failed.state.acquired_at, first.state.acquired_at)
                self.assertFalse(failed.state.current(self.now[0]))
                self.advance(100)
                self.assertEqual(self.reading(), failed)
                self.assertEqual(self.get_attributes.call_count, 2)
                self.advance(200)
                self.get_attributes.side_effect = lambda path, interface: self.attributes[(path, interface)]
                self.assertFalse(self.reading().state.error)

    def test_bus_failure_cannot_present_cached_attributes_as_current(self):
        first = self.reading()
        self.advance(11)
        self.fetch.side_effect = OSError('bus gone')
        second = self.reading()
        self.assertEqual(second.percentage_used, first.percentage_used)
        self.assertEqual(second.state.acquired_at, first.state.acquired_at)
        self.assertFalse(second.state.current(self.now[0]))
        self.advance(1)
        self.assertEqual(self.reading().state.error, second.state.error)
        self.assertEqual(self.get_attributes.call_count, 1)

    def test_expired_deadline_stops_before_post_attribute_verification(self):
        original = self.get_attributes.side_effect
        def expire(path, interface):
            self.provider._deadline = time.monotonic() - 1
            return original(path, interface)
        self.get_attributes.side_effect = expire
        reading = self.reading()
        self.assertIn('TimeoutError', reading.state.error)
        self.assertIsNone(reading.state.acquired_at)
        self.fetch.assert_called_once()

    def test_empty_attributes_and_missing_health_properties_are_not_a_measurement(self):
        for interface, props, attrs in ((NVME, {'State': 'live'}, {}),
                                         (ATA, {'SmartSupported': True, 'SmartEnabled': True}, [])):
            with self.subTest(interface=interface), self.assertRaises(ValueError):
                parse_properties(interface, props, attrs)

    def test_malformed_missing_disabled_and_future_properties_never_measure(self):
        for change in ({'SmartUpdated': 0}, {'SmartUpdated': 1002}, {'SmartUpdated': True},
                       {'SmartCriticalWarning': 'spare'}, {'SmartTemperature': float('nan')},
                       {'SmartPowerOnHours': -1}, {'State': 'dead'}):
            with self.subTest(change=change):
                self.setUp()
                self.props().update(change)
                reading = self.reading()
                self.assertIsNone(reading.state.acquired_at)
                self.assertTrue(reading.state.error)
        for prop in ('SmartSupported', 'SmartEnabled'):
            self.setUp()
            self.props('sda')[prop] = False
            self.assertFalse(self.reading('sda').state.current(100))

    def test_absent_values_remain_unknown_and_no_zero_hours_are_invented(self):
        self.attributes[(self.path(), NVME)] = {}
        self.props().update(SmartPowerOnHours=0, SmartTemperature=0)
        reading = self.reading()
        self.assertEqual(reading.health, 'passed')
        for value in (reading.percentage_used, reading.available_spare, reading.media_errors,
                      reading.unsafe_shutdowns, reading.power_on_hours, reading.temperature_c):
            self.assertIsNone(value)

    def test_nvme_attribute_validation_and_critical_warning_strings(self):
        for key, value in (('percent_used', -1), ('percent_used', 256), ('percent_used', True),
                           ('avail_spare', 101), ('media_errors', -1), ('unsafe_shutdowns', '104')):
            with self.subTest(key=key, value=value):
                self.setUp()
                self.attributes[(self.path(), NVME)][key] = value
                self.assertTrue(self.reading().state.error)
        self.setUp()
        self.props()['SmartCriticalWarning'] = ['spare', 'future-warning']
        reading = self.reading()
        self.assertEqual(reading.health, 'failed')
        self.assertTrue(any('future-warning' in warning for warning in reading.warnings))

    def test_ata_ambiguous_crc_name_units_and_id_are_not_guessed(self):
        attrs = self.attributes[(self.path('sda'), ATA)]
        props = self.props('sda')
        for row in ((199, 'attribute-199', 50, 100, 100, 0, 131109, 1, {}),
                    (199, 'udma-crc-error-count', 50, 100, 100, 0, 131109, 0, {}),
                    (231, 'udma-crc-error-count', 50, 100, 100, 0, 131109, 1, {})):
            with self.subTest(row=row):
                reading = parse_properties(ATA, props, [row])
                self.assertIsNone(reading.interface_crc_errors)
                self.assertIsNone(reading.percentage_used)
        for rows in ([attrs[0], attrs[0]], [(199,)], ['bad'], [list(attrs[0][:6]) + [True, 1, {}]]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                parse_properties(ATA, props, rows)

    def test_physical_serial_and_model_mismatch_never_uses_cached_sample(self):
        for field in ('Serial', 'Model'):
            with self.subTest(field=field):
                self.setUp()
                self.reading()
                self.advance(11)
                self.objects[self.path()][DRIVE][field] = 'OTHER'
                reading = self.reading()
                self.assertIsNone(reading.percentage_used)
                self.assertIn('identidad', reading.state.error)
                self.assertNotIn(self.contexts['nvme0n1'], self.provider._health_samples)

    def test_changes_during_getattributes_reject_a_mixed_measurement(self):
        for race in ('timestamp', 'identity', 'disappearance'):
            with self.subTest(race=race):
                self.setUp()
                original = self.get_attributes.side_effect
                def changed(path, interface):
                    result = original(path, interface)
                    if race == 'timestamp':
                        self.props()['SmartUpdated'] = 1001
                    elif race == 'identity':
                        self.objects[self.path()][DRIVE]['Serial'] = 'replacement'
                    else:
                        self.objects.clear()
                    return result
                self.get_attributes.side_effect = changed
                reading = self.reading()
                self.assertIsNone(reading.state.acquired_at)
                self.assertTrue(reading.state.error)

    def test_context_rename_enrichment_and_replacement_lifecycle(self):
        first = self.reading()
        old = self.contexts['nvme0n1']
        enriched = replace(old, identity=replace(old.identity, wwn='synthetic-wwn'))
        self.provider.rebind(old, enriched)
        self.contexts['nvme0n1'] = enriched
        self.assertIs(self.reading(), first)
        block = self.objects.pop('/org/freedesktop/UDisks2/block_devices/nvme0n1')
        block[BLOCK]['Device'] = list(b'/dev/nvme2n1\0')
        self.objects['/org/freedesktop/UDisks2/block_devices/nvme2n1'] = block
        self.contexts['nvme2n1'] = enriched
        self.advance(11)
        self.assertIs(self.reading('nvme2n1'), first)
        replacement = replace(enriched, generation=4, identity=replace(enriched.identity, diskseq=4))
        self.provider.invalidate(enriched)
        self.contexts['nvme2n1'] = replacement
        self.assertIsNot(self.reading('nvme2n1'), first)
        self.assertEqual(self.get_attributes.call_count, 2)


class UDisksSmartIntegration(SmartFixture, unittest.TestCase):
    def test_kernel_identity_change_during_primary_acquisition_discards_every_value(self):
        with mock.patch('src.data.storage.shutil.which', return_value=None):
            monitor = StorageMonitor(clock=lambda: self.now[0], udisks=self.provider,
                                     identity_reader=lambda name, node: self.contexts[name].identity,
                                     smart_access=lambda path: False)
        monitor._prepare_devices([{'name': 'nvme0n1', 'type': 'disk'}])
        original = self.get_attributes.side_effect
        def replace_disk(path, interface):
            old = self.contexts['nvme0n1']
            self.contexts['nvme0n1'] = replace(old, generation=4, identity=replace(old.identity, diskseq=4))
            return original(path, interface)
        self.get_attributes.side_effect = replace_disk
        reading = monitor._health('nvme0n1')
        self.assertIsNone(reading.percentage_used)
        self.assertFalse(reading.state.current(100))
        self.assertFalse(self.provider._health_samples)
        self.assertNotIn('udisks:smart:nvme0n1', monitor.sources)

    def test_primary_avoids_smartctl_for_all_three_disks_without_device_access(self):
        with mock.patch('src.data.storage.shutil.which', return_value=None):
            monitor = StorageMonitor(clock=lambda: self.now[0], udisks=self.provider,
                                     identity_reader=lambda name, node: self.contexts[name].identity,
                                     smart_access=lambda path: False)
        monitor._smartctl = '/synthetic/smartctl'
        monitor._read_mountinfo = mock.Mock(return_value={})
        monitor._run_lsblk = mock.Mock(return_value=[{'name': name, 'type': 'disk'} for name in self.contexts])
        monitor._nvme_temperature = mock.Mock(return_value=None)
        monitor._drivetemp_temperature = mock.Mock(return_value=None)
        collector = Collector(storage=monitor, clock=lambda: self.now[0])
        with mock.patch('src.data.storage.subprocess.run') as run:
            snap = collector.collect_once()
        disks = {disk.name: disk for disk in snap.disks}
        self.assertEqual(disks['nvme0n1'].smart.percentage_used, 7)
        self.assertEqual(disks['nvme1n1'].smart.percentage_used, 0)
        self.assertEqual(disks['sda'].smart.interface_crc_errors, 131109)
        self.assertTrue(all(disk.smart.source.startswith('udisks2') for disk in snap.disks))
        self.assertTrue(all(disk.temperature_source.startswith('udisks:') for disk in snap.disks))
        self.assertIn('udisks:smart:sda', snap.sources)
        self.assertEqual(snap.errors, {})
        run.assert_not_called()

    def test_fallback_requires_access_and_primary_recovers_without_blending(self):
        with mock.patch('src.data.storage.shutil.which', return_value=None):
            monitor = StorageMonitor(clock=lambda: self.now[0], udisks=self.provider,
                                     identity_reader=lambda name, node: self.contexts[name].identity,
                                     smart_access=lambda path: False)
        monitor._smartctl = '/synthetic/smartctl'
        monitor._prepare_devices([{'name': 'nvme0n1', 'type': 'disk'}])
        self.get_attributes.side_effect = PermissionError('UDisks denied')
        with mock.patch('src.data.storage.subprocess.run') as run:
            unavailable = monitor._health('nvme0n1')
        self.assertFalse(unavailable.state.current(100))
        run.assert_not_called()
        self.assertIn('sin acceso', monitor.errors['smart:nvme0n1'])
        self.advance(300)
        monitor._smart_access = lambda path: True
        with mock.patch('src.data.storage.subprocess.run', return_value=SimpleNamespace(
                returncode=0, stdout=smart_json(temperature=42), stderr='')) as run:
            fallback = monitor._health('nvme0n1')
        self.assertEqual(fallback.source, 'smartctl')
        self.assertIsNone(fallback.percentage_used)
        run.assert_called_once()
        self.advance(300)
        self.get_attributes.side_effect = lambda path, interface: self.attributes[(path, interface)]
        self.props()['SmartUpdated'] = int(self.wall[0])
        with mock.patch('src.data.storage.subprocess.run') as run:
            recovered = monitor._health('nvme0n1')
        self.assertEqual(recovered.source, 'udisks2-nvme')
        self.assertEqual(recovered.percentage_used, 7)
        run.assert_not_called()


class UDisksSmartTransport(unittest.TestCase):
    def test_read_only_attribute_signatures_and_noninteractive_options(self):
        from gi.repository import Gio
        for interface, reply, signature in ((NVME, {'percent_used': 7}, '(a{sv})'), (ATA, [], '(a(ysqiiixia{sv}))')):
            with self.subTest(interface=interface):
                provider = UDisksSMART(timeout=.1)
                provider._connection = mock.Mock()
                provider._connection.is_closed.return_value = False
                provider._connection.call_sync.return_value.unpack.return_value = (reply,)
                self.assertEqual(provider._get_attributes('/drive/test', interface), reply)
                args = provider._connection.call_sync.call_args.args
                self.assertEqual(args[1:4], ('/drive/test', interface, 'SmartGetAttributes'))
                self.assertTrue(args[4].unpack()[0]['auth.no_user_interaction'])
                self.assertEqual(args[5].dup_string(), signature)
                self.assertEqual(args[6], Gio.DBusCallFlags.NO_AUTO_START)
                self.assertLessEqual(args[7], 100)

    def test_expired_shared_deadline_cancels_without_another_bus_call(self):
        provider = UDisksSMART(timeout=.1)
        provider._connection = mock.Mock()
        provider._deadline = time.monotonic() - 1
        with self.assertRaises(TimeoutError):
            provider._get_attributes('/drive/test', NVME)
        provider._connection.call_sync.assert_not_called()
