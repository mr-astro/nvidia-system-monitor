import time
import unittest
from unittest import mock

from src.data.collector import Collector
from src.data.cpu import CpuMetrics
from src.data.nvidia import GpuMetrics
from src.data.ram import RamMetrics
from src.data.errors import SourceState, freshness
from src.data.nvidia import NvidiaMonitor
from src.data.sensors import SensorMonitor
import subprocess
from types import SimpleNamespace


class Cpu:
    def get_metrics(self):
        return CpuMetrics(model="X", usage_percent=10.0)


class Ram:
    def get_metrics(self):
        return RamMetrics(total_mb=100.0)


class BoomRam:
    def get_metrics(self):
        raise RuntimeError("boom")


class Gpu:
    available = True

    def get_metrics(self):
        return [GpuMetrics(index=0, name="G")]

    def get_cuda_version(self):
        return "12.6"


class NoGpu:
    available = False

    def get_metrics(self):
        return []


class Sensors:
    def __init__(self):
        self.calls = 0

    def get_readings(self):
        self.calls += 1
        return ["reading"]


class CollectOnce(unittest.TestCase):
    def test_direct_usage_publication_error_recovery_and_cleanup(self):
        from tests.test_storage import fake_monitor

        now = [0.0]
        monitor = fake_monitor(clock=lambda: now[0])
        devices = [{'name': 'sdb', 'type': 'disk', 'fstype': 'ext4', 'mountpoints': ['/mnt/datos']}]
        mounts = {'/mnt/datos': ('10', '8:16', '/', 'ext4', '/dev/sdb')}
        stat = SimpleNamespace(f_blocks=1000, f_bfree=400, f_bavail=300, f_frsize=4096)
        collector = Collector(storage=monitor, clock=lambda: now[0])
        with mock.patch.object(monitor, '_run_lsblk', side_effect=lambda: list(devices)), \
                mock.patch.object(monitor, '_read_mountinfo', return_value=mounts), \
                mock.patch.object(monitor, '_temperature', return_value=None), \
                mock.patch('src.data.storage.os.statvfs', return_value=stat) as statvfs:
            first = collector.collect_once()
            self.assertEqual(first.disks[0].direct_filesystem.used_bytes, 600 * 4096)
            self.assertEqual(first.sources['usage:sdb'].acquired_at, 0)
            now[0] = 1
            statvfs.side_effect = OSError('synthetic temporary failure')
            failed = collector.collect_once()
            self.assertIn('usage:sdb', failed.errors)
            self.assertEqual(failed.sources['usage:sdb'].acquired_at, 0)
            self.assertEqual(failed.disks[0].direct_filesystem.used_bytes, 600 * 4096)
            now[0] = 2
            statvfs.side_effect = None
            recovered = collector.collect_once()
            self.assertNotIn('usage:sdb', recovered.errors)
            self.assertEqual(recovered.sources['usage:sdb'].acquired_at, 2)
            self.assertNotIn('usage:sdb', first.errors)
            self.assertEqual(first.sources['usage:sdb'].acquired_at, 0)
            devices.clear()
            now[0] = 16
            gone = collector.collect_once()
            self.assertEqual(gone.disks, [])
            self.assertNotIn('usage:sdb', gone.sources)
            self.assertNotIn('usage:sdb', gone.errors)

    def test_full_snapshot(self):
        snap = Collector(cpu=Cpu(), gpu=Gpu(), ram=Ram()).collect_once()
        self.assertEqual(snap.cpu.model, "X")
        self.assertEqual(snap.ram.total_mb, 100.0)
        self.assertEqual(snap.gpus[0].name, "G")
        self.assertEqual(snap.cuda_version, "12.6")
        self.assertTrue(snap.gpu_available)
        self.assertEqual(snap.errors, {})

    def test_disabled_monitors_are_skipped(self):
        snap = Collector(cpu=Cpu()).collect_once()
        self.assertIsNone(snap.ram)
        self.assertEqual(snap.gpus, [])

    def test_missing_gpu_is_flagged(self):
        snap = Collector(gpu=NoGpu()).collect_once()
        self.assertFalse(snap.gpu_available)
        self.assertEqual(snap.gpus, [])

    def test_one_failing_source_does_not_affect_the_others(self):
        snap = Collector(cpu=Cpu(), ram=BoomRam()).collect_once()
        self.assertEqual(snap.cpu.model, "X")
        self.assertIsNone(snap.ram)
        self.assertIn("RuntimeError: boom", snap.errors["ram"])

    def test_sequence_increases(self):
        c = Collector(cpu=Cpu())
        self.assertEqual([c.collect_once().sequence for _ in range(3)], [1, 2, 3])

    def test_sensors_are_polled_at_their_own_cadence(self):
        now = [0.0]
        sensors = Sensors()
        c = Collector(sensors=sensors, sensors_interval=5, clock=lambda: now[0])
        c.collect_once()
        c.collect_once()
        self.assertEqual(sensors.calls, 1)
        now[0] = 6
        snap = c.collect_once()
        self.assertEqual(sensors.calls, 2)
        self.assertEqual(snap.sensors, ["reading"])

    def test_udisks_errors_and_ages_persist_then_recover_and_disappear(self):
        storage = SimpleNamespace(
            errors={'udisks:sda': 'bus timeout'},
            sources={'udisks:sda': SourceState(1, 1, 752, 'bus timeout')},
            get_metrics=mock.Mock(return_value=[]),
        )
        now = [10.0]
        collector = Collector(storage=storage, cpu=Cpu(), clock=lambda: now[0])
        first = collector.collect_once()
        now[0] = 20
        second = collector.collect_once()
        self.assertEqual(first.errors['udisks:sda'], second.errors['udisks:sda'])
        self.assertEqual(second.sources['udisks:sda'].age(20), 19)
        self.assertIsNotNone(second.cpu)
        storage.errors = {}
        storage.sources['udisks:sda'] = SourceState(1, 1, 752)
        recovered = collector.collect_once()
        self.assertNotIn('udisks:sda', recovered.errors)
        self.assertEqual(recovered.sources['udisks:sda'].acquired_at, 1)
        storage.sources = {}
        removed = collector.collect_once()
        self.assertNotIn('udisks:sda', removed.sources)
        self.assertNotIn('udisks:sda', removed.errors)


