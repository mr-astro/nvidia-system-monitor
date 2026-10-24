"""Phase 3.2: render SMART states, reflow and preserve the storage card."""
from dataclasses import replace
import time
import unittest
from unittest import mock

from tests.test_gui_smoke import HAVE_GTK
from tests.test_storage_chart_model import layout_for
from tests.test_storage_usage_model import filesystem
from src.data.collector import Snapshot
from src.data.errors import SourceState
from src.data.smart import SmartMetrics
from src.data.storage import DiskMetrics
from src.data.storage_presentation import StoragePresentation
from src.utils.disk_aliases import DiskIdentity


@unittest.skipUnless(HAVE_GTK, 'GTK 4 o pantalla no disponibles')
class SmartRendering(unittest.TestCase):
    def setUp(self):
        from gi.repository import Gdk, Gtk, GLib
        from src.gui.storage_smart import SmartSection, SMART_CSS
        self.Gtk, self.GLib = Gtk, GLib
        self.section = SmartSection()
        self.css = Gtk.CssProvider()
        self.css.load_from_data(SMART_CSS.encode())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.css,
                                                   Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.windows = []
        self.addCleanup(self.cleanup)
        self.reading = SmartMetrics(health='passed', percentage_used=7, power_on_hours=2306,
                                    source='udisks2-nvme', state=SourceState(100, 100, 752))

    def cleanup(self):
        from gi.repository import Gdk
        for window in self.windows:
            window.destroy()
        self.Gtk.StyleContext.remove_provider_for_display(Gdk.Display.get_default(), self.css)
        self.drain()

    def drain(self):
        deadline = time.monotonic() + .08
        while time.monotonic() < deadline:
            while self.GLib.MainContext.default().pending():
                self.GLib.MainContext.default().iteration(False)
            time.sleep(.002)

    def show(self, widget, width=720, height=500):
        window = self.Gtk.Window(default_width=width, default_height=height)
        scroll = self.Gtk.ScrolledWindow()
        scroll.set_policy(self.Gtk.PolicyType.NEVER, self.Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(widget)
        window.set_child(scroll)
        self.windows.append(window)
        window.present()
        self.drain()
        return window

    def test_valid_unknown_stale_and_error_produce_rendered_nodes(self):
        from gi.repository import Gtk
        self.section.update(self.reading, 110)
        window = self.show(self.section)
        for state, sample in (
            ('valid', self.reading), ('unknown', SmartMetrics()),
            ('stale', replace(self.reading, state=SourceState(0, 0, 77))),
            ('error', replace(self.reading, state=SourceState(100, 100, 752, 'Permission denied'))),
        ):
            with self.subTest(state=state):
                self.section.update(sample, 110)
                self.drain()
                self.assertTrue(self.section.has_css_class('storage-smart-' + state))
                self.assertGreater(self.section.get_width(), 0)
                self.assertGreater(self.section.get_height(), 0)
                paintable = Gtk.WidgetPaintable.new(self.section)
                snapshot = Gtk.Snapshot()
                paintable.snapshot(snapshot, self.section.get_width(), self.section.get_height())
                node = snapshot.to_node()
                self.assertIsNotNone(node)
                texture = window.get_renderer().render_texture(node, None)
                self.assertGreater(texture.get_width(), 0)
                self.assertGreater(texture.get_height(), 0)

    def test_metrics_reflow_when_window_shrinks(self):
        self.section.update(self.reading, 110)
        window = self.show(self.section)
        children = [self.section.metrics.get_child_at_index(index) for index in range(3)]
        self.assertEqual(len({child.get_allocation().y for child in children}), 1)
        window.set_default_size(240, 500)
        self.drain()
        self.assertEqual(window.get_width(), 240)
        self.assertGreater(len({child.get_allocation().y for child in children}), 1)
        self.assertEqual(self.section.values['health'].get_label(), 'Sin fallo declarado')
        for widget in self.section.values.values():
            self.assertFalse(widget.get_layout().is_ellipsized())
            self.assertGreaterEqual(widget.get_height(), widget.get_layout().get_pixel_size().height)

    def test_long_errors_and_warnings_wrap_without_fixed_height_or_ellipsis(self):
        from gi.repository import Pango
        error = 'Consulta denegada: ' + 'diagnóstico_muy_largo_' * 30
        reading = replace(self.reading, state=SourceState(100, 100, 752, error),
                          warnings=('Advertencia anterior con contexto ' * 12,))
        self.section.update(reading, 110)
        window = self.show(self.section, width=240)
        self.assertEqual(window.get_width(), 240)
        for widget in (self.section.status, self.section.freshness, self.section.error, self.section.warnings):
            self.assertTrue(widget.get_wrap())
            self.assertEqual(widget.get_ellipsize(), Pango.EllipsizeMode.NONE)
            self.assertEqual(widget.get_size_request(), (-1, -1))
            self.assertLessEqual(widget.get_width(), self.section.get_width())
            self.assertGreaterEqual(widget.get_height(), widget.get_layout().get_pixel_size().height)
        self.assertIn('lectura anterior', self.section.warnings_heading.get_label())
        self.assertEqual(self.section.error.get_label(), error)

    def test_smart_changes_preserve_both_rings_temperature_partitions_and_alias_identity(self):
        from src.gui.storage import StoragePanel
        panel = StoragePanel()
        source = SourceState(100, 100, 1000)
        disk = DiskMetrics('sda', model='KINGSTON SA400S37480G', kind='SSD SATA',
                           temperature_c=26.85, temperature_source='udisks:sda',
                           partitions=[filesystem()], layout=layout_for(source=source),
                           smart=replace(self.reading, source='udisks2-ata', percentage_used=None))
        snapshot = Snapshot(sequence=1, disks=[disk], sources={
            'topology': source, 'usage:sda1': source, 'udisks:sda': source},
            storage_presentation=StoragePresentation(identities=(
                ('sda', DiskIdentity('synthetic:sda', 'Archivo', expansion_key='physical:A')),)))
        panel.update(snapshot, 110)
        self.show(panel, width=900, height=720)
        card = panel.disk_cards['synthetic:sda']
        card.expander.set_expanded(True)
        self.drain()
        chart, area = card.donut, card.donut.area
        outer, inner = chart.model.segments, chart._usage_arcs
        rows, temperature = dict(card.partition_rows), card.temperature.get_label()
        section = card.smart
        self.assertTrue(outer)
        self.assertTrue(inner)
        with mock.patch.object(chart, '_invalidate') as invalidate:
            for sample in (SmartMetrics(), replace(disk.smart, interface_crc_errors=131109),
                           replace(disk.smart, state=SourceState(0, 0, 77)),
                           replace(disk.smart, state=SourceState(100, 100, 752, 'No access')),
                           disk.smart):
                disk.smart = sample
                panel.update(snapshot, 110)
                self.drain()
        invalidate.assert_not_called()
        self.assertIs(card.donut.area, area)
        self.assertEqual(chart.model.segments, outer)
        self.assertEqual(chart._usage_arcs, inner)
        self.assertEqual(card.partition_rows, rows)
        self.assertEqual(card.temperature.get_label(), temperature)
        self.assertTrue(card.expander.get_expanded())
        self.assertIs(card.smart, section)
        self.assertEqual(panel._expansion_keys['synthetic:sda'], 'physical:A')
        self.assertEqual(dict(snapshot.storage_presentation.identities)['sda'].alias, 'Archivo')
