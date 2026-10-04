import os
import tempfile
import types
import unittest
from unittest import mock

from src.data.storage import StorageMonitor

LSBLK = {"blockdevices": [
    {"name": "zram0", "type": "disk", "size": 8589934592, "model": None, "tran": None,
     "mountpoints": ["[SWAP]"], "rota": False, "fstype": "swap"},
    {"name": "loop0", "type": "loop", "size": 1000, "mountpoints": ["/snap/x"], "rota": False},
    {"name": "nvme0n1", "type": "disk", "size": 1000204886016,
     "model": "Samsung SSD 980 PRO 1TB", "tran": "nvme", "mountpoints": [None],
     "rota": False, "fstype": None,
     "children": [
         {"name": "nvme0n1p1", "type": "part", "size": 629145600,
          "mountpoints": ["/boot/efi"], "fstype": "vfat", "rota": False},
         {"name": "nvme0n1p2", "type": "part", "size": 1000000000000,
          "mountpoints": ["/", "/home"], "fstype": "btrfs", "rota": False}]},
    {"name": "sda", "type": "disk", "size": 2000398934016, "model": "ST2000DM008",
     "tran": "sata", "mountpoints": [None], "rota": True, "fstype": None,
     "children": [
         {"name": "sda1", "type": "part", "size": 2000397795328, "mountpoints": [None],
          "fstype": "crypto_LUKS", "rota": True,
          "children": [
              {"name": "luks-abc", "type": "crypt", "size": 2000395698176,
               "mountpoints": ["/mnt/datos"], "fstype": "ext4", "rota": True}]}]},
]}

ATA = """ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
  1 Raw_Read_Error_Rate     0x000f   100   100   006    Pre-fail  Always       -       0
194 Temperature_Celsius     0x0022   062   045   000    Old_age   Always       -       38 (Min/Max 20/55)
"""
NVME = "Temperature:                        41 Celsius\nTemperature Sensor 1:               44 Celsius\n"
SCSI = "Current Drive Temperature:     30 C\n"


def fake_monitor(**kwargs):
    with mock.patch("shutil.which", return_value=None):
        return StorageMonitor(**kwargs)


class Topology(unittest.TestCase):
    def setUp(self):
        self.m = fake_monitor()
        statvfs = types.SimpleNamespace(f_blocks=1000, f_bfree=400, f_bavail=300, f_frsize=4096)
        patcher = mock.patch("src.data.storage.os.statvfs", return_value=statvfs)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.disks = self.m.build_metrics(LSBLK["blockdevices"])
        self.by_name = {d.name: d for d in self.disks}

    def test_virtual_devices_are_skipped(self):
        self.assertEqual([d.name for d in self.disks], ["nvme0n1", "sda"])

    def test_kinds(self):
        self.assertEqual(self.by_name["nvme0n1"].kind, "NVMe")
        self.assertEqual(self.by_name["sda"].kind, "HDD")

    def test_rota_as_string_is_understood(self):
        self.assertEqual(StorageMonitor._kind({"name": "sdb", "tran": "sata", "rota": "1"}), "HDD")
        self.assertEqual(StorageMonitor._kind({"name": "sdb", "tran": "sata", "rota": "0"}), "SSD SATA")
        self.assertEqual(StorageMonitor._kind({"name": "sdb", "tran": "usb"}), "USB")
        self.assertEqual(StorageMonitor._kind({"name": "mmcblk0"}), "eMMC/SD")

    def test_nested_luks_volume_is_found(self):
        names = [(p.name, p.depth) for p in self.by_name["sda"].partitions]
        self.assertEqual(names, [("sda1", 1), ("luks-abc", 2)])

    def test_multi_mount_filesystem_reports_usage(self):
        # Fedora's default btrfs layout mounts one device at / and /home.
        p = {p.name: p for p in self.by_name["nvme0n1"].partitions}["nvme0n1p2"]
        self.assertEqual(p.mountpoints, ["/", "/home"])
        self.assertEqual(p.used_bytes, 600 * 4096)
        self.assertEqual(p.free_bytes, 300 * 4096)
        self.assertAlmostEqual(p.usage_percent, 600 / 900 * 100)

    def test_unmounted_partition_has_no_usage(self):
        p = self.by_name["sda"].partitions[0]
        self.assertEqual(p.mountpoints, [])
        self.assertIsNone(p.used_bytes)

    def test_legacy_single_mountpoint_key(self):
        node = {"name": "sdc1", "mountpoint": "/data"}
        self.assertEqual(StorageMonitor._mountpoints(node), ["/data"])

    def test_swap_mountpoint_is_not_stat(self):
        self.assertEqual(StorageMonitor._filesystem_usage(["[SWAP]"]), (None, None, None))


class LsblkParsing(unittest.TestCase):
    def test_invalid_json(self):
        self.assertIsNone(StorageMonitor.parse_lsblk("not json"))
        self.assertIsNone(StorageMonitor.parse_lsblk("[]"))
        self.assertIsNone(StorageMonitor.parse_lsblk('{"other": 1}'))

    def test_topology_is_cached_and_refreshed(self):
        now = [0.0]
        m = fake_monitor(topology_interval=15, clock=lambda: now[0])
        m._lsblk = "/usr/bin/lsblk"
        with mock.patch.object(m, "_run_lsblk", return_value=[]) as run:
            m.get_metrics()
            m.get_metrics()
            self.assertEqual(run.call_count, 1)
            now[0] = 16
            m.get_metrics()
            self.assertEqual(run.call_count, 2)

    def test_failed_lsblk_keeps_previous_topology(self):
        now = [0.0]
        m = fake_monitor(topology_interval=1, clock=lambda: now[0])
        with mock.patch.object(m, "_run_lsblk", return_value=[LSBLK["blockdevices"][2]]):
            self.assertEqual(len(m.get_metrics()), 1)
        now[0] = 5
        with mock.patch.object(m, "_run_lsblk", return_value=None):
            self.assertEqual(len(m.get_metrics()), 1)


