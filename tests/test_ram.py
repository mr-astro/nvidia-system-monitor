import os
import tempfile
import unittest

from src.data.ram import RamMonitor, parse_meminfo

MEMINFO = """MemTotal:       16384000 kB
MemFree:         2048000 kB
MemAvailable:    8192000 kB
Buffers:         1024000 kB
Cached:          4096000 kB
SReclaimable:    1024000 kB
SwapTotal:       4194304 kB
SwapFree:        2097152 kB
"""


class Ram(unittest.TestCase):
    def _monitor(self, text):
        tmp = tempfile.NamedTemporaryFile("w", delete=False)
        tmp.write(text)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return RamMonitor(tmp.name)

    def test_metrics(self):
        m = self._monitor(MEMINFO).get_metrics()
        self.assertAlmostEqual(m.total_mb, 16000.0)
        self.assertAlmostEqual(m.available_mb, 8000.0)
        self.assertAlmostEqual(m.used_mb, 8000.0)
        self.assertAlmostEqual(m.usage_percent, 50.0)
        self.assertAlmostEqual(m.cached_mb, 6000.0)  # buffers + cache + reclaimable
        self.assertAlmostEqual(m.swap_total_mb, 4096.0)
        self.assertAlmostEqual(m.swap_used_mb, 2048.0)
        self.assertAlmostEqual(m.swap_usage_percent, 50.0)

    def test_no_swap(self):
        text = MEMINFO.replace("SwapTotal:       4194304", "SwapTotal:       0")
        m = self._monitor(text).get_metrics()
        self.assertEqual(m.swap_total_mb, 0.0)
        self.assertIsNone(m.swap_usage_percent)

    def test_malformed_line_does_not_discard_everything(self):
        data = parse_meminfo("this line has no colon\nMemTotal: 100 kB\nBad: xyz kB\n")
        self.assertEqual(data, {"MemTotal": 100})

    def test_missing_file_gives_empty_metrics(self):
        m = RamMonitor("/nonexistent/meminfo").get_metrics()
        self.assertIsNone(m.total_mb)

    def test_missing_memavailable_is_approximated(self):
        text = "\n".join(l for l in MEMINFO.splitlines() if "MemAvailable" not in l)
        m = self._monitor(text).get_metrics()
        self.assertAlmostEqual(m.available_mb, 2000 + 6000)


if __name__ == "__main__":
    unittest.main()
