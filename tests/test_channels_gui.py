"""Two real GTK applications and log files on an isolated session bus."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.test_gui_smoke import HAVE_GTK
from src.utils.channel import DEV, STABLE

ROOT = Path(__file__).resolve().parent.parent


def probe(channel, directory):
    from gi.repository import GLib, Gtk
    from src.gui.app import SystemMonitorApp

    root = Path(directory)
    app = SystemMonitorApp() if channel == 'stable' else SystemMonitorApp(channel=channel)
    if not app.register(None) or app.get_is_remote():
        raise RuntimeError('Channel did not acquire its own GTK identity')
    app._build_window()
    app.window.present()
    icon = Gtk.IconTheme.get_for_display(app.window.get_display()).lookup_icon(
        app.channel.icon_name, [], 48, 1, Gtk.TextDirection.NONE, Gtk.IconLookupFlags.FORCE_REGULAR)
    app.logger.info('%s_PROCESS_ONLY', channel.upper())
    for handler in app.logger.handlers:
        handler.flush()
    (root / (channel + '.ready')).write_text(json.dumps({
        'id': app.get_application_id(), 'remote': app.get_is_remote(),
        'title': app.window.get_title(), 'config': str(app.config.path),
        'icon': app.window.get_icon_name(),
        'icon_file': icon.get_file().get_path() if icon.get_file() else None,
        'aliases': str(app.disk_aliases.config_path),
        'interval': app.interval, 'logger': app.logger.name,
        'log': app.logger.handlers[0].baseFilename,
    }))
    loop = GLib.MainLoop()

    def check():
        if (root / 'stop').exists():
            loop.quit()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    GLib.timeout_add(20, check)
    loop.run()
    app.window.destroy()
    for handler in app.logger.handlers:
        handler.close()


def coordinator(directory):
    root = Path(directory)
    processes = []
    try:
        for channel in ('stable', 'dev'):
            processes.append(subprocess.Popen(
                [sys.executable, '-B', '-m', 'tests.test_channels_gui', '--probe', channel, directory],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        deadline = time.monotonic() + 10
        while not all((root / (name + '.ready')).exists() for name in ('stable', 'dev')):
            if time.monotonic() >= deadline or any(p.poll() is not None for p in processes):
                raise RuntimeError('GTK probes failed or timed out')
            time.sleep(.02)
        # Both applications still own their IDs while this third process checks
        # that a second stable instance forwards to the first stable instance.
        duplicate = subprocess.run([
            sys.executable, '-B', '-c',
            "from gi.repository import Gtk; "
            "a=Gtk.Application(application_id='cl.mastro.NvidiaSystemMonitor'); "
            "assert a.register(None); assert a.get_is_remote()",
        ], capture_output=True, text=True, timeout=5)
        if duplicate.returncode:
            raise RuntimeError(duplicate.stderr)
        result = {name: json.loads((root / (name + '.ready')).read_text()) for name in ('stable', 'dev')}
        (root / 'stop').touch()
        for process in processes:
            stdout, stderr = process.communicate(timeout=5)
            if process.returncode:
                raise RuntimeError(stdout + stderr)
        print(json.dumps(result))
    finally:
        (root / 'stop').touch()
        for process in processes:
            if process.poll() is None:
                process.terminate()
            stdout, stderr = process.communicate(timeout=5)
            if process.returncode:
                sys.stderr.write(stdout + stderr)


@unittest.skipUnless(HAVE_GTK and shutil.which('dbus-run-session'), 'GTK display or dbus-run-session unavailable')
class ConcurrentChannels(unittest.TestCase):
    def test_real_gtk_ids_titles_configs_and_logs_are_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preserved = {}
            for channel, interval in ((STABLE, 1), (DEV, 3)):
                icons = root / 'data/icons/hicolor/scalable/apps'
                icons.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / 'assets/icons/cl.mastro.NvidiaSystemMonitor.png',
                                icons / (channel.icon_name + '.png'))
                config = root / 'config' / channel.directory_name
                config.mkdir(parents=True)
                config_file = config / 'config.json'
                config_file.write_text(json.dumps({
                    'refresh_interval_seconds': interval, 'logging_enabled': True,
                    'panels': {name: False for name in ('cpu', 'gpu', 'ram', 'storage', 'sensors')},
                }))
                aliases = config / 'storage-aliases.json'
                aliases.write_text('{"aliases":{"wwn-example-A":"Synthetic"}}')
                for path in (config_file, aliases):
                    preserved[path] = (path.read_bytes(), path.stat().st_mtime_ns)
            env = dict(os.environ, HOME=str(root / 'home'), XDG_DATA_HOME=str(root / 'data'),
                       XDG_CONFIG_HOME=str(root / 'config'), XDG_STATE_HOME=str(root / 'state'))
            result = subprocess.run([
                'dbus-run-session', '--', sys.executable, '-B', '-m',
                'tests.test_channels_gui', '--coordinator', directory,
            ], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(result.stdout)
            for channel, interval in ((STABLE, 1), (DEV, 3)):
                observed = data[channel.name]
                self.assertEqual(observed['id'], channel.application_id)
                self.assertFalse(observed['remote'])
                self.assertEqual(observed['title'], channel.title)
                self.assertEqual(observed['icon'], channel.icon_name)
                self.assertEqual(Path(observed['icon_file']),
                                 root / 'data/icons/hicolor/scalable/apps' / (channel.icon_name + '.png'))
                self.assertEqual(observed['interval'], interval)
                self.assertEqual(observed['logger'], channel.logger_name)
                self.assertEqual(Path(observed['config']), root / 'config' / channel.directory_name / 'config.json')
                self.assertEqual(Path(observed['aliases']), root / 'config' / channel.directory_name / 'storage-aliases.json')
                log = root / 'state' / channel.directory_name / 'monitor.log'
                self.assertEqual(Path(observed['log']), log)
                text = log.read_text()
                self.assertIn(channel.name.upper() + '_PROCESS_ONLY', text)
                other = DEV if channel == STABLE else STABLE
                self.assertNotIn(other.name.upper() + '_PROCESS_ONLY', text)
            for path, snapshot in preserved.items():
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), snapshot)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--probe']:
        probe(sys.argv[2], sys.argv[3])
    elif sys.argv[1:2] == ['--coordinator']:
        coordinator(sys.argv[2])
    else:
        unittest.main()
