"""Phase 3.1: synthetic SMART JSON, failures, cadence and attachment races."""
from dataclasses import FrozenInstanceError, replace
import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock

from src.data.collector import Collector
from src.data.errors import SourceState
from src.data.identity import DeviceIdentity
from src.data.smart import parse_smart_json
from src.data.storage import StorageMonitor
from src.data.udisks import TemperatureReading
from tests.smart_fixtures import smart_json


def nvme_json(exit_status=0, **overrides):
    log = dict(percentage_used=12, power_on_hours=1234, critical_warning=0,
               media_errors=0, num_err_log_entries=0)
    log.update(overrides)
    return smart_json(exit_status=exit_status, device={"protocol": "NVMe"},
                      nvme_smart_health_information_log=log)


class SmartParser(unittest.TestCase):
    def test_nvme_wear_health_hours_and_counters_are_independent(self):
        sample = parse_smart_json(nvme_json(), 0)
        self.assertEqual((sample.health, sample.percentage_used, sample.power_on_hours),
                         ('passed', 12, 1234))
        self.assertEqual((sample.temperature_c, sample.media_errors, sample.error_log_entries), (38, 0, 0))
        self.assertEqual(sample.warnings, ())
        with self.assertRaises(FrozenInstanceError):
            sample.percentage_used = 99

    def test_nvme_wear_above_100_is_not_clamped_or_remaining_life(self):
        sample = parse_smart_json(nvme_json(percentage_used=150), 0)
        self.assertEqual(sample.percentage_used, 150)
        self.assertTrue(sample.warnings)

    def test_critical_warning_and_counters_survive_success_status(self):
        sample = parse_smart_json(nvme_json(critical_warning=4, media_errors=8,
                                          num_err_log_entries=2**80), 0)
        self.assertEqual(sample.critical_warning, 4)
        self.assertEqual(sample.health, 'failed')
        self.assertEqual(sample.error_log_entries, 2**80)
        self.assertEqual(len(sample.warnings), 3)

    def test_sata_vendor_attributes_never_become_wear_or_hours(self):
        text = smart_json(device={"protocol": "ATA"},
                          ata_smart_attributes={"table": [
                              {"id": 9, "name": "Power_On_Hours", "raw": {"value": 999}},
                              {"id": 202, "name": "Percent_Lifetime_Remain", "value": 95},
                              {"id": 231, "name": "SSD_Life_Left", "value": 20}]},
                          endurance_used={"current_percent": 5})
        sample = parse_smart_json(text, 0)
        self.assertEqual(sample.health, 'passed')
        self.assertIsNone(sample.percentage_used)
        self.assertIsNone(sample.power_on_hours)
        decoded = json.loads(text)
        decoded['power_on_time'] = {'hours': 123}
        self.assertEqual(parse_smart_json(json.dumps(decoded), 0).power_on_hours, 123)

    def test_absent_optional_attributes_remain_unknown(self):
        sample = parse_smart_json('{"smartctl":{"exit_status":0},"smart_status":{"passed":true}}', 0)
        self.assertEqual(sample.health, 'passed')
        for field in ('percentage_used', 'power_on_hours', 'temperature_c', 'critical_warning', 'media_errors'):
            self.assertIsNone(getattr(sample, field))
        self.assertEqual(parse_smart_json('{"smartctl":{"exit_status":0},"power_on_time":{"hours":0}}', 0).health,
                         'unknown')

    def test_malformed_json_structures_types_and_status_are_rejected(self):
        invalid = ['', 'Permission denied', '{', '[]', 'null', '{}',
                   '{"smartctl":{"exit_status":0}}',
                   '{"smartctl":{"exit_status":true}}',
                   smart_json(exit_status=8), smart_json(smart_status={'passed': 'true'}),
                   smart_json(smart_status=[]), smart_json(temperature=float('nan')),
                   smart_json(temperature=True), smart_json(temperature=-274), smart_json(temperature=10**400),
                   smart_json(smart_support={'available': False}),
                   smart_json(smart_support={'enabled': False}),
                   smart_json(power_on_time={'hours': -1}), smart_json(power_on_time={'hours': True}),
                   nvme_json(percentage_used=256), nvme_json(percentage_used=-1),
                   nvme_json(percentage_used=3.5), nvme_json(critical_warning='0x04'),
                   smart_json(device={'protocol': 'ATA'}, nvme_smart_health_information_log={'percentage_used': 3})]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_smart_json(text, 0)

    def test_health_warning_bits_are_not_acquisition_failures(self):
        for status in (8, 16, 32, 64, 128, 248):
            with self.subTest(status=status):
                sample = parse_smart_json(nvme_json(status), status)
                self.assertEqual(sample.health, 'failed' if status & 8 else 'passed')
                self.assertTrue(sample.warnings)


