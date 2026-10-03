import unittest

from src.data.sensors import parse_sensors_output

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


if __name__ == "__main__":
    unittest.main()
