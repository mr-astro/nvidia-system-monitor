import os
import tempfile
import types
import unittest
import subprocess
import json
from unittest import mock

from src.data.storage import StorageMonitor
from src.data.errors import SourceState, freshness
from src.data.udisks import TemperatureReading
from src.data.identity import DeviceIdentity

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
    kwargs.setdefault("udisks", False)
    kwargs.setdefault('sys_dir', '/synthetic/missing-sysfs')
    kwargs.setdefault('identity_reader', lambda name, node: DeviceIdentity(
        diskseq=sum(map(ord, name)) + 1, serial='synthetic-' + name, model='synthetic'))
    with mock.patch("shutil.which", return_value=None):
        return StorageMonitor(**kwargs)


class Topology(unittest.TestCase):
    def setUp(self):
        self.m = fake_monitor()
        statvfs = types.SimpleNamespace(f_blocks=1000, f_bfree=400, f_bavail=300, f_frsize=4096)
        patcher = mock.patch("src.data.storage.os.statvfs", return_value=statvfs)
        patcher.start()
        self.addCleanup(patcher.stop)
        mounts = {
            "/": ("10", "0:1", "/", "btrfs", "/dev/nvme0n1p2"),
            "/home": ("11", "0:1", "/home", "btrfs", "/dev/nvme0n1p2"),
            "/boot/efi": ("12", "259:1", "/", "vfat", "/dev/nvme0n1p1"),
            "/mnt/datos": ("13", "253:0", "/", "ext4", "/dev/luks-abc"),
        }
        self.m._valid_mounts = mounts
        self.m._bind_mounts(LSBLK["blockdevices"], mounts)
        patcher = mock.patch.object(self.m, "_read_mountinfo", return_value=mounts)
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


