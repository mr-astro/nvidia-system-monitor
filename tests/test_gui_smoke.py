"""Builds the real GTK widgets. Skipped when GTK 4 or a display is unavailable."""
import tempfile
import unittest
import itertools
from unittest import mock
from pathlib import Path

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, GLib, Gtk

    HAVE_GTK = bool(Gtk.init_check()) and Gdk.Display.get_default() is not None
except (ImportError, ValueError):
    HAVE_GTK = False

from src.data.collector import Snapshot
from src.data.cpu import CpuMetrics
from src.data.nvidia import GpuMetrics
from src.data.ram import RamMetrics
from src.data.sensors import SensorReading
from src.data.storage import DiskMetrics, PartitionMetrics
from src.data.errors import SourceState, freshness
from src.data.storage_presentation import StoragePresentation
from src.utils.disk_aliases import DiskIdentity


def full_snapshot(n_gpus=1):
    return Snapshot(
        sequence=1, timestamp=0.0, gpu_available=True, cuda_version="12.6",
        cpu=CpuMetrics(model="AMD Ryzen", usage_percent=42.0, cores=8, threads=16,
                       frequency_mhz=3800.0, max_frequency_mhz=4700.0, temperature_c=55.0,
                       load_1m=0.5, load_5m=0.4, load_15m=0.3),
        gpus=[GpuMetrics(index=i, name=f"RTX {i}", utilization_percent=90.0,
                         memory_total_mb=24576, memory_used_mb=20000, temperature_c=70.0,
                         power_draw_w=300.0, power_limit_w=350.0, driver_version="560")
              for i in range(n_gpus)],
        ram=RamMetrics(total_mb=16000, used_mb=8000, available_mb=8000, free_mb=2000,
                       cached_mb=6000, usage_percent=50.0, swap_total_mb=4096,
                       swap_used_mb=100, swap_usage_percent=2.4),
        storage_presentation=StoragePresentation(identities=(("nvme0n1", DiskIdentity("synthetic:nvme0n1")),)),
        disks=[DiskMetrics(name="nvme0n1", kind="NVMe", size_bytes=10 ** 12, temperature_c=38.0,
                           partitions=[PartitionMetrics(
                               name="nvme0n1p2", size_bytes=10 ** 12, fstype="btrfs",
                               mountpoints=["/", "/home"], used_bytes=6 * 10 ** 11,
                               free_bytes=3 * 10 ** 11, usage_percent=66.7)])],
        sensors=[SensorReading("k10temp-pci-00c3", "Tctl", 45.25)],
    )


