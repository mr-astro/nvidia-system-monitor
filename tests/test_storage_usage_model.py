"""Filesystem scopes are never summed or substituted by physical disk capacity."""
import copy
from dataclasses import replace
import unittest

from src.data.errors import SourceState
from src.data.storage import DiskMetrics, PartitionMetrics
from src.data.storage_order import DiskOrder, RootBacking
from src.gui.storage_usage_model import prepare_usage


def filesystem(name='sda1', **overrides):
    values = dict(fstype='ext4', mountpoints=['/datos'], used_bytes=60,
                  free_bytes=20, usage_percent=75)
    values.update(overrides)
    return PartitionMetrics(name, **values)


def usage_fixture(*parts, direct=None):
    disk = DiskMetrics('sda', size_bytes=10**12, partitions=list(parts), direct_filesystem=direct)
    sources = {'usage:' + p.name: SourceState(10, 11, 20) for p in (*parts, *((direct,) if direct else ()))}
    return disk, sources


class FilesystemUsageModels(unittest.TestCase):
    def model(self, *parts, now=11, direct=None, errors=None, sources=None):
        disk, readings = usage_fixture(*parts, direct=direct)
        return prepare_usage(disk, readings if sources is None else sources, errors or {}, now)

    def test_one_measured_filesystem_uses_published_df_percentage_not_disk_size(self):
        model = self.model(filesystem())
        self.assertEqual(model.state, 'current')
        self.assertEqual(model.fraction, .75)
        self.assertIn('sda1', model.scope)
        self.assertIn('ext4', model.scope)
        self.assertIn('/datos', model.scope)
        self.assertEqual(model.summary, '75,0% usado · 25,0% disponible')
        self.assertIn('Denominador: usado + disponible', model.details)
        self.assertIn('nunca utilización global', model.details)

    def test_several_independent_measured_filesystems_are_neutral(self):
        model = self.model(filesystem(), filesystem('sda2', used_bytes=20, free_bytes=80, usage_percent=20))
        self.assertIsNone(model.fraction)
        self.assertEqual(model.state, 'multiple')
        self.assertIn('sda1 · ext4 · 75.0%', model.details)
        self.assertIn('sda2 · ext4 · 20.0%', model.details)

    def test_btrfs_subvolume_mounts_share_one_published_measurement(self):
        model = self.model(filesystem(fstype='btrfs', mountpoints=['/', '/home', '/var']))
        self.assertEqual(model.fraction, .75)
        self.assertIn('3 montajes compartidos', model.scope)
        self.assertIn('Montajes: /, /home, /var', model.details)
        self.assertIn('no se suman subvolúmenes', model.details)

    def test_duplicate_nodes_and_bind_mounts_are_counted_once_without_mutating_input(self):
        for fstype in ('ext4', 'btrfs'):
            parts = [filesystem(fstype=fstype, mountpoints=['/']), filesystem(fstype=fstype, mountpoints=['/home'])]
            disk, sources = usage_fixture(*parts)
            original = copy.deepcopy(disk)
            model = prepare_usage(disk, sources, {}, 11)
            self.assertEqual(model.fraction, .75)
            self.assertIn('Montajes: /, /home', model.details)
            self.assertEqual(disk, original)

    def test_equal_readings_do_not_prove_btrfs_identity_across_different_nodes(self):
        model = self.model(filesystem(fstype='btrfs'), filesystem('sda2', fstype='btrfs'))
        self.assertEqual(model.state, 'multiple')
        self.assertIsNone(model.fraction)

    def test_conflicting_repeated_node_is_explicitly_ambiguous(self):
        model = self.model(filesystem(), filesystem(used_bytes=20, free_bytes=80, usage_percent=20))
        self.assertEqual(model.state, 'ambiguous')
        self.assertIsNone(model.fraction)
        self.assertIn('contradictorias', model.reason)

    def test_unmounted_ntfs_never_invents_zero_or_uses_cached_values(self):
        model = self.model(filesystem(fstype='ntfs', mountpoints=[]))
        self.assertEqual(model.state, 'unavailable')
        self.assertIsNone(model.fraction)
        self.assertIn('sin montar', model.scope)

    def test_one_measurable_scope_can_coexist_with_an_unmounted_filesystem(self):
        model = self.model(filesystem(), filesystem('sda2', fstype='ntfs', mountpoints=[],
                                                   used_bytes=None, free_bytes=None, usage_percent=None))
        self.assertEqual(model.fraction, .75)
        self.assertIn('FS sda1', model.scope)
        self.assertNotIn('sda2', model.scope)
        self.assertIn('los demás están en detalles', model.details)

    def test_stale_reading_has_no_colored_sectors_and_keeps_last_reading_as_diagnostic(self):
        model = self.model(filesystem(), now=31)
        self.assertEqual(model.state, 'stale')
        self.assertIsNone(model.fraction)
        self.assertIn('75.0%', model.details)
        self.assertIn('hace 21 s', model.reason)

    def test_error_in_source_or_snapshot_wins_over_recent_cached_values(self):
        for sources, errors in [({'usage:sda1': SourceState(10, 11, 20, 'falló statvfs')}, {}),
                                ({'usage:sda1': SourceState(10, 11, 20)}, {'usage:sda1': 'cambió el montaje'})]:
            model = self.model(filesystem(), sources=sources, errors=errors)
            self.assertEqual(model.state, 'error')
            self.assertIsNone(model.fraction)
            self.assertIn(next(iter(errors.values())) if errors else 'falló statvfs', model.details)

    def test_missing_data_or_source_is_not_zero_usage(self):
        for overrides, sources in [({'used_bytes': None}, None), ({'free_bytes': None}, None),
                                    ({'usage_percent': None}, None), ({}, {}),
                                    ({}, {'usage:sda1': SourceState()})]:
            model = self.model(filesystem(**overrides), sources=sources)
            self.assertIsNone(model.fraction)
            self.assertEqual(model.state, 'unavailable')

    def test_verified_zero_and_full_usage_are_valid_extremes(self):
        for used, free, percent in ((0, 80, 0), (80, 0, 100)):
            model = self.model(filesystem(used_bytes=used, free_bytes=free, usage_percent=percent))
            self.assertEqual(model.fraction, percent / 100)
            self.assertEqual(model.state, 'current')

    def test_invalid_or_contradictory_values_are_neutral(self):
        for overrides in ({'used_bytes': -1}, {'free_bytes': float('inf')}, {'usage_percent': float('nan')},
                          {'usage_percent': True}, {'usage_percent': 100.1}, {'usage_percent': 10},
                          {'used_bytes': 0, 'free_bytes': 0, 'usage_percent': 0}):
            with self.subTest(overrides=overrides):
                self.assertIsNone(self.model(filesystem(**overrides)).fraction)

    def test_rounded_published_percentage_matches_existing_bar(self):
        self.assertEqual(self.model(filesystem(used_bytes=2, free_bytes=1, usage_percent=66.7)).fraction, .667)

    def test_mount_change_and_unmount_do_not_retain_previous_scope(self):
        disk, sources = usage_fixture(filesystem())
        before = prepare_usage(disk, sources, {}, 11)
        disk.partitions[0].mountpoints = ['/respaldo']
        changed = prepare_usage(disk, sources, {}, 11)
        self.assertNotEqual(before.scope, changed.scope)
        self.assertIn('/respaldo', changed.scope)
        disk.partitions[0].mountpoints = []
        self.assertIsNone(prepare_usage(disk, sources, {}, 11).fraction)

    def test_direct_filesystem_and_nested_container_never_add_layers(self):
        container = filesystem(fstype='crypto_LUKS', mountpoints=[], used_bytes=None)
        leaf = filesystem('vg-root', fstype='btrfs')
        model = self.model(container, leaf)
        self.assertEqual(model.fraction, .75)
        self.assertIn('vg-root', model.scope)
        direct = self.model(direct=filesystem('sda', fstype='btrfs'))
        self.assertEqual(direct.fraction, .75)
        self.assertIn('FS sda ·', direct.scope)

    def test_btrfs_filesystem_larger_than_member_is_never_reported_as_disk_usage(self):
        model = self.model(filesystem(fstype='btrfs', used_bytes=6 * 10**12,
                                     free_bytes=2 * 10**12, usage_percent=75))
        self.assertEqual(model.fraction, .75)
        self.assertIn('miembros físicos', model.details)
        self.assertNotIn('del disco', model.summary)

    def test_no_filesystem_or_only_swap_is_unavailable(self):
        self.assertIsNone(self.model().fraction)
        self.assertIsNone(self.model(filesystem(fstype='swap')).fraction)

    def test_root_is_selected_before_other_fresh_filesystems_in_any_order(self):
        root = filesystem('vg-root', fstype='btrfs', mountpoints=['/', '/home'])
        boot = filesystem('sda1', mountpoints=['/boot'], used_bytes=20, free_bytes=80, usage_percent=20)
        efi = filesystem('sda2', fstype='vfat', mountpoints=['/boot/efi'])
        for parts in ((boot, root, efi), (efi, boot, root)):
            model = self.model(*parts)
            self.assertEqual(model.fraction, .75)
            self.assertTrue(model.root_selected)
            self.assertTrue(model.scope.startswith('Interior · FS / · vg-root'))
            self.assertIn('Selección prioritaria', model.details)
            self.assertNotIn('Solo este filesystem tiene una lectura reciente', model.details)

    def test_invalid_root_never_falls_back_to_fresh_boot_filesystem(self):
        root = filesystem('vg-root', fstype='btrfs', mountpoints=['/', '/home'])
        boot = filesystem('sda1', mountpoints=['/boot'])
        for source, error, expected in ((SourceState(0, 0, 5), {}, 'stale'),
                                        (SourceState(10, 11, 20, 'error raíz'), {}, 'error'),
                                        (SourceState(), {}, 'unavailable'),
                                        (SourceState(10, 11, 20), {'usage:vg-root': 'error snapshot'}, 'error')):
            model = self.model(root, boot, sources={'usage:vg-root': source, 'usage:sda1': SourceState(10, 11, 20)}, errors=error)
            self.assertEqual(model.state, expected)
            self.assertIsNone(model.fraction)
            self.assertTrue(model.root_selected)
            self.assertIn('FS /', model.scope)
        model = self.model(replace(root, used_bytes=None), boot)
        self.assertIsNone(model.fraction)
        self.assertTrue(model.root_selected)

    def test_two_distinct_nodes_claiming_root_are_ambiguous_without_selection(self):
        model = self.model(filesystem(mountpoints=['/']), filesystem('sda2', mountpoints=['/']))
        self.assertEqual(model.state, 'ambiguous')
        self.assertIsNone(model.fraction)
        self.assertEqual(model.scope, 'Interior · FS /')

    def test_verified_root_backing_without_root_measurement_does_not_select_boot(self):
        disk, sources = usage_fixture(filesystem(mountpoints=['/boot']))
        disk.order = DiskOrder(root=RootBacking(('sda',), None))
        model = prepare_usage(disk, sources, {}, 11)
        self.assertIsNone(model.fraction)
        self.assertIn('FS /', model.scope)
        self.assertIn('sin montaje verificable', model.reason)

    def test_root_mount_removal_and_reassignment_follow_current_snapshot(self):
        disk, sources = usage_fixture(filesystem(mountpoints=['/']),
                                      filesystem('sda2', mountpoints=['/datos'], used_bytes=20, free_bytes=80, usage_percent=20))
        self.assertTrue(prepare_usage(disk, sources, {}, 11).root_selected)
        disk.partitions[0].mountpoints = ['/respaldo']
        self.assertEqual(prepare_usage(disk, sources, {}, 11).state, 'multiple')
        disk.partitions[1].mountpoints = ['/']
        model = prepare_usage(disk, sources, {}, 11)
        self.assertTrue(model.root_selected)
        self.assertEqual(model.fraction, .2)
        self.assertIn('FS / · sda2', model.scope)

    def test_direct_root_and_published_zero_usage_are_supported(self):
        model = self.model(direct=filesystem('sda', fstype='btrfs', mountpoints=['/', '/home'],
                                             used_bytes=0, free_bytes=80, usage_percent=0))
        self.assertTrue(model.root_selected)
        self.assertEqual(model.fraction, 0)
        self.assertIn('FS / · sda', model.scope)

    def test_home_and_root_prefix_are_not_root_mounts(self):
        model = self.model(filesystem(mountpoints=['/home']), filesystem('sda2', mountpoints=['/root']))
        self.assertEqual(model.state, 'multiple')
        self.assertFalse(model.root_selected)

    def test_conflict_in_an_unselected_filesystem_does_not_override_valid_root(self):
        root = filesystem('vg-root', fstype='btrfs', mountpoints=['/', '/home'])
        boot = filesystem(mountpoints=['/boot'])
        conflicting_boot = replace(boot, used_bytes=20, free_bytes=80, usage_percent=20)
        model = self.model(boot, root, conflicting_boot)
        self.assertEqual(model.fraction, .75)
        self.assertTrue(model.root_selected)
        self.assertIn('revisar detalles', model.reason)
        self.assertIn('Otros nodos con lecturas contradictorias: sda1', model.details)
