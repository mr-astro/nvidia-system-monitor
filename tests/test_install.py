"""Run the installer only in a copied project with isolated HOME/XDG and fake sudo."""
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from src.utils.channel import DEV, STABLE, parse_channel_args
from src.utils import installer

ROOT = Path(__file__).resolve().parent.parent


class InstallerIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'copied project'
        self.project.mkdir()
        files = ('install.sh', 'config.json.example', 'bin/system_monitor.py',
                 'cl.mastro.NvidiaSystemMonitor.desktop.in', 'cl.mastro.NvidiaSystemMonitor.Dev.desktop.in',
                 'src/__init__.py', 'src/utils/__init__.py', 'src/utils/channel.py', 'src/utils/installer.py',
                 'assets/icons/cl.mastro.NvidiaSystemMonitor.png')
        for name in files:
            destination = self.project / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
        self.apps = self.root / 'data/applications'
        self.config = self.root / 'config'
        self.state = self.root / 'state'
        self.apps.mkdir(parents=True)
        self.command_log = self.root / 'commands.log'
        commands = self.root / 'commands'
        commands.mkdir()
        for name in ('sudo', 'update-desktop-database'):
            path = commands / name
            path.write_text('#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$MONITOR_COMMAND_LOG"\n')
            path.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.root / 'home'), XDG_DATA_HOME=str(self.root / 'data'),
                        XDG_CONFIG_HOME=str(self.config), XDG_STATE_HOME=str(self.state),
                        PATH=str(commands) + ':/usr/bin:/bin', MONITOR_COMMAND_LOG=str(self.command_log))
        self.stable_launcher = self.apps / STABLE.desktop_id
        self.stable_launcher.write_text(self.launcher(STABLE))
        for path, content in (
            (self.config / STABLE.directory_name / 'config.json', '{"logging_enabled":false}'),
            (self.config / STABLE.directory_name / 'storage-aliases.json', '{"aliases":{}}'),
            (self.state / STABLE.directory_name / 'monitor.log', 'STABLE_ONLY\n'),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def launcher(self, channel, *, explicit=True):
        flag = ' --channel ' + channel.name if explicit else ''
        return ('[Desktop Entry]\nType=Application\nName=' + channel.title + '\nExec=python3 "'
                + str(self.project / 'bin/system_monitor.py') + '"' + flag + '\n')

    def execute(self, *args, success=True):
        result = subprocess.run(['bash', str(self.project / 'install.sh'), *args],
                                cwd=self.project, env=self.env, capture_output=True, text=True, timeout=10)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def stable_snapshot(self):
        paths = [self.stable_launcher]
        paths += list((self.config / STABLE.directory_name).rglob('*'))
        paths += list((self.state / STABLE.directory_name).rglob('*'))
        return {str(p): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns,
                         p.stat().st_mode, p.stat().st_nlink) for p in paths if p.is_file()}

    def assert_channel(self, channel):
        text = (self.apps / channel.desktop_id).read_text()
        fields = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
        selected, remaining = parse_channel_args(shlex.split(fields['Exec'])[1:])
        self.assertEqual(selected, channel)
        self.assertIn('--channel ' + channel.name, fields['Exec'])
        self.assertEqual(remaining, [str(self.project / 'bin/system_monitor.py')])
        self.assertEqual(fields['Name'], channel.title)

    def test_dev_install_never_changes_stable_resources(self):
        before = self.stable_snapshot()
        self.execute('--channel', 'dev')
        self.assert_channel(DEV)
        self.assertEqual(self.stable_snapshot(), before)
        self.assertEqual((self.config / DEV.directory_name / 'config.json').read_bytes(),
                         (self.project / 'config.json.example').read_bytes())
        self.assertNotIn('sudo', self.command_log.read_text())
        self.assertFalse((self.state / DEV.directory_name).exists())  # Installer does not create logs.

    def test_stable_install_preserves_historical_config_and_default(self):
        config = self.config / STABLE.directory_name / 'config.json'
        before = config.read_bytes(), config.stat().st_mtime_ns
        self.execute()
        self.assert_channel(STABLE)
        self.assertEqual((config.read_bytes(), config.stat().st_mtime_ns), before)
        self.assertFalse((self.apps / DEV.desktop_id).exists())
        self.assertIn('sudo', self.command_log.read_text())  # Fake command, never real privilege.
        self.execute('--channel', 'stable')
        self.assert_channel(STABLE)

    def test_legacy_dev_check_is_read_only_and_regeneration_is_isolated(self):
        dev = self.apps / DEV.desktop_id
        dev.write_text(self.launcher(DEV, explicit=False))
        old = dev.read_bytes(), dev.stat().st_mtime_ns
        before = self.stable_snapshot()
        result = self.execute('--channel', 'dev', '--check-launcher', success=False)
        self.assertEqual(result.returncode, 3)
        self.assertIn('antiguo', result.stdout)
        self.assertEqual((dev.read_bytes(), dev.stat().st_mtime_ns), old)
        self.assertFalse(self.command_log.exists())
        self.execute('--channel', 'dev')
        self.assert_channel(DEV)
        self.execute('--channel', 'dev', '--check-launcher')
        self.assertEqual(self.stable_snapshot(), before)

    def test_missing_launcher_check_creates_no_directories(self):
        shutil.rmtree(self.apps.parent)
        result = self.execute('--channel', 'dev', '--check-launcher')
        self.assertIn('ausente', result.stdout)
        self.assertFalse(self.apps.parent.exists())
        self.assertFalse(self.command_log.exists())

    def test_dev_uninstall_preserves_stable_and_dev_preferences(self):
        self.execute('--channel', 'dev')
        before = self.stable_snapshot()
        dev_config = self.config / DEV.directory_name / 'config.json'
        config_bytes = dev_config.read_bytes()
        self.execute('--uninstall', '--channel=dev')
        self.assertFalse((self.apps / DEV.desktop_id).exists())
        self.assertEqual(dev_config.read_bytes(), config_bytes)
        self.assertEqual(self.stable_snapshot(), before)
        self.assertNotIn('sudo', self.command_log.read_text())

    def test_linked_and_dangling_launcher_destinations_are_rejected(self):
        for target in (self.stable_launcher, self.root / 'missing.desktop'):
            with self.subTest(target=target):
                dev = self.apps / DEV.desktop_id
                dev.symlink_to(target)
                before = self.stable_snapshot()
                for action in ((), ('--uninstall',), ('--check-launcher',)):
                    self.execute('--channel', 'dev', *action, success=False)
                    self.assertTrue(dev.is_symlink())
                    self.assertEqual(self.stable_snapshot(), before)
                dev.unlink()
        self.assertFalse(self.command_log.exists())

    def test_hard_link_to_stable_launcher_is_rejected(self):
        dev = self.apps / DEV.desktop_id
        os.link(self.stable_launcher, dev)
        before = self.stable_snapshot()
        self.execute('--channel', 'dev', success=False)
        self.execute('--channel', 'dev', '--uninstall', success=False)
        self.assertEqual(self.stable_snapshot(), before)

    def test_symlinked_parent_directories_are_rejected(self):
        for position in ('applications', 'data', 'config-channel', 'config-root'):
            with self.subTest(position=position):
                before = self.stable_snapshot()
                if position == 'applications':
                    original = self.apps
                elif position == 'data':
                    original = self.apps.parent
                elif position == 'config-channel':
                    original = self.config / DEV.directory_name
                    original.mkdir()
                else:
                    original = self.config
                saved = original.with_name(original.name + '.saved')
                original.rename(saved)
                original.symlink_to(saved, target_is_directory=True)
                try:
                    self.execute('--channel', 'dev', success=False)
                    self.assertFalse((saved / DEV.desktop_id).exists())
                finally:
                    original.unlink()
                    saved.rename(original)
                self.assertEqual(self.stable_snapshot(), before)
        self.assertFalse(self.command_log.exists())

    def test_linked_configuration_is_rejected_before_launcher_write(self):
        dev_config = self.config / DEV.directory_name
        dev_config.mkdir()
        destination = dev_config / 'config.json'
        for target in (self.config / STABLE.directory_name / 'config.json', self.root / 'missing.json'):
            with self.subTest(target=target):
                destination.symlink_to(target)
                before = self.stable_snapshot()
                self.execute('--channel', 'dev', success=False)
                self.assertFalse((self.apps / DEV.desktop_id).exists())
                self.assertEqual(self.stable_snapshot(), before)
                destination.unlink()

    def test_dangling_parent_is_rejected_without_creating_its_target(self):
        missing = self.root / 'missing-config'
        parent = self.config / DEV.directory_name
        parent.symlink_to(missing, target_is_directory=True)
        before = self.stable_snapshot()
        self.execute('--channel', 'dev', success=False)
        self.assertFalse(missing.exists())
        self.assertTrue(parent.is_symlink())
        self.assertEqual(self.stable_snapshot(), before)

    def test_stable_linked_destination_is_rejected_before_privileged_operations(self):
        dev = self.apps / DEV.desktop_id
        dev.write_text(self.launcher(DEV))
        self.stable_launcher.unlink()
        self.stable_launcher.symlink_to(dev)
        before = dev.read_bytes(), dev.stat().st_mtime_ns
        self.execute('--channel', 'stable', success=False)
        self.assertEqual((dev.read_bytes(), dev.stat().st_mtime_ns), before)
        self.assertFalse(self.command_log.exists())

    def test_foreign_channel_launcher_and_wrong_template_are_rejected(self):
        dev = self.apps / DEV.desktop_id
        dev.write_text(self.launcher(STABLE))
        original = dev.read_bytes()
        self.execute('--channel', 'dev', success=False)
        self.execute('--channel', 'dev', '--uninstall', success=False)
        self.assertEqual(dev.read_bytes(), original)
        dev.unlink()
        template = self.project / (DEV.desktop_id + '.in')
        template.write_text(template.read_text().replace('--channel dev', '--channel stable'))
        self.execute('--channel', 'dev', success=False)
        self.assertFalse(dev.exists())
        self.assertFalse(self.command_log.exists())

    def test_atomic_launcher_replace_does_not_follow_raced_symlink(self):
        target = self.apps / DEV.desktop_id
        original_replace = os.replace
        before = self.stable_snapshot()

        def race(source, destination, **kwargs):
            self.assertEqual(destination, DEV.desktop_id)
            target.symlink_to(self.stable_launcher)
            return original_replace(source, destination, **kwargs)

        with installer.directory_fd(self.apps) as descriptor, mock.patch.object(installer.os, 'replace', side_effect=race):
            installer.publish(descriptor, DEV.desktop_id, self.launcher(DEV))
        self.assertFalse(target.is_symlink())
        self.assertEqual(self.stable_snapshot(), before)
        self.assertFalse(list(self.apps.glob('.nvidia-monitor-*')))

    def test_parent_swap_during_replace_keeps_original_directory_pinned(self):
        pinned = self.root / 'pinned-applications'
        foreign = self.root / 'foreign-applications'
        foreign.mkdir()
        protected = foreign / DEV.desktop_id
        protected.write_text('foreign resource')
        original_replace = os.replace

        def race(source, destination, **kwargs):
            self.apps.rename(pinned)
            self.apps.symlink_to(foreign, target_is_directory=True)
            return original_replace(source, destination, **kwargs)

        with installer.directory_fd(self.apps) as descriptor, mock.patch.object(installer.os, 'replace', side_effect=race):
            installer.publish(descriptor, DEV.desktop_id, self.launcher(DEV))
        self.assertEqual(protected.read_text(), 'foreign resource')
        self.assertEqual((pinned / DEV.desktop_id).read_text(), self.launcher(DEV))

    def test_failed_replace_keeps_old_launcher_and_removes_temporary(self):
        target = self.apps / DEV.desktop_id
        target.write_text(self.launcher(DEV, explicit=False))
        before = target.read_bytes(), target.stat().st_mtime_ns
        with installer.directory_fd(self.apps) as descriptor, \
                mock.patch.object(installer.os, 'replace', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                installer.publish(descriptor, DEV.desktop_id, self.launcher(DEV))
        self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns), before)
        self.assertFalse(list(self.apps.glob('.nvidia-monitor-*')))

    def test_configuration_created_during_publication_is_not_overwritten(self):
        directory = self.config / DEV.directory_name
        directory.mkdir()
        destination = directory / 'config.json'
        original_link = os.link

        def race(source, target, **kwargs):
            destination.write_text('{"user":"synthetic preference"}')
            return original_link(source, target, **kwargs)

        with installer.directory_fd(directory) as descriptor, mock.patch.object(installer.os, 'link', side_effect=race):
            installer.publish(descriptor, 'config.json', '{}', preserve=True)
        self.assertEqual(destination.read_text(), '{"user":"synthetic preference"}')
        self.assertFalse(list(directory.glob('.nvidia-monitor-*')))

    def test_uninstall_does_not_follow_symlink_introduced_after_validation(self):
        target = self.apps / DEV.desktop_id
        target.write_text(self.launcher(DEV))
        before = self.stable_snapshot()
        original_unlink = os.unlink

        def race(name, **kwargs):
            original_unlink(name, **kwargs)
            target.symlink_to(self.stable_launcher)
            return original_unlink(name, **kwargs)

        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(installer.os, 'unlink', side_effect=race):
            self.assertEqual(installer.run(self.project, 'dev', action='uninstall'), 0)
        self.assertEqual(self.stable_snapshot(), before)