class Temperature(unittest.TestCase):
    def test_ata_uses_raw_value_not_max(self):
        # v2 returned 55 (the "Max" of Min/Max) instead of 38.
        self.assertEqual(StorageMonitor.parse_smart_temperature(ATA), 38.0)

    def test_nvme_and_scsi_layouts(self):
        self.assertEqual(StorageMonitor.parse_smart_temperature(NVME), 41.0)
        self.assertEqual(StorageMonitor.parse_smart_temperature(SCSI), 30.0)

    def test_no_temperature(self):
        self.assertIsNone(StorageMonitor.parse_smart_temperature(""))
        self.assertIsNone(StorageMonitor.parse_smart_temperature("Permission denied"))

    def _sysfs(self, relative, millidegrees):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, relative)
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write(str(millidegrees))
        return tmp.name

    def test_nvme_sysfs_layout_hwmon_inside_controller(self):
        base = self._sysfs("nvme0/hwmon3/temp1_input", 38850)
        self.assertAlmostEqual(fake_monitor(nvme_sysfs=base)._nvme_temperature("nvme0n1"), 38.85)

    def test_nvme_sysfs_layout_via_device(self):
        base = self._sysfs("nvme1/device/hwmon/hwmon5/temp1_input", 41000)
        self.assertAlmostEqual(fake_monitor(nvme_sysfs=base)._nvme_temperature("nvme1n1"), 41.0)

    def test_nvme_missing_sysfs(self):
        self.assertIsNone(fake_monitor(nvme_sysfs="/nonexistent")._nvme_temperature("nvme0n1"))
        self.assertIsNone(fake_monitor()._nvme_temperature("sda"))

    def _fake_sata_sys(self, temp="27000"):
        """sysfs with one drivetemp hwmon bound to sda and an unrelated k10temp."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        dev = f"{root}/devices/pci0000:00/ata3/host2/target2:0:0/2:0:0:0"
        os.makedirs(f"{dev}/block/sda")
        os.makedirs(f"{root}/block")
        os.symlink(f"{dev}/block/sda", f"{root}/block/sda")
        hw = f"{root}/class/hwmon/hwmon4"
        os.makedirs(hw)
        with open(f"{hw}/name", "w") as f:
            f.write("drivetemp\n")
        with open(f"{hw}/temp1_input", "w") as f:
            f.write(temp)
        os.symlink(dev, f"{hw}/device")
        other = f"{root}/class/hwmon/hwmon1"
        os.makedirs(other)
        with open(f"{other}/name", "w") as f:
            f.write("k10temp\n")
        with open(f"{other}/temp1_input", "w") as f:
            f.write("99000")
        return root

    def test_drivetemp_maps_hwmon_to_its_block_device(self):
        m = fake_monitor(sys_dir=self._fake_sata_sys())
        self.assertAlmostEqual(m._drivetemp_temperature("sda"), 27.0)

    def test_drivetemp_does_not_leak_to_other_disks(self):
        m = fake_monitor(sys_dir=self._fake_sata_sys())
        self.assertIsNone(m._drivetemp_temperature("sdb"))

    def test_drivetemp_missing_sysfs(self):
        self.assertIsNone(fake_monitor(sys_dir="/nonexistent")._drivetemp_temperature("sda"))

    def test_sata_disk_uses_drivetemp_before_smartctl(self):
        m = fake_monitor(sys_dir=self._fake_sata_sys())
        with mock.patch("src.data.storage.subprocess.run") as run:
            self.assertAlmostEqual(m._temperature("sda"), 27.0)
        run.assert_not_called()

    def _smart_monitor(self, clock):
        with mock.patch("shutil.which", side_effect=lambda n: f"/usr/bin/{n}"):
            return StorageMonitor(temperature_interval=10, failure_backoff=300, clock=clock)

    def test_smartctl_output_is_parsed_even_with_nonzero_exit(self):
        m = self._smart_monitor(lambda: 0.0)
        result = types.SimpleNamespace(returncode=4, stdout=ATA, stderr="")
        with mock.patch("src.data.storage.subprocess.run", return_value=result):
            self.assertEqual(m._smart_temperature("sda"), 38.0)

    def test_smartctl_success_is_cached_for_interval(self):
        now = [0.0]
        m = self._smart_monitor(lambda: now[0])
        result = types.SimpleNamespace(returncode=0, stdout=ATA, stderr="")
        with mock.patch("src.data.storage.subprocess.run", return_value=result) as run:
            m._smart_temperature("sda")
            now[0] = 5
            m._smart_temperature("sda")
            self.assertEqual(run.call_count, 1)
            now[0] = 11
            m._smart_temperature("sda")
            self.assertEqual(run.call_count, 2)

    def test_smartctl_failure_backs_off(self):
        now = [0.0]
        m = self._smart_monitor(lambda: now[0])
        result = types.SimpleNamespace(returncode=2, stdout="Permission denied", stderr="")
        with mock.patch("src.data.storage.subprocess.run", return_value=result) as run:
            self.assertIsNone(m._smart_temperature("sda"))
            now[0] = 60
            m._smart_temperature("sda")
            self.assertEqual(run.call_count, 1)
            now[0] = 301
            m._smart_temperature("sda")
            self.assertEqual(run.call_count, 2)

    def test_unsafe_device_name_is_rejected(self):
        m = self._smart_monitor(lambda: 0.0)
        with mock.patch("src.data.storage.subprocess.run") as run:
            self.assertIsNone(m._smart_temperature("../etc/passwd"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
