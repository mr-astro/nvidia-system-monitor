"""Synthetic identities only: no host WWNs or serials are stored in tests."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.data.storage import DiskMetrics
from src.utils.disk_aliases import DiskAliasResolver
from src.utils.paths import config_dir, dev_config_dir


class DiskAliases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.devices = self.root / 'dev'
        self.links = self.root / 'by-id'
        self.devices.mkdir()
        self.links.mkdir()
        self.config = self.root / 'config' / 'storage-aliases.json'
        self.resolver = DiskAliasResolver(self.config, self.links, self.devices)

    def link(self, identifier, name):
        target = self.devices / name
        target.touch()
        (self.links / identifier).symlink_to(target)

    def aliases(self, aliases):
        self.config.parent.mkdir(exist_ok=True)
        self.config.write_text(json.dumps({'aliases': aliases}))

    def test_no_alias_configuration_and_wwn_preference(self):
        self.link('ata-example-serial', 'sda')
        self.link('wwn-example-A', 'sda')
        result = self.resolver.refresh([DiskMetrics('sda')], 1)['sda']
        self.assertEqual(result.key, 'id:wwn-example-A')
        self.assertIsNone(result.alias)
        self.assertIsNone(self.resolver.error)

    def test_alias_persists_across_restart_and_device_rename(self):
        self.link('wwn-example-A', 'sda')
        self.aliases({'wwn-example-A': 'Respaldo'})
        first = self.resolver.refresh([DiskMetrics('sda')], 1)['sda']
        self.assertEqual(first.alias, 'Respaldo')
        (self.links / 'wwn-example-A').unlink()
        self.link('wwn-example-A', 'sdc')
        restarted = DiskAliasResolver(self.config, self.links, self.devices)
        second = restarted.refresh([DiskMetrics('sdc')], 2)['sdc']
        self.assertEqual(first, second)
        self.assertEqual(self.resolver.refresh([DiskMetrics('sdc')], 2)['sdc'], first)

    def test_partitions_and_unrelated_ids_are_not_used(self):
        self.link('wwn-example-A-part1', 'sda1')
        self.link('wwn-example-B', 'sdb')
        self.aliases({'wwn-example-B': 'Other'})
        result = self.resolver.refresh([DiskMetrics('sda')], 1)['sda']
        self.assertEqual(result.key, 'device:sda')
        self.assertIsNone(result.alias)

    def test_duplicate_device_or_conflicting_aliases_are_ambiguous(self):
        self.link('wwn-example-A', 'sda')
        self.link('ata-example-serial', 'sda')
        self.aliases({'wwn-example-A': 'One', 'ata-example-serial': 'Two'})
        self.assertIsNone(self.resolver.refresh([DiskMetrics('sda')], 1)['sda'].alias)
        self.assertIn('ambiguos', self.resolver.error)
        self.aliases({'wwn-example-A': 'One'})
        self.assertIsNone(self.resolver.refresh([DiskMetrics('sda'), DiskMetrics('sda')], 2)['sda'].alias)

    def test_symlink_chain_resolves_to_whole_device(self):
        self.link('ata-example-serial', 'sda')
        (self.links / 'wwn-example-A').symlink_to(self.links / 'ata-example-serial')
        self.aliases({'wwn-example-A': 'Respaldo'})
        self.assertEqual(self.resolver.refresh([DiskMetrics('sda')], 1)['sda'].alias, 'Respaldo')

    def test_missing_directory_broken_link_and_invalid_config_degrade_safely(self):
        (self.links / 'wwn-example-A').symlink_to(self.devices / 'missing')
        self.aliases({'../unsafe': 'Not allowed'})
        result = self.resolver.refresh([DiskMetrics('sda')], 1)['sda']
        self.assertIsNone(result.alias)
        self.assertIsNotNone(self.resolver.error)
        resolver = DiskAliasResolver(self.config, self.root / 'missing-directory', self.devices)
        self.assertIsNone(resolver.refresh([DiskMetrics('sda')], 1)['sda'].alias)

    def test_id_reassigned_does_not_keep_old_alias(self):
        self.link('wwn-example-A', 'sda')
        self.aliases({'wwn-example-A': 'Respaldo'})
        self.assertEqual(self.resolver.refresh([DiskMetrics('sda')], 1)['sda'].alias, 'Respaldo')
        (self.links / 'wwn-example-A').unlink()
        self.link('wwn-example-B', 'sda')
        self.assertIsNone(self.resolver.refresh([DiskMetrics('sda')], 2)['sda'].alias)

    def test_filesystem_resolution_is_cached_until_topology_changes(self):
        self.link('wwn-example-A', 'sda')
        self.resolver.refresh([DiskMetrics('sda')], 1)
        with mock.patch.object(Path, 'iterdir', side_effect=AssertionError('unexpected scan')):
            self.resolver.refresh([DiskMetrics('sda')], 1)

    def test_xdg_user_configs_and_stable_are_independent(self):
        with mock.patch.dict('os.environ', {'XDG_CONFIG_HOME': str(self.root / 'user-a')}):
            first = DiskAliasResolver().config_path
            self.assertEqual(first.parent, dev_config_dir())
            self.assertNotEqual(first.parent, config_dir())
        with mock.patch.dict('os.environ', {'XDG_CONFIG_HOME': str(self.root / 'user-b')}):
            second = DiskAliasResolver().config_path
        self.assertNotEqual(first, second)


if __name__ == '__main__':
    unittest.main()
