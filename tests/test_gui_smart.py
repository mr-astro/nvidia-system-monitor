"""SMART presentation and GTK responsiveness during a blocked SMART command."""
from dataclasses import replace
import copy
import threading
import time
import unittest

from tests.test_gui_smoke import HAVE_GTK
from tests import test_smart
from tests import test_sata_smart_regression
from src.data.collector import Collector, Snapshot
from src.data.errors import SourceState
from src.data.smart import SmartMetrics
from src.data.storage import DiskMetrics
from src.data.storage import StorageMonitor
from src.data.udisks import ATA, NVME
from src.data.udisks_smart import UDisksSMART, parse_properties
from tests.udisks_smart_fixtures import three_disks
from unittest import mock


@unittest.skipUnless(HAVE_GTK, 'GTK 4 o pantalla no disponibles')
class SmartGui(unittest.TestCase):
    def setUp(self):
        from src.gui.storage import DiskCard
        self.card = DiskCard('synthetic')

    def render(self, reading, now=100):
        disk = DiskMetrics('sda', smart=reading)
        snap = Snapshot(disks=[disk], sources={'smart:sda': reading.state})
        self.card.update(disk, None, snap, now, 'C', 60, 85)
        return self.card.smart

    def test_current_health_wear_hours_and_age_have_separate_meanings(self):
        reading = SmartMetrics(health='passed', percentage_used=12, power_on_hours=1234,
                               state=SourceState(90, 90, 77), source='udisks2-nvme')
        section = self.render(reading)
        self.assertEqual(section.status.get_label(), 'Vigente')
        self.assertEqual(section.values['health'].get_label(), 'Sin fallo declarado')
        self.assertEqual(section.values['wear'].get_label(), '12%')
        self.assertEqual(section.values['hours'].get_label(), '1234 h')
        self.assertIn('hace 10 s', section.freshness.get_label())
        self.assertIn('caduca en 67 s', section.freshness.get_label())
        self.assertIn('no mide espacio ocupado', section.get_tooltip_text())
        self.assertFalse(section.warnings_box.get_visible())

    def test_fedora_sata_recovery_removes_false_warnings_and_preserves_temperature(self):
        harness = test_sata_smart_regression.SataSmartRegression()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        harness.monitor._read_smartctl('sda')
        snapshot = Collector(storage=harness.monitor, clock=lambda: harness.now[0]).collect_once()
        self.card.update(snapshot.disks[0], None, snapshot, 100, 'C', 60, 85)
        section = self.card.smart
        self.assertIn('26,9 °C', self.card.temperature.get_label())
        self.assertEqual(section.values['hours'].get_label(), '2305 h')
        self.assertEqual(section.values['wear'].get_label(), 'N/D')
        self.assertEqual(section.values['health'].get_label(), 'Sin fallo declarado')
        self.assertIn('131109', section.warnings.get_label())
        self.assertIn('no declara un fallo actual', section.warnings.get_label())
        self.assertNotIn('SMART: sin permisos', self.card.status.get_label())
        self.assertNotIn('SMART: error', self.card.status.get_label())
        self.assertNotIn('Error de adquisición', self.card.status.get_label())
        self.assertIn('udisks:sda', self.card.temperature.get_tooltip_text())
        self.card.update(snapshot.disks[0], None, snapshot, 853, 'C', 60, 85)
        self.assertEqual(self.card.temperature.get_label(), 'N/D')
        self.assertEqual(section.status.get_label(), 'Obsoleto')
        self.assertIn('Lectura anterior', section.freshness.get_label())
        harness.run.assert_not_called()

    def test_expired_or_failed_values_appear_only_as_explicit_history(self):
        for state, expected in ((SourceState(0, 0, 77), 'stale'),
                                (SourceState(90, 90, 77, 'Permission denied'), 'error')):
            with self.subTest(state=state):
                section = self.render(SmartMetrics(health='passed', percentage_used=12,
                                                   power_on_hours=1234, state=state, source='smartctl'))
                self.assertTrue(section.has_css_class('storage-smart-' + expected))
                self.assertEqual([widget.get_label() for widget in section.values.values()], ['N/D'] * 3)
                self.assertIn('Lectura anterior', section.freshness.get_label())
                self.assertIn('Última lectura (anterior)', section.get_tooltip_text())
                self.assertIn('12%', section.get_tooltip_text())
                self.assertEqual(section.error.get_visible(), expected == 'error')

    def test_absent_fields_are_nd_and_never_zero_or_healthy(self):
        section = self.render(SmartMetrics(state=SourceState(100, 100, 77)))
        self.assertEqual(section.status.get_label(), 'Desconocido')
        self.assertEqual([widget.get_label() for widget in section.values.values()], ['N/D'] * 3)
        section = self.render(SmartMetrics(state=SourceState(error='Permission denied')))
        self.assertEqual(section.status.get_label(), 'Error de consulta')
        self.assertEqual(section.error.get_label(), 'Permission denied')
        self.assertTrue(section.error.get_visible())
        self.assertFalse(section.warnings_box.get_visible())

    def test_warning_counters_and_recovery_update_existing_widget(self):
        section = self.card.smart
        values = dict(section.values)
        reading = SmartMetrics(health='failed', percentage_used=150, critical_warning=4,
                               media_errors=8, error_log_entries=12, warnings=('Alerta simulada',),
                               state=SourceState(90, 90, 77), exit_status=8, source='smartctl')
        self.render(reading)
        self.assertEqual(section.values['health'].get_label(), 'Fallo declarado')
        self.assertEqual(section.values['wear'].get_label(), '150%')
        self.assertTrue(section.values['health'].has_css_class('storage-smart-failed'))
        self.assertEqual(section.warnings.get_label(), 'Alerta simulada')
        self.assertTrue(section.warnings_box.get_visible())
        self.assertIn('Errores de integridad de medios NVMe: 8', section.get_tooltip_text())
        self.assertIn('Entradas del registro de errores NVMe: 12', section.get_tooltip_text())
        self.render(replace(reading, state=SourceState(0, 0, 77)))
        self.assertFalse(section.values['health'].has_css_class('storage-smart-failed'))
        self.render(replace(reading, health='passed', warnings=(), state=SourceState(100, 100, 77)))
        self.assertIs(self.card.smart, section)
        self.assertEqual(section.values, values)
        self.assertFalse(section.has_css_class('storage-smart-stale'))
        self.assertFalse(section.warnings_box.get_visible())

    def test_udisks_source_and_confirmed_counters_have_no_sata_wear_interpretation(self):
        objects, attributes, _ = three_disks()
        for name in ('nvme0n1', 'nvme1n1', 'sda'):
            with self.subTest(name=name):
                path = '/org/freedesktop/UDisks2/drives/synthetic_' + name
                interface = ATA if name == 'sda' else NVME
                reading = parse_properties(interface, objects[path][interface], attributes[(path, interface)])
                section = self.render(replace(reading, state=SourceState(100, 100, 752)))
                detail = section.get_tooltip_text()
                self.assertIn('Fuente SMART: udisks2-', detail)
                self.assertNotIn('Código de salida smartctl', detail)
                if name == 'sda':
                    self.assertIn('131109', section.warnings.get_label())
                    self.assertIn('Atributo ATA 231: temperature-celsius', detail)
                    self.assertEqual(section.values['wear'].get_label(), 'N/D')
                else:
                    self.assertIn('Reserva disponible NVMe: 100%', detail)
                    self.assertEqual(section.values['wear'].get_label(), '7%' if name == 'nvme0n1' else '0%')
                if name == 'nvme0n1':
                    self.assertIn('Apagados inseguros NVMe (contador histórico): 104', detail)

    def test_slow_smart_command_does_not_block_gtk_heartbeat_or_latest_snapshot(self):
        # Use the same synthetic monitor harness as the acquisition tests.
        harness = test_smart.SmartAcquisition()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        entered, release = threading.Event(), threading.Event()
        original = harness.run.return_value
        callers = []
        def blocked(*args, **kwargs):
            callers.append(threading.current_thread().name)
            entered.set()
            if not release.wait(2):
                raise TimeoutError('test failed to release SMART worker')
            return original
        harness.run.side_effect = blocked
        collector = Collector(storage=harness.monitor)
        self._check_heartbeat(collector, entered, release, callers)

    def test_slow_udisks_attributes_do_not_block_gtk(self):
        objects, attributes, contexts = three_disks()
        entered, release = threading.Event(), threading.Event()
        callers = []
        def blocked(path, interface):
            callers.append(threading.current_thread().name)
            entered.set()
            if not release.wait(2):
                raise TimeoutError('test failed to release UDisks worker')
            return attributes[(path, interface)]
        provider = UDisksSMART(fetch=lambda: copy.deepcopy(objects), attributes=blocked,
                               clock=lambda: 100, wall_clock=lambda: 1000)
        with mock.patch('src.data.storage.shutil.which', return_value=None):
            monitor = StorageMonitor(udisks=provider, clock=lambda: 100,
                                     identity_reader=lambda name, node: contexts[name].identity)
        monitor._read_mountinfo = mock.Mock(return_value={})
        monitor._run_lsblk = mock.Mock(return_value=[{'name': 'nvme0n1', 'type': 'disk'}])
        monitor._nvme_temperature = mock.Mock(return_value=40)
        collector = Collector(storage=monitor)
        self._check_heartbeat(collector, entered, release, callers)
        self.assertEqual(collector.latest().disks[0].smart.source, 'udisks2-nvme')

    def _check_heartbeat(self, collector, entered, release, callers):
        from gi.repository import GLib
        self.addCleanup(collector.stop)
        self.addCleanup(release.set)
        collector.start()
        self.assertTrue(entered.wait(2))
        beats, errors = [], []
        loop = GLib.MainLoop()
        def heartbeat():
            try:
                self.assertFalse(collector.latest().disks)
                self.assertFalse(release.is_set())
                self.render(SmartMetrics())
                beats.append(time.monotonic())
                if len(beats) < 8:
                    return GLib.SOURCE_CONTINUE
            except BaseException as exc:
                errors.append(exc)
            loop.quit()
            return GLib.SOURCE_REMOVE
        pulse = GLib.timeout_add(5, heartbeat)
        def deadline():
            errors.append(AssertionError('GTK heartbeat timed out'))
            loop.quit()
            return GLib.SOURCE_REMOVE
        guard = GLib.timeout_add(1000, deadline)
        loop.run()
        if len(beats) == 8:
            GLib.source_remove(guard)
        else:
            GLib.source_remove(pulse)
        release.set()
        collector.stop()
        if errors:
            raise errors[0]
        self.assertEqual(callers, ['hw-collector'])
        self.assertEqual(len(beats), 8)
        self.assertIsNotNone(collector.latest().disks[0].smart.state.acquired_at)