@unittest.skipUnless(HAVE_GTK, "GTK 4 o pantalla no disponibles")
class GuiSmoke(unittest.TestCase):
    _application_ids = itertools.count()

    def _app(self, *, channel="stable", **overrides):
        from src.gui.app import SystemMonitorApp
        from src.utils.config import Config

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.json"
        import json
        path.write_text(json.dumps({"logging_enabled": False, **overrides}))
        app = SystemMonitorApp(Config(path, channel=channel), channel=channel)
        from src.data.storage_presentation import StorageAliasWorker
        app.disk_aliases = StorageAliasWorker(Path(tmp.name) / "aliases.json", Path(tmp.name) / "by-id", channel=channel)
        app.collector.storage_aliases = app.disk_aliases if 'storage' in app.enabled else None
        self.addCleanup(app.disk_aliases.stop)
        # Each fixture exports its own D-Bus object, including when the host
        # already has the real monitor open. Keep the production ID unchanged.
        app.set_application_id(f"cl.mastro.NvidiaSystemMonitor.Test{next(self._application_ids)}")
        app.register(None)  # emits "startup", as in a real launch
        app._install_css()
        app._build_window()
        self.addCleanup(app.window.destroy)
        return app

    def test_render_all_panels_twice_updates_in_place(self):
        app = self._app()
        app._render(full_snapshot())
        rows_after_first = {n: dict(c._rows) for n, c in app.cards.items()}
        app._render(full_snapshot())
        for name, card in app.cards.items():
            # same widgets reused: nothing was destroyed and rebuilt
            self.assertEqual(set(card._rows), set(rows_after_first[name]))
            for key, widgets in card._rows.items():
                self.assertIs(widgets[1], rows_after_first[name][key][1])
        self.assertEqual(app._last_render_error, {})

    def test_values_are_rendered(self):
        app = self._app()
        app._render(full_snapshot())
        self.assertEqual(app.cards["cpu"]._rows["usage"][1].get_label(), "42.0%")
        self.assertEqual(app.cards["cpu"]._rows["temp"][1].get_label(), "55.0 °C")
        self.assertIn("19.5 GiB", app.cards["gpu"]._rows["gpu0:vram"][1].get_label())

    def test_featured_metrics_keep_technical_values_accessible(self):
        app = self._app()
        app._render(full_snapshot(n_gpus=2))
        for name, key in (('cpu', 'usage'), ('ram', 'used'), ('gpu', 'gpu0:usage'), ('gpu', 'gpu1:usage')):
            value = app.cards[name]._rows[key][1]
            self.assertTrue(value.has_css_class('metric-hero-value'))
            self.assertIsNone(value.get_ancestor(Gtk.Expander))
        self.assertEqual(app.cards['ram']._rows['used'][1].get_label(), '50.0%')
        self.assertIn('7.8 GiB usados de 15.6 GiB', app.cards['ram']._rows['allocation'][1].get_label())
        self.assertEqual(app.cards['gpu']._rows['gpu1:power'][1].get_label(), '300.0 W')
        for name, keys in (('cpu', ('maxfreq', 'cores', 'load')),
                           ('ram', ('free', 'cache')),
                           ('gpu', ('gpu0:limit', 'gpu1:core', 'gpu1:mem', 'gpu1:fan', 'driver', 'cuda'))):
            for key in keys:
                value = app.cards[name]._rows[key][1]
                details = app.cards[name]._sections['details']
                details.set_expanded(True)
                self.assertIs(value.get_ancestor(Gtk.Expander), details)
                self.assertTrue(value.get_visible())

    def test_featured_metrics_and_technical_expansion_survive_updates_and_navigation(self):
        app = self._app()
        snap = full_snapshot()
        app._render(snap)
        cpu = app.cards['cpu']
        usage = cpu._rows['usage']
        temperature = cpu._rows['temp']
        details = cpu._sections['details']
        details.set_expanded(True)
        for cycle in range(10):
            app.stack.set_visible_child_name('storage' if cycle % 2 else 'performance')
            snap.sequence += 1
            snap.cpu.usage_percent = float(cycle)
            snap.cpu.frequency_mhz = 3000.0 + cycle
            app._render(snap)
            self.assertIs(cpu._rows['usage'], usage)
            self.assertIs(cpu._rows['temp'], temperature)
            self.assertIs(cpu._rows['cores'][1].get_ancestor(Gtk.Expander), details)
            self.assertTrue(details.get_expanded())
            self.assertEqual(usage[1].get_label(), f'{cycle:.1f}%')
            self.assertEqual(cpu._rows['freq'][1].get_label(), f'{3000 + cycle} MHz')

    def test_featured_sections_recover_after_partial_render_error(self):
        app = self._app()
        snap = full_snapshot(n_gpus=2)
        app._render(snap)
        gpu = app.cards['gpu']
        original = app._render_gpu
        def broken(card, snapshot):
            original(card, snapshot)
            raise ValueError('synthetic featured render failure')
        snap.sequence += 1
        with mock.patch.object(app, '_render_gpu', side_effect=broken):
            app._render(snap)
        self.assertEqual(set(gpu._rows), {'state'})
        self.assertEqual(gpu._sections, {})
        snap.sequence += 1
        app._render(snap)
        self.assertEqual(app._last_render_error, {})
        self.assertEqual(len(gpu._sections), 3)
        power = gpu._rows['gpu1:power']
        details = gpu._sections['details']
        details.set_expanded(True)
        snap.sequence += 1
        snap.gpus[1].power_draw_w = 123.0
        app._render(snap)
        self.assertIs(gpu._rows['gpu1:power'], power)
        self.assertEqual(power[1].get_label(), '123.0 W')
        self.assertTrue(details.get_expanded())
        for section in gpu._sections.values():
            self.assertIs(section.get_ancestor(Gtk.ApplicationWindow), app.window)

    def test_compact_dev_header_keeps_channel_and_status_without_duplicate_title(self):
        app = self._app(channel='dev')
        self.assertEqual(app.window.get_default_size(), (880, 700))
        self.assertEqual(app.window.get_title(), 'NVIDIA System Monitor DEV')
        badge = app.window.get_child().get_first_child().get_first_child()
        self.assertEqual(badge.get_label(), 'DEV')
        self.assertEqual(badge.get_tooltip_text(), app.window.get_title())
        self.assertEqual(app.status.get_label(), 'Recopilando datos…')

    def test_usage_bar_gets_level_class(self):
        app = self._app()
        app._render(full_snapshot())
        bar = app.cards["gpu"]._rows["gpu0:usage"][2]  # 90 % -> high
        self.assertTrue(bar.has_css_class("usage-high"))
        cpu_bar = app.cards["cpu"]._rows["usage"][2]    # 42 % -> low
        self.assertTrue(cpu_bar.has_css_class("usage-low"))

    def test_cuda_is_never_rendered_as_none(self):
        app = self._app()
        snap = full_snapshot()
        snap.cuda_version = None
        app._render(snap)
        self.assertEqual(app.cards["gpu"]._rows["cuda"][1].get_label(), "N/D")
        app._render(full_snapshot())
        self.assertEqual(app.cards["gpu"]._rows["cuda"][1].get_label(), "12.6")

    def test_collector_uses_the_configured_interval(self):
        # Regression: the interval was once dropped when building the Collector.
        self.assertEqual(self._app(refresh_interval_seconds=2.5).collector.interval, 2.5)

    def test_sensors_panel_is_off_by_default_and_opt_in(self):
        self.assertNotIn("sensors", self._app().cards)
        app = self._app(panels={"sensors": True})
        app._render(full_snapshot())
        self.assertIn("sensors", app.cards)
        self.assertEqual(app.cards["sensors"]._rows["s0"][1].get_label(), "45.2 °C")

    def test_config_colors_are_applied_to_the_css(self):
        # Regression: colors from the configuration were once ignored.
        from src.gui.app import build_css
        colors = {"low_usage_color": "#010203", "medium_usage_color": "#040506",
                  "high_usage_color": "#070809"}
        css = build_css(colors, dark=True)
        for value in colors.values():
            self.assertIn(value, css)

    def test_css_has_no_parse_errors_in_either_theme(self):
        from src.gui.app import build_css
        from src.utils.config import DEFAULT_CONFIG
        for dark in (True, False):
            errors = []
            provider = Gtk.CssProvider()
            provider.connect("parsing-error", lambda p, section, err: errors.append(err.message))
            css = build_css(DEFAULT_CONFIG["color_scheme"], dark)
            try:
                provider.load_from_string(css)
            except AttributeError:
                provider.load_from_data(css.encode("utf-8"))
            self.assertEqual(errors, [], f"dark={dark}")

    def test_system_theme_does_not_force_dark_colors(self):
        from src.gui.app import build_css
        from src.utils.config import DEFAULT_CONFIG
        colors = DEFAULT_CONFIG["color_scheme"]
        for color in ("#101410", "#1B231C", "#76B900", "#F2F5F2", "#A5B0A5"):
            self.assertIn(color, build_css(colors, dark=True))
            self.assertNotIn(color, build_css(colors, dark=False))
        self.assertFalse(self._app(theme="system").dark)
        self.assertTrue(self._app().dark)

    def test_performance_columns_and_storage_have_separate_pages(self):
        app = self._app()
        cpu, ram, gpu, storage = (app.cards[n] for n in ("cpu", "ram", "gpu", "storage"))
        self.assertIs(cpu.get_parent(), ram.get_parent())       # left column
        self.assertIsNot(cpu.get_parent(), gpu.get_parent())    # right column
        self.assertIsNot(gpu.get_parent(), storage.get_parent())
        self.assertIsInstance(cpu.get_parent().get_parent().get_parent(), Gtk.FlowBox)
        for card in (cpu, ram, gpu):
            self.assertIs(card.get_ancestor(Gtk.ScrolledWindow), app.pages['performance'])
        self.assertIs(storage.get_ancestor(Gtk.ScrolledWindow), app.pages['storage'])

    def test_both_titled_pages_share_one_switcher_and_independent_scrollers(self):
        app = self._app()
        self.assertIs(app.switcher.get_stack(), app.stack)
        pages = app.stack.get_pages()
        self.assertEqual(pages.get_n_items(), 2)
        for position, (name, title) in enumerate((('performance', 'RENDIMIENTO'), ('storage', 'ALMACENAMIENTO'))):
            page = pages.get_item(position)
            self.assertEqual(page.get_name(), name)
            self.assertEqual(page.get_title(), title)
            self.assertIs(page.get_child(), app.pages[name])
            self.assertIsInstance(page.get_child(), Gtk.ScrolledWindow)
        self.assertIsNot(app.pages['performance'].get_vadjustment(), app.pages['storage'].get_vadjustment())
        self.assertEqual(app.stack.get_visible_child_name(), 'performance')
        self.assertEqual(app.stack.get_transition_type(), Gtk.StackTransitionType.NONE)
        performance_button = app.switcher.get_first_child()
        storage_button = performance_button.get_next_sibling()
        storage_button.set_active(True)
        self.assertEqual(app.stack.get_visible_child_name(), 'storage')
        self.assertFalse(performance_button.get_active())
        performance_button.set_active(True)
        self.assertEqual(app.stack.get_visible_child_name(), 'performance')
        self.assertFalse(storage_button.get_active())

    def test_switching_pages_preserves_cards_rows_and_expanded_disk_without_duplicates(self):
        app = self._app(panels={'sensors': True})
        snap = full_snapshot(n_gpus=2)
        app._render(snap)
        cards = dict(app.cards)
        parents = {name: card.get_parent() for name, card in cards.items()}
        cpu_row = cards['cpu']._rows['usage']
        gpu_row = cards['gpu']._rows['gpu1:vram']
        disk = next(iter(cards['storage'].disk_cards.values()))
        partition = next(iter(disk.partition_rows.values()))
        disk.expander.set_expanded(True)
        for cycle in range(20):
            app.stack.set_visible_child_name('storage' if cycle % 2 else 'performance')
            snap.sequence += 1
            app._render(snap)
            self.assertEqual(app.cards, cards)
            for name, card in cards.items():
                self.assertIs(card.get_parent(), parents[name])
            self.assertIs(cards['cpu']._rows['usage'], cpu_row)
            self.assertIs(cards['gpu']._rows['gpu1:vram'], gpu_row)
            self.assertEqual(list(cards['storage'].disk_cards.values()), [disk])
            self.assertIs(next(iter(disk.partition_rows.values())), partition)
            self.assertTrue(disk.expander.get_expanded())
        descendants = []
        def visit(widget):
            descendants.append(widget)
            child = widget.get_first_child()
            while child is not None:
                visit(child)
                child = child.get_next_sibling()
        visit(app.stack)
        for card in cards.values():
            self.assertEqual(sum(widget is card for widget in descendants), 1)

    def test_hidden_pages_receive_new_metrics_and_keep_them_when_shown(self):
        app = self._app()
        snap = full_snapshot(n_gpus=2)
        app._render(snap)
        app.stack.set_visible_child_name('storage')
        snap.sequence += 1
        snap.cpu.usage_percent = 73.0
        snap.gpus[1].memory_used_mb = 1024
        with mock.patch.object(app.collector, 'latest', return_value=snap):
            app._on_tick()
        self.assertEqual(app.cards['cpu']._rows['usage'][1].get_label(), '73.0%')
        self.assertIn('1.0 GiB', app.cards['gpu']._rows['gpu1:vram'][1].get_label())
        app.stack.set_visible_child_name('performance')
        snap.sequence += 1
        snap.disks[0].model = 'Updated disk'
        with mock.patch.object(app.collector, 'latest', return_value=snap):
            app._on_tick()
        disk = next(iter(app.cards['storage'].disk_cards.values()))
        self.assertEqual(disk.title.get_label(), 'Updated disk')
        app.stack.set_visible_child_name('storage')
        self.assertIs(next(iter(app.cards['storage'].disk_cards.values())), disk)
        self.assertEqual(disk.title.get_label(), 'Updated disk')

    def test_navigation_build_and_tick_do_not_poll_hardware(self):
        from contextlib import ExitStack
        methods = ('src.data.cpu.CpuMonitor.get_metrics', 'src.data.ram.RamMonitor.get_metrics',
                   'src.data.nvidia.NvidiaMonitor.get_metrics', 'src.data.nvidia.NvidiaMonitor.get_cuda_version',
                   'src.data.storage.StorageMonitor.get_metrics', 'src.data.sensors.SensorMonitor.get_readings')
        with ExitStack() as patches:
            queries = [patches.enter_context(mock.patch(method, side_effect=AssertionError('hardware polling on GTK')))
                       for method in methods]
            app = self._app(panels={'sensors': True})
            with mock.patch.object(app.collector, 'latest', return_value=full_snapshot()):
                for name in ('performance', 'storage', 'performance'):
                    app.stack.set_visible_child_name(name)
                    app._on_tick()
            self.assertEqual(app._last_render_error, {})
            for query in queries:
                query.assert_not_called()

    def test_disabled_panels_leave_both_pages_available_without_acquisition(self):
        for enabled in ('cpu', 'gpu', 'ram', 'sensors', 'storage', None):
            with self.subTest(enabled=enabled):
                app = self._app(panels={name: name == enabled for name in ('cpu', 'gpu', 'ram', 'storage', 'sensors')})
                for name in ('cpu', 'gpu', 'ram', 'storage', 'sensors'):
                    self.assertEqual(getattr(app.collector, name) is not None, name == enabled)
                for page in ('performance', 'storage'):
                    app.stack.set_visible_child_name(page)
                    app._render(full_snapshot())
                self.assertEqual(set(app.cards), {enabled} if enabled else set())
                for page, names in (('performance', {'cpu', 'gpu', 'ram', 'sensors'}), ('storage', {'storage'})):
                    if enabled not in names:
                        viewport = app.pages[page].get_child()
                        content = viewport.get_child() if isinstance(viewport, Gtk.Viewport) else viewport
                        self.assertIsInstance(content.get_first_child(), Gtk.Label)

    def test_hidden_storage_reconnection_preserves_physical_expansion_without_inheriting_replacement(self):
        app = self._app()
        snap = full_snapshot()
        name = snap.disks[0].name
        disk = snap.disks[0]
        def present(key, physical):
            snap.sequence += 1
            snap.disks = [disk]
            snap.storage_presentation = StoragePresentation(identities=(
                (name, DiskIdentity(key, expansion_key=physical)),))
            app._render(snap)
            return next(iter(app.cards['storage'].disk_cards.values()))
        first = present('presence:1', 'physical-A')
        first.expander.set_expanded(True)
        snap.sequence += 1
        snap.disks = []
        app._render(snap)
        self.assertEqual(app.cards['storage'].disk_cards, {})
        reconnected = present('presence:2', 'physical-A')
        self.assertIsNot(reconnected, first)
        self.assertTrue(reconnected.expander.get_expanded())
        replacement = present('presence:3', 'physical-B')
        self.assertFalse(replacement.expander.get_expanded())
        app.stack.set_visible_child_name('storage')
        self.assertEqual(list(app.cards['storage'].disk_cards.values()), [replacement])

    def _resize(self, app, width, height=720):
        app.window.set_default_size(width, height)
        app.window.present()
        loop = GLib.MainLoop()
        deadline = GLib.get_monotonic_time() + 2_000_000
        settled = 0

        def check_allocation():
            nonlocal settled
            size = (app.window.get_width(), app.window.get_height())
            settled = settled + 1 if size == (width, height) else 0
            if settled >= 2 or GLib.get_monotonic_time() >= deadline:
                loop.quit()
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add(20, check_allocation)
        loop.run()
        self.assertEqual(app.window.get_width(), width)
        self.assertEqual(app.window.get_height(), height)

    def _bounds(self, widget, relative):
        success, bounds = widget.compute_bounds(relative)
        self.assertTrue(success)
        return bounds

    def test_page_switching_preserves_independent_scroll_positions(self):
        app = self._app()
        snap = full_snapshot(n_gpus=5)
        snap.disks[0].partitions = [PartitionMetrics(f'nvme0n1p{i}', fstype='ext4') for i in range(30)]
        app._render(snap)
        next(iter(app.cards['storage'].disk_cards.values())).expander.set_expanded(True)
        self._resize(app, 380)
        performance = app.pages['performance'].get_vadjustment()
        self.assertGreater(performance.get_upper() - performance.get_page_size(), 100)
        performance.set_value(100)
        app.stack.set_visible_child_name('storage')
        self._resize(app, 380)
        storage = app.pages['storage'].get_vadjustment()
        self.assertGreater(storage.get_upper() - storage.get_page_size(), 200)
        storage.set_value(200)
        app.stack.set_visible_child_name('performance')
        self._resize(app, 380)
        self.assertAlmostEqual(performance.get_value(), 100, delta=1)
        app.stack.set_visible_child_name('storage')
        self._resize(app, 380)
        self.assertAlmostEqual(storage.get_value(), 200, delta=1)

    def test_responsive_layout_reflows_without_recreating_cards_or_expanders(self):
        app = self._app()
        snap = full_snapshot()
        snap.sources['usage:nvme0n1p2'] = SourceState(0, 0, 20)
        snap.disks.extend([DiskMetrics('sda', model='KINGSTON SA400S37480G'), DiskMetrics('sdb')])
        with mock.patch('src.gui.app.time.monotonic', return_value=1):
            app._render(snap)
        storage = app.cards['storage']
        disk = next(iter(storage.disk_cards.values()))
        self.assertTrue(all(not c.expander.get_expanded() for c in storage.disk_cards.values()))
        cards = dict(app.cards)
        row = next(iter(disk.partition_rows.values()))
        bar = disk.usage_bar
        left, right = app.cards['cpu'].get_parent(), app.cards['gpu'].get_parent()
        columns = left.get_parent().get_parent()
        scroller = columns.get_ancestor(Gtk.ScrolledWindow)

        self._resize(app, 1000)
        wide_left, wide_right = self._bounds(left, columns), self._bounds(right, columns)
        self.assertAlmostEqual(wide_left.get_y(), wide_right.get_y(), delta=1)
        self.assertGreater(wide_right.get_x(), wide_left.get_x())

        disk.expander.set_expanded(True)
        for width, height in ((480, 720), (380, 320), (1000, 720)):
            self._resize(app, width, height)
            left_bounds, right_bounds = self._bounds(left, columns), self._bounds(right, columns)
            if width < 700:
                self.assertAlmostEqual(left_bounds.get_x(), right_bounds.get_x(), delta=1)
                self.assertGreaterEqual(right_bounds.get_y(), left_bounds.get_y() + left_bounds.get_height())
                self.assertLessEqual(right_bounds.get_y() - left_bounds.get_height(), 13)
                vertical = scroller.get_vadjustment()
                self.assertGreater(vertical.get_upper(), vertical.get_page_size())
            else:
                self.assertAlmostEqual(left_bounds.get_y(), right_bounds.get_y(), delta=1)
                self.assertGreater(right_bounds.get_x(), left_bounds.get_x())
            horizontal = scroller.get_hadjustment()
            self.assertLessEqual(horizontal.get_upper(), horizontal.get_page_size() + 1)
            snap.sequence += 1
            with mock.patch('src.gui.app.time.monotonic', return_value=1):
                app._render(snap)
            self.assertEqual(app.cards, cards)
            self.assertIs(next(iter(storage.disk_cards.values())), disk)
            self.assertIs(next(iter(disk.partition_rows.values())), row)
            self.assertIs(disk.usage_bar, bar)
            self.assertTrue(disk.expander.get_expanded())
            self.assertTrue(bar.get_visible())
            self.assertIs(left, app.cards['cpu'].get_parent())
            self.assertIs(right, app.cards['gpu'].get_parent())
            app.stack.set_visible_child_name('storage')
            self._resize(app, width, height)
            storage_scroller = app.pages['storage']
            self.assertLessEqual(storage.measure(Gtk.Orientation.HORIZONTAL, -1)[0], storage.get_width())
            adjustment = storage_scroller.get_hadjustment()
            self.assertLessEqual(adjustment.get_upper(), adjustment.get_page_size() + 1)
            self.assertIs(next(iter(storage.disk_cards.values())), disk)
            self.assertTrue(disk.expander.get_expanded())
            app.stack.set_visible_child_name('performance')
            self._resize(app, width, height)

    def test_long_names_diagnostics_and_temperature_ages_fit_a_narrow_window(self):
        app = self._app(panels={'sensors': True})
        snap = full_snapshot()
        snap.cpu.model = 'CPU_' + 'x' * 300
        snap.gpus[0].name = 'GPU_' + 'x' * 300
        snap.sensors = [SensorReading('sensor_' + 'x' * 300, 'label_' + 'x' * 300, 42)]
        snap.disks[0].model = 'DISK_' + 'x' * 300
        snap.disks[0].temperature_source = 'udisks:nvme0n1'
        snap.sources['udisks:nvme0n1'] = SourceState(0, 0, 1000)
        snap.errors = {'smart:nvme0n1': 'Permission denied ' + 'x' * 300}
        with mock.patch('src.gui.app.time.monotonic', return_value=296):
            app._render(snap)
            app._update_status(snap)
        for page in ('performance', 'storage'):
            app.stack.set_visible_child_name(page)
            self._resize(app, 380)
            for card in app.cards.values():
                if card.get_ancestor(Gtk.ScrolledWindow) is app.pages[page]:
                    self.assertLessEqual(card.measure(Gtk.Orientation.HORIZONTAL, -1)[0], card.get_width())
        disk = next(iter(app.cards['storage'].disk_cards.values()))
        self.assertIn('hace 296 s', disk.temperature.get_label())
        self.assertIn('Permission denied', app.status.get_tooltip_text())
        self.assertNotIn('Permission denied', app.status.get_label())

    def test_expanded_details_and_horizontal_disk_summary_fit_narrow_windows_in_both_themes(self):
        for theme in ('dark', 'system'):
            with self.subTest(theme=theme):
                app = self._app(theme=theme)
                self.assertEqual(app.window.has_css_class('monitor-dark'), theme == 'dark')
                snap = full_snapshot(n_gpus=2)
                snap.cpu.model = 'CPU_' + 'x' * 120
                snap.gpus[1].name = 'GPU_' + 'x' * 120
                app._render(snap)
                for name in ('cpu', 'ram', 'gpu'):
                    app.cards[name]._sections['details'].set_expanded(True)
                disk = next(iter(app.cards['storage'].disk_cards.values()))
                disk.expander.set_expanded(True)
                for page in ('performance', 'storage'):
                    app.stack.set_visible_child_name(page)
                    self._resize(app, 380)
                    scroller = app.pages[page]
                    horizontal = scroller.get_hadjustment()
                    self.assertLessEqual(horizontal.get_upper(), horizontal.get_page_size() + 1)
                    for card in app.cards.values():
                        if card.get_ancestor(Gtk.ScrolledWindow) is scroller:
                            self.assertLessEqual(card.measure(Gtk.Orientation.HORIZONTAL, -1)[0], card.get_width())
                left = self._bounds(disk.capacity_box, disk.overview)
                right = self._bounds(disk.summary, disk.overview)
                self.assertGreaterEqual(right.get_x(), left.get_x() + left.get_width())
                self.assertLessEqual(right.get_x() + right.get_width(), disk.overview.get_width() + 1)

    def test_one_enabled_column_uses_the_available_width_and_no_panels_still_fits(self):
        for enabled in ('cpu', 'storage', None):
            with self.subTest(enabled=enabled):
                app = self._app(panels={name: name == enabled for name in ('cpu', 'gpu', 'ram', 'storage', 'sensors')})
                app._render(full_snapshot())
                app.stack.set_visible_child_name('storage' if enabled == 'storage' else 'performance')
                self._resize(app, 380)
                self.assertEqual(set(app.cards), {enabled} if enabled else set())
                if enabled:
                    self.assertGreater(app.cards[enabled].get_width(), 320)

    def test_fahrenheit(self):
        app = self._app(temperature_unit="F")
        app._render(full_snapshot())
        self.assertEqual(app.cards["cpu"]._rows["temp"][1].get_label(), "131.0 °F")

    def test_disabled_panels_are_not_built(self):
        app = self._app(panels={"gpu": False, "sensors": False})
        self.assertEqual(set(app.cards), {"cpu", "ram", "storage"})
        self.assertIsNone(app.collector.gpu)
        app._render(full_snapshot())

    def test_missing_data_shows_state_instead_of_crashing(self):
        app = self._app()
        app._render(Snapshot(sequence=1, gpu_available=False))
        self.assertEqual(app.cards["gpu"]._rows["state"][1].get_label(), "nvidia-smi no disponible")
        self.assertEqual(app.cards["cpu"]._rows["state"][1].get_label(), "Sin datos")
        self.assertEqual(app._last_render_error, {})

    def test_gpu_panel_recovers_when_gpu_appears(self):
        app = self._app()
        app._render(Snapshot(sequence=1, gpu_available=True))
        app._render(full_snapshot())
        self.assertIn("gpu0:name", app.cards["gpu"]._rows)

    def test_multi_gpu(self):
        app = self._app()
        app._render(full_snapshot(n_gpus=2))
        self.assertIn("gpu1:name", app.cards["gpu"]._rows)
        self.assertTrue(app.cards["gpu"]._rows["gpu1:name"][0].get_label().startswith("GPU 1"))

    def test_render_failure_clears_partial_values_reuses_error_row_and_recovers(self):
        app = self._app()
        snap = full_snapshot()
        app._render(snap)
        original = app._render_gpu

        def broken(card, snapshot):
            original(card, snapshot)
            raise RuntimeError("failed after drawing")

        snap.sequence = 2
        with mock.patch.object(app, "_render_gpu", side_effect=broken) as render:
            app._render(snap)
            card = app.cards["gpu"]
            self.assertEqual(set(card._rows), {"state"})
            error_label = card._rows["state"][1]
            app._render(snap)
            self.assertEqual(render.call_count, 1)
            snap.sequence = 3
            app._render(snap)
            self.assertIs(card._rows["state"][1], error_label)
            app._update_status(snap)
            self.assertIn("Interfaz · GPU", app.status.get_label())
            self.assertIn("interfaz:gpu", app.status.get_tooltip_text())
            self.assertIn("failed after drawing", app.status.get_tooltip_text())
        self.assertIn("usage", app.cards["cpu"]._rows)
        snap.sequence = 4
        app._render(snap)
        app._update_status(snap)
        self.assertIn("gpu0:usage", app.cards["gpu"]._rows)
        self.assertNotIn("gpu", app._last_render_error)
        self.assertNotIn("Interfaz · GPU", app.status.get_label())

    def test_failure_before_render_also_invalidates_old_values(self):
        app = self._app()
        app._render(full_snapshot())
        with mock.patch.object(app, "_render_cpu", side_effect=ValueError("bad input")):
            app._render(full_snapshot())
        self.assertEqual(set(app.cards["cpu"]._rows), {"state"})
        self.assertIn("gpu0:usage", app.cards["gpu"]._rows)

    def test_tick_ages_measurements_without_new_snapshot_or_wall_clock(self):
        app = self._app()
        snap = full_snapshot()
        snap.timestamp = 101
        snap.sources = {"gpu": SourceState(100, 101, freshness(1, 2))}
        with mock.patch.object(app.collector, "latest", return_value=snap), mock.patch(
            "src.gui.app.time.monotonic", return_value=102
        ), mock.patch("src.gui.app.time.strftime", side_effect=AssertionError("wall clock used")):
            app._on_tick()
            self.assertEqual(app.status.get_label(), "Instantánea recibida hace 1 s")
            self.assertIn("hace 2 s", app.cards["gpu"]._rows["freshness"][1].get_label())
            self.assertIn("gpu0:usage", app.cards["gpu"]._rows)
        with mock.patch.object(app.collector, "latest", return_value=snap), mock.patch(
            "src.gui.app.time.monotonic", return_value=105
        ):
            app._on_tick()
            self.assertEqual(set(app.cards["gpu"]._rows), {"state"})
            self.assertIn("Obsoleto", app.cards["gpu"]._rows["state"][1].get_label())
            stale_label = app.cards["gpu"]._rows["state"][1]
        with mock.patch.object(app.collector, "latest", return_value=snap), mock.patch(
            "src.gui.app.time.monotonic", return_value=106
        ):
            app._on_tick()
            self.assertIs(app.cards["gpu"]._rows["state"][1], stale_label)
            self.assertIn("hace 6 s", stale_label.get_label())

    def test_a6_tick_ignores_old_snapshot_and_keeps_aging_latest(self):
        app = self._app()
        current = full_snapshot()
        current.sequence, current.timestamp = 10, 100
        current.sources = {'usage:nvme0n1p2': SourceState(100, 100, 2)}
        old = Snapshot(sequence=9)
        with mock.patch.object(app.collector, 'latest', side_effect=[current, old]):
            with mock.patch('src.gui.app.time.monotonic', return_value=101):
                app._on_tick()
            card = next(iter(app.cards['storage'].disk_cards.values()))
            with mock.patch('src.gui.app.time.monotonic', return_value=105):
                app._on_tick()
        self.assertEqual(app._last_sequence, 10)
        self.assertIs(app._last_snapshot, current)
        self.assertIs(next(iter(app.cards['storage'].disk_cards.values())), card)
        self.assertIn('hace 5 s', card.usage_warning.get_label())

    def test_a6_app_construction_and_render_do_not_construct_alias_resolver(self):
        with mock.patch('src.utils.disk_aliases.DiskAliasResolver.__init__',
                        side_effect=AssertionError('resolver construction on GTK')):
            app = self._app()
            self.assertIsNone(app.disk_aliases._thread)
            app._render(full_snapshot())
            self.assertNotIn('storage', app._last_render_error)
            self.assertIsNone(app.disk_aliases._thread)

    def test_storage_stale_values_remain_visible_with_age(self):
        app = self._app()
        snap = full_snapshot()
        snap.sources = {
            "topology": SourceState(0, 0, freshness(15, 2), "lsblk timeout"),
            "smart:nvme0n1": SourceState(0, 0, freshness(10, 2), "smart timeout"),
            "usage:nvme0n1p2": SourceState(0, 0, freshness(1), "statvfs failed"),
        }
        with mock.patch("src.gui.app.time.monotonic", return_value=20):
            app._render(snap)
            app._update_status(snap)
        card = next(iter(app.cards["storage"].disk_cards.values()))
        row = next(iter(card.partition_rows.values()))
        self.assertIn("usados", row.usage.get_label())
        self.assertIn("Datos obsoletos", card.status.get_label())
        self.assertIn("hace 20 s", card.status.get_label())
        self.assertIn("smart timeout", card.status.get_tooltip_text())
        self.assertEqual(card.temperature.get_label(), "N/D")
        self.assertIn("obsoleto", row.warning.get_label().lower())
        snap.disks[0].partitions[0].used_bytes = None
        snap.disks[0].partitions[0].usage_percent = None
        snap.sequence = 2
        with mock.patch("src.gui.app.time.monotonic", return_value=21):
            app._render(snap)
        self.assertFalse(row.usage.get_visible())
        self.assertFalse(row.bar.get_visible())

    def test_independent_cadences_do_not_warn_during_normal_caches(self):
        app = self._app(panels={"sensors": True})
        snap = full_snapshot()
        snap.sources = {
            "cpu": SourceState(13, 13, freshness(1)),
            "ram": SourceState(13, 13, freshness(1)),
            "gpu": SourceState(13, 13, freshness(1, 2)),
            "sensors": SourceState(10, 10, freshness(5, 2)),
            "topology": SourceState(0, 0, freshness(15, 2)),
            "smart:nvme0n1": SourceState(5, 5, freshness(10, 2)),
        }
        with mock.patch("src.gui.app.time.monotonic", return_value=14):
            app._render(snap)
            app._update_status(snap)
        self.assertNotIn("Avisos", app.status.get_label())
        self.assertIn("s0", app.cards["sensors"]._rows)
        self.assertEqual(app._last_render_error, {})

    def test_cached_smart_age_is_local_and_global_status_describes_snapshot_reception(self):
        app = self._app()
        snap = full_snapshot()
        snap.timestamp = 999
        snap.disks[0].temperature_source = 'smart:nvme0n1'
        snap.sources = {
            'cpu': SourceState(999, 999, freshness(1)),
            'gpu': SourceState(999, 999, freshness(1, 2)),
            'ram': SourceState(999, 999, freshness(1)),
            'topology': SourceState(986, 986, freshness(15, 2)),
            'smart:nvme0n1': SourceState(704, 704, 300),
        }
        states = dict(snap.sources)
        with mock.patch('src.gui.app.time.monotonic', return_value=1000):
            app._render(snap)
            app._update_status(snap)
        disk = next(iter(app.cards['storage'].disk_cards.values()))
        self.assertIn('hace 296 s', disk.temperature.get_label())
        self.assertEqual(app.status.get_label(), 'Instantánea recibida hace 1 s')
        self.assertIn('SMART · nvme0n1 (smart:nvme0n1): Medido · hace 296 s', app.status.get_tooltip_text())
        self.assertEqual(snap.sources, states)

        snap.timestamp = 1005
        for key in ('cpu', 'gpu', 'ram'):
            snap.sources[key] = SourceState(1005, 1005, snap.sources[key].max_age)
        with mock.patch('src.gui.app.time.monotonic', return_value=1005):
            app._render(snap)
            app._update_status(snap)
        self.assertEqual(app.status.get_label(), 'Avisos: SMART · nvme0n1')
        self.assertIn('Obsoleto · hace 301 s', app.status.get_tooltip_text())
        self.assertEqual(disk.temperature.get_label(), 'N/D')
        self.assertIn('hace 301 s', disk.status.get_label())
        self.assertIs(snap.sources['smart:nvme0n1'], states['smart:nvme0n1'])

    def test_global_warnings_identify_each_source_and_preserve_technical_details(self):
        app = self._app()
        snap = full_snapshot()
        snap.timestamp = 100
        snap.errors = {'smart:sda': 'Permission denied', 'udisks:sdb': 'D-Bus error',
                       'usage:dm-0': 'statvfs failed', 'topology': 'lsblk timeout',
                       'temperature:nvme0n1': 'sysfs failed', 'cuda': 'query failed'}
        app._last_render_error['gpu'] = 'render failed'
        with mock.patch('src.gui.app.time.monotonic', return_value=101):
            app._update_status(snap)
        for title in ('SMART · sda', 'Temperatura UDisks2 · sdb', 'Uso · dm-0',
                      'Topología de almacenamiento', 'Temperatura · nvme0n1',
                      'NVIDIA/CUDA', 'Interfaz · GPU'):
            self.assertIn(title, app.status.get_label())
        for key, error in snap.errors.items():
            self.assertIn(f'({key}): {error}', app.status.get_tooltip_text())

    def test_delayed_publication_is_distinguished_from_cached_measurement_age(self):
        app = self._app()
        snap = full_snapshot()
        snap.timestamp = 100
        snap.sources = {'smart:nvme0n1': SourceState(0, 0, 300)}
        with mock.patch('src.gui.app.time.monotonic', return_value=111):
            app._update_status(snap)
        self.assertEqual(app.status.get_label(), 'Sin instantáneas recientes')
        self.assertIn('Instantánea recibida hace 11 s', app.status.get_tooltip_text())
        self.assertIn('SMART · nvme0n1 (smart:nvme0n1): Medido · hace 111 s', app.status.get_tooltip_text())
        self.assertNotIn('Obsoleto', app.status.get_tooltip_text())

    def test_snapshot_with_no_successful_measurement_keeps_its_unavailable_status(self):
        app = self._app()
        snap = full_snapshot()
        snap.timestamp = 100
        snap.sources = {'cpu': SourceState(), 'gpu': SourceState()}
        with mock.patch('src.gui.app.time.monotonic', return_value=101):
            app._update_status(snap)
        self.assertEqual(app.status.get_label(), 'Sin mediciones válidas')
        self.assertIn('CPU (cpu): Sin medición válida', app.status.get_tooltip_text())

    def test_sensor_warning_persists_and_valid_render_recovers(self):
        app = self._app(panels={"sensors": True})
        snap = full_snapshot()
        snap.sources = {"sensors": SourceState(0, 0, freshness(5, 2), "timeout")}
        snap.errors = {"sensors": "timeout"}
        with mock.patch("src.gui.app.time.monotonic", return_value=6):
            app._render(snap)
            app._update_status(snap)
        self.assertEqual(set(app.cards["sensors"]._rows), {"state"})
        self.assertIn("Sensores", app.status.get_label())
        self.assertIn("sensors", app.status.get_tooltip_text())
        snap.sources = {"sensors": SourceState(10, 10, freshness(5, 2))}
        snap.errors = {}
        snap.sequence = 2
        with mock.patch("src.gui.app.time.monotonic", return_value=10):
            app._render(snap)
            app._update_status(snap)
        self.assertIn("s0", app.cards["sensors"]._rows)
        self.assertNotIn("Avisos", app.status.get_label())

    def test_storage_render_failure_recovery_keeps_expansion_and_error_signature(self):
        app = self._app()
        snap = full_snapshot()
        app._render(snap)
        panel = app.cards['storage']
        next(iter(panel.disk_cards.values())).expander.set_expanded(True)
        original = app._render_storage

        def broken(card, snapshot):
            original(card, snapshot)
            raise RuntimeError('storage render failed')

        snap.sequence = 2
        with mock.patch.object(app, '_render_storage', side_effect=broken):
            app._render(snap)
            self.assertFalse(panel.disk_cards)
            error_label = panel._rows['state'][1]
            snap.sequence = 3
            app._render(snap)
            self.assertIs(error_label, panel._rows['state'][1])
        snap.sequence = 4
        app._render(snap)
        self.assertTrue(next(iter(panel.disk_cards.values())).expander.get_expanded())
        self.assertNotIn('storage', app._last_render_error)

    def test_explicit_dev_config_and_application_id_are_independent(self):
        from src.gui.app import SystemMonitorApp
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict('os.environ', {'XDG_CONFIG_HOME': temporary}):
            root = Path(temporary)
            stable = root / 'nvidia-system-monitor'
            dev = root / 'nvidia-system-monitor-dev'
            stable.mkdir()
            dev.mkdir()
            stable_text = '{"refresh_interval_seconds": 1, "logging_enabled": false}'
            (stable / 'config.json').write_text(stable_text)
            (dev / 'config.json').write_text('{"refresh_interval_seconds": 3, "logging_enabled": false}')
            app = SystemMonitorApp(channel='dev')
            self.assertEqual(app.interval, 3)
            self.assertEqual(app.config.path, dev / 'config.json')
            self.assertTrue(app.get_application_id().endswith('.Dev'))
            self.assertEqual((stable / 'config.json').read_text(), stable_text)


if __name__ == "__main__":
    unittest.main()
