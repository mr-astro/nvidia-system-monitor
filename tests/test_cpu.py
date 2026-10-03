import os
import tempfile
import unittest

from src.data.cpu import (
    CpuMonitor, parse_cpuinfo, parse_stat_cpu_line, usage_between,
)

SMT_CPUINFO = """processor\t: 0
model name\t: AMD Ryzen 7 5800X 8-Core Processor
physical id\t: 0
core id\t\t: 0

processor\t: 1
model name\t: AMD Ryzen 7 5800X 8-Core Processor
physical id\t: 0
core id\t\t: 1

processor\t: 2
model name\t: AMD Ryzen 7 5800X 8-Core Processor
physical id\t: 0
core id\t\t: 0

processor\t: 3
model name\t: AMD Ryzen 7 5800X 8-Core Processor
physical id\t: 0
core id\t\t: 1
"""

ARM_CPUINFO = "processor : 0\nprocessor : 1\nHardware : Foo SoC\n"


class CpuInfo(unittest.TestCase):
    def test_smt_topology(self):
        info = parse_cpuinfo(SMT_CPUINFO)
        self.assertEqual(info["model"], "AMD Ryzen 7 5800X 8-Core Processor")
        self.assertEqual(info["threads"], 4)
        self.assertEqual(info["cores"], 2)

    def test_without_topology_reports_logical_cpus(self):
        info = parse_cpuinfo(ARM_CPUINFO)
        self.assertEqual(info["threads"], 2)
        self.assertEqual(info["cores"], 2)
        self.assertEqual(info["model"], "Foo SoC")

    def test_realistic_arm_cpuinfo_with_blank_lines(self):
        text = "processor : 0\nBogoMIPS : 50.0\n\nprocessor : 1\nBogoMIPS : 50.0\n\nHardware : Foo SoC\n"
        info = parse_cpuinfo(text)
        self.assertEqual((info["threads"], info["cores"], info["model"]), (2, 2, "Foo SoC"))

    def test_empty(self):
        info = parse_cpuinfo("")
        self.assertEqual((info["model"], info["threads"], info["cores"]), ("N/D", 0, 0))


class CpuUsage(unittest.TestCase):
    def test_guest_time_is_not_double_counted(self):
        # user nice system idle iowait irq softirq steal guest guest_nice
        line = "cpu  100 0 50 800 20 5 5 0 40 10\ncpu0 1 1 1 1 1 1 1 1 1 1\n"
        self.assertEqual(parse_stat_cpu_line(line), (980, 820))

    def test_usage_between(self):
        self.assertAlmostEqual(usage_between((1000, 800), (1100, 850)), 50.0)
        self.assertEqual(usage_between((1000, 800), (1100, 900)), 0.0)
        self.assertIsNone(usage_between(None, (1, 1)))
        self.assertIsNone(usage_between((10, 5), (10, 5)))

    def test_bad_stat(self):
        self.assertIsNone(parse_stat_cpu_line(""))
        self.assertIsNone(parse_stat_cpu_line("cpu  a b c d"))
        self.assertIsNone(parse_stat_cpu_line("cpu  1 2"))


class CpuMonitorWithFakeSystem(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = os.path.join(self.tmp.name, "proc")
        self.sys = os.path.join(self.tmp.name, "sys")
        os.makedirs(self.proc)
        self._write(self.proc, "cpuinfo", SMT_CPUINFO)
        self._write(self.proc, "stat", "cpu  100 0 100 800 0 0 0 0 0 0\n")
        self._write(self.proc, "loadavg", "0.52 0.40 0.30 1/500 1234\n")

    @staticmethod
    def _write(directory, name, text):
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name), "w") as f:
            f.write(text)

    def test_end_to_end(self):
        hw = os.path.join(self.sys, "class", "hwmon")
        self._write(os.path.join(hw, "hwmon0"), "name", "nvme\n")
        self._write(os.path.join(hw, "hwmon1"), "name", "k10temp\n")
        self._write(os.path.join(hw, "hwmon1"), "temp1_input", "45250\n")
        cpu_dir = os.path.join(self.sys, "devices", "system", "cpu")
        self._write(os.path.join(cpu_dir, "cpu0", "cpufreq"), "scaling_cur_freq", "3600000\n")
        self._write(os.path.join(cpu_dir, "cpu1", "cpufreq"), "scaling_cur_freq", "4000000\n")
        self._write(os.path.join(cpu_dir, "cpu0", "cpufreq"), "cpuinfo_max_freq", "4700000\n")

        monitor = CpuMonitor(self.proc, self.sys)
        # +200 total jiffies, +100 idle -> 50 % busy
        self._write(self.proc, "stat", "cpu  150 0 150 900 0 0 0 0 0 0\n")
        m = monitor.get_metrics()

        self.assertEqual((m.cores, m.threads), (2, 4))
        self.assertAlmostEqual(m.usage_percent, 50.0)
        self.assertAlmostEqual(m.temperature_c, 45.25)
        self.assertAlmostEqual(m.frequency_mhz, 3800.0)
        self.assertAlmostEqual(m.max_frequency_mhz, 4700.0)
        self.assertEqual((m.load_1m, m.load_5m, m.load_15m), (0.52, 0.40, 0.30))

    def test_missing_sources_degrade_to_none(self):
        monitor = CpuMonitor(self.proc, self.sys)  # no sysfs at all
        m = monitor.get_metrics()
        self.assertIsNone(m.temperature_c)
        self.assertIsNone(m.frequency_mhz)
        self.assertIsNone(m.max_frequency_mhz)

    def test_intel_coretemp_is_recognised(self):
        hw = os.path.join(self.sys, "class", "hwmon", "hwmon0")
        self._write(hw, "name", "coretemp\n")
        self._write(hw, "temp1_input", "61000\n")
        self.assertAlmostEqual(CpuMonitor(self.proc, self.sys).get_metrics().temperature_c, 61.0)


if __name__ == "__main__":
    unittest.main()
