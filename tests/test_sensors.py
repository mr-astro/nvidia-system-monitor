import unittest
import subprocess
from types import SimpleNamespace
from unittest import mock

from src.data.sensors import SensorMonitor, parse_sensors_output

SAMPLE = """k10temp-pci-00c3
Adapter: PCI adapter
Tctl:
  temp1_input: 45.250

nvme-pci-0100
Adapter: PCI adapter
Composite:
  temp1_input: 38.850
  temp1_max: 83.850
  temp1_min: -273.150
  temp1_crit: 87.850
  temp1_alarm: 0.000
Sensor 1:
  temp2_input: 40.850
  temp2_max: 65261.850

coretemp-isa-0000
Adapter: ISA adapter
Package id 0:
  temp1_input: 61.000
  temp1_crit: 100.000
Core 0:
  temp2_input: 58.000
fan-chip-1
Adapter: ISA adapter
fan1:
  fan1_input: 1200.000
"""


class Sensors(unittest.TestCase):
    def setUp(self):
        self.readings = parse_sensors_output(SAMPLE)
        self.by_key = {(r.chip, r.label): r.value_c for r in self.readings}

    def test_values_are_celsius_not_millidegrees(self):
        # v2 divided these by 1000 and reported 0.045 °C.
        self.assertAlmostEqual(self.by_key[("k10temp-pci-00c3", "Tctl")], 45.25)

    def test_chip_names_survive_adapter_and_label_lines(self):
        chips = {r.chip for r in self.readings}
        self.assertEqual(chips, {"k10temp-pci-00c3", "nvme-pci-0100", "coretemp-isa-0000"})

    def test_labels_and_only_input_values(self):
        self.assertAlmostEqual(self.by_key[("nvme-pci-0100", "Composite")], 38.85)
        self.assertAlmostEqual(self.by_key[("nvme-pci-0100", "Sensor 1")], 40.85)
        self.assertAlmostEqual(self.by_key[("coretemp-isa-0000", "Package id 0")], 61.0)
        self.assertEqual(len(self.readings), 5)  # max/min/crit/alarm and fans ignored

    def test_empty_and_garbage(self):
        self.assertEqual(parse_sensors_output(""), [])
        self.assertEqual(parse_sensors_output("  temp1_input: 5\nrandom"), [])


class SensorCommands(unittest.TestCase):
    def test_command_errors_and_recovery(self):
        with mock.patch("shutil.which", return_value="/usr/bin/sensors"):
            monitor = SensorMonitor()
        for failure in (OSError("unavailable"), subprocess.TimeoutExpired("sensors", 2)):
            with self.subTest(failure=failure), mock.patch("src.data.sensors.subprocess.run", side_effect=failure):
                self.assertEqual(monitor.get_readings(), [])
                self.assertIn("sensors", monitor.errors)
        with mock.patch("src.data.sensors.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="No sensors found")):
            monitor.get_readings()
        self.assertIn("No sensors found", monitor.errors["sensors"])
        with mock.patch("src.data.sensors.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=SAMPLE)):
            self.assertEqual(len(monitor.get_readings()), 5)
        self.assertEqual(monitor.errors, {})

    def test_missing_utility_and_malformed_temperature(self):
        with mock.patch("shutil.which", return_value=None):
            monitor = SensorMonitor()
        monitor.get_readings()
        self.assertIn("no disponible", monitor.errors["sensors"])
        monitor._binary = "/usr/bin/sensors"
        with mock.patch("src.data.sensors.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="chip\nTemp:\n  temp1_input: broken\n")):
            monitor.get_readings()
        self.assertIn("malformada", monitor.errors["sensors"])

    def test_chip_with_only_fans_is_not_a_command_failure(self):
        with mock.patch("shutil.which", return_value="/usr/bin/sensors"):
            monitor = SensorMonitor()
        with mock.patch("src.data.sensors.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="chip\nfan1:\n  fan1_input: 1200\n")):
            self.assertEqual(monitor.get_readings(), [])
        self.assertEqual(monitor.errors, {})

    def test_partial_temperature_parse_reports_missing_reading(self):
        with mock.patch("shutil.which", return_value="/usr/bin/sensors"):
            monitor = SensorMonitor()
        with mock.patch("src.data.sensors.subprocess.run", return_value=SimpleNamespace(
            returncode=0, stdout="chip\nTemp:\n  temp1_input: 35\n  temp2_input: broken\n"
        )):
            self.assertEqual(len(monitor.get_readings()), 1)
        self.assertIn("malformada", monitor.errors["sensors"])


if __name__ == "__main__":
    unittest.main()
