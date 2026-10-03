import time
import unittest
from unittest import mock

from src.data.collector import Collector
from src.data.cpu import CpuMetrics
from src.data.nvidia import GpuMetrics
from src.data.ram import RamMetrics


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


if __name__ == "__main__":
    unittest.main()
