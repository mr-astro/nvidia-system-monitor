"""Storage widgets use simulated metrics and synthetic persistent IDs."""
import json
import tempfile
import unittest
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.test_gui_smoke import HAVE_GTK, full_snapshot
from src.data.collector import Snapshot
from src.data.storage import DiskMetrics, PartitionMetrics
from src.data.errors import SourceState
from src.utils.disk_aliases import DiskIdentity
from src.data.storage_presentation import StoragePresentation, StorageAliasWorker, presentation_request
from src.data.identity import DeviceIdentity, Presence


@unittest.skipUnless(HAVE_GTK, 'GTK 4 o pantalla no disponibles')
class StorageGui(unittest.TestCase):
    def setUp(self):
        from src.gui.storage import StoragePanel
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.devices = self.root / 'dev'
        self.by_id = self.root / 'by-id'
        self.devices.mkdir()
        self.by_id.mkdir()
        self.config = self.root / 'aliases.json'
        self.identifiers = {}
        self.configured_aliases = {}
        self.panel = StoragePanel()

    def identify(self, name, identifier):
        (self.devices / name).touch()
        (self.by_id / identifier).symlink_to(self.devices / name)
        self.identifiers[name] = identifier

    def aliases(self, aliases):
        self.config.write_text(json.dumps({'aliases': aliases}))
        self.configured_aliases = dict(aliases)

    def card(self, name):
        return next(card for card in self.panel.disk_cards.values() if name in card.technical.get_label().split(' · '))

    def snapshot(self, *disks, generation=0):
        identities = tuple((d.name, DiskIdentity(
            'synthetic:' + self.identifiers.get(d.name, d.name),
            self.configured_aliases.get(self.identifiers.get(d.name)),
            self.identifiers.get(d.name))) for d in disks)
        return Snapshot(sequence=generation + 1, disks=list(disks),
                        sources={'topology': SourceState(generation, generation, 20)},
                        storage_presentation=StoragePresentation(identities=identities))

    def test_one_card_per_disk_collapsed_by_default_and_models_override_aliases(self):
        self.identify('sda', 'wwn-example-A')
        self.aliases({'wwn-example-A': 'Respaldo'})
        snap = self.snapshot(DiskMetrics('sda', model='Kingston', kind='SSD SATA', size_bytes=480103981056),
                             DiskMetrics('nvme0n1', model='Samsung'), DiskMetrics('sdb'))
        self.panel.update(snap, 0)
        self.assertEqual(len(self.panel.disk_cards), 3)
        self.assertEqual(self.card('sda').title.get_label(), 'Kingston')
        self.assertEqual(self.card('sda').model.get_label(), 'Kingston')
        self.assertFalse(self.card('sda').model.get_visible())
        self.assertIn('447,1 GiB', self.card('sda').technical.get_label())
        self.assertEqual(self.card('nvme0n1').title.get_label(), 'Samsung')
        self.assertEqual(self.card('sdb').title.get_label(), 'sdb')
        self.assertTrue(all(not card.expander.get_expanded() for card in self.panel.disk_cards.values()))

    def test_widgets_and_independent_expansion_survive_refresh_reorder_and_rename(self):
        self.identify('sda', 'wwn-example-A')
        self.aliases({'wwn-example-A': 'Respaldo'})
        disk = DiskMetrics('sda', model='Kingston', partitions=[PartitionMetrics('sda1')])
        other = DiskMetrics('sdb')
        self.panel.update(self.snapshot(disk, other), 0)
        first = self.card('sda')
        first.expander.set_expanded(True)
        second = self.card('sdb')
        row = next(iter(first.partition_rows.values()))
        self.panel.update(self.snapshot(other, disk), 1)
        self.assertIs(first, self.card('sda'))
        self.assertIs(row, next(iter(first.partition_rows.values())))
        self.assertTrue(first.expander.get_expanded())
        self.assertFalse(second.expander.get_expanded())
        (self.by_id / 'wwn-example-A').unlink()
        self.identify('sdc', 'wwn-example-A')
        disk.name = 'sdc'
        self.panel.update(self.snapshot(other, disk, generation=2), 2)
        self.assertIs(first, self.card('sdc'))
        self.assertTrue(first.expander.get_expanded())
        self.assertEqual(first.title.get_label(), 'Kingston')
        prepared = self.snapshot(other, disk, generation=2).storage_presentation
        self.assertEqual(dict(prepared.identities)['sdc'].alias, 'Respaldo')

    def test_hotplug_and_partition_changes_keep_expander(self):
        disk = DiskMetrics('sda', partitions=[PartitionMetrics('sda1')])
        self.panel.update(self.snapshot(disk), 0)
        card = self.card('sda')
        card.expander.set_expanded(True)
        disk.partitions.append(PartitionMetrics('sda2'))
        self.panel.update(self.snapshot(disk, DiskMetrics('sdb'), generation=1), 1)
        self.assertTrue(card.expander.get_expanded())
        self.assertEqual(len(card.partition_rows), 2)
        disk.partitions.pop(0)
        self.panel.update(self.snapshot(disk, generation=2), 2)
        self.assertEqual(len(self.panel.disk_cards), 1)
        self.assertEqual(len(card.partition_rows), 1)
        self.assertTrue(card.expander.get_expanded())

    def test_data_ordering_reuses_cards_and_expansion_across_reorder_and_hotplug(self):
        from src.data.storage_order import disk_orders, order_disks
        from tests.storage_fixtures import disk, part, presence, root_mount
        nodes = [disk('nvme0n1', '259:0', 'nvme'), disk('sdb', '8:16', children=[
                 part('sdb1', parent='sdb', number='8:17', mountpoints=['/'])]), disk()]
        root = root_mount('/dev/sdb1', '8:17')
        metrics = {node['name']: DiskMetrics(node['name'], partitions=[PartitionMetrics(node['name'] + '-part')])
                   for node in nodes}
        def publication(generation):
            orders = disk_orders(nodes, {'/': root}, {('sdb1', '/'): root},
                                 {node['name']: presence(node) for node in nodes})
            values = []
            for node in nodes:
                metric = metrics.setdefault(node['name'], DiskMetrics(node['name']))
                metric.order = orders[node['name']]
                values.append(metric)
            self.panel.update(self.snapshot(*order_disks(values), generation=generation), generation)
            children, child = [], self.panel.body.get_first_child()
            while child is not None:
                children.append(child)
                child = child.get_next_sibling()
            return children
        first = publication(0)
        self.assertEqual(first, [self.card('sdb'), self.card('nvme0n1'), self.card('sda')])
        original = {name: self.card(name) for name in metrics}
        original['sdb'].expander.set_expanded(True)
        row = next(iter(original['sdb'].partition_rows.values()))
        nodes.reverse()
        nodes.append(disk('sdc', '8:32', 'usb'))
        connected = publication(1)
        self.assertEqual(connected[:3], first)
        self.assertIs(connected[-1], self.card('sdc'))
        nodes.pop()
        self.assertEqual(publication(2), first)
        for name, card in original.items():
            self.assertIs(self.card(name), card)
        self.assertTrue(original['sdb'].expander.get_expanded())
        self.assertIs(next(iter(original['sdb'].partition_rows.values())), row)

    def test_collapsed_card_keeps_capacity_temperature_mount_summary_and_usage_outside_details(self):
        from gi.repository import Gtk
        part = PartitionMetrics('sda1', fstype='ext4', mountpoints=['/data'],
                                used_bytes=60, free_bytes=20, usage_percent=75)
        snap = self.usage_snapshot(part)
        snap.disks[0].temperature_c = 38
        snap.sources['smart:sda'] = SourceState(0, 0, 15)
        self.panel.update(snap, 1)
        card = self.card('sda')
        self.assertFalse(card.expander.get_expanded())
        for widget in (card.title, card.capacity, card.temperature, card.partition_summary,
                       card.usage, card.usage_bar, card.status):
            self.assertIsNone(widget.get_ancestor(Gtk.Expander))
            self.assertTrue(widget.get_visible())
        self.assertIn('38,0 °C', card.temperature.get_label())
        self.assertEqual(card.partition_summary.get_label(), '1 volumen · 1 montado')
        self.assertIn('/data', card.partition_summary.get_tooltip_text())
        self.assertEqual(card.overview.get_orientation(), Gtk.Orientation.HORIZONTAL)
        self.assertIs(card.summary.get_parent(), card.overview)
        card.expander.set_expanded(True)
        self.assertIs(card.technical.get_ancestor(Gtk.Expander), card.expander)
        card.expander.set_expanded(False)
        part.mountpoints = []
        self.panel.update(snap, 2)
        self.assertEqual(card.partition_summary.get_label(), '1 volumen · Sin montar')
        self.assertFalse(card.usage_bar.get_visible())

    def test_alias_changes_preserve_model_title_widgets_and_configuration(self):
        self.identify('sda', 'wwn-example-A')
        self.aliases({'wwn-example-A': 'Respaldo'})
        disk = DiskMetrics('sda', model='Kingston', partitions=[PartitionMetrics('sda1', label='Windows 11')])
        self.panel.update(self.snapshot(disk), 0)
        card = self.card('sda')
        card.expander.set_expanded(True)
        title, model = card.title, card.model
        row = next(iter(card.partition_rows.values()))
        for alias in ('Archivo', 'Kingston', '', '  '):
            self.aliases({'wwn-example-A': alias})
            prepared = self.snapshot(disk)
            self.panel.update(prepared, 1)
            self.assertIs(self.card('sda'), card)
            self.assertIs(card.title, title)
            self.assertIs(card.model, model)
            self.assertIs(next(iter(card.partition_rows.values())), row)
            self.assertEqual(title.get_label(), 'Kingston')
            self.assertFalse(model.get_visible())
            self.assertEqual(dict(prepared.storage_presentation.identities)['sda'].alias, alias)
            self.assertEqual(json.loads(self.config.read_text()), {'aliases': {'wwn-example-A': alias}})
            self.assertTrue(card.expander.get_expanded())
        disk.model = 'N/D'
        self.panel.update(self.snapshot(disk), 2)
        self.assertEqual(title.get_label(), 'sda')
        disk.partitions[0].label = 'Fedora'
        self.panel.update(self.snapshot(disk), 3)
        self.assertEqual(title.get_label(), 'sda')

    def test_alias_refresh_does_not_reanchor_or_reveal_expired_temperature(self):
        self.identify('sda', 'wwn-example-A')
        self.aliases({'wwn-example-A': 'Respaldo'})
        disk = DiskMetrics('sda', model='Kingston', temperature_c=38, temperature_source='smart:sda')
        state = SourceState(0, 0, 15)
        snap = self.snapshot(disk)
        snap.sources['smart:sda'] = state
        self.panel.update(snap, 1)
        card = self.card('sda')
        temperature = card.temperature
        card.expander.set_expanded(True)
        self.aliases({'wwn-example-A': 'Archivo'})
        updated = self.snapshot(disk, generation=1)
        updated.sources['smart:sda'] = state
        self.panel.update(updated, 20)
        self.assertIs(card.temperature, temperature)
        self.assertEqual(card.title.get_label(), 'Kingston')
        self.assertEqual(dict(updated.storage_presentation.identities)['sda'].alias, 'Archivo')
        self.assertEqual(temperature.get_label(), 'N/D')
        self.assertIn('38.0 °C', temperature.get_tooltip_text())
        self.assertIn('hace 20 s', temperature.get_tooltip_text())
        self.assertIn('Datos obsoletos', card.status.get_label())
        self.assertIs(updated.sources['smart:sda'], state)
        self.assertTrue(card.expander.get_expanded())

    def test_labels_are_not_guessed_and_duplicates_keep_technical_ids(self):
        a = PartitionMetrics('sda1', fstype='ntfs', label='Windows')
        b = PartitionMetrics('sda2', fstype='ext4', label='Windows')
        c = PartitionMetrics('sda3', fstype='ntfs')
        d = PartitionMetrics('sda4')
        d.description = 'Available description'
        self.panel.update(self.snapshot(DiskMetrics('sda', partitions=[a, b, c, d])), 0)
        rows = list(self.card('sda').partition_rows.values())
        self.assertEqual(rows[0].title.get_label(), 'Windows')
        self.assertEqual(rows[1].title.get_label(), 'Windows')
        self.assertIn('sda1 · ntfs', rows[0].details.get_label())
        self.assertIn('sda2 · ext4', rows[1].details.get_label())
        self.assertEqual(rows[2].title.get_label(), 'sda3')
        self.assertIn('sda3 · ntfs', rows[2].details.get_label())
        self.assertEqual(rows[3].title.get_label(), 'sda4')

    def test_physical_model_and_real_partition_labels_remain_separate(self):
        self.identify('sda', 'wwn-example-A')
        self.aliases({'wwn-example-A': 'Respaldo'})
        parts = [PartitionMetrics('sda1', fstype='ntfs', label='Respaldo', size_bytes=1024**3,
                                  mountpoints=['/data']),
                 PartitionMetrics('sda2', fstype='vfat', label='BIOS_GIGABY')]
        disk = DiskMetrics('sda', model='KINGSTON SA400S37480G', partitions=parts)
        self.panel.update(self.snapshot(disk), 0)
        card = self.card('sda')
        rows = list(card.partition_rows.values())
        self.assertEqual(card.title.get_label(), disk.model)
        self.assertEqual(card.model.get_label(), disk.model)
        self.assertEqual([row.title.get_label() for row in rows], ['Respaldo', 'BIOS_GIGABY'])
        self.assertEqual(rows[0].details.get_label(), 'sda1 · ntfs · 1.0 GiB · /data')
        self.assertIn('sda2 · vfat', rows[1].details.get_label())
        self.assertIn('Sin montar', rows[1].details.get_label())
        self.assertEqual(self.config.read_text(), json.dumps({'aliases': {'wwn-example-A': 'Respaldo'}}))
        card.expander.set_expanded(True)
        parts[0].label = 'Etiqueta actualizada'
        self.panel.update(self.snapshot(disk, generation=1), 1)
        self.assertIs(next(iter(card.partition_rows.values())), rows[0])
        self.assertEqual(rows[0].title.get_label(), 'Etiqueta actualizada')
        self.assertTrue(card.expander.get_expanded())

    def test_missing_models_and_partition_labels_use_technical_names_even_with_alias(self):
        self.identify('sda', 'wwn-example-A')
        self.aliases({'wwn-example-A': 'Respaldo'})
        part = PartitionMetrics('sda1', fstype='ntfs')
        part.description = 'Nombre descriptivo anterior'
        disk = DiskMetrics('sda', partitions=[part])
        for model, volume_label, expected in ((None, None, 'sda1'), ('', '', 'sda1'),
                                               ('   ', '  ', 'sda1'), (' N/D ', 'N/D', 'N/D')):
            with self.subTest(model=model, label=volume_label):
                disk.model, part.label = model, volume_label
                self.panel.update(self.snapshot(disk), 0)
                card = self.card('sda')
                self.assertEqual(card.title.get_label(), 'sda')
                row = next(iter(card.partition_rows.values()))
                self.assertEqual(row.title.get_label(), expected)
                self.assertIn('sda1 · ntfs', row.details.get_label())

    def test_unmounted_volumes_have_no_usage_and_mount_transitions_reuse_widgets(self):
        partition = PartitionMetrics('sda1', used_bytes=100, usage_percent=20)
        disk = DiskMetrics('sda', partitions=[partition])
        self.panel.update(self.snapshot(disk), 0)
        row = next(iter(self.card('sda').partition_rows.values()))
        self.assertIn('Sin montar', row.details.get_label())
        self.assertFalse(row.bar.get_visible())
        self.assertFalse(row.usage.get_visible())
        partition.mountpoints = ['/data']
        self.panel.update(self.snapshot(disk), 1)
        self.assertTrue(row.bar.get_visible())
        self.assertTrue(row.usage.get_visible())
        partition.mountpoints = []
        partition.used_bytes = None
        self.panel.update(self.snapshot(disk), 2)
        self.assertFalse(row.bar.get_visible())
        self.assertFalse(row.usage.get_visible())
        self.assertIs(row, next(iter(self.card('sda').partition_rows.values())))

    def test_btrfs_multimount_has_one_shared_usage_and_nested_volumes_keep_depth(self):
        parts = [PartitionMetrics('sda1', fstype='crypto_LUKS'),
                 PartitionMetrics('luks-example', fstype='LVM2_member', depth=2),
                 PartitionMetrics('vg-root', fstype='btrfs', depth=3, mountpoints=['/', '/home'],
                                  used_bytes=100, free_bytes=100, usage_percent=50)]
        self.panel.update(self.snapshot(DiskMetrics('sda', partitions=parts)), 0)
        rows = list(self.card('sda').partition_rows.values())
        self.assertEqual(sum(row.bar.get_visible() for row in rows), 1)
        self.assertIn('/, /home', rows[2].details.get_label())
        self.assertIn('espacio compartido', rows[2].details.get_label())
        self.assertEqual(rows[2].get_margin_start(), 24)

    def test_temperature_caducity_smart_warning_and_recovery(self):
        disk = DiskMetrics('sda', temperature_c=38)
        snap = self.snapshot(disk)
        snap.sources['smart:sda'] = SourceState(0, 0, 15, 'smartctl salida 4: checksum warning')
        self.panel.update(snap, 1)
        card = self.card('sda')
        self.assertIn('38,0 °C', card.temperature.get_label())
        self.assertIn('hace 1 s', card.temperature.get_label())
        self.assertIn('anterior', card.temperature.get_label())
        self.assertIn('SMART: error de consulta', card.status.get_label())
        self.assertIn('checksum warning', card.status.get_tooltip_text())
        self.panel.update(snap, 20)
        self.assertEqual(card.temperature.get_label(), 'N/D')
        self.assertIn('Datos obsoletos', card.status.get_label())
        snap.sources['smart:sda'] = SourceState(21, 21, 15)
        snap.sources['topology'] = SourceState(21, 21, 20)
        self.panel.update(snap, 21)
        self.assertEqual(card.temperature.get_label(), '38,0 °C · hace 0 s')
        self.assertNotIn('SMART:', card.status.get_label())
        disk.temperature_c = None
        self.panel.update(snap, 22)
        self.assertEqual(card.temperature.get_label(), 'N/D')

    def test_long_diagnostics_only_in_tooltips_and_permissions_not_disk_failure(self):
        message = 'smartctl: salida 2 — Permission denied ' + 'technical detail ' * 80
        disk = DiskMetrics('sda', model='Very long model ' * 20)
        snap = self.snapshot(disk)
        snap.sources['smart:sda'] = SourceState(error=message, max_age=15)
        snap.errors['smart:sda'] = message
        self.panel.update(snap, 0)
        card = self.card('sda')
        self.assertIn('SMART: sin permisos', card.status.get_label())
        self.assertNotIn('technical detail', card.status.get_label())
        self.assertIn(message, card.status.get_tooltip_text())
        from gi.repository import Gtk, Pango
        self.assertEqual(card.title.get_ellipsize(), Pango.EllipsizeMode.END)
        self.assertLess(card.measure(Gtk.Orientation.HORIZONTAL, -1)[0], 300)

    def test_no_repeated_normal_age_rows_and_stale_usage_is_explicit(self):
        part = PartitionMetrics('sda1', mountpoints=['/data'], used_bytes=100, usage_percent=50)
        snap = self.snapshot(DiskMetrics('sda', partitions=[part]))
        snap.sources['usage:sda1'] = SourceState(0, 0, 2)
        self.panel.update(snap, 1)
        row = next(iter(self.card('sda').partition_rows.values()))
        self.assertFalse(row.warning.get_visible())
        self.panel.update(snap, 3)
        self.assertTrue(row.warning.get_visible())
        self.assertIn('Uso obsoleto', row.warning.get_label())
        self.assertIn('hace 3 s', row.warning.get_label())

    def test_empty_failure_and_recovery_preserve_expansion(self):
        disk = DiskMetrics('sda')
        self.panel.update(self.snapshot(disk), 0)
        self.card('sda').expander.set_expanded(True)
        self.panel.reset(('render-error', 'storage'))
        self.panel.set_row('state', 'Estado', 'Error al mostrar las métricas')
        error_label = self.panel._rows['state'][1]
        self.panel.reset(('render-error', 'storage'))
        self.assertIs(error_label, self.panel._rows['state'][1])
        self.assertFalse(self.panel.disk_cards)
        self.panel.update(self.snapshot(disk, generation=1), 1)
        self.assertTrue(self.card('sda').expander.get_expanded())
        self.panel.update(Snapshot(sequence=3, errors={'topology': 'lsblk timeout'}), 2)
        self.assertEqual(self.panel._rows['state'][1].get_label(), 'Almacenamiento no disponible')
        self.assertIn('timeout', self.panel._rows['state'][1].get_tooltip_text())

    def usage_snapshot(self, *parts):
        snap = self.snapshot(DiskMetrics('sda', size_bytes=10**15, partitions=list(parts)))
        snap.sources.update({'usage:' + part.name: SourceState(0, 0, 2) for part in parts})
        return snap

    def direct_snapshot(self, fstype='ext4', mounts=None):
        fs = PartitionMetrics('sdb', fstype=fstype, label='Datos', depth=0,
                              mountpoints=['/mnt/datos'] if mounts is None else mounts,
                              used_bytes=60, free_bytes=20, usage_percent=75)
        snap = self.snapshot(DiskMetrics('sdb', size_bytes=10**12, direct_filesystem=fs))
        snap.sources['usage:sdb'] = SourceState(0, 0, 2)
        return snap

    def test_direct_filesystem_has_only_header_usage_and_physical_capacity(self):
        for fstype in ('ext4', 'xfs'):
            with self.subTest(fstype=fstype):
                snap = self.direct_snapshot(fstype)
                self.panel.update(snap, 1)
                card = self.card('sdb')
                self.assertEqual(card.partition_rows, {})
                self.assertEqual(snap.disks[0].partitions, [])
                self.assertTrue(card.usage_bar.get_visible())
                self.assertEqual(card.usage_bar.get_fraction(), .75)
                self.assertIn('931,3 GiB', card.technical.get_label())
                self.assertIn('60,0 B usados', card.usage.get_label())
                self.assertIn('20,0 B disponibles', card.usage.get_label())
                self.assertIn('Uso de Datos (sdb)', card.usage_scope.get_label())
                self.assertIn('sistema de archivos en disco completo', card.usage_scope.get_label())
                self.assertIn(fstype, card.usage_scope.get_label())
                self.assertIn('/mnt/datos', card.usage_scope.get_label())
                self.assertIn('usage:sdb', card.status.get_tooltip_text())

    def test_direct_btrfs_routes_share_one_bar_and_do_not_sum_usage(self):
        snap = self.direct_snapshot('btrfs', ['/', '/home'])
        self.panel.update(snap, 1)
        card = self.card('sdb')
        self.assertEqual(len(self.panel.disk_cards), 1)
        self.assertEqual(card.partition_rows, {})
        self.assertTrue(card.usage_bar.get_visible())
        self.assertEqual(card.usage_bar.get_fraction(), .75)
        self.assertIn('60,0 B usados', card.usage.get_label())
        self.assertIn('/, /home', card.usage_scope.get_label())
        self.assertIn('espacio compartido', card.usage_scope.get_label())
        self.assertIn('Montajes: /, /home', card.usage.get_tooltip_text())

    def test_direct_mount_error_unmount_and_recovery_reuse_card_and_expansion(self):
        snap = self.direct_snapshot()
        self.panel.update(snap, 1)
        card = self.card('sdb')
        bar = card.usage_bar
        card.expander.set_expanded(True)
        snap.sources['usage:sdb'] = SourceState(0, 0, 2, 'No se pudo leer el uso del montaje')
        snap.errors['usage:sdb'] = 'No se pudo leer el uso del montaje'
        self.panel.update(snap, 3)
        self.assertTrue(bar.get_visible())
        self.assertIn('Uso obsoleto · hace 3 s', card.usage_warning.get_label())
        self.assertIn('No se pudo leer', card.status.get_tooltip_text())
        fs = snap.disks[0].direct_filesystem
        fs.mountpoints = []
        fs.used_bytes = fs.free_bytes = fs.usage_percent = None
        snap.sources['usage:sdb'] = SourceState(0, 0, 2, 'Montaje desaparecido')
        self.panel.update(snap, 4)
        self.assertFalse(bar.get_visible())
        self.assertEqual(card.usage.get_label(), 'Uso no disponible')
        self.assertIn('Sin montar', card.usage_scope.get_label())
        fs.mountpoints = ['/mnt/datos']
        fs.used_bytes, fs.free_bytes, fs.usage_percent = 40, 60, 40
        snap.sources['usage:sdb'] = SourceState(5, 5, 2)
        snap.errors.clear()
        self.panel.update(snap, 5)
        self.assertIs(self.card('sdb'), card)
        self.assertIs(card.usage_bar, bar)
        self.assertTrue(card.expander.get_expanded())
        self.assertTrue(bar.get_visible())
        self.assertEqual(bar.get_fraction(), .4)
        self.assertFalse(card.usage_warning.get_visible())
        self.assertEqual(card.partition_rows, {})

    def test_direct_unmounted_or_unmeasured_filesystem_has_no_bar(self):
        snap = self.direct_snapshot(mounts=[])
        self.panel.update(snap, 1)
        card = self.card('sdb')
        self.assertFalse(card.usage_bar.get_visible())
        self.assertIn('Sin montar', card.usage_scope.get_label())
        snap.disks[0].direct_filesystem.mountpoints = ['/mnt/datos']
        snap.sources['usage:sdb'] = SourceState(error='No se pudo leer el uso del montaje')
        self.panel.update(snap, 1)
        self.assertFalse(card.usage_bar.get_visible())
        self.assertEqual(card.usage.get_label(), 'Uso no disponible')

    def test_switching_direct_and_partitioned_layout_keeps_card_and_expansion(self):
        snap = self.direct_snapshot()
        self.panel.update(snap, 0)
        card = self.card('sdb')
        card.expander.set_expanded(True)
        disk = snap.disks[0]
        disk.direct_filesystem = None
        disk.partitions = [PartitionMetrics('sdb1', fstype='ext4', mountpoints=['/part'],
                                           used_bytes=50, free_bytes=50, usage_percent=50)]
        snap.sources = {'topology': SourceState(1, 1, 20), 'usage:sdb1': SourceState(1, 1, 2)}
        self.panel.update(snap, 1)
        self.assertIs(self.card('sdb'), card)
        self.assertTrue(card.expander.get_expanded())
        self.assertEqual(len(card.partition_rows), 1)
        self.assertIn('Uso de sdb1', card.usage_scope.get_label())
        snap = self.direct_snapshot()
        self.panel.update(snap, 1)
        self.assertIs(self.card('sdb'), card)
        self.assertTrue(card.expander.get_expanded())
        self.assertEqual(card.partition_rows, {})

    def test_direct_mount_changes_keep_temperature_source_and_age_visible(self):
        snap = self.direct_snapshot()
        disk = snap.disks[0]
        disk.temperature_c = 38
        disk.temperature_source = 'smart:sdb'
        snap.sources['smart:sdb'] = SourceState(0, 0, 15)
        self.panel.update(snap, 1)
        card = self.card('sdb')
        disk.direct_filesystem.mountpoints = []
        disk.direct_filesystem.used_bytes = None
        snap.sources['usage:sdb'] = SourceState(0, 0, 2, 'Montaje desaparecido')
        self.panel.update(snap, 3)
        self.assertIn('38,0 °C', card.temperature.get_label())
        self.assertIn('hace 3 s', card.temperature.get_label())
        self.assertIn('smart:sdb', card.temperature.get_tooltip_text())
        self.assertNotIn('Temperatura no disponible', card.status.get_label())

    def test_header_bar_names_filesystem_and_uses_its_percentage_not_disk_size(self):
        part = PartitionMetrics('sda1', fstype='ext4', label='Datos', mountpoints=['/data'],
                                used_bytes=60, free_bytes=20, usage_percent=75)
        self.panel.update(self.usage_snapshot(part), 1)
        card = self.card('sda')
        self.assertTrue(card.usage_bar.get_visible())
        self.assertEqual(card.usage_bar.get_fraction(), .75)
        self.assertIn('Uso de Datos (sda1)', card.usage_scope.get_label())
        self.assertIn('75,0%', card.usage.get_label())
        self.assertIn('60,0 B usados', card.usage.get_label())
        self.assertIn('20,0 B disponibles', card.usage.get_label())
        self.assertIn('sistema de archivos sda1', card.usage.get_tooltip_text())
        row = next(iter(card.partition_rows.values()))
        self.assertTrue(row.bar.get_visible())
        self.assertEqual(row.bar.get_fraction(), .75)
        self.assertFalse(card.expander.get_expanded())

    def test_no_measurement_is_unavailable_and_zero_is_shown_only_when_measured(self):
        part = PartitionMetrics('sda1', fstype='ext4')
        snap = self.usage_snapshot(part)
        self.panel.update(snap, 0)
        card = self.card('sda')
        row = next(iter(card.partition_rows.values()))
        self.assertEqual(card.usage.get_label(), 'Uso no disponible')
        self.assertFalse(card.usage_bar.get_visible())
        self.assertIn('Sin montar', row.details.get_label())
        self.assertIn('Uso no disponible', row.details.get_label())
        part.mountpoints, part.used_bytes, part.free_bytes, part.usage_percent = ['/data'], 0, 100, 0
        self.panel.update(snap, 1)
        self.assertTrue(card.usage_bar.get_visible())
        self.assertEqual(card.usage_bar.get_fraction(), 0)
        self.assertIn('0,0%', card.usage.get_label())
        snap.sources['usage:sda1'] = SourceState()
        self.panel.update(snap, 1)
        self.assertFalse(card.usage_bar.get_visible())

    def test_independent_filesystems_have_only_individual_bars_even_if_values_equal(self):
        a = PartitionMetrics('sda1', fstype='ext4', mountpoints=['/one'], used_bytes=50, free_bytes=50, usage_percent=50)
        b = PartitionMetrics('sda2', fstype='ext4', mountpoints=['/two'], used_bytes=50, free_bytes=50, usage_percent=50)
        self.panel.update(self.usage_snapshot(a, b), 1)
        card = self.card('sda')
        self.assertEqual(card.usage.get_label(), 'Uso por particiones')
        self.assertFalse(card.usage_bar.get_visible())
        self.assertEqual(sum(row.bar.get_visible() for row in card.partition_rows.values()), 2)
        b.mountpoints = []
        self.panel.update(self.usage_snapshot(a, b), 1)
        self.assertFalse(card.usage_bar.get_visible())

    def test_btrfs_repeated_node_mounts_share_one_measurement_without_summing(self):
        a = PartitionMetrics('sda1', fstype='btrfs', mountpoints=['/'], used_bytes=50, free_bytes=50, usage_percent=50)
        b = PartitionMetrics('sda1', fstype='btrfs', mountpoints=['/home'], used_bytes=50, free_bytes=50, usage_percent=50)
        snap = self.usage_snapshot(a, b)
        self.panel.update(snap, 1)
        card = self.card('sda')
        self.assertEqual(len(card.partition_rows), 1)
        self.assertEqual(card.usage_bar.get_fraction(), .5)
        self.assertIn('50,0 B usados', card.usage.get_label())
        row = next(iter(card.partition_rows.values()))
        self.assertIn('/, /home', row.details.get_label())
        self.assertIn('espacio compartido', row.details.get_label())
        self.assertEqual(a.mountpoints, ['/'])
        b.name = 'sda2'
        self.panel.update(self.usage_snapshot(a, b), 1)
        self.assertFalse(card.usage_bar.get_visible())
        self.assertEqual(len(card.partition_rows), 2)

    def test_nested_containers_do_not_create_artificial_filesystem_percentages(self):
        parts = [PartitionMetrics('sda1', fstype='crypto_LUKS'),
                 PartitionMetrics('crypt-example', fstype='LVM2_member', depth=2),
                 PartitionMetrics('vg-root', fstype='btrfs', depth=3, mountpoints=['/', '/home'],
                                  used_bytes=80, free_bytes=20, usage_percent=80)]
        self.panel.update(self.usage_snapshot(*parts), 1)
        card = self.card('sda')
        self.assertTrue(card.usage_bar.get_visible())
        self.assertIn('Uso de vg-root', card.usage_scope.get_label())
        self.assertEqual(card.usage_bar.get_fraction(), .8)

    def test_usage_error_retains_original_age_then_mount_invalidation_hides_bar(self):
        part = PartitionMetrics('sda1', fstype='ext4', mountpoints=['/data'], used_bytes=80, free_bytes=20, usage_percent=80)
        snap = self.usage_snapshot(part)
        self.panel.update(snap, 1)
        card = self.card('sda')
        bar = card.usage_bar
        card.expander.set_expanded(True)
        snap.sources['usage:sda1'] = SourceState(0, 0, 2, 'statvfs failed')
        self.panel.update(snap, 3)
        self.assertTrue(bar.get_visible())
        self.assertIn('Uso obsoleto · hace 3 s', card.usage_warning.get_label())
        self.assertIn('statvfs failed', card.usage_warning.get_tooltip_text())
        self.assertIs(card.usage_bar, bar)
        self.assertTrue(card.expander.get_expanded())
        part.mountpoints = []
        part.used_bytes = part.free_bytes = part.usage_percent = None
        snap.sources['usage:sda1'] = SourceState(0, 0, 2, 'Montaje sustituido')
        self.panel.update(snap, 4)
        self.assertFalse(bar.get_visible())
        self.assertEqual(card.usage.get_label(), 'Uso no disponible')
        self.assertFalse(next(iter(card.partition_rows.values())).bar.get_visible())
        part.mountpoints, part.used_bytes, part.free_bytes, part.usage_percent = ['/data'], 40, 60, 40
        snap.sources['usage:sda1'] = SourceState(5, 5, 2)
        self.panel.update(snap, 5)
        self.assertTrue(bar.get_visible())
        self.assertEqual(bar.get_fraction(), .4)
        self.assertFalse(card.usage_warning.get_visible())
        self.assertTrue(card.expander.get_expanded())

    def test_header_bar_caducity_without_acquisition_failure_is_explicit(self):
        part = PartitionMetrics('sda1', mountpoints=['/data'], used_bytes=50, free_bytes=50, usage_percent=50)
        snap = self.usage_snapshot(part)
        self.panel.update(snap, 3)
        card = self.card('sda')
        self.assertTrue(card.usage_bar.get_visible())
        self.assertIn('Uso obsoleto', card.usage_warning.get_label())

    def test_header_bar_rejects_missing_negative_and_nonfinite_data(self):
        for used, available, percent in ((None, 50, 50), (-1, 50, 50), (50, None, 50),
                                         (50, 50, float('nan')), (50, 50, 150)):
            with self.subTest(values=(used, available, percent)):
                part = PartitionMetrics('sda1', mountpoints=['/data'], used_bytes=used,
                                        free_bytes=available, usage_percent=percent)
                self.panel.update(self.usage_snapshot(part), 1)
                self.assertFalse(self.card('sda').usage_bar.get_visible())

    def test_valid_udisks_temperature_survives_smart_permissions_with_separate_diagnostics(self):
        disk = DiskMetrics('sda', temperature_c=40, temperature_source='udisks:sda')
        snap = self.snapshot(disk)
        snap.sources['udisks:sda'] = SourceState(0, 0, 752)
        snap.sources['smart:sda'] = SourceState(-100, -100, 15, 'Permission denied')
        snap.errors['smart:sda'] = 'Permission denied'
        self.panel.update(snap, 5)
        card = self.card('sda')
        self.assertIn('40,0 °C', card.temperature.get_label())
        self.assertIn('hace 5 s', card.temperature.get_label())
        self.assertIn('udisks:sda', card.temperature.get_tooltip_text())
        self.assertNotIn('Temperatura no disponible', card.status.get_label())
        self.assertIn('SMART: sin permisos', card.status.get_label())
        self.assertIn('Permission denied', card.status.get_tooltip_text())
        self.panel.update(snap, 753)
        self.assertEqual(card.temperature.get_label(), 'N/D')
        self.assertIn('Temperatura no disponible', card.status.get_label())

    def test_many_usage_updates_reuse_all_widgets_and_keep_expansion(self):
        part = PartitionMetrics('sda1', fstype='ext4', mountpoints=['/data'],
                                used_bytes=0, free_bytes=100, usage_percent=0)
        snap = self.usage_snapshot(part)
        self.panel.update(snap, 0)
        card = self.card('sda')
        card.expander.set_expanded(True)
        bar, row = card.usage_bar, next(iter(card.partition_rows.values()))
        for cycle in range(500):
            part.used_bytes = part.usage_percent = cycle % 101
            part.free_bytes = 100 - part.used_bytes
            snap.sources['usage:sda1'] = SourceState(cycle, cycle, 2)
            snap.sources['topology'] = SourceState(cycle, cycle, 20)
            self.panel.update(snap, cycle)
            self.assertIs(self.card('sda'), card)
            self.assertIs(card.usage_bar, bar)
            self.assertIs(next(iter(card.partition_rows.values())), row)
            self.assertEqual(bar.get_fraction(), part.usage_percent / 100)
            self.assertTrue(card.expander.get_expanded())
        self.assertEqual(len(card.partition_rows), 1)
        self.assertEqual(len(self.panel.disk_cards), 1)

    def test_a6_prepared_aliases_rename_reconnect_and_replacement_keep_correct_expansion(self):
        disk = DiskMetrics('sda', model='Synthetic')
        def snap(sequence, key, physical):
            return Snapshot(sequence=sequence, disks=[disk], storage_presentation=StoragePresentation(
                identities=((disk.name, DiskIdentity(key, expansion_key=physical)),)))
        self.panel.update(snap(1, 'presence:1', 'physical:A'), 0)
        first = self.card('sda')
        first.expander.set_expanded(True)
        ready = snap(2, 'presence:1', 'physical:A')
        ready.storage_presentation = StoragePresentation(identities=(('sda', DiskIdentity(
            'presence:1', 'Ready alias', 'physical:A')),))
        self.panel.update(ready, 1)
        self.assertIs(first, self.card('sda'))
        self.assertTrue(first.expander.get_expanded())
        disk.name = 'sdc'
        self.panel.update(snap(3, 'presence:1', 'physical:A'), 2)
        self.assertIs(first, self.card('sdc'))
        self.panel.update(snap(4, 'presence:2', 'physical:A'), 3)
        reconnected = self.card('sdc')
        self.assertIsNot(first, reconnected)
        self.assertTrue(reconnected.expander.get_expanded())
        self.panel.update(snap(5, 'presence:3', 'physical:B'), 4)
        self.assertFalse(self.card('sdc').expander.get_expanded())
        self.panel.update(Snapshot(sequence=6), 5)
        self.assertEqual(self.panel.disk_cards, {})
        self.panel.update(snap(7, 'presence:4', 'physical:A'), 6)
        self.assertTrue(self.card('sdc').expander.get_expanded())

    def test_a6_old_snapshot_is_rejected_while_current_usage_age_advances(self):
        current = self.direct_snapshot()
        current.sequence = 10
        self.panel.update(current, 1)
        card = self.card('sdb')
        old = self.snapshot(DiskMetrics('sda'))
        old.sequence = 9
        self.panel.update(old, 5)
        self.assertIs(card, self.card('sdb'))
        self.assertEqual(len(self.panel.disk_cards), 1)
        self.assertIn('hace 5 s', card.usage_warning.get_label())
        self.assertIn('75,0%', card.usage.get_label())

    def test_a6_unverified_names_do_not_inherit_expansion_or_accumulate_old_states(self):
        disk = DiskMetrics('sda')
        self.panel.update(Snapshot(sequence=1, disks=[disk]), 0)
        card = self.card('sda')
        card.expander.set_expanded(True)
        for sequence in range(2, 102):
            self.panel.update(Snapshot(sequence=sequence, disks=[disk]), sequence)
            self.assertFalse(self.card('sda').expander.get_expanded())
        self.assertIsNot(self.card('sda'), card)
        self.assertEqual(len(self.panel.expansion_state), 1)

    def test_a6_alias_error_is_separate_from_usage_temperature_and_their_times(self):
        snap = self.direct_snapshot()
        disk = snap.disks[0]
        disk.temperature_c = 38
        disk.temperature_source = 'smart:sdb'
        snap.sources['smart:sdb'] = SourceState(0, 0, 15)
        measurements = dict(snap.sources)
        snap.storage_presentation = StoragePresentation(identities=snap.storage_presentation.identities,
                                                        state=SourceState(0, 0, 20, 'JSON denied'))
        self.panel.update(snap, 3)
        card = self.card('sdb')
        self.assertIn('JSON denied', self.panel._rows['alias-error'][1].get_tooltip_text())
        self.assertIn('hace 3 s', self.panel._rows['alias-error'][1].get_tooltip_text())
        self.assertEqual(snap.sources, measurements)
        self.assertIn('38,0 °C', card.temperature.get_label())
        self.assertTrue(card.usage_bar.get_visible())
        self.assertIn('75,0%', card.usage.get_label())

    def test_a6_gtk_stress_has_no_alias_io_with_worker_blocked(self):
        from gi.repository import GLib
        from src.gui.storage import StoragePanel

        entered, release = threading.Event(), threading.Event()
        main = threading.get_ident()
        class SlowResolver:
            error = None
            def __init__(self, *args, **kwargs):
                if threading.get_ident() == main:
                    raise AssertionError('resolver constructed on GTK')
            def refresh(self, devices, generation):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('synthetic slow JSON/symlink')
                return {d.name: DiskIdentity('id:wwn-500a', 'Alias') for d in devices}
        identity = DeviceIdentity(diskseq=1, wwn='500a')
        worker = StorageAliasWorker('/synthetic/aliases.json', resolver_factory=SlowResolver,
                                    identity_reader=lambda name: identity)
        self.addCleanup(worker.stop)
        self.addCleanup(release.set)
        disk = DiskMetrics('sda', model='Synthetic')
        req = presentation_request([disk], SourceState(0, 0, 20), {'sda': Presence(identity, 1)})
        worker.request(req, 1)
        self.assertTrue(entered.wait(2))
        durations, beats = [], []
        loop = GLib.MainLoop()
        errors = []
        with mock.patch.object(Path, 'read_text', side_effect=AssertionError('GTK JSON I/O')), \
                mock.patch.object(Path, 'iterdir', side_effect=AssertionError('GTK device discovery')), \
                mock.patch.object(Path, 'resolve', side_effect=AssertionError('GTK link resolution')), \
                mock.patch.object(Path, 'is_symlink', side_effect=AssertionError('GTK path check')):
            panel = StoragePanel()
            snapshot = Snapshot(sequence=1, disks=[disk], storage_presentation=worker.request(req, 1))
            panel.update(snapshot, 0)
            card = next(iter(panel.disk_cards.values()))
            card.expander.set_expanded(True)
            def publish():
                try:
                    sequence = len(durations) + 2
                    publication = Snapshot(sequence=sequence, disks=[disk],
                                           storage_presentation=worker.request(req, sequence))
                    started = time.perf_counter()
                    panel.update(publication, sequence / 1000)
                    durations.append(time.perf_counter()-started)
                    self.assertIs(next(iter(panel.disk_cards.values())), card)
                    self.assertTrue(card.expander.get_expanded())
                    if len(durations) < 1000:
                        return GLib.SOURCE_CONTINUE
                except BaseException as error:
                    errors.append(error)
                loop.quit()
                return GLib.SOURCE_REMOVE
            heartbeat = GLib.timeout_add(5, lambda: (beats.append(time.perf_counter()), GLib.SOURCE_CONTINUE)[1])
            GLib.idle_add(publish)
            loop.run()
            GLib.source_remove(heartbeat)
        release.set()
        worker.stop()
        worker._thread.join(2)
        if errors:
            raise errors[0]
        self.assertEqual(len(durations), 1000)
        self.assertGreater(len(beats), 1)
        self.assertLess(max(durations), .1)
        self.assertFalse(worker._thread.is_alive())
        print(f'A6 GTK stress: 1000 publications, update max={max(durations)*1000:.3f} ms, '
              f'mean={sum(durations)/len(durations)*1000:.3f} ms, '
              f'heartbeat gap max={max(b-a for a,b in zip(beats, beats[1:]))*1000:.3f} ms')


if __name__ == '__main__':
    unittest.main()
