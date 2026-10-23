"""Real GTK/Cairo regression tests with synthetic, validated physical geometry."""
from dataclasses import replace
import gc
import math
import time
import unittest
from unittest import mock
import weakref

from tests.test_gui_smoke import HAVE_GTK
from tests.test_storage_chart_model import layout_for
from tests.storage_fixtures import disk, part
from tests.test_storage_usage_model import filesystem, usage_fixture
from src.data.collector import Snapshot
from src.data.errors import SourceState
from src.data.storage import DiskMetrics, PartitionMetrics
from src.data.storage_presentation import StoragePresentation
from src.utils.disk_aliases import DiskIdentity
from src.gui.storage_usage_model import prepare_usage


@unittest.skipUnless(HAVE_GTK, 'GTK 4 o pantalla no disponibles')
class StorageCharts(unittest.TestCase):
    def setUp(self):
        import cairo
        from gi.repository import Gtk, GLib
        from src.gui.storage_chart import PhysicalDonut
        self.Gtk, self.GLib, self.cairo = Gtk, GLib, cairo
        self.chart = PhysicalDonut()
        self.windows = []
        self.addCleanup(self.close_windows)

    def close_windows(self):
        for window in self.windows:
            window.destroy()
        self.drain()

    def drain(self, duration=0.06):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            while self.GLib.MainContext.default().pending():
                self.GLib.MainContext.default().iteration(False)
            time.sleep(0.002)

    def show(self, child, width=900, height=650):
        window = self.Gtk.Window(default_width=width, default_height=height)
        scroller = self.Gtk.ScrolledWindow()
        scroller.set_policy(self.Gtk.PolicyType.NEVER, self.Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(child)
        window.set_child(scroller)
        self.windows.append(window)
        window.present()
        self.drain()
        return window

    def snapshot(self, *disks, sequence=1):
        return Snapshot(sequence=sequence, disks=list(disks),
                        sources={'topology': SourceState(10, 11, 20)},
                        storage_presentation=StoragePresentation(identities=tuple(
                            (d.name, DiskIdentity('synthetic:' + d.name)) for d in disks)))

    def panel(self):
        from src.gui.storage import StoragePanel
        return StoragePanel()

    def test_one_persistent_drawing_area_per_identified_disk(self):
        panel = self.panel()
        disks = [DiskMetrics('sda', layout=layout_for()), DiskMetrics('sdb', layout=layout_for(disk('sdb', '8:16')))]
        snap = self.snapshot(*disks)
        panel.update(snap, 11)
        cards = dict(panel.disk_cards)
        areas = {key: card.donut.area for key, card in cards.items()}
        for _ in range(3):
            panel.update(snap, 12)
        self.assertEqual(panel.disk_cards, cards)
        self.assertEqual(len(areas), 2)
        for key, card in panel.disk_cards.items():
            self.assertIs(card.donut.area, areas[key])
            self.assertIsInstance(areas[key], self.Gtk.DrawingArea)

    def test_no_queue_draw_for_equal_geometry_or_usage_temperature_and_age_changes(self):
        chart = self.chart
        chart.update(layout_for(), 11)
        self.show(chart)
        with mock.patch.object(chart.area, 'queue_draw') as redraw:
            for now in (12, 13, 14):
                chart.update(layout_for(), now)
            chart.update(replace(layout_for(), source=SourceState(14, 15, 20)), 15)
        redraw.assert_not_called()
        self.assertTrue(chart.model.available)
        panel = self.panel()
        metric = DiskMetrics('sda', layout=layout_for())
        snap = self.snapshot(metric)
        panel.update(snap, 11)
        donut = next(iter(panel.disk_cards.values())).donut
        with mock.patch.object(donut, '_invalidate') as invalidate:
            metric.temperature_c = 42
            metric.partitions = [PartitionMetrics('sda1', mountpoints=['/'], used_bytes=123)]
            panel.update(snap, 12)
        invalidate.assert_not_called()

    def test_geometry_change_updates_segments_and_reuses_legend_rows_and_area(self):
        chart = self.chart
        chart.update(layout_for(), 11)
        self.show(chart)
        area, rows = chart.area, dict(chart.rows)
        node = disk(children=[part(start=10, sectors=100), part('sda2', start=200, sectors=400, number='8:2')])
        with mock.patch.object(chart.area, 'queue_draw') as redraw:
            chart.update(layout_for(node), 12)
        redraw.assert_called_once()
        self.assertIs(chart.area, area)
        key = chart.model.segments[1].key
        self.assertIs(chart.rows[key][0], rows[key][0])
        self.assertEqual(chart.model.segments[3].percentage, '40,00%')

    def test_unavailable_state_has_neutral_ring_and_diagnostic(self):
        chart = self.chart
        chart.update(layout_for(disk(children=[part(start=None)])), 11)
        self.assertFalse(chart.model.available)
        self.assertFalse(chart.rows)
        self.assertTrue(chart.state.get_visible())
        self.assertEqual(chart.state.get_label(), 'Distribución no disponible')
        self.assertTrue(chart.reason.get_label())
        self.assertNotIn('Sin particionar', chart.area.get_tooltip_text())

    def test_same_snapshot_expires_and_recovers_without_replacing_area(self):
        chart = self.chart
        layout = layout_for()
        chart.update(layout, 11)
        area = chart.area
        chart.update(layout, 31)
        self.assertFalse(chart.model.available)
        self.assertFalse(chart.rows)
        self.assertIn('obsoleta', chart.reason.get_label())
        chart.update(replace(layout, source=SourceState(31, 32, 20)), 32)
        self.assertTrue(chart.model.available)
        self.assertIs(chart.area, area)

    def test_acquisition_errors_discard_sectors_even_with_recent_geometry(self):
        chart = self.chart
        layout = layout_for()
        chart.update(layout, 11)
        chart.update(replace(layout, source=SourceState(10, 11, 20, 'lsblk: error')), 12)
        self.assertEqual(chart.model.segments, ())
        self.assertIn('lsblk: error', chart.reason.get_label())

    def test_empty_and_direct_filesystem_have_real_full_circle_legends(self):
        for node, title in [(disk(), 'Sin particionar'),
                            (disk(fstype='xfs', pttype=None), 'Dispositivo completo')]:
            with self.subTest(title=title):
                self.chart.update(layout_for(node), 11)
                self.assertEqual(len(self.chart.rows), 1)
                text = next(iter(self.chart.rows.values()))[2].get_label()
                self.assertIn(title, text)
                self.assertIn('100,00%', text)

    def test_tiny_partition_remains_in_accessible_legend(self):
        self.chart.update(layout_for(disk(size=512 * 10**9, children=[part(start=2, sectors=1)])), 11)
        self.assertIn('sda1', self.chart.area.get_tooltip_text())
        self.assertIn('<0,01%', self.chart.area.get_tooltip_text())
        self.assertIn('no indica espacio disponible', self.chart.note.get_label())

    def test_usb_hotplug_keeps_other_areas_and_expansion(self):
        panel = self.panel()
        metric = DiskMetrics('sda', model='Modelo real', layout=layout_for())
        panel.update(self.snapshot(metric), 11)
        card = panel.disk_cards['synthetic:sda']
        area = card.donut.area
        card.expander.set_expanded(True)
        usb = DiskMetrics('sdb', transport='usb', layout=layout_for(disk('sdb', '8:16', 'usb')))
        panel.update(self.snapshot(metric, usb, sequence=2), 12)
        panel.update(self.snapshot(metric, sequence=3), 13)
        panel.update(self.snapshot(metric, usb, sequence=4), 14)
        self.assertIs(panel.disk_cards['synthetic:sda'], card)
        self.assertIs(card.donut.area, area)
        self.assertTrue(card.expander.get_expanded())
        self.assertEqual(card.title.get_label(), 'Modelo real')

    def test_hidden_stack_page_defers_drawing_until_mapped(self):
        stack = self.Gtk.Stack()
        stack.add_named(self.Gtk.Label(label='Rendimiento'), 'performance')
        stack.add_named(self.chart, 'storage')
        self.show(stack)
        self.assertFalse(self.chart.area.get_mapped())
        with mock.patch.object(self.chart.area, 'queue_draw') as redraw:
            self.chart.update(layout_for(), 11)
            self.chart.update(layout_for(disk()), 12)
            redraw.assert_not_called()
            stack.set_visible_child_name('storage')
            self.drain()
        redraw.assert_called_once()
        self.assertTrue(self.chart.model.available)
        self.assertEqual(len(self.chart.model.segments), 1)

    def test_resizing_reflows_without_horizontal_scroll_or_recreating_area(self):
        chart = self.chart
        chart.update(layout_for(), 11)
        window = self.show(chart)
        area = chart.area
        ok, left = chart.visual.compute_bounds(chart)
        self.assertTrue(ok)
        ok, right = chart.legend.compute_bounds(chart)
        self.assertTrue(ok)
        self.assertGreaterEqual(right.get_x(), left.get_x() + left.get_width())
        window.set_default_size(340, 650)
        self.drain(0.12)
        self.assertEqual(window.get_width(), 340)
        ok, left = chart.visual.compute_bounds(chart)
        ok, right = chart.legend.compute_bounds(chart)
        self.assertGreaterEqual(right.get_y(), left.get_y() + left.get_height())
        self.assertLessEqual(right.get_x() + right.get_width(), chart.get_width())
        self.assertIs(chart.area, area)

    def test_real_cairo_drawing_works_with_system_and_configurable_dark_colors(self):
        from src.gui.app import build_css
        from src.utils.config import DEFAULT_CONFIG
        chart = self.chart
        chart.update(layout_for(), 11)
        window = self.show(chart)
        window.add_css_class('monitor-window')
        for dark in (False, True):
            with self.subTest(dark=dark):
                provider = self.Gtk.CssProvider()
                errors = []
                provider.connect('parsing-error', lambda *args: errors.append(args))
                colors = dict(DEFAULT_CONFIG['color_scheme'], low_usage_color='#FF8800')
                provider.load_from_data(build_css(colors, dark).encode())
                self.Gtk.StyleContext.add_provider_for_display(window.get_display(), provider, self.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
                if dark:
                    window.add_css_class('monitor-dark')
                self.drain()
                image = self.cairo.ImageSurface(self.cairo.FORMAT_ARGB32, 200, 200)
                chart._draw(chart.area, self.cairo.Context(image), 200, 200)
                self.assertIsNone(chart._render_error)
                self.assertTrue(any(image.get_data()))
                self.assertFalse(errors)
                swatch = next(row[1] for key, row in chart.rows.items() if key[0] == 'partition')
                color = swatch.get_style_context().get_color()
                self.assertGreater(max(color.red,color.green,color.blue)-min(color.red,color.green,color.blue),0.1)
                self.Gtk.StyleContext.remove_provider_for_display(window.get_display(), provider)

    def test_cairo_error_is_atomic_diagnosed_and_recovers_on_next_update(self):
        chart = self.chart
        chart.update(layout_for(), 11)
        area = chart.area
        def broken(context, width, height):
            context.set_source_rgb(1, 0, 0)
            context.paint()
            raise ValueError('synthetic Cairo error')
        image = self.cairo.ImageSurface(self.cairo.FORMAT_ARGB32, 200, 200)
        with mock.patch.object(chart, '_paint', side_effect=broken) as painter:
            chart._draw(area, self.cairo.Context(image), 200, 200)
            self.drain()
            chart._draw(area, self.cairo.Context(image), 200, 200)
            self.assertEqual(painter.call_count, 1)
        self.assertFalse(chart.model.available)
        self.assertFalse(chart.rows)
        self.assertIn('synthetic Cairo error', chart.reason.get_label())
        # Corner must be transparent; the failed red group was discarded.
        self.assertEqual(bytes(image.get_data())[:4], bytes(4))
        chart.update(layout_for(), 12)
        chart._draw(area, self.cairo.Context(image), 200, 200)
        self.assertTrue(chart.model.available)
        self.assertIsNone(chart._render_error)
        self.assertIs(chart.area, area)

    def test_legend_rows_remain_in_physical_order_after_geometry_change(self):
        self.chart.update(layout_for(), 11)
        self.chart.update(layout_for(disk(children=[part(start=10, sectors=90)])), 12)
        labels, child = [], self.chart.entries.get_first_child()
        while child:
            labels.append(child.get_last_child().get_label())
            child = child.get_next_sibling()
        self.assertEqual(labels, [s.text for s in self.chart.model.segments])

    def test_static_chart_does_not_repaint_on_idle_frames_or_normal_updates(self):
        self.chart.update(layout_for(), 11)
        self.show(self.chart)
        self.drain()
        with mock.patch.object(self.chart, '_paint', wraps=self.chart._paint) as paint:
            for now in (12, 13, 14):
                self.chart.update(layout_for(), now)
                self.drain()
        paint.assert_not_called()

    def test_configured_accent_changes_prepared_cairo_colors_without_geometry_change(self):
        from src.gui.app import build_css
        from src.utils.config import DEFAULT_CONFIG
        self.chart.update(layout_for(disk(fstype='ext4', pttype=None)), 11)
        window = self.show(self.chart)
        window.add_css_class('monitor-window')
        window.add_css_class('monitor-dark')
        area = self.chart.area
        for accent, expected in [('#FF8800', (1, 136/255, 0)), ('#3388FF', (51/255, 136/255, 1))]:
            provider = self.Gtk.CssProvider()
            colors = dict(DEFAULT_CONFIG['color_scheme'], low_usage_color=accent)
            provider.load_from_data(build_css(colors, True).encode())
            self.Gtk.StyleContext.add_provider_for_display(window.get_display(), provider, self.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.drain()
            for actual, wanted in zip(self.chart._arcs[0][2][:3], expected):
                self.assertAlmostEqual(actual, wanted, places=5)
            self.assertIs(self.chart.area, area)
            self.Gtk.StyleContext.remove_provider_for_display(window.get_display(), provider)

    def test_hidden_geometry_can_expire_before_page_is_shown(self):
        stack = self.Gtk.Stack()
        stack.add_named(self.Gtk.Label(label='Rendimiento'), 'performance')
        stack.add_named(self.chart, 'storage')
        self.show(stack)
        self.chart.update(layout_for(), 11)
        self.chart.update(layout_for(), 31)
        stack.set_visible_child_name('storage')
        self.drain()
        self.assertFalse(self.chart.model.available)
        self.assertEqual(self.chart._arcs, ())
        self.assertTrue(self.chart.state.get_visible())

    def test_removed_usb_chart_is_released_after_pending_idles(self):
        panel = self.panel()
        main = DiskMetrics('sda', layout=layout_for())
        usb = DiskMetrics('sdb', transport='usb', layout=layout_for(disk('sdb', '8:16', 'usb')))
        panel.update(self.snapshot(main, usb), 11)
        retired = weakref.ref(panel.disk_cards['synthetic:sdb'].donut)
        panel.update(self.snapshot(main, sequence=2), 12)
        self.drain()
        gc.collect()
        self.assertIsNone(retired())

    def scoped_snapshot(self, *parts, direct=None):
        metric, sources = usage_fixture(*parts, direct=direct)
        metric.layout = layout_for()
        metric.model = 'Modelo físico real'
        snap = self.snapshot(metric)
        snap.sources.update(sources)
        return snap

    def test_inner_ring_uses_one_filesystem_scope_and_keeps_physical_capacity(self):
        panel = self.panel()
        snap = self.scoped_snapshot(filesystem())
        panel.update(snap, 11)
        card = panel.disk_cards['synthetic:sda']
        self.show(panel)
        chart = card.donut
        self.assertEqual(chart.usage_model.fraction, .75)
        self.assertEqual(len(chart._usage_arcs), 2)
        self.assertAlmostEqual(chart._usage_arcs[0][1] - chart._usage_arcs[0][0], math.tau * .75)
        self.assertIn('FS sda1', chart.usage_scope.get_label())
        self.assertIn('75,0%', chart.used_value.get_label())
        self.assertEqual(card.capacity.get_label(), '500,0 KiB')
        self.assertIsNone(chart.usage_scope.get_ancestor(self.Gtk.Expander))

    def test_usage_change_reuses_area_without_repreparing_physical_geometry(self):
        panel = self.panel()
        snap = self.scoped_snapshot(filesystem())
        panel.update(snap, 11)
        self.show(panel)
        chart = panel.disk_cards['synthetic:sda'].donut
        physical, area = chart.model, chart.area
        snap.disks[0].partitions[0].used_bytes = 20
        snap.disks[0].partitions[0].free_bytes = 80
        snap.disks[0].partitions[0].usage_percent = 20
        with mock.patch('src.gui.storage_chart.prepare_chart') as prepare, mock.patch.object(area, 'queue_draw') as redraw:
            panel.update(snap, 12)
        prepare.assert_not_called()
        redraw.assert_called_once()
        self.assertIs(chart.area, area)
        self.assertIs(chart.model, physical)
        self.assertEqual(chart.usage_model.fraction, .2)

    def test_usage_age_only_updates_text_without_requesting_or_performing_repaint(self):
        panel = self.panel()
        snap = self.scoped_snapshot(filesystem())
        panel.update(snap, 11)
        self.show(panel)
        chart = panel.disk_cards['synthetic:sda'].donut
        self.drain()
        with mock.patch.object(chart.area, 'queue_draw') as redraw, mock.patch.object(chart, '_paint', wraps=chart._paint) as paint:
            panel.update(snap, 12)
            self.drain()
            panel.update(snap, 13)
            self.drain()
        redraw.assert_not_called()
        paint.assert_not_called()
        self.assertIn('hace 3 s', chart.usage_status.get_label())

    def test_multiple_filesystems_have_neutral_inner_ring_and_individual_detail_bars(self):
        panel = self.panel()
        snap = self.scoped_snapshot(filesystem(), filesystem('sda2', used_bytes=20, free_bytes=80, usage_percent=20))
        panel.update(snap, 11)
        card = panel.disk_cards['synthetic:sda']
        self.assertEqual(card.donut.usage_model.state, 'multiple')
        self.assertEqual(card.donut._usage_arcs, ())
        self.assertIn('Varios filesystems', card.donut.usage_summary.get_label())
        card.expander.set_expanded(True)
        self.assertEqual([row.bar.get_fraction() for row in card.partition_rows.values()], [.75, .2])
        self.assertIn('Sin porcentaje agregado', card.donut.usage_status.get_label())

    def test_shared_btrfs_and_unmounted_ntfs_never_inflate_usage(self):
        panel = self.panel()
        snap = self.scoped_snapshot(filesystem(fstype='btrfs', mountpoints=['/']),
                                    filesystem(fstype='btrfs', mountpoints=['/home']))
        panel.update(snap, 11)
        chart = panel.disk_cards['synthetic:sda'].donut
        self.assertEqual(chart.usage_model.fraction, .75)
        self.assertIn('2 montajes compartidos', chart.usage_scope.get_label())
        snap.disks[0].partitions = [filesystem(fstype='ntfs', mountpoints=[])]
        panel.update(snap, 12)
        self.assertEqual(chart.usage_model.state, 'unavailable')
        self.assertEqual(chart._usage_arcs, ())
        self.assertIn('Sin montar', chart.usage_status.get_label())

    def test_stale_error_and_recovery_are_independent_from_outer_ring(self):
        panel = self.panel()
        snap = self.scoped_snapshot(filesystem())
        snap.disks[0].layout = replace(layout_for(), source=SourceState(10, 11, 200))
        panel.update(snap, 11)
        chart = panel.disk_cards['synthetic:sda'].donut
        area = chart.area
        panel.update(snap, 31)
        self.assertTrue(chart.model.available)
        self.assertEqual(chart.usage_model.state, 'stale')
        self.assertEqual(chart._usage_arcs, ())
        snap.sources['usage:sda1'] = SourceState(31, 32, 20, 'Montaje sustituido')
        panel.update(snap, 32)
        self.assertEqual(chart.usage_model.state, 'error')
        self.assertIn('Montaje sustituido', chart.area.get_tooltip_text())
        snap.sources['usage:sda1'] = SourceState(32, 33, 20)
        panel.update(snap, 33)
        self.assertEqual(chart.usage_model.fraction, .75)
        self.assertIs(chart.area, area)

    def test_mount_change_and_usb_disconnect_preserve_other_chart_and_expansion(self):
        panel = self.panel()
        snap = self.scoped_snapshot(filesystem())
        panel.update(snap, 11)
        card = panel.disk_cards['synthetic:sda']
        area = card.donut.area
        card.expander.set_expanded(True)
        usb = DiskMetrics('sdb', transport='usb', layout=layout_for(disk('sdb', '8:16', 'usb')))
        connected = self.snapshot(snap.disks[0], usb, sequence=2)
        connected.sources.update(snap.sources)
        panel.update(connected, 12)
        snap.sequence = 3
        snap.disks[0].partitions[0].mountpoints = ['/nuevo']
        panel.update(snap, 13)
        self.assertIn('/nuevo', card.donut.usage_scope.get_label())
        snap.disks[0].partitions[0].mountpoints = []
        panel.update(snap, 14)
        self.assertEqual(card.donut._usage_arcs, ())
        self.assertTrue(card.expander.get_expanded())
        self.assertIs(card.donut.area, area)

    def test_both_rings_recover_after_atomic_render_failure(self):
        layout = layout_for()
        metric, sources = usage_fixture(filesystem())
        usage = prepare_usage(metric, sources, {}, 11)
        self.chart.update(layout, 11, usage)
        area = self.chart.area
        surface = self.cairo.ImageSurface(self.cairo.FORMAT_ARGB32, 164, 164)
        with mock.patch.object(self.chart, '_paint', side_effect=ValueError('double ring failure')):
            self.chart._draw(area, self.cairo.Context(surface), 164, 164)
            self.drain()
        self.assertEqual(self.chart._arcs, ())
        self.assertEqual(self.chart._usage_arcs, ())
        self.assertIsNone(self.chart.usage_model.fraction)
        self.chart.update(layout, 12, prepare_usage(metric, sources, {}, 12))
        self.chart._draw(area, self.cairo.Context(surface), 164, 164)
        self.assertTrue(self.chart.model.available)
        self.assertEqual(self.chart.usage_model.fraction, .75)
        self.assertIs(self.chart.area, area)

    def test_compact_legend_keeps_every_region_in_details_and_accessible_text(self):
        panel = self.panel()
        panel.update(self.scoped_snapshot(filesystem()), 11)
        card = panel.disk_cards['synthetic:sda']
        chart = card.donut
        self.assertLessEqual(len(chart.summary_rows), 3)
        self.assertEqual(len(chart.rows), len(chart.model.segments))
        self.assertIs(chart.full_legend.get_parent(), card.partitions_box)
        self.assertTrue(chart.more.get_visible())
        for segment in chart.model.segments:
            self.assertIn(segment.text, chart.area.get_tooltip_text())
        self.assertIn('Interior', chart.area.get_tooltip_text())

    def test_collapsed_details_do_not_change_ring_palette(self):
        from src.gui.app import build_css
        from src.utils.config import DEFAULT_CONFIG
        panel = self.panel()
        panel.update(self.scoped_snapshot(filesystem()), 11)
        window = self.show(panel)
        window.add_css_class('monitor-window')
        window.add_css_class('monitor-dark')
        provider = self.Gtk.CssProvider()
        provider.load_from_data(build_css(DEFAULT_CONFIG['color_scheme'], True).encode())
        self.Gtk.StyleContext.add_provider_for_display(window.get_display(), provider, self.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        try:
            self.drain()
            card = panel.disk_cards['synthetic:sda']
            before = card.donut._arcs
            self.assertTrue(any(max(rgba[:3]) - min(rgba[:3]) > .1 for _, _, rgba in before))
            card.expander.set_expanded(True)
            self.drain()
            self.assertEqual(card.donut._arcs, before)
            card.expander.set_expanded(False)
            self.drain()
            self.assertEqual(card.donut._arcs, before)
        finally:
            self.Gtk.StyleContext.remove_provider_for_display(window.get_display(), provider)

    def test_hidden_page_defers_usage_redraw_and_shows_latest_fraction(self):
        stack = self.Gtk.Stack()
        stack.add_named(self.Gtk.Label(label='Rendimiento'), 'performance')
        stack.add_named(self.chart, 'storage')
        self.show(stack)
        metric, sources = usage_fixture(filesystem())
        with mock.patch.object(self.chart.area, 'queue_draw') as redraw:
            self.chart.update(layout_for(), 11, prepare_usage(metric, sources, {}, 11))
            metric.partitions[0] = filesystem(used_bytes=20, free_bytes=80, usage_percent=20)
            self.chart.update(layout_for(), 12, prepare_usage(metric, sources, {}, 12))
            redraw.assert_not_called()
            stack.set_visible_child_name('storage')
            self.drain()
        redraw.assert_called_once()
        self.assertEqual(self.chart.usage_model.fraction, .2)

    def test_root_priority_shows_emphasized_percentage_and_survives_usb_hotplug(self):
        panel = self.panel()
        snap = self.scoped_snapshot(filesystem('sda1', mountpoints=['/boot'], used_bytes=20, free_bytes=80, usage_percent=20),
                                    filesystem('sda2', fstype='btrfs', mountpoints=['/', '/home']))
        panel.update(snap, 11)
        card = panel.disk_cards['synthetic:sda']
        area, physical = card.donut.area, card.donut.model
        card.expander.set_expanded(True)
        self.show(panel, width=380)
        self.assertIn('Interior · FS /', card.donut.usage_scope.get_label())
        self.assertIn('75,0%', card.donut.used_value.get_label())
        self.assertTrue(card.donut.used_value.has_css_class('storage-root-usage'))
        self.assertEqual(card.donut.usage_model.fraction, .75)
        usb = DiskMetrics('sdb', transport='usb', layout=layout_for(disk('sdb', '8:16', 'usb')))
        connected = self.snapshot(snap.disks[0], usb, sequence=2)
        connected.sources.update(snap.sources)
        panel.update(connected, 12)
        snap.sequence = 3
        panel.update(snap, 13)
        self.assertIs(card.donut.area, area)
        self.assertIs(card.donut.model, physical)
        self.assertTrue(card.expander.get_expanded())
        self.assertEqual(card.donut.usage_model.fraction, .75)

    def test_root_mount_change_updates_scope_without_replacing_area(self):
        panel = self.panel()
        snap = self.scoped_snapshot(filesystem(mountpoints=['/']), filesystem('sda2', mountpoints=['/datos']))
        panel.update(snap, 11)
        chart = panel.disk_cards['synthetic:sda'].donut
        area = chart.area
        snap.disks[0].partitions[0].mountpoints = ['/otro']
        panel.update(snap, 12)
        self.assertEqual(chart.usage_model.state, 'multiple')
        self.assertEqual(chart._usage_arcs, ())
        self.assertFalse(chart.used_value.has_css_class('storage-root-usage'))
        snap.disks[0].partitions[1].mountpoints = ['/']
        panel.update(snap, 13)
        self.assertIn('FS / · sda2', chart.usage_scope.get_label())
        self.assertEqual(chart.usage_model.fraction, .75)
        self.assertIs(chart.area, area)

    def test_stale_root_remains_neutral_with_fresh_boot_and_recovers_after_cairo_error(self):
        panel = self.panel()
        snap = self.scoped_snapshot(filesystem('sda1', mountpoints=['/boot']),
                                    filesystem('sda2', fstype='btrfs', mountpoints=['/', '/home']))
        snap.sources['usage:sda2'] = SourceState(0, 0, 5)
        panel.update(snap, 11)
        chart = panel.disk_cards['synthetic:sda'].donut
        area = chart.area
        self.assertEqual(chart.usage_model.state, 'stale')
        self.assertEqual(chart._usage_arcs, ())
        self.assertIn('FS /', chart.usage_scope.get_label())
        snap.sources['usage:sda2'] = SourceState(10, 11, 20)
        panel.update(snap, 12)
        self.assertEqual(chart.usage_model.fraction, .75)
        image = self.cairo.ImageSurface(self.cairo.FORMAT_ARGB32, 164, 164)
        with mock.patch.object(chart, '_paint', side_effect=ValueError('root render error')):
            chart._draw(area, self.cairo.Context(image), 164, 164)
            self.drain()
        self.assertEqual(chart._usage_arcs, ())
        panel.update(snap, 13)
        self.assertEqual(chart.usage_model.fraction, .75)
        self.assertTrue(chart.used_value.has_css_class('storage-root-usage'))
        self.assertIs(chart.area, area)

    def install_ring_css(self, window, *, dark=True, colors=None):
        from src.gui.app import build_css
        from src.utils.config import DEFAULT_CONFIG
        window.add_css_class('monitor-window')
        if dark:
            window.add_css_class('monitor-dark')
        else:
            window.remove_css_class('monitor-dark')
        provider = self.Gtk.CssProvider()
        provider.load_from_data(build_css(DEFAULT_CONFIG['color_scheme'], dark,
                                storage_colors=colors or DEFAULT_CONFIG['storage_usage']['color_scheme']).encode())
        display = window.get_display()
        self.Gtk.StyleContext.add_provider_for_display(display, provider, self.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.addCleanup(self.Gtk.StyleContext.remove_provider_for_display, display, provider)
        self.drain()
        return provider

    def assert_rgb(self, actual, hexadecimal):
        expected = tuple(int(hexadecimal[i:i+2], 16) / 255 for i in (1, 3, 5))
        for value, wanted in zip(actual[:3], expected):
            self.assertAlmostEqual(value, wanted, places=5)

    def test_inner_colors_at_exact_boundaries_preserve_outer_palette_and_area(self):
        from src.gui.storage_usage_model import UsageModel
        self.chart.update(layout_for(), 11)
        window = self.show(self.chart)
        self.install_ring_css(window)
        outer, area = self.chart._arcs, self.chart.area
        for percent, css, text in ((0, '#76B900', 'Normal'), (69.9, '#76B900', 'Normal'),
                                   (70, '#E5A93D', 'Atención'), (78, '#E5A93D', 'Atención'),
                                   (84.9, '#E5A93D', 'Atención'), (85, '#E65B57', 'Crítico'),
                                   (100, '#E65B57', 'Crítico')):
            with self.subTest(percent=percent):
                self.chart.update(layout_for(), 11, UsageModel(state='current', fraction=percent / 100))
                self.drain()
                self.assert_rgb(self.chart._usage_arcs[0][2], css)
                self.assertIn(text, self.chart.usage_status.get_label())
                self.assertIn(text, self.chart.area.get_tooltip_text())
                self.assertEqual(self.chart._arcs, outer)
                self.assertIs(self.chart.area, area)

    def test_same_level_percentage_and_threshold_crossing_draw_only_visual_changes(self):
        from src.gui.storage_usage_model import UsageModel
        self.chart.update(layout_for(), 11, UsageModel(state='current', fraction=.72))
        window = self.show(self.chart)
        self.install_ring_css(window)
        physical = self.chart.model
        previous = self.chart._usage_arcs
        for percent, level in ((80, 'medium'), (85, 'high'), (60, 'low')):
            with mock.patch.object(self.chart.area, 'queue_draw') as redraw, mock.patch('src.gui.storage_chart.prepare_chart') as prepare:
                self.chart.update(layout_for(), 11, UsageModel(state='current', fraction=percent / 100))
            redraw.assert_called_once()
            prepare.assert_not_called()
            self.assertNotEqual(self.chart._usage_arcs, previous)
            self.assertEqual(self.chart.usage_style.level, level)
            self.assertIs(self.chart.model, physical)
            previous = self.chart._usage_arcs
            self.drain()
        with mock.patch.object(self.chart.area, 'queue_draw') as redraw:
            self.chart.update(layout_for(), 12, UsageModel(state='current', fraction=.60, reason='hace 2 s'))
            self.drain()
        redraw.assert_not_called()

    def test_unavailable_stale_and_errors_render_gray_with_unknown_text(self):
        from src.gui.storage_usage_model import UsageModel
        window = self.show(self.chart)
        self.install_ring_css(window)
        for state in ('unavailable', 'stale', 'error', 'ambiguous', 'multiple'):
            with self.subTest(state=state):
                self.chart.update(layout_for(), 11, UsageModel(state='current', fraction=.95))
                self.chart.update(layout_for(), 11, UsageModel(state=state))
                self.drain()
                self.assertEqual(self.chart._usage_arcs, ())
                self.assertIn('Desconocido', self.chart.usage_status.get_label())
                self.assertFalse(self.chart.usage_values.get_visible())
                self.assert_rgb(self.chart._inner_neutral, '#8A969E')
                image = self.cairo.ImageSurface(self.cairo.FORMAT_ARGB32, 164, 164)
                self.chart._draw(self.chart.area, self.cairo.Context(image), 164, 164)
                offset = 82 * image.get_stride() + 135 * 4
                self.assertEqual(bytes(image.get_data())[offset:offset+3], bytes((0x9E, 0x96, 0x8A)))

    def test_custom_inner_palette_and_both_themes_leave_outer_colors_unchanged(self):
        from src.gui.storage_usage_model import UsageModel
        window = self.show(self.chart)
        self.install_ring_css(window)
        self.chart.update(layout_for(), 11, UsageModel(state='current', fraction=.2))
        self.drain()
        outer = self.chart._arcs
        for dark in (False, True):
            colors = {'low_usage_color': '#114477', 'medium_usage_color': '#885522', 'high_usage_color': '#AA3377'}
            self.install_ring_css(window, dark=dark, colors=colors)
            for fraction, color in ((.2, '#114477'), (.75, '#885522'), (.95, '#AA3377')):
                self.chart.update(layout_for(), 11, UsageModel(state='current', fraction=fraction))
                self.drain()
                self.assert_rgb(self.chart._usage_arcs[0][2], color)
                self.assertEqual(self.chart._arcs, outer)

    def test_critical_usage_survives_hotplug_and_recovers_after_render_error(self):
        panel = self.panel()
        snap = self.scoped_snapshot(filesystem(mountpoints=['/'], used_bytes=95, free_bytes=5, usage_percent=95))
        panel.update(snap, 11)
        window = self.show(panel)
        self.install_ring_css(window)
        card = panel.disk_cards['synthetic:sda']
        area = card.donut.area
        card.expander.set_expanded(True)
        usb = DiskMetrics('sdb', layout=layout_for(disk('sdb', '8:16', 'usb')))
        connected = self.snapshot(snap.disks[0], usb, sequence=2)
        connected.sources.update(snap.sources)
        panel.update(connected, 12)
        snap.sequence = 3
        panel.update(snap, 13)
        image = self.cairo.ImageSurface(self.cairo.FORMAT_ARGB32, 164, 164)
        with mock.patch.object(card.donut, '_paint', side_effect=ValueError('color render failure')):
            card.donut._draw(area, self.cairo.Context(image), 164, 164)
            self.drain()
        self.assertEqual(card.donut.usage_style.label, 'Desconocido')
        self.assertEqual(card.donut._usage_arcs, ())
        panel.update(snap, 14)
        self.drain()
        self.assertEqual(card.donut.usage_style.label, 'Crítico')
        self.assert_rgb(card.donut._usage_arcs[0][2], '#E65B57')
        self.assertTrue(card.expander.get_expanded())
        self.assertIs(card.donut.area, area)

    def test_cairo_uses_prepared_colors_without_classifying_or_reading_css(self):
        from src.gui.storage_usage_model import UsageModel
        self.chart.update(layout_for(), 11, UsageModel(state='current', fraction=.75))
        image = self.cairo.ImageSurface(self.cairo.FORMAT_ARGB32, 164, 164)
        with mock.patch('src.gui.storage_chart.usage_style', side_effect=AssertionError('classification inside Cairo')), \
             mock.patch.object(self.chart.used_swatch, 'get_style_context', side_effect=AssertionError('CSS inside Cairo')):
            self.chart._draw(self.chart.area, self.cairo.Context(image), 164, 164)
        self.assertIsNone(self.chart._render_error)
