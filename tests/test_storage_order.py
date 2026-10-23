import copy
from dataclasses import replace
import unittest

from src.data.identity import DeviceIdentity, Presence
from src.data.storage import DiskMetrics
from src.data.storage_order import BtrfsMembers, disk_orders, order_disks, resolve_root, transport_priority
from tests.storage_fixtures import disk, part, presence, root_mount


class StorageOrdering(unittest.TestCase):
    def orders(self, nodes, root=None, bindings=None, members=None, identities=None):
        identities = identities or {node['name']: presence(node) for node in nodes if node['type'] == 'disk'}
        return disk_orders(nodes, {'/': root} if root else {}, bindings or {}, identities, members)

    def sorted_names(self, nodes, **kwargs):
        orders = self.orders(nodes, **kwargs)
        metrics = [DiskMetrics(node['name'], order=orders[node['name']]) for node in nodes if node['name'] in orders]
        return [metric.name for metric in order_disks(metrics)]

    def test_root_nvme_is_not_selected_by_name_or_model(self):
        root = root_mount('/dev/nvme1n1p1', '259:3')
        nodes = [disk('nvme0n1', '259:0', 'nvme', model='Fedora'),
                 disk('nvme1n1', '259:2', 'nvme', model='Windows', children=[
                     part('nvme1n1p1', parent='nvme1n1', number='259:3', mountpoints=['/'])])]
        orders = self.orders(nodes, root, {('nvme1n1p1', '/'): root})
        self.assertEqual(orders['nvme1n1'].priority, 0)
        self.assertEqual(orders['nvme0n1'].priority, 1)
        self.assertEqual(orders['nvme1n1'].root.primary, 'nvme1n1')

    def test_root_sata_precedes_other_nvme_sata_usb_and_unknown(self):
        root = root_mount('/dev/sdb1', '8:17')
        nodes = [disk('sdc', '8:32', 'usb'), disk('sda'), disk('mystery', '9:0', None),
                 disk('nvme0n1', '259:0', 'nvme'), disk('sdb', '8:16', children=[
                     part('sdb1', parent='sdb', number='8:17', mountpoints=['/'])])]
        self.assertEqual(self.sorted_names(nodes, root=root, bindings={('sdb1', '/'): root}),
                         ['sdb', 'nvme0n1', 'sda', 'sdc', 'mystery'])

    def test_usb_root_still_has_root_priority(self):
        root = root_mount()
        nodes = [disk(transport='usb', children=[part(mountpoints=['/'])])]
        self.assertEqual(self.orders(nodes, root, {('sda1', '/'): root})['sda'].priority, 0)

    def test_usb_uses_transport_or_subsystems_not_rotational_or_model_names(self):
        self.assertEqual(transport_priority(disk(transport='nvme', subsystems='block:scsi:usb:pci')), 3)
        self.assertEqual(transport_priority(disk(transport='usb', model='Internal SATA')), 3)
        self.assertEqual(transport_priority(disk(transport=None, model='USB drive', rota=True)), 4)
        self.assertEqual(transport_priority(disk('nvme0n1', transport=None)), 4)
        self.assertEqual(transport_priority(disk(transport='tcp')), 4)

    def test_ties_are_deterministic_and_ignore_aliases_and_input_order(self):
        nodes = [disk('sdb', '8:16'), disk()]
        self.assertEqual(self.sorted_names(nodes), self.sorted_names(list(reversed(nodes))))
        metrics = [DiskMetrics('b', order=self.orders(nodes)['sdb']), DiskMetrics('a', order=self.orders(nodes)['sda'])]
        result = order_disks(metrics)
        self.assertIs(result[0], metrics[1])
        self.assertIs(result[1], metrics[0])
        self.assertEqual([m.name for m in metrics], ['b', 'a'])

    def test_rename_of_verified_device_keeps_tie_breaker(self):
        node = disk(); first = self.orders([node])['sda'].stable_key
        identity = presence(node)
        renamed = disk('sdc', '8:32', diskseq=node['diskseq'])
        after = self.orders([renamed], identities={'sdc': identity})['sdc'].stable_key
        self.assertEqual(first, after)

    def test_root_under_luks(self):
        root = root_mount('/dev/mapper/crypt-root', '253:0')
        nodes = [disk(children=[part(fstype='crypto_LUKS', children=[dict(
            name='crypt-root', kname='dm-0', type='crypt', size=500000, fstype='ext4',
            mountpoints=['/'], **{'maj:min': '253:0'})])])]
        result = self.orders(nodes, root, {('crypt-root', '/'): root})['sda'].root
        self.assertEqual(result.disks, ('sda',))
        self.assertTrue(result.available)

    def test_root_under_lvm(self):
        root = root_mount('/dev/mapper/vg-root', '253:1')
        volume = dict(name='vg-root', kname='dm-1', type='lvm', size=400000, fstype='ext4', **{'maj:min': '253:1'})
        nodes = [disk(children=[part(fstype='LVM2_member', children=[volume])])]
        self.assertEqual(self.orders(nodes, root, {('vg-root', '/'): root})['sda'].priority, 0)

    def test_lvm_root_with_two_backing_disks_has_no_unique_primary(self):
        root = root_mount('/dev/mapper/vg-root', '253:1')
        volume = dict(name='vg-root', kname='dm-1', type='lvm', size=800000, fstype='ext4', **{'maj:min': '253:1'})
        nodes = [disk(children=[part(fstype='LVM2_member', children=[volume])]),
                 disk('sdb', '8:16', children=[part('sdb1', parent='sdb', number='8:17',
                     fstype='LVM2_member', children=[copy.deepcopy(volume)])])]
        orders = self.orders(nodes, root, {('vg-root', '/'): root})
        self.assertEqual([orders[name].priority for name in ('sda', 'sdb')], [0, 0])
        self.assertEqual(orders['sda'].root.disks, ('sda', 'sdb'))
        self.assertIsNone(orders['sda'].root.primary)

    def btrfs(self):
        root = root_mount('/dev/sda1', '0:31', 'btrfs')
        nodes = [disk(children=[part(fstype='btrfs', uuid='shared', mountpoints=['/', '/home'])]),
                 disk('sdb', '8:16', children=[part('sdb1', parent='sdb', number='8:17', fstype='btrfs', uuid='shared')])]
        return nodes, root, {('sda1', '/'): root}

    def test_btrfs_single_member_is_corroborated_not_assumed(self):
        nodes, root, bindings = self.btrfs()
        members = {'shared': BtrfsMembers(frozenset({'8:1'}))}
        orders = self.orders(nodes, root, bindings, members)
        self.assertEqual(orders['sda'].priority, 0)
        # Another disk with the same UUID may be a clone, not a mounted member.
        self.assertEqual(orders['sdb'].priority, 2)

    def test_btrfs_multidevice_root_promotes_all_corroborated_members(self):
        nodes, root, bindings = self.btrfs()
        orders = self.orders(nodes, root, bindings, {'shared': BtrfsMembers(frozenset({'8:1', '8:17'}))})
        self.assertTrue(all(value.priority == 0 for value in orders.values()))
        self.assertEqual(orders['sda'].root.disks, ('sda', 'sdb'))
        self.assertIsNone(orders['sda'].root.primary)

    def test_btrfs_root_on_shared_lvm_volume_resolves_all_physical_backings(self):
        root = root_mount('/dev/mapper/vg-root', '0:31', 'btrfs')
        volume = dict(name='vg-root', kname='dm-1', type='lvm', size=800000,
                      fstype='btrfs', uuid='shared', **{'maj:min': '253:1'})
        nodes = [disk(children=[part(fstype='LVM2_member', children=[volume])]),
                 disk('sdb', '8:16', children=[part('sdb1', parent='sdb', number='8:17',
                     fstype='LVM2_member', children=[copy.deepcopy(volume)])])]
        orders = self.orders(nodes, root, {('vg-root', '/'): root},
                             {'shared': BtrfsMembers(frozenset({'253:1'}))})
        self.assertEqual(orders['sda'].root.disks, ('sda', 'sdb'))
        self.assertTrue(all(order.priority == 0 for order in orders.values()))

    def test_btrfs_missing_degraded_or_contradictory_membership_does_not_guess(self):
        nodes, root, bindings = self.btrfs()
        for members in (None, {'shared': BtrfsMembers(reason='degraded')},
                        {'shared': BtrfsMembers(frozenset({'8:17'}))},
                        {'shared': BtrfsMembers(frozenset({'8:1', '8:33'}))}):
            with self.subTest(members=members):
                orders = self.orders(nodes, root, bindings, members)
                self.assertFalse(orders['sda'].root.available)
                self.assertTrue(all(value.priority != 0 for value in orders.values()))

    def test_unknown_btrfs_uuid_is_not_root_proof(self):
        nodes, root, bindings = self.btrfs()
        nodes[0]['children'][0].pop('uuid')
        self.assertFalse(self.orders(nodes, root, bindings)['sda'].root.available)

    def test_old_mount_binding_cannot_authorize_a_replaced_root_mount(self):
        root = root_mount(); nodes = [disk(children=[part(mountpoints=['/'])])]
        changed = root_mount(mount_id='11')
        self.assertNotEqual(self.orders(nodes, changed, {('sda1', '/'): root})['sda'].priority, 0)

    def test_mountpoint_name_alone_is_not_root_evidence(self):
        nodes = [disk(children=[part(mountpoints=['/'])])]
        self.assertNotEqual(self.orders(nodes, root_mount())['sda'].priority, 0)

    def test_missing_or_contradictory_major_minor_is_unavailable(self):
        root = root_mount(); node = disk(children=[part()])
        for number in (None, '8:2'):
            current = list(root); current[1] = number; current = tuple(current)
            self.assertFalse(self.orders([node], current, {('sda1', '/'): current})['sda'].root.available)

    def test_ambiguous_physical_identity_and_session_do_not_promote_a_disk(self):
        root = root_mount(); nodes = [disk(children=[part()]), disk('sdb', '8:16')]
        identities = {node['name']: presence(node) for node in nodes}
        for field in ('wwn', 'diskseq'):
            ambiguous = dict(identities)
            ambiguous['sdb'] = replace(identities['sdb'], identity=replace(
                identities['sdb'].identity, **{field: getattr(identities['sda'].identity, field)}))
            self.assertFalse(self.orders(nodes, root, {('sda1', '/'): root}, identities=ambiguous)['sda'].root.available)

    def test_unknown_identity_or_cached_old_attachment_is_not_root_proof(self):
        root = root_mount(); node = disk(children=[part()])
        for identity in (DeviceIdentity(), DeviceIdentity(error='ambiguous'), DeviceIdentity(diskseq=999)):
            orders = self.orders([node], root, {('sda1', '/'): root}, identities={'sda': Presence(identity, 1)})
            self.assertNotEqual(orders['sda'].priority, 0)

    def test_conflicting_logical_node_representations_are_ambiguous(self):
        root = root_mount(); node = disk(children=[part()])
        other = copy.deepcopy(node['children'][0]); other['size'] = 123
        node['children'].append(other)
        self.assertFalse(self.orders([node], root, {('sda1', '/'): root})['sda'].root.available)

    def test_repeated_physical_roots_are_ambiguous(self):
        root = root_mount(); node = disk(children=[part()])
        result = self.orders([node, copy.deepcopy(node)], root, {('sda1', '/'): root})
        self.assertFalse(result['sda'].root.available)

    def test_root_on_a_whole_disk_is_supported(self):
        node = disk(fstype='ext4', pttype=None); root = root_mount('/dev/sda', '8:0')
        self.assertEqual(self.orders([node], root, {('sda', '/'): root})['sda'].priority, 0)

    def test_hotplug_does_not_shuffle_existing_same_priority_devices(self):
        base = [disk(), disk('sdc', '8:32')]
        before = self.sorted_names(base)
        usb = disk('sdb', '8:16', 'usb')
        after = self.sorted_names([usb, *reversed(base)])
        self.assertEqual([name for name in after if name != 'sdb'], before)
        self.assertEqual(after[-1], 'sdb')
        self.assertEqual(self.sorted_names(list(reversed(base))), before)


if __name__ == '__main__':
    unittest.main()