class ErrorLogging(unittest.TestCase):
    def test_repeated_error_is_logged_once_per_minute(self):
        now = [0.0]
        logger = mock.MagicMock()
        c = Collector(ram=BoomRam(), logger=logger, clock=lambda: now[0])
        for _ in range(10):
            c.collect_once()
        self.assertEqual(logger.error.call_count, 1)
        now[0] = 61
        c.collect_once()
        self.assertEqual(logger.error.call_count, 2)

    def test_changed_error_message_is_logged_immediately(self):
        logger = mock.MagicMock()
        c = Collector(ram=BoomRam(), logger=logger, clock=lambda: 0.0)
        c.collect_once()
        c.ram = mock.MagicMock()
        c.ram.get_metrics.side_effect = ValueError("otro")
        c.collect_once()
        self.assertEqual(logger.error.call_count, 2)


class Threading(unittest.TestCase):
    def test_background_thread_publishes_snapshots_and_stops(self):
        c = Collector(cpu=Cpu(), interval=0.05)
        self.assertEqual(c.latest().sequence, 0)
        c.start()
        try:
            deadline = time.monotonic() + 3
            while c.latest().sequence < 3 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(c.latest().sequence, 3)
        finally:
            c.stop()
        self.assertFalse(c._thread.is_alive())

    def test_slow_source_does_not_block_reader(self):
        class Slow:
            def get_metrics(self):
                time.sleep(0.5)
                return CpuMetrics()

        c = Collector(cpu=Slow(), interval=0.05)
        c.start()
        try:
            started = time.monotonic()
            c.latest()  # what the GTK main loop does: must be instantaneous
            self.assertLess(time.monotonic() - started, 0.05)
        finally:
            c.stop()

    def test_start_is_idempotent(self):
        c = Collector(cpu=Cpu(), interval=0.05)
        c.start()
        first = c._thread
        c.start()
        try:
            self.assertIs(c._thread, first)
        finally:
            c.stop()


