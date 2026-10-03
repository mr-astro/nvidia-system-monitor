import unittest
from unittest import mock

from src.data.nvidia import NvidiaMonitor, GpuMetrics

ROW = ("0, NVIDIA GeForce RTX 3090, 12, 24576, 1200, 23000, 45, 30.50, 350.00, "
       "210, 405, [N/A], 560.35.03")


class ParseQueryOutput(unittest.TestCase):
    def test_single_gpu(self):
        (g,) = NvidiaMonitor.parse_query_output(ROW)
        self.assertEqual(g.index, 0)
        self.assertEqual(g.name, "NVIDIA GeForce RTX 3090")
        self.assertEqual(g.utilization_percent, 12.0)
        self.assertEqual(g.memory_total_mb, 24576.0)
        self.assertEqual(g.memory_used_mb, 1200.0)
        self.assertEqual(g.temperature_c, 45.0)
        self.assertEqual(g.power_draw_w, 30.5)
        self.assertEqual(g.power_limit_w, 350.0)
        self.assertEqual(g.core_clock_mhz, 210.0)
        self.assertEqual(g.memory_clock_mhz, 405.0)
        self.assertEqual(g.driver_version, "560.35.03")

    def test_unsupported_field_is_none_not_zero(self):
        (g,) = NvidiaMonitor.parse_query_output(ROW)
        self.assertIsNone(g.fan_percent)

    def test_not_supported_variants(self):
        row = ROW.replace("[N/A]", "[Not Supported]")
        (g,) = NvidiaMonitor.parse_query_output(row)
        self.assertIsNone(g.fan_percent)

    def test_multiple_gpus(self):
        text = ROW + "\n" + ROW.replace("0,", "1,", 1).replace("3090", "3060")
        gpus = NvidiaMonitor.parse_query_output(text)
        self.assertEqual([g.index for g in gpus], [0, 1])
        self.assertTrue(gpus[1].name.endswith("3060"))

    def test_gpu_name_with_comma_is_folded(self):
        text = ROW.replace("NVIDIA GeForce RTX 3090", "Weird, Name 9000")
        (g,) = NvidiaMonitor.parse_query_output(text)
        self.assertEqual(g.name, "Weird, Name 9000")
        self.assertEqual(g.utilization_percent, 12.0)
        self.assertEqual(g.driver_version, "560.35.03")

    def test_malformed_and_empty_input(self):
        self.assertEqual(NvidiaMonitor.parse_query_output(""), [])
        self.assertEqual(NvidiaMonitor.parse_query_output("garbage"), [])
        self.assertEqual(NvidiaMonitor.parse_query_output("0, only, three"), [])

    def test_memory_percent(self):
        g = GpuMetrics(memory_used_mb=6144, memory_total_mb=24576)
        self.assertAlmostEqual(g.memory_percent, 25.0)
        self.assertIsNone(GpuMetrics().memory_percent)
        self.assertIsNone(GpuMetrics(memory_used_mb=1, memory_total_mb=0).memory_percent)


class Availability(unittest.TestCase):
    def test_missing_binary_returns_empty_list(self):
        with mock.patch("shutil.which", return_value=None):
            m = NvidiaMonitor()
        self.assertFalse(m.available)
        self.assertEqual(m.get_metrics(), [])
        self.assertEqual(m.get_cuda_version(), "N/D")

    def test_cuda_banner(self):
        banner = "| NVIDIA-SMI 560.35.03  Driver Version: 560.35.03  CUDA Version: 12.6 |"
        self.assertEqual(NvidiaMonitor.parse_cuda_version(banner), "12.6")
        self.assertEqual(NvidiaMonitor.parse_cuda_version("nada"), "N/D")

    def test_cuda_is_cached_once_found(self):
        with mock.patch("shutil.which", return_value="/usr/bin/nvidia-smi"):
            m = NvidiaMonitor()
        with mock.patch.object(m, "_run", return_value="CUDA Version: 12.6") as run:
            self.assertEqual(m.get_cuda_version(), "12.6")
            self.assertEqual(m.get_cuda_version(), "12.6")
        self.assertEqual(run.call_count, 1)

    def test_cuda_failure_is_retried_only_after_backoff(self):
        with mock.patch("shutil.which", return_value="/usr/bin/nvidia-smi"):
            m = NvidiaMonitor(cuda_retry_seconds=60)
        with mock.patch.object(m, "_run", return_value="") as run:
            m.get_cuda_version()
            m.get_cuda_version()
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
