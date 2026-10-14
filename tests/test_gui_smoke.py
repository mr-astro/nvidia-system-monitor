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

    def _app(self, **overrides):
        from src.gui.app import SystemMonitorApp
        from src.utils.config import Config

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.json"
        import json
        path.write_text(json.dumps({"logging_enabled": False, **overrides}))
        app = SystemMonitorApp(Config(path))
        from src.utils.disk_aliases import DiskAliasResolver
        app.disk_aliases = DiskAliasResolver(Path(tmp.name) / "aliases.json", Path(tmp.name) / "by-id")
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
            provider.load_from_string(build_css(DEFAULT_CONFIG["color_scheme"], dark))
            self.assertEqual(errors, [], f"dark={dark}")

    def test_system_theme_does_not_force_dark_colors(self):
        from src.gui.app import build_css
        from src.utils.config import DEFAULT_CONFIG
        colors = DEFAULT_CONFIG["color_scheme"]
        self.assertIn("#1e1e1e", build_css(colors, dark=True))
        self.assertNotIn("#1e1e1e", build_css(colors, dark=False))
        self.assertFalse(self._app(theme="system").dark)
        self.assertTrue(self._app().dark)

    def test_storage_shares_the_gpu_column_and_ram_shares_the_cpu_column(self):
        app = self._app()
        cpu, ram, gpu, storage = (app.cards[n] for n in ("cpu", "ram", "gpu", "storage"))
        self.assertIs(cpu.get_parent(), ram.get_parent())       # left column
        self.assertIsNot(cpu.get_parent(), gpu.get_parent())    # right column
        self.assertIs(gpu.get_parent(), storage.get_parent())
        self.assertIsInstance(cpu.get_parent().get_parent().get_parent(), Gtk.FlowBox)

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
        self.assertLess(self._bounds(storage, scroller).get_y(), scroller.get_height())
        self.assertLess(self._bounds(storage, columns).get_y(), wide_left.get_height())

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
            self.assertIs(right, storage.get_parent())

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
        self._resize(app, 380)
        for card in app.cards.values():
            self.assertLessEqual(card.measure(Gtk.Orientation.HORIZONTAL, -1)[0], card.get_width())
        disk = next(iter(app.cards['storage'].disk_cards.values()))
        self.assertIn('hace 296 s', disk.temperature.get_label())
        self.assertIn('Permission denied', app.status.get_tooltip_text())
        self.assertNotIn('Permission denied', app.status.get_label())

    def test_one_enabled_column_uses_the_available_width_and_no_panels_still_fits(self):
        for enabled in ('cpu', 'storage', None):
            with self.subTest(enabled=enabled):
                app = self._app(panels={name: name == enabled for name in ('cpu', 'gpu', 'ram', 'storage', 'sensors')})
                app._render(full_snapshot())
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
