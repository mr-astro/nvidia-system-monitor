"""Acquisition integration for ordering/layout, using only simulated hardware."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from src.data.storage import LSBLK_COLUMNS
from tests.test_storage import fake_monitor
from tests.storage_fixtures import disk, part, root_mount


FSID = '11111111-2222-3333-4444-555555555555'


class TopologyIntegration(unittest.TestCase):
    def monitor(self, nodes, mounts=None, sys_dir='/synthetic/missing-sysfs'):
        now = [0.0]
        mounts = mounts if mounts is not None else {}
        monitor = fake_monitor(clock=lambda: now[0], sys_dir=sys_dir)
        patches = [mock.patch.object(monitor, '_run_lsblk', side_effect=lambda: copy.deepcopy(nodes)),
                   mock.patch.object(monitor, '_read_mountinfo', side_effect=lambda: dict(mounts)),
                   mock.patch.object(monitor, '_temperature', return_value=None),
                   mock.patch('src.data.storage.os.statvfs', return_value=SimpleNamespace(
                       f_blocks=100, f_bfree=40, f_bavail=30, f_frsize=4096))]
        for patch in patches:
            patch.start(); self.addCleanup(patch.stop)
        return monitor, now

    def btrfs_sysfs(self, numbers=('8:1', '8:17')):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        base = root / 'fs/btrfs' / FSID
        for index, number in enumerate(numbers, 1):
            device = base / 'devices' / f'device{index}'
            device.mkdir(parents=True)
            (device / 'dev').write_text(number)
            (base / 'devinfo' / str(index)).mkdir(parents=True)
        return root, base

    def btrfs_nodes(self):
        return [disk(children=[part(fstype='btrfs', uuid=FSID, mountpoints=['/', '/home'])]),
                disk('sdb', '8:16', children=[part('sdb1', parent='sdb', number='8:17', fstype='btrfs', uuid=FSID)])]

    def test_model_uses_cached_topology_and_preserves_partition_metrics(self):
        nodes = [disk(children=[part(mountpoints=['/data'])])]
        mounts = {'/data': root_mount()}
        monitor, now = self.monitor(nodes, mounts)
        first = monitor.get_metrics()[0]
        self.assertTrue(first.layout.available)
        self.assertEqual(first.partitions[0].name, 'sda1')
        self.assertEqual(first.partitions[0].used_bytes, 60 * 4096)
        self.assertAlmostEqual(first.partitions[0].usage_percent, 60 / 90 * 100)
        for value in range(1, 15):
            now[0] = value
            current = monitor.get_metrics()[0]
            self.assertIs(current.layout, first.layout)
            self.assertEqual(current.layout.source.acquired_at, 0)
        self.assertEqual(monitor._run_lsblk.call_count, 1)
        now[0] = 15
        refreshed = monitor.get_metrics()[0]
        self.assertIsNot(refreshed.layout, first.layout)
        self.assertEqual(refreshed.layout.source.acquired_at, 15)
        self.assertEqual(monitor._run_lsblk.call_count, 2)

    def test_root_role_changes_only_after_mount_and_topology_are_correlated(self):
        nodes = [disk('nvme0n1', '259:0', 'nvme', children=[part('nvme0n1p1', parent='nvme0n1', number='259:1')]),
                 disk(children=[part(mountpoints=['/'])])]
        mounts = {'/': root_mount()}
        monitor, now = self.monitor(nodes, mounts)
        self.assertEqual([d.name for d in monitor.get_metrics()], ['sda', 'nvme0n1'])
        mounts['/'] = root_mount('/dev/nvme0n1p1', '259:1', mount_id='11')
        now[0] = 1
        before_refresh = monitor.get_metrics()
        self.assertFalse(before_refresh[0].order.root.available)
        self.assertEqual(monitor._run_lsblk.call_count, 1)
        nodes[0]['children'][0]['mountpoints'] = ['/']
        nodes[1]['children'][0]['mountpoints'] = []
        now[0] = 15
        current = monitor.get_metrics()
        self.assertEqual(current[0].name, 'nvme0n1')
        self.assertEqual(current[0].order.root.primary, 'nvme0n1')

    def test_hotplug_removes_only_disconnected_layout_and_keeps_other_identity(self):
        nodes = [disk(children=[part()])]
        monitor, now = self.monitor(nodes)
        first = monitor.get_metrics()[0]
        binding = monitor.presentation_bindings()['sda']
        nodes.append(disk('sdb', '8:16', 'usb'))
        now[0] = 15
        connected = monitor.get_metrics()
        self.assertEqual([d.name for d in connected], ['sda', 'sdb'])
        self.assertEqual(monitor.presentation_bindings()['sda'], binding)
        nodes.pop()
        now[0] = 30
        self.assertEqual([d.name for d in monitor.get_metrics()], ['sda'])
        self.assertNotIn('sdb', monitor._layout_cache)
        self.assertEqual(first.layout.source.acquired_at, 0)

    def test_missing_geometry_keeps_legacy_partition_usage_and_no_global_error(self):
        child = part(mountpoints=['/data']); child.pop('start')
        monitor, now = self.monitor([disk(children=[child])], {'/data': root_mount()})
        value = monitor.get_metrics()[0]
        self.assertFalse(value.layout.available)
        self.assertIn('Inicio', value.layout.reason)
        self.assertEqual(value.partitions[0].used_bytes, 60 * 4096)
        self.assertNotIn('storage', monitor.errors)

    def test_malformed_optional_metadata_cannot_break_existing_filesystem_metrics(self):
        for target, field, value in (('disk', 'pttype', []), ('disk', 'kname', ['sda']),
                                     ('part', 'kname', {'bad': 'data'}), ('part', 'parttype', ['bad'])):
            with self.subTest(target=target, field=field):
                node = disk(children=[part(mountpoints=['/data'])])
                (node if target == 'disk' else node['children'][0])[field] = value
                monitor, now = self.monitor([node], {'/data': root_mount()})
                metrics = monitor.get_metrics()[0]
                self.assertFalse(metrics.layout.available)
                self.assertEqual(metrics.partitions[0].used_bytes, 60 * 4096)
                self.assertNotIn('storage', monitor.errors)

    def test_duplicate_disk_records_do_not_publish_verified_physical_layouts(self):
        node = disk(children=[part(mountpoints=['/'])])
        monitor, now = self.monitor([node, copy.deepcopy(node)], {'/': root_mount()})
        values = monitor.get_metrics()
        self.assertTrue(all(not value.layout.available for value in values))
        self.assertTrue(all(not value.order.root.available for value in values))

    def test_topology_failure_preserves_original_geometry_age_and_diagnostics(self):
        monitor, now = self.monitor([disk(children=[part(mountpoints=['/'])])], {'/': root_mount()})
        first = monitor.get_metrics()[0]
        now[0] = 16
        monitor._run_lsblk.side_effect = None
        monitor._run_lsblk.return_value = None
        failed = monitor.get_metrics()[0]
        self.assertEqual(failed.layout.source.acquired_at, 0)
        self.assertIn('topology', monitor.errors)
        self.assertFalse(failed.layout.current(16))
        self.assertFalse(failed.order.root.available)
        self.assertEqual(failed.layout.regions, first.layout.regions)
        self.assertIsNone(first.layout.source.error)

    def test_btrfs_multi_root_inventory_is_read_only_on_topology_refresh(self):
        root, base = self.btrfs_sysfs()
        monitor, now = self.monitor(self.btrfs_nodes(), {'/': root_mount(fstype='btrfs', number='0:1')}, str(root))
        with mock.patch.object(monitor, '_read_btrfs_members', wraps=monitor._read_btrfs_members) as read:
            first = monitor.get_metrics()
            self.assertTrue(all(d.order.priority == 0 for d in first))
            self.assertEqual(first[0].order.root.disks, ('sda', 'sdb'))
            self.assertIsNone(first[0].order.root.primary)
            for value in range(1, 15):
                now[0] = value
                monitor.get_metrics()
            self.assertEqual(read.call_count, 1)
            self.assertEqual(monitor._run_lsblk.call_count, 1)
            now[0] = 15
            monitor.get_metrics()
            self.assertEqual(read.call_count, 2)

    def test_btrfs_missing_member_is_not_guessed_from_shared_uuid(self):
        root, base = self.btrfs_sysfs()
        (base / 'devices/device2/dev').unlink()
        monitor, now = self.monitor(self.btrfs_nodes(), {'/': root_mount(fstype='btrfs', number='0:1')}, str(root))
        metrics = monitor.get_metrics()
        self.assertTrue(all(d.order.priority != 0 for d in metrics))
        self.assertFalse(metrics[0].order.root.available)
        self.assertIn('Btrfs', metrics[0].order.root.reason)

    def test_btrfs_degraded_inventory_has_explicit_diagnostic(self):
        root, base = self.btrfs_sysfs()
        (base / 'devinfo/3').mkdir()
        monitor, now = self.monitor(self.btrfs_nodes(), {'/': root_mount(fstype='btrfs', number='0:1')}, str(root))
        self.assertIn('degradado', monitor.get_metrics()[0].order.root.reason)

    def test_changed_or_duplicate_btrfs_numbers_are_unavailable(self):
        root, base = self.btrfs_sysfs()
        monitor, now = self.monitor(self.btrfs_nodes(), {'/': root_mount(fstype='btrfs', number='0:1')}, str(root))
        monitor._valid_mounts = monitor._read_mountinfo()
        monitor._bind_mounts(monitor._run_lsblk(), monitor._valid_mounts)
        original = Path.read_text
        count = [0]
        def racing(path, *args, **kwargs):
            if path.name == 'dev':
                count[0] += 1
                if count[0] > 2:
                    return '8:33' if path.parent.name == 'device2' else '8:1'
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'read_text', racing):
            self.assertIn('cambió', monitor._read_btrfs_members(monitor._run_lsblk())[FSID].reason)
        (base / 'devices/device2/dev').write_text('8:1')
        self.assertIsNotNone(monitor._read_btrfs_members(monitor._run_lsblk())[FSID].reason)

    def test_layout_is_invalidated_when_acquisition_changes_attachment(self):
        monitor, now = self.monitor([disk(children=[part()])])
        def changed(name):
            monitor._bindings.pop(name, None)
        monitor._temperature.side_effect = changed
        value = monitor.get_metrics()[0]
        self.assertFalse(value.layout.available)
        self.assertIsNone(value.layout.identity)

    def test_lsblk_requests_geometry_in_the_same_process_and_normalizes_disk_seq(self):
        monitor = fake_monitor()
        monitor._lsblk = '/synthetic/lsblk'
        node = disk(children=[part()]); node['disk-seq'] = node.pop('diskseq')
        response = SimpleNamespace(returncode=0, stdout=json.dumps({'blockdevices': [node]}))
        with mock.patch('src.data.storage.subprocess.run', return_value=response) as run:
            value, = monitor._run_lsblk()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(value['diskseq'], node['disk-seq'])
        columns = run.call_args.args[0][-1].split(',')
        for field in ('TYPE', 'SIZE', 'START', 'LOG-SEC', 'PKNAME', 'PARTUUID', 'PARTTYPE', 'PTTYPE', 'PARTN', 'UUID', 'SUBSYSTEMS'):
            self.assertIn(field, columns)

    def test_optional_field_fallback_does_not_drop_multimounts_or_other_metadata(self):
        monitor = fake_monitor(); monitor._lsblk = '/synthetic/lsblk'
        bad = SimpleNamespace(returncode=1, stdout='', stderr='lsblk: unknown column: START')
        success = SimpleNamespace(returncode=0, stdout='{"blockdevices": []}')
        with mock.patch('src.data.storage.subprocess.run', side_effect=[bad, success]) as run:
            self.assertEqual(monitor._run_lsblk(), [])
        self.assertEqual(run.call_count, 2)
        self.assertNotIn('START', monitor._lsblk_columns.split(','))
        self.assertIn('MOUNTPOINTS', monitor._lsblk_columns.split(','))
        self.assertIn('PARTUUID', monitor._lsblk_columns.split(','))
        self.assertIn('DISK-SEQ', monitor._lsblk_columns.split(','))
        with mock.patch('src.data.storage.subprocess.run', return_value=success) as run:
            monitor._run_lsblk()
        self.assertEqual(run.call_count, 1)

    def test_multiple_unsupported_fields_are_removed_without_dropping_legacy_metrics(self):
        monitor = fake_monitor(); monitor._lsblk = '/synthetic/lsblk'
        responses = [SimpleNamespace(returncode=1, stdout='', stderr='unknown column: PARTN'),
                     SimpleNamespace(returncode=1, stdout='', stderr='unknown column: PARTUUID'),
                     SimpleNamespace(returncode=0, stdout='{"blockdevices": []}')]
        with mock.patch('src.data.storage.subprocess.run', side_effect=responses) as run:
            monitor._run_lsblk()
        self.assertEqual(run.call_count, 3)
        self.assertNotIn('PARTN', monitor._lsblk_columns.split(','))
        self.assertNotIn('PARTUUID', monitor._lsblk_columns.split(','))
        for column in ('NAME', 'SIZE', 'FSTYPE', 'MOUNTPOINTS', 'LABEL', 'START'):
            self.assertIn(column, monitor._lsblk_columns.split(','))


if __name__ == '__main__':
    unittest.main()