class Reliability(unittest.TestCase):
    def test_acquisition_times_are_independent_of_publication_and_wall_clock(self):
        now = [10.0]

        class SlowGpu(Gpu):
            timeout = 2.0

            def get_metrics(self):
                now[0] += 1.8
                return super().get_metrics()

        collector = Collector(cpu=Cpu(), ram=Ram(), gpu=SlowGpu(), clock=lambda: now[0])
        with mock.patch("time.time", return_value=-10000):
            snap = collector.collect_once()
        self.assertEqual(snap.acquisition_started_at, 10)
        self.assertEqual(snap.sources["cpu"].acquired_at, 10)
        self.assertEqual(snap.sources["gpu"].acquired_at, 10)
        self.assertEqual(snap.timestamp, 11.8)
        self.assertEqual(snap.sources["gpu"].completed_at, 11.8)
        self.assertAlmostEqual(snap.sources["gpu"].age(snap.timestamp), 1.8)
        self.assertEqual(snap.sources["cpu"].max_age, freshness(1))
        self.assertEqual(snap.sources["ram"].max_age, freshness(1))
        self.assertEqual(snap.sources["gpu"].max_age, freshness(1, 2))

    def test_sensor_failure_persists_until_success_and_keeps_original_age(self):
        now = [0.0]
        sensor = mock.Mock(timeout=2, errors={})
        sensor.get_readings.side_effect = [["valid"], RuntimeError("disconnected"), ["recovered"]]
        collector = Collector(sensors=sensor, clock=lambda: now[0])
        first = collector.collect_once()
        now[0] = 5
        failed = collector.collect_once()
        self.assertIn("sensors", failed.errors)
        self.assertEqual(failed.sensors, ["valid"])
        self.assertEqual(failed.sources["sensors"].acquired_at, 0)
        now[0] = 6
        cached = collector.collect_once()
        self.assertEqual(cached.errors, failed.errors)
        self.assertEqual(sensor.get_readings.call_count, 2)
        self.assertEqual(first.errors, {})  # published snapshots are not mutated
        now[0] = 10
        recovered = collector.collect_once()
        self.assertNotIn("sensors", recovered.errors)
        self.assertEqual(recovered.sensors, ["recovered"])
        self.assertEqual(recovered.sources["sensors"].acquired_at, 10)

    def test_real_sensor_timeout_survives_unscheduled_cycles(self):
        now = [0.0]
        with mock.patch("shutil.which", return_value="/usr/bin/sensors"):
            sensor = SensorMonitor()
        collector = Collector(cpu=Cpu(), sensors=sensor, clock=lambda: now[0])
        with mock.patch("src.data.sensors.subprocess.run", side_effect=subprocess.TimeoutExpired("sensors", 2)):
            snap = collector.collect_once()
        now[0] = 1
        cached = collector.collect_once()
        self.assertEqual(snap.errors, cached.errors)
        self.assertIn("timeout", cached.errors["sensors"])
        self.assertIsNotNone(cached.cpu)

    def test_normal_sensor_cache_does_not_refresh_measurement_or_warn(self):
        now = [0.0]
        collector = Collector(sensors=Sensors(), clock=lambda: now[0])
        collector.collect_once()
        now[0] = 4.9
        snap = collector.collect_once()
        state = snap.sources["sensors"]
        self.assertEqual(state.acquired_at, 0)
        self.assertFalse(state.stale(now[0]))
        self.assertTrue(state.stale(state.max_age + 0.1))

    def test_thousands_of_gpu_cycles_fail_recover_and_rate_limit_logs(self):
        from tests.test_nvidia import ROW
        now = [0.0]
        logger = mock.Mock()
        with mock.patch("shutil.which", return_value="/usr/bin/nvidia-smi"):
            gpu = NvidiaMonitor()
        gpu._cuda = "13.4"
        collector = Collector(cpu=Cpu(), ram=Ram(), gpu=gpu, logger=logger, clock=lambda: now[0])
        good = SimpleNamespace(returncode=0, stdout=ROW)
        for cycle in range(3600):
            now[0] = float(cycle)
            failure = 100 <= cycle < 220
            with mock.patch("src.data.nvidia.subprocess.run", **(
                {"side_effect": subprocess.TimeoutExpired("nvidia-smi", 2)} if failure else {"return_value": good}
            )):
                snap = collector.collect_once()
            self.assertEqual(snap.sequence, cycle + 1)
            self.assertIsNotNone(snap.cpu)
            self.assertIsNotNone(snap.ram)
            self.assertEqual("gpu" in snap.errors, failure)
            if failure:
                self.assertEqual(snap.sources["gpu"].acquired_at, 99)
            else:
                self.assertEqual(snap.sources["gpu"].acquired_at, cycle)
        self.assertEqual(logger.error.call_count, 2)
        self.assertEqual(len(collector._states), 3)


if __name__ == "__main__":
    unittest.main()
