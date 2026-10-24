"""Phase 3.2: display truth, independently of GTK and SMART acquisition."""
from dataclasses import replace
import unittest

from src.data.errors import SourceState
from src.data.smart import SmartMetrics
from src.gui.storage_smart_model import prepare_smart
from tests.udisks_smart_fixtures import three_disks
from src.data.udisks import ATA, NVME
from src.data.udisks_smart import parse_properties


class SmartPresentation(unittest.TestCase):
    def sample(self, **fields):
        return SmartMetrics(health='passed', percentage_used=7, power_on_hours=2306,
                            state=SourceState(100, 100, 752), source='udisks2-nvme', **fields)

    def test_current_values_and_remaining_freshness_are_independent(self):
        view = prepare_smart(self.sample(), 154)
        self.assertEqual((view.state, view.status), ('valid', 'Vigente'))
        self.assertEqual((view.health, view.wear, view.hours), ('Sin fallo declarado', '7%', '2306 h'))
        self.assertIn('hace 54 s', view.freshness)
        self.assertIn('caduca en 698 s', view.freshness)
        self.assertEqual(view.warnings, ())
        self.assertFalse(view.failed)

    def test_zero_and_wear_above_100_are_kept_as_reported(self):
        for used in (0, 100, 150, 255):
            with self.subTest(used=used):
                view = prepare_smart(replace(self.sample(), percentage_used=used), 100)
                self.assertEqual(view.wear, f'{used}%')
                self.assertEqual(view.health, 'Sin fallo declarado')

    def test_sata_unknown_sources_and_invalid_values_cannot_assert_wear(self):
        for source in ('udisks2-ata', 'udisks2', None, 'unverified'):
            with self.subTest(source=source):
                view = prepare_smart(replace(self.sample(), source=source, percentage_used=95), 100)
                self.assertEqual(view.wear, 'N/D')
        for used in (-1, 256, True, float('nan'), 5.5, '7'):
            with self.subTest(used=used):
                self.assertEqual(prepare_smart(replace(self.sample(), percentage_used=used), 100).wear, 'N/D')
        self.assertEqual(prepare_smart(replace(self.sample(), source='smartctl'), 100).wear, '7%')

    def test_absent_and_partial_readings_do_not_invent_health_or_hours(self):
        missing = prepare_smart(SmartMetrics(), 100)
        self.assertEqual(missing.state, 'unknown')
        self.assertEqual((missing.health, missing.wear, missing.hours), ('N/D', 'N/D', 'N/D'))
        self.assertEqual(missing.freshness, 'Sin lectura válida')
        partial = prepare_smart(SmartMetrics(state=SourceState(100, 100, 77), source='smartctl'), 100)
        self.assertEqual(partial.state, 'unknown')
        self.assertIn('caduca en 77 s', partial.freshness)
        zero_hours = prepare_smart(replace(self.sample(), health='unknown', power_on_hours=0), 100)
        self.assertEqual(zero_hours.health, 'N/D')
        self.assertEqual(zero_hours.hours, '0 h')

    def test_expiry_only_uses_original_acquisition_time(self):
        reading = self.sample()
        at_limit = prepare_smart(reading, 852)
        self.assertEqual(at_limit.state, 'valid')
        self.assertIn('caduca en 0 s', at_limit.freshness)
        expired = prepare_smart(reading, 853)
        self.assertEqual((expired.state, expired.status), ('stale', 'Obsoleto'))
        self.assertEqual((expired.health, expired.wear, expired.hours), ('N/D', 'N/D', 'N/D'))
        self.assertIn('Lectura anterior', expired.freshness)
        self.assertIn('caducada', expired.freshness)
        self.assertIn('2306 h', expired.detail)
        self.assertEqual(reading.state.acquired_at, 100)

    def test_failed_acquisition_hides_values_but_preserves_history(self):
        reading = replace(self.sample(), state=SourceState(100, 100, 752, 'Permission denied'))
        view = prepare_smart(reading, 110)
        self.assertEqual(view.state, 'error')
        self.assertEqual(view.error, 'Permission denied')
        self.assertEqual((view.health, view.wear, view.hours), ('N/D', 'N/D', 'N/D'))
        self.assertIn('no vigente', view.freshness)
        self.assertIn('Última lectura (anterior)', view.detail)
        self.assertIn('2306 h', view.detail)
        empty = prepare_smart(SmartMetrics(state=SourceState(error='no access')), 110)
        self.assertEqual(empty.state, 'error')
        self.assertNotIn('Última lectura', empty.detail)

    def test_crc_is_a_separate_historical_warning_not_a_health_failure(self):
        reading = replace(self.sample(), source='udisks2-ata', percentage_used=None,
                          interface_crc_errors=131109,
                          warnings=('Contador histórico CRC de interfaz SATA: 131109',))
        view = prepare_smart(reading, 110)
        self.assertEqual(view.health, 'Sin fallo declarado')
        self.assertEqual(view.wear, 'N/D')
        self.assertFalse(view.failed)
        self.assertEqual(len(view.warnings), 1)
        self.assertIn('no declara un fallo actual', view.warnings[0])
        historical = prepare_smart(reading, 853)
        self.assertIn('lectura anterior', historical.warnings_title)
        self.assertFalse(historical.failed)
        failed = prepare_smart(replace(reading, health='failed'), 110)
        self.assertTrue(failed.failed)
        self.assertEqual(failed.health, 'Fallo declarado')

    def test_three_disk_fixtures_show_only_confirmed_wear_and_counters(self):
        objects, attributes, _ = three_disks()
        expected = {'nvme0n1': '7%', 'nvme1n1': '0%', 'sda': 'N/D'}
        for name, wear in expected.items():
            with self.subTest(name=name):
                path = '/org/freedesktop/UDisks2/drives/synthetic_' + name
                interface = ATA if name == 'sda' else NVME
                sample = parse_properties(interface, objects[path][interface], attributes[(path, interface)])
                view = prepare_smart(replace(sample, state=SourceState(100, 100, 752)), 100)
                self.assertEqual(view.wear, wear)
                self.assertEqual(view.state, 'valid')
                if name == 'sda':
                    self.assertFalse(view.failed)
                    self.assertIn('131109', '\n'.join(view.warnings))
                    self.assertNotIn('95%', view.detail)