class StorageReliability(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]
        self.monitor = fake_monitor(clock=lambda: self.now[0])
        self.devices = [LSBLK["blockdevices"][2]]
        self.mounts = {
            "/": ("10", "0:1", "/", "btrfs", "/dev/nvme0n1p2"),
            "/home": ("11", "0:1", "/home", "btrfs", "/dev/nvme0n1p2"),
            "/boot/efi": ("12", "259:1", "/", "vfat", "/dev/nvme0n1p1"),
        }
        for patcher in (
            mock.patch.object(self.monitor, "_read_mountinfo", side_effect=lambda: dict(self.mounts)),
            mock.patch.object(self.monitor, "_run_lsblk", return_value=self.devices),
            mock.patch.object(self.monitor, "_temperature", return_value=None),
            mock.patch("src.data.storage.os.statvfs", return_value=types.SimpleNamespace(
                f_blocks=1000, f_bfree=400, f_bavail=300, f_frsize=4096)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _partition(self):
        return self.monitor.get_metrics()[0].partitions[1]

    def test_topology_failure_keeps_values_age_and_warning_until_recovery(self):
        first = self._partition()
        self.now[0] = 16
        with mock.patch.object(self.monitor, "_run_lsblk", return_value=None) as run:
            second = self._partition()
            self.assertEqual(first.used_bytes, second.used_bytes)
            state = self.monitor.sources["topology"]
            self.assertEqual(state.acquired_at, 0)
            self.assertTrue(state.stale(16))
            self.assertEqual(state.age(16), 16)
            self.now[0] = 20
            self._partition()
            self.assertIn("topology", self.monitor.errors)
            self.assertEqual(run.call_count, 1)
        self.now[0] = 31
        self._partition()
        self.assertNotIn("topology", self.monitor.errors)
        self.assertEqual(self.monitor.sources["topology"].acquired_at, 31)

    def test_topology_cache_has_its_own_cadence(self):
        self._partition()
        self.now[0] = 14.9
        self._partition()
        state = self.monitor.sources["topology"]
        self.assertEqual(state.acquired_at, 0)
        self.assertEqual(state.max_age, freshness(15, 2))
        self.assertFalse(state.stale(14.9))
        self.assertTrue(state.stale(state.max_age + 0.1))

    def test_disappeared_and_replaced_mounts_invalidate_usage_without_lsblk(self):
        for replacement in (None, ("99", "0:9", "/", "btrfs", "/dev/sdc1")):
            with self.subTest(replacement=replacement):
                self.mounts["/"] = ("10", "0:1", "/", "btrfs", "/dev/nvme0n1p2")
                self.mounts["/home"] = ("11", "0:1", "/home", "btrfs", "/dev/nvme0n1p2")
                self.now[0] += 20
                self.assertIsNotNone(self._partition().used_bytes)
                self.mounts.pop("/home")
                if replacement is None:
                    self.mounts.pop("/")
                else:
                    self.mounts["/"] = replacement
                self.now[0] += 1
                partition = self._partition()
                self.assertIsNone(partition.used_bytes)
                self.assertIsNone(partition.usage_percent)
                self.assertEqual(partition.mountpoints, [])
                self.assertIn("usage:nvme0n1p2", self.monitor.errors)
                self.assertNotIn("nvme0n1p2", self.monitor._usage_cache)

    def test_one_remaining_btrfs_mount_can_still_supply_usage(self):
        self._partition()
        self.mounts.pop("/home")
        self.now[0] = 1
        partition = self._partition()
        self.assertIsNotNone(partition.used_bytes)
        self.assertEqual(partition.mountpoints, ["/"])

    def test_statvfs_failure_keeps_old_values_and_original_acquisition_time(self):
        first = self._partition()
        self.now[0] = 1
        with mock.patch("src.data.storage.os.statvfs", side_effect=OSError("temporarily unreadable")):
            second = self._partition()
        self.assertEqual(second.used_bytes, first.used_bytes)
        self.assertEqual(self.monitor.sources["usage:nvme0n1p2"].acquired_at, 0)
        self.assertIn("usage:nvme0n1p2", self.monitor.errors)
        self.now[0] = 2
        self._partition()
        self.assertNotIn("usage:nvme0n1p2", self.monitor.errors)

    def test_mount_changes_during_statvfs_reject_result(self):
        self._partition()
        before = dict(self.mounts)
        after = {"/": ("88", "0:9", "/", "btrfs", "/dev/sdc1")}
        self.now[0] = 1
        with mock.patch.object(self.monitor, "_read_mountinfo", side_effect=[before, before, after]):
            self.assertIsNone(self._partition().used_bytes)

    def test_lvm_alias_and_major_minor_match(self):
        self.monitor._mount_identities.clear()
        nodes = [{"name": "fedora-root", "mountpoints": ["/root"]},
                 {"name": "dm-1", "maj:min": "253:1", "mountpoints": ["/data"]}]
        mounts = {"/root": ("20", "253:0", "/", "ext4", "/dev/mapper/fedora-root"),
                  "/data": ("21", "253:1", "/", "ext4", "/dev/other-alias")}
        self.monitor._bind_mounts(nodes, mounts)
        self.assertEqual(len(self.monitor._mount_identities), 2)

    def test_mountinfo_escaped_paths_and_optional_fields(self):
        text = "10 1 253:0 /dir\\040name /mnt\\040data rw shared:1 - ext4 /dev/mapper/vg-lv rw\n"
        with mock.patch("builtins.open", mock.mock_open(read_data=text)):
            mounts = StorageMonitor._read_mountinfo(self.monitor)
        self.assertIn("/mnt data", mounts)
        self.assertEqual(mounts["/mnt data"][-1], "/dev/mapper/vg-lv")


class TemperatureSelection(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.reading = TemperatureReading(40, SourceState(95, 95, 752))
        self.provider = types.SimpleNamespace(read=mock.Mock(side_effect=lambda name, context=None: self.reading),
                                              invalidate=mock.Mock(), prepare=mock.Mock(), rebind=mock.Mock())
        self.monitor = fake_monitor(udisks=self.provider, clock=lambda: self.now[0], sys_dir='/nonexistent')
        self.monitor._smartctl = '/usr/bin/smartctl'
        self.monitor._prepare_devices([{'name': 'sda', 'type': 'disk'}])

    def test_udisks_valid_avoids_smartctl_and_preserves_permission_diagnostic(self):
        self.monitor.sources['smart:sda'] = SourceState(max_age=15, error='Permission denied')
        self.monitor.errors['smart:sda'] = 'Permission denied'
        with mock.patch('src.data.storage.subprocess.run') as run:
            self.assertEqual(self.monitor._temperature('sda'), 40)
        run.assert_not_called()
        self.assertEqual(self.monitor._temperature_sources['sda'], 'udisks:sda')
        self.assertIn('Permission denied', self.monitor.errors['smart:sda'])
        self.assertEqual(self.monitor.sources['udisks:sda'].acquired_at, 95)

    def test_new_udisks_reading_updates_value_without_pairing_old_value_with_new_time(self):
        self.assertEqual(self.monitor._temperature('sda'), 40)
        self.now[0] = 110
        self.reading = TemperatureReading(42, SourceState(110, 110, 752))
        self.assertEqual(self.monitor._temperature('sda'), 42)
        self.assertEqual(self.monitor.sources['udisks:sda'].acquired_at, 110)

    def test_udisks_expiring_during_dbus_call_still_evaluates_fallback(self):
        self.now[0] = 4

        def delayed(name, context=None):
            self.now[0] = 6
            return TemperatureReading(40, SourceState(0, 0, 5))

        self.provider.read.side_effect = delayed
        with mock.patch('src.data.storage.subprocess.run', return_value=types.SimpleNamespace(returncode=0, stdout=ATA)) as run:
            self.assertEqual(self.monitor._temperature('sda'), 38)
        run.assert_called_once()
        self.assertEqual(self.monitor._temperature_sources['sda'], 'smart:sda')

    def test_stale_and_unavailable_udisks_use_recent_smart(self):
        for reading in (TemperatureReading(40, SourceState(-700, -700, 752)),
                        TemperatureReading(None, SourceState(error='bus unavailable')),
                        TemperatureReading(40, SourceState(95, 95, 752, 'bus failed'))):
            with self.subTest(reading=reading):
                self.reading = reading
                with mock.patch('src.data.storage.subprocess.run', return_value=types.SimpleNamespace(returncode=0, stdout=ATA)):
                    self.assertEqual(self.monitor._temperature('sda'), 38)
                self.assertEqual(self.monitor._temperature_sources['sda'], 'smart:sda')

    def test_valid_older_preferred_source_does_not_replace_recent_smart_then_takes_over(self):
        self.reading = TemperatureReading(None, SourceState(error='not available'))
        with mock.patch('src.data.storage.subprocess.run', return_value=types.SimpleNamespace(returncode=0, stdout=ATA)):
            self.assertEqual(self.monitor._temperature('sda'), 38)
        self.now[0] = 101
        self.reading = TemperatureReading(40, SourceState(90, 90, 752))
        with mock.patch('src.data.storage.subprocess.run') as run:
            self.assertEqual(self.monitor._temperature('sda'), 38)
            self.now[0] = 116
            self.assertEqual(self.monitor._temperature('sda'), 40)
        run.assert_not_called()
        self.assertEqual(self.monitor._temperature_sources['sda'], 'udisks:sda')

    def test_sysfs_priority_and_failure_does_not_rejuvenate_last_measurement(self):
        with mock.patch.object(self.monitor, '_drivetemp_temperature', return_value=45):
            self.assertEqual(self.monitor._temperature('sda'), 45)
        self.provider.read.assert_not_called()
        self.now[0] = 101
        self.assertEqual(self.monitor._temperature('sda'), 45)
        self.assertEqual(self.monitor.sources['temperature:sda'].acquired_at, 100)
        self.assertIn('temperature:sda', self.monitor.errors)
        self.now[0] = 103
        self.assertEqual(self.monitor._temperature('sda'), 40)
        with mock.patch.object(self.monitor, '_drivetemp_temperature', return_value=46):
            self.assertEqual(self.monitor._temperature('sda'), 46)
        self.assertNotIn('temperature:sda', self.monitor.errors)

    def test_invalidated_udisks_identity_discards_previous_temperature(self):
        self.assertEqual(self.monitor._temperature('sda'), 40)
        self.reading = TemperatureReading(None, SourceState(error='identity changed'))
        self.monitor._smartctl = None
        self.assertIsNone(self.monitor._temperature('sda'))

    def test_all_expired_sources_preserve_age_and_finite_last_value_for_diagnostic(self):
        self.reading = TemperatureReading(40, SourceState(-700, -700, 752))
        self.monitor._smartctl = None
        self.assertEqual(self.monitor._temperature('sda'), 40)
        self.assertTrue(self.monitor.sources['udisks:sda'].stale(100))
        self.assertEqual(self.monitor.sources['udisks:sda'].age(100), 800)
        self.assertNotIn('smart:sda', self.monitor.sources)

    def test_disk_metrics_publish_selected_source_and_prune_disappeared_states(self):
        nodes = [{'name': 'sda', 'type': 'disk', 'tran': 'sata'}]
        with mock.patch.object(self.monitor, '_run_lsblk', return_value=nodes), \
                mock.patch.object(self.monitor, '_read_mountinfo', return_value={}):
            disk, = self.monitor.get_metrics()
        self.assertEqual(disk.temperature_source, 'udisks:sda')
        self.now[0] += 20
        with mock.patch.object(self.monitor, '_run_lsblk', return_value=[]), \
                mock.patch.object(self.monitor, '_read_mountinfo', return_value={}):
            self.assertEqual(self.monitor.get_metrics(), [])
        self.assertNotIn('udisks:sda', self.monitor.sources)
        self.assertNotIn('sda', self.monitor._selected_temperatures)


class StorageCommandFailures(unittest.TestCase):
    def test_labels_use_existing_lsblk_query_and_preserve_missing_values(self):
        monitor = fake_monitor(clock=lambda: 10.0)
        monitor._lsblk = '/usr/bin/lsblk'
        output = {'blockdevices': [{'name': 'sda', 'type': 'disk', 'children': [
            {'name': 'sda1', 'fstype': 'ext4', 'label': ' Datos '},
            {'name': 'sda2', 'fstype': 'ntfs', 'label': None},
            {'name': 'sda3', 'fstype': 'btrfs'},
        ]}]}
        result = types.SimpleNamespace(returncode=0, stdout=json.dumps(output))
        with mock.patch.object(monitor, '_read_mountinfo', return_value={}), mock.patch.object(
            monitor, '_temperature', return_value=None
        ), mock.patch('src.data.storage.subprocess.run', return_value=result) as run:
            disk, = monitor.get_metrics()
        self.assertEqual([part.label for part in disk.partitions], ['Datos', None, None])
        self.assertIn('LABEL', run.call_args.args[0][-1].split(','))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(monitor.sources['topology'].acquired_at, 10)

    def test_lsblk_errors_missing_binary_and_malformed_json(self):
        monitor = fake_monitor()
        self.assertIsNone(monitor._run_lsblk())
        self.assertIn("no disponible", monitor.errors["topology"])
        monitor._lsblk = "/usr/bin/lsblk"
        for failure in (OSError("failed"), subprocess.TimeoutExpired("lsblk", 2)):
            with self.subTest(failure=failure), mock.patch("src.data.storage.subprocess.run", side_effect=failure):
                self.assertIsNone(monitor._run_lsblk())
                self.assertIn("lsblk", monitor.errors["topology"])
        for result in (types.SimpleNamespace(returncode=1, stdout="", stderr="permission denied"),
                       types.SimpleNamespace(returncode=0, stdout='{"blockdevices": [42]}')):
            with mock.patch("src.data.storage.subprocess.run", return_value=result):
                self.assertIsNone(monitor._run_lsblk())
                self.assertIn("topology", monitor.errors)

    def test_legacy_column_fallback_clears_first_failure(self):
        monitor = fake_monitor()
        monitor._lsblk = "/usr/bin/lsblk"
        results = [types.SimpleNamespace(returncode=1, stdout="", stderr="unknown column MOUNTPOINTS"),
                   types.SimpleNamespace(returncode=0, stdout='{"blockdevices": []}')]
        with mock.patch("src.data.storage.subprocess.run", side_effect=results) as run:
            self.assertEqual(monitor._run_lsblk(), [])
        self.assertEqual(run.call_count, 2)
        self.assertNotIn("topology", monitor.errors)

    def test_smart_failure_preserves_valid_reading_and_warning_during_backoff(self):
        now = [0.0]
        monitor = fake_monitor(clock=lambda: now[0])
        monitor._smartctl = "/usr/bin/smartctl"
        with mock.patch("src.data.storage.subprocess.run", return_value=types.SimpleNamespace(returncode=8, stdout=ATA)):
            self.assertEqual(monitor._smart_temperature("sda"), 38)
        self.assertEqual(monitor.sources["smart:sda"].max_age, freshness(10, 2))
        now[0] = 9
        self.assertFalse(monitor.sources["smart:sda"].stale(9))
        with mock.patch("src.data.storage.subprocess.run", side_effect=subprocess.TimeoutExpired("smartctl", 2)) as run:
            now[0] = 10
            self.assertEqual(monitor._smart_temperature("sda"), 38)
            now[0] = 100
            self.assertEqual(monitor._smart_temperature("sda"), 38)
            self.assertEqual(run.call_count, 1)
        self.assertEqual(monitor.sources["smart:sda"].acquired_at, 0)
        self.assertTrue(monitor.sources["smart:sda"].stale(100))
        now[0] = 310
        with mock.patch("src.data.storage.subprocess.run", return_value=types.SimpleNamespace(returncode=0, stdout=ATA)):
            monitor._smart_temperature("sda")
        self.assertNotIn("smart:sda", monitor.errors)
        self.assertEqual(monitor.sources["smart:sda"].acquired_at, 310)

    def test_smart_command_error_bits_report_warning_without_discarding_temperature(self):
        monitor = fake_monitor(clock=lambda: 5.0)
        monitor._smartctl = "/usr/bin/smartctl"
        with mock.patch("src.data.storage.subprocess.run", return_value=types.SimpleNamespace(
            returncode=4, stdout=ATA, stderr="checksum error"
        )):
            self.assertEqual(monitor._smart_temperature("sda"), 38)
        self.assertIn("checksum error", monitor.errors["smart:sda"])
        self.assertEqual(monitor.sources["smart:sda"].acquired_at, 5)


if __name__ == "__main__":
    unittest.main()