class SmartAcquisition(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.live = {'sda': DeviceIdentity(diskseq=1, serial='synthetic-A', model='Synthetic')}
        with mock.patch('src.data.storage.shutil.which', return_value=None):
            self.monitor = StorageMonitor(clock=lambda: self.now[0], udisks=False, smart_access=lambda path: True,
                                          identity_reader=lambda name, node: self.live.get(name, DeviceIdentity()))
        self.monitor._smartctl = '/synthetic/smartctl'
        self.monitor._drivetemp_temperature = mock.Mock(return_value=40)
        self.monitor._read_mountinfo = mock.Mock(return_value={})
        self.monitor._run_lsblk = mock.Mock(side_effect=lambda: [
            {'name': name, 'type': 'disk', 'tran': 'sata', 'serial': identity.serial}
            for name, identity in self.live.items()])
        patcher = mock.patch('src.data.storage.subprocess.run', return_value=SimpleNamespace(
            returncode=0, stdout=nvme_json(), stderr=''))
        self.run = patcher.start()
        self.addCleanup(patcher.stop)

    def disk(self):
        return self.monitor.get_metrics()[0]

    def test_health_is_acquired_with_kernel_temperature_and_uses_slow_shared_cache(self):
        first = self.disk()
        self.assertEqual(first.temperature_c, 40)
        self.assertEqual(first.smart.percentage_used, 12)
        for moment in range(101, 160):
            self.now[0] = moment
            self.assertIs(self.disk().smart, first.smart)
        self.run.assert_called_once_with(['/synthetic/smartctl', '-j', '-a', '/dev/sda'],
                                         capture_output=True, text=True, timeout=2.0, check=False)
        self.now[0] = 160
        self.assertEqual(self.disk().smart.state.acquired_at, 160)
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(first.smart.state.acquired_at, 100)

    def test_temperature_fallback_and_health_share_one_json_query(self):
        self.monitor._drivetemp_temperature.return_value = None
        disk = self.disk()
        self.assertEqual(disk.temperature_c, 38)
        self.assertEqual(disk.smart.percentage_used, 12)
        self.run.assert_called_once()

    def test_missing_binary_reports_unavailable_even_with_kernel_temperature(self):
        self.monitor._smartctl = None
        disk = self.disk()
        self.assertEqual(disk.temperature_c, 40)
        self.assertIn('utilidad no disponible', disk.smart.state.error)
        self.assertFalse(disk.smart.state.current(100))
        self.assertIn('smart:sda', self.monitor.errors)
        self.run.assert_not_called()

    def test_health_refresh_never_dates_an_old_temperature_as_new(self):
        self.monitor._drivetemp_temperature.return_value = None
        provider = mock.Mock()
        provider.read.return_value = TemperatureReading(None, SourceState(error='not available'))
        self.monitor._udisks = provider
        first = self.disk()
        self.assertEqual(first.temperature_c, 38)
        provider.read.return_value = TemperatureReading(40, SourceState(95, 95, 752))
        self.now[0] = 160
        self.run.return_value.stdout = smart_json(temperature=55)
        second = self.disk()
        self.assertEqual(second.temperature_c, 55)
        self.assertEqual(second.temperature_source, 'smart:sda')
        self.assertEqual(self.monitor.sources['smart:sda'].acquired_at, 160)
        self.assertEqual(first.temperature_c, 38)
        self.now[0] = 220
        self.run.return_value.stdout = '{"smartctl":{"exit_status":0},"smart_status":{"passed":true}}'
        third = self.disk()
        self.assertIsNone(third.smart.temperature_c)
        self.assertEqual(third.temperature_c, 40)  # Valid UDisks measurement, with its own old age.
        self.assertEqual(third.temperature_source, 'udisks:sda')
        self.assertEqual(self.monitor.sources['udisks:sda'].acquired_at, 95)
        self.assertEqual(self.run.call_count, 3)

    def test_permission_failure_discards_plausible_values(self):
        self.run.return_value = SimpleNamespace(returncode=2, stdout=nvme_json(2), stderr='Permission denied')
        reading = self.disk().smart
        self.assertIsNone(reading.percentage_used)
        self.assertIsNone(reading.state.acquired_at)
        self.assertIn('Permission denied', reading.state.error)
        self.now[0] = 200
        self.disk()
        self.run.assert_called_once()

    def test_exit_masks_keep_health_warnings_separate_from_command_errors(self):
        for code in (0, 1, 2, 3, 4, 8, 16, 32, 64, 128, 12, 255, -9, 256):
            with self.subTest(code=code):
                self.monitor._clear_thermal('sda')
                self.run.return_value = SimpleNamespace(returncode=code, stdout=nvme_json(code), stderr='failure')
                reading = self.disk().smart
                valid = 0 <= code <= 255 and not code & 7
                self.assertEqual(reading.state.current(100), valid)
                if valid:
                    self.assertEqual(reading.percentage_used, 12)
                    self.assertEqual(bool(reading.warnings), code != 0)
                elif code in (4, 12):
                    self.assertEqual(reading.percentage_used, 12)  # Partial, explicitly invalid.
                    self.assertTrue(reading.state.error)
                else:
                    self.assertIsNone(reading.percentage_used)

    def test_failure_backoff_never_rejuvenates_old_data_and_recovery_replaces_it(self):
        first = self.disk().smart
        self.now[0] = 160
        self.run.side_effect = subprocess.TimeoutExpired('smartctl', 2)
        failed = self.disk().smart
        self.assertIn('timeout', failed.state.error)
        self.assertEqual(failed.state.acquired_at, 100)
        self.assertEqual(failed.state.completed_at, 100)
        self.assertFalse(failed.state.current(160))
        for moment in (161, 200, 459):
            self.now[0] = moment
            self.assertIs(self.disk().smart, failed)
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(first.state, SourceState(100, 100, 77))
        self.run.side_effect = None
        self.now[0] = 460
        self.assertTrue(self.disk().smart.state.current(460))
        self.assertEqual(self.run.call_count, 3)
        self.assertNotIn('smart:sda', self.monitor.errors)

    def test_malformed_empty_unsupported_and_os_errors_back_off(self):
        for payload in ('{', '[]', '{"smartctl":{"exit_status":0}}',
                        smart_json(smart_support={'available': False}), None):
            with self.subTest(payload=payload):
                self.monitor._clear_thermal('sda')
                self.run.reset_mock()
                self.run.side_effect = OSError('synthetic I/O') if payload is None else None
                self.run.return_value.stdout = payload
                first = self.disk().smart
                self.assertFalse(first.state.current(self.now[0]))
                self.assertTrue(first.state.error)
                self.now[0] += 1
                self.assertIs(self.disk().smart, first)
                self.run.assert_called_once()

    def test_expiry_without_acquisition_error_is_not_current(self):
        reading = self.disk().smart
        self.assertTrue(reading.state.current(177))
        self.assertFalse(reading.state.current(178))
        self.assertFalse(SourceState().current(100))

    def test_replace_disconnect_and_rename_do_not_transfer_old_health(self):
        first = self.disk().smart
        self.live = {'sdb': self.live['sda']}
        self.monitor._topology_at = None
        self.now[0] = 101
        self.assertIs(self.disk().smart, first)
        self.assertNotIn('sda', self.monitor._smart_cache)
        self.assertEqual(self.run.call_count, 1)
        self.live['sdb'] = replace(self.live['sdb'], diskseq=2)  # Same serial, new attachment.
        self.monitor._topology_at = None
        self.now[0] = 102
        self.run.return_value.stdout = nvme_json(percentage_used=50)
        self.assertEqual(self.disk().smart.percentage_used, 50)
        self.assertEqual(first.percentage_used, 12)
        self.live.clear()
        self.monitor._topology_at = None
        self.assertEqual(self.monitor.get_metrics(), [])
        self.assertFalse(self.monitor._smart_cache)
        self.assertNotIn('smart:sdb', self.monitor.sources)

    def test_identity_change_during_health_read_invalidates_temperature_too(self):
        def race(*args, **kwargs):
            self.live['sda'] = replace(self.live['sda'], diskseq=2)
            return SimpleNamespace(returncode=0, stdout=nvme_json(), stderr='')
        self.run.side_effect = race
        disk = self.disk()
        self.assertIsNone(disk.temperature_c)
        self.assertIsNone(disk.smart.percentage_used)
        self.assertIn('identidad', disk.smart.state.error)
        self.assertFalse(self.monitor._smart_cache)

    def test_identity_change_during_temperature_reselection_discards_health(self):
        self.monitor._drivetemp_temperature.return_value = None
        provider = mock.Mock()
        provider.read.return_value = TemperatureReading(None, SourceState(error='not available'))
        self.monitor._udisks = provider
        self.disk()
        provider.read.return_value = TemperatureReading(40, SourceState(95, 95, 752))
        self.now[0] = 160
        self.run.return_value.stdout = smart_json(temperature=55)
        post_refresh_probes = []
        def read_identity(name, node):
            # SMART has just refreshed; the follow-up temperature check races
            # with a detach. Do not publish the health sample from that session.
            if self.run.call_count == 2 and self.monitor._smart_cache:
                current = self.monitor._smart_cache[name][1]
                if current.state.acquired_at == 160:
                    post_refresh_probes.append(name)
                    if len(post_refresh_probes) == 2:
                        self.live[name] = replace(self.live[name], diskseq=2)
            return self.live[name]
        self.monitor._identity_reader = read_identity
        disk = self.disk()
        self.assertIsNone(disk.smart.percentage_used)
        self.assertFalse(disk.smart.state.current(160))
        self.assertIsNone(disk.temperature_c)
        self.assertEqual(len(post_refresh_probes), 2)

    def test_missing_attachment_token_never_caches_health(self):
        self.live['sda'] = replace(self.live['sda'], diskseq=None)
        self.assertIn('no verificable', self.disk().smart.state.error)
        self.run.assert_not_called()

    def test_collector_publishes_immutable_smart_values_and_diagnostics(self):
        collector = Collector(storage=self.monitor, clock=lambda: self.now[0])
        first = collector.collect_once()
        self.now[0] = 160
        self.run.return_value = SimpleNamespace(returncode=2, stdout='', stderr='Permission denied')
        failed = collector.collect_once()
        self.assertTrue(first.disks[0].smart.state.current(100))
        self.assertFalse(failed.disks[0].smart.state.current(160))
        self.assertEqual(failed.sources['smart:sda'], failed.disks[0].smart.state)
        self.assertIn('Permission denied', failed.errors['smart:sda'])
