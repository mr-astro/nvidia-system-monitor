import builtins
import contextlib
import importlib.util
import io
import os
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from src.utils.channel import DEV, STABLE, get_channel, parse_channel_args
from src.utils.config import Config
from src.utils.disk_aliases import DiskAliasResolver
from src.utils.paths import config_dir, dev_config_dir, state_dir

ROOT = Path(__file__).resolve().parent.parent


class Channels(unittest.TestCase):
    def test_historical_stable_identity_and_default(self):
        self.assertIs(get_channel(), STABLE)
        self.assertEqual(STABLE.application_id, 'cl.mastro.NvidiaSystemMonitor')
        self.assertEqual(STABLE.title, 'NVIDIA System Monitor')
        self.assertEqual(STABLE.directory_name, 'nvidia-system-monitor')
        self.assertEqual(STABLE.logger_name, 'nvidia-system-monitor')
        self.assertEqual(STABLE.desktop_id, 'cl.mastro.NvidiaSystemMonitor.desktop')

    def test_dev_identity_is_independent(self):
        self.assertIs(get_channel('dev'), DEV)
        self.assertEqual(DEV.application_id, 'cl.mastro.NvidiaSystemMonitor.Dev')
        self.assertEqual(DEV.title, 'NVIDIA System Monitor DEV')
        self.assertEqual(DEV.directory_name, 'nvidia-system-monitor-dev')
        self.assertNotEqual(DEV.logger_name, STABLE.logger_name)
        self.assertNotEqual(DEV.desktop_id, STABLE.desktop_id)
        for value in ('DEV', '', None, 'production'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                get_channel(value)

    def test_xdg_configs_state_and_aliases_follow_channel(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
            'XDG_CONFIG_HOME': directory + '/config', 'XDG_STATE_HOME': directory + '/state',
        }):
            for channel in (STABLE, DEV):
                with self.subTest(channel=channel.name):
                    config = Path(directory) / 'config' / channel.directory_name
                    state = Path(directory) / 'state' / channel.directory_name
                    self.assertEqual(config_dir(channel.name), config)
                    self.assertEqual(state_dir(channel.name), state)
                    self.assertEqual(Config(channel=channel.name).path, config / 'config.json')
                    self.assertEqual(DiskAliasResolver(channel=channel.name).config_path,
                                     config / 'storage-aliases.json')
            self.assertEqual(Config().path, config_dir('stable') / 'config.json')
            self.assertEqual(DiskAliasResolver().config_path,
                             config_dir('stable') / 'storage-aliases.json')
            self.assertEqual(dev_config_dir(), config_dir('dev'))

    def test_relative_and_empty_xdg_variables_use_historical_fallback(self):
        for invalid in ('relative/path', ''):
            with self.subTest(value=invalid), mock.patch.dict(os.environ, {
                'XDG_CONFIG_HOME': invalid, 'XDG_STATE_HOME': invalid,
            }):
                self.assertEqual(config_dir(), Path.home() / '.config/nvidia-system-monitor')
                self.assertEqual(state_dir(), Path.home() / '.local/state/nvidia-system-monitor')
                self.assertEqual(config_dir('dev'), Path.home() / '.config/nvidia-system-monitor-dev')
                self.assertEqual(state_dir('dev'), Path.home() / '.local/state/nvidia-system-monitor-dev')


class ChannelArguments(unittest.TestCase):
    def test_default_and_both_option_forms_preserve_gtk_arguments(self):
        args = ['monitor', '--display=:99', '--gapplication-service']
        self.assertEqual(parse_channel_args(args), (STABLE, args))
        for selection in (['--channel', 'dev'], ['--channel=dev']):
            supplied = [args[0], args[1], *selection, args[2]]
            self.assertEqual(parse_channel_args(supplied), (DEV, args))
            self.assertEqual(supplied[1], '--display=:99')
        self.assertEqual(parse_channel_args(['monitor', '--channel', 'stable']),
                         (STABLE, ['monitor']))

    def test_delimiter_and_unrelated_arguments_are_preserved_exactly(self):
        argv = ['monitor', '--help', '--channels', '--', '--channel', 'dev']
        self.assertEqual(parse_channel_args(argv), (STABLE, argv))

    def test_bad_missing_and_repeated_channel_are_rejected(self):
        for args in (['--channel'], ['--channel='], ['--channel', 'unknown'],
                     ['--channel', '--display=:99'], ['--channel=dev', '--channel=stable']):
            with self.subTest(args=args), self.assertRaises(ValueError):
                parse_channel_args(['monitor', *args])

    def _entry(self):
        spec = importlib.util.spec_from_file_location('monitor_entry', ROOT / 'bin/system_monitor.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_invalid_argument_is_processed_without_importing_gtk(self):
        original_import = builtins.__import__

        def guarded(name, *args, **kwargs):
            if name == 'gi' or name.startswith(('gi.', 'src.gui')):
                self.fail('GTK was imported before channel validation')
            return original_import(name, *args, **kwargs)

        with mock.patch('builtins.__import__', side_effect=guarded), contextlib.redirect_stderr(io.StringIO()) as error:
            entry = self._entry()
            self.assertEqual(entry.main(['monitor', '--channel=invalid']), 2)
        self.assertIn('Canal no válido', error.getvalue())

    def test_channel_is_removed_before_forwarding_to_gtk(self):
        entry = self._entry()
        gi = types.ModuleType('gi')
        gi.require_version = mock.Mock()
        repository = types.ModuleType('gi.repository')
        repository.Gtk = types.SimpleNamespace(get_major_version=lambda: 4, get_minor_version=lambda: 6)
        app = types.ModuleType('src.gui.app')
        app.main = mock.Mock(return_value=7)
        with mock.patch.dict('sys.modules', {'gi': gi, 'gi.repository': repository, 'src.gui.app': app}):
            self.assertEqual(entry.main(['monitor', '--channel=dev', '--display=:99', '--help']), 7)
        app.main.assert_called_once_with(channel='dev', argv=['monitor', '--display=:99', '--help'])

    def test_direct_entry_rejects_invalid_channel_without_site_packages(self):
        result = subprocess.run([
            sys.executable, '-B', '-S', str(ROOT / 'bin/system_monitor.py'), '--channel=invalid',
        ], cwd=ROOT, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Canal no válido', result.stderr)
        self.assertNotIn('GTK', result.stderr)

    def test_unsupported_gtk_version_fails_before_constructing_application(self):
        entry = self._entry()
        gi = types.ModuleType('gi')
        gi.require_version = mock.Mock()
        repository = types.ModuleType('gi.repository')
        repository.Gtk = types.SimpleNamespace(get_major_version=lambda: 4, get_minor_version=lambda: 4)
        with mock.patch.dict('sys.modules', {'gi': gi, 'gi.repository': repository}), \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(entry.main(['monitor', '--channel=dev']), 1)
        self.assertIn('se requiere GTK >= 4.6', error.getvalue())

    def test_templates_choose_channels_explicitly(self):
        for channel in (STABLE, DEV):
            with self.subTest(channel=channel.name):
                fields = dict(line.split('=', 1) for line in
                              (ROOT / (channel.desktop_id + '.in')).read_text().splitlines() if '=' in line)
                self.assertEqual(fields['Name'], channel.title)
                selected, forwarded = parse_channel_args(shlex.split(fields['Exec'])[1:])
                self.assertEqual(selected, channel)
                self.assertEqual(forwarded, ['@APP_DIR@/bin/system_monitor.py'])
