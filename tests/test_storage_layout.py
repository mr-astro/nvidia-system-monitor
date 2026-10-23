import copy
from dataclasses import FrozenInstanceError, replace
import unittest

from src.data.errors import SourceState
from src.data.storage_layout import build_layout
from tests.storage_fixtures import disk, part, presence


class PhysicalLayouts(unittest.TestCase):
    def layout(self, node):
        result = build_layout(node, presence(node), SourceState(10, 11, 20))
        if result.available:
            cursor = 0
            for region in result.regions:
                self.assertEqual(region.start_bytes, cursor)
                self.assertGreater(region.size_bytes, 0)
                self.assertLessEqual(region.end_bytes, node['size'])
                cursor = region.end_bytes
            self.assertEqual(cursor, node['size'])
            self.assertEqual(sum(r.size_bytes for r in result.regions), node['size'])
        else:
            self.assertEqual(result.regions, ())
            self.assertEqual(result.containers, ())
            self.assertIsNotNone(result.reason)
            self.assertIsNone(result.unpartitioned_bytes)
        return result

    def assert_unavailable(self, node):
        result = self.layout(node)
        self.assertFalse(result.available)
        return result

    def test_one_partition_and_verified_identity(self):
        node = disk(children=[part()])
        result = self.layout(node)
        self.assertTrue(result.available)
        self.assertEqual(result.identity, presence(node))
        self.assertEqual(result.capacity_bytes, 512000)
        self.assertEqual(result.regions[1].partition.name, 'sda1')
        self.assertEqual(result.regions[1].partition.partuuid, 'synthetic-sda1')
        self.assertEqual(result.unpartitioned_bytes, 1024)

    def test_several_partitions_and_gaps_cover_capacity_once(self):
        node = disk(children=[part(start=10, sectors=100), part('sda2', start=200, sectors=300, number='8:2')])
        result = self.layout(node)
        self.assertTrue(result.available)
        self.assertEqual([r.kind for r in result.regions], ['unpartitioned', 'partition', 'unpartitioned', 'partition', 'unpartitioned'])
        self.assertEqual(result.unpartitioned_bytes, 600 * 512)

    def test_partition_input_order_does_not_change_geometry(self):
        node = disk(children=[part(start=5, sectors=95), part('sda2', start=100, sectors=900, number='8:2')])
        first = self.layout(node)
        node['children'].reverse()
        self.assertEqual(self.layout(node), first)

    def test_empty_disk_is_explicit(self):
        for table in (None, 'gpt', 'dos'):
            with self.subTest(table=table):
                result = self.layout(disk(pttype=table))
                self.assertEqual(result.state, 'empty')
                self.assertEqual(result.unpartitioned_bytes, result.capacity_bytes)

    def test_whole_device_filesystems_do_not_invent_partitions(self):
        for fstype in ('ext4', 'xfs', 'btrfs'):
            with self.subTest(fstype=fstype):
                result = self.layout(disk(fstype=fstype, pttype=None, mountpoints=['/', '/home']))
                self.assertEqual(result.state, 'whole_device')
                self.assertEqual(len(result.regions), 1)
                self.assertIsNone(result.regions[0].partition)
                self.assertEqual(result.unpartitioned_bytes, 0)

    def test_whole_device_containers_are_not_unpartitioned(self):
        for fstype in ('crypto_LUKS', 'LVM2_member', 'linux_raid_member'):
            node = disk(fstype=fstype, pttype=None, children=[dict(name='mapper', type='crypt', size=400000)])
            self.assertEqual(self.layout(node).state, 'whole_device')

    def test_nested_luks_and_lvm_do_not_add_physical_extents(self):
        node = disk(children=[part(fstype='crypto_LUKS', children=[
            dict(name='crypt', type='crypt', size=500000, fstype='LVM2_member', children=[
                dict(name='vg-root', type='lvm', size=250000, fstype='ext4'),
                dict(name='vg-home', type='lvm', size=250000, fstype='ext4')])])])
        result = self.layout(node)
        self.assertTrue(result.available)
        self.assertEqual([r.partition.name for r in result.regions if r.partition], ['sda1'])

    def test_btrfs_subvolumes_do_not_change_physical_geometry(self):
        node = disk(children=[part(fstype='btrfs', mountpoints=['/', '/home', '/var'])])
        before = self.layout(node)
        node['children'][0]['mountpoints'] = ['/']
        self.assertEqual(self.layout(node), before)

    def test_btrfs_multidevice_has_local_capacity_on_each_disk(self):
        nodes = [disk(children=[part(fstype='btrfs', uuid='shared')]),
                 disk('sdb', '8:16', children=[part('sdb1', parent='sdb', number='8:17', fstype='btrfs', uuid='shared')])]
        for node in nodes:
            result = self.layout(node)
            self.assertTrue(result.available)
            self.assertEqual(result.capacity_bytes, node['size'])

    def extended(self, nested=False):
        primary = part(start=2, sectors=98, parttype='0x83')
        container = part('sda2', start=100, sectors=900, number='8:2', parttype='0x0f', partn=2, fstype=None)
        logical = [part('sda5', start=110, sectors=90, number='8:5', parttype='0x83', partn=5),
                   part('sda6', start=210, sectors=790, number='8:6', parttype='0x83', partn=6)]
        if nested:
            container['children'] = logical
        return disk(pttype='dos', children=[primary, container] + ([] if nested else logical))

    def test_extended_and_logical_partitions_are_nonoverlapping_regions(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                result = self.layout(self.extended(nested))
                self.assertTrue(result.available)
                self.assertEqual(len(result.containers), 1)
                self.assertNotIn('sda2', [r.partition.name for r in result.regions if r.partition])
                self.assertEqual(sum(r.size_bytes for r in result.regions if r.kind == 'extended_reserved'), 20 * 512)

    def test_extended_kernel_placeholder_size_cannot_authorize_logical_extents(self):
        node = self.extended()
        node['children'][1]['size'] = 1024
        self.assert_unavailable(node)

    def test_missing_logical_number_is_ambiguous(self):
        node = self.extended()
        del node['children'][2]['partn']
        self.assert_unavailable(node)

    def test_logical_partition_requires_a_container(self):
        self.assert_unavailable(disk(pttype='dos', children=[part(parttype='0x83', partn=5)]))

    def test_extended_overlap_with_primary_is_rejected(self):
        node = self.extended()
        node['children'][0]['size'] = 200 * 512
        self.assert_unavailable(node)

    def test_overlapping_logical_partitions_are_rejected(self):
        node = self.extended()
        node['children'][3]['start'] = 190
        self.assert_unavailable(node)

    def test_multiple_extended_containers_are_unavailable(self):
        node = self.extended()
        node['children'].append(part('sda3', start=20, sectors=20, parttype='0x05', partn=3))
        self.assert_unavailable(node)

    def test_4kn_start_uses_512_byte_units_not_logical_sector_size(self):
        node = disk(size=8192, children=[part(start=8, sectors=8)], **{'log-sec': 4096})
        result = self.layout(node)
        self.assertTrue(result.available)
        self.assertEqual(result.regions[1].start_bytes, 4096)

    def test_logical_sector_alignment_and_bounds_are_strict(self):
        for start, sectors in ((1, 8), (8, 7), (8, 16)):
            with self.subTest(start=start, sectors=sectors):
                self.assert_unavailable(disk(size=8192, children=[part(start=start, sectors=sectors)], **{'log-sec': 4096}))

    def test_invalid_sector_sizes_are_unavailable(self):
        for sector in (0, 511, 1000, True, float('nan'), 'bad'):
            with self.subTest(sector=sector):
                self.assert_unavailable(disk(children=[part()], **{'log-sec': sector}))

    def test_missing_geometry_does_not_become_a_zero_sized_partition(self):
        for field in ('start', 'size'):
            for value in (None, -1, True, 2.5, float('nan'), 'bad'):
                with self.subTest(field=field, value=value):
                    child = part(); child[field] = value
                    self.assert_unavailable(disk(children=[child]))

    def test_zero_partition_size_is_not_valid(self):
        self.assert_unavailable(disk(children=[part(sectors=0)]))

    def test_optional_partition_identifiers_are_not_required_for_gpt(self):
        node = disk(children=[part()])
        for field in ('partuuid', 'parttype', 'pkname', 'partn'):
            node['children'][0].pop(field)
        node.pop('log-sec')
        result = self.layout(node)
        self.assertTrue(result.available)
        self.assertIsNone(result.regions[1].partition.partuuid)

    def test_dos_needs_type_to_distinguish_an_extended_partition(self):
        self.assert_unavailable(disk(pttype='dos', children=[part(parttype=None)]))

    def test_duplicate_partition_or_partuuid_is_rejected(self):
        child = part(start=10, sectors=100)
        self.assert_unavailable(disk(children=[child, copy.deepcopy(child)]))
        self.assert_unavailable(disk(children=[child, part('sda2', start=200, sectors=100, partuuid=child['partuuid'])]))

    def test_duplicate_or_malformed_partition_numbers_are_unavailable(self):
        self.assert_unavailable(disk(children=[part(start=2, sectors=100),
                                              part('sda2', start=200, sectors=100, partn=1)]))
        for number in (0, -1, True, 1.5, 'bad'):
            with self.subTest(number=number):
                self.assert_unavailable(disk(children=[part(partn=number)]))

    def test_invalid_or_contradictory_identifiers_are_rejected(self):
        for overrides in ({'pkname': 'other-disk'}, {'pkname': ['sda']}, {'partuuid': 123}, {'log-sec': 4096}):
            with self.subTest(overrides=overrides):
                self.assert_unavailable(disk(children=[part(**overrides)]))

    def test_overlaps_and_oversized_regions_are_not_normalized(self):
        self.assert_unavailable(disk(children=[part(sectors=1001)]))
        self.assert_unavailable(disk(children=[part(start=2, sectors=100), part('sda2', start=50, sectors=100)]))

    def test_missing_disk_capacity_or_table_metadata_is_unavailable(self):
        for value in (None, 0, -1, True, 513):
            self.assert_unavailable(disk(size=value, children=[part()]))
        for table in (None, 'unknown'):
            self.assert_unavailable(disk(pttype=table, children=[part()]))
        node = disk(); node.pop('fstype')
        self.assert_unavailable(node)

    def test_unverified_identity_does_not_inherit_old_geometry(self):
        node = disk(children=[part()])
        self.assertFalse(build_layout(node, None).available)
        self.assertFalse(build_layout(node, replace(presence(node), identity=replace(presence(node).identity, diskseq=999))).available)

    def test_usb_uses_the_same_physical_geometry(self):
        result = self.layout(disk(transport='usb', children=[part()]))
        self.assertTrue(result.available)

    def test_nested_partition_table_under_mapper_is_unavailable(self):
        child = part(children=[dict(name='crypt', type='crypt', children=[part('mapped-part')])])
        self.assert_unavailable(disk(children=[child]))

    def test_direct_filesystem_and_physical_partitions_are_contradictory(self):
        self.assert_unavailable(disk(fstype='ext4', children=[part()]))
        self.assert_unavailable(disk(fstype='ext4'))
        self.assert_unavailable(disk(pttype=None, mountpoints=['/']))

    def test_freshness_is_not_reset_by_cached_geometry(self):
        result = self.layout(disk(children=[part()]))
        self.assertTrue(result.current(15))
        self.assertFalse(result.current(31))
        self.assertEqual(result.source.acquired_at, 10)
        self.assertFalse(replace(result, source=replace(result.source, error='lsblk failed')).current(15))
        with self.assertRaises(FrozenInstanceError):
            result.regions[0].end_bytes = 10


if __name__ == '__main__':
    unittest.main()
