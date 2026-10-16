"""Per-user channel resources: no-follow traversal and atomic file publication.

Descriptors pin the selected directories even if a parent path is replaced.
This is not a security boundary against malicious processes with the same UID.
Each file is published atomically; the installation is not a joint transaction.
"""
from __future__ import annotations

import argparse
import contextlib
import os
from pathlib import Path
import secrets
import shlex
import shutil
import stat
import subprocess
import sys

from src.utils.channel import DEFAULT_CHANNEL, get_channel, parse_channel_args

CACHE_TIMEOUT = 3


class UnsafeDestination(ValueError):
    pass


@contextlib.contextmanager
def directory_fd(path, *, create=False):
    """Open every component relative to its parent, never following symlinks."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise UnsafeDestination(f"Directorio no absoluto o con '..': {path}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open('/', flags)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def regular_destination(descriptor, name):
    """lstat also catches dangling symlinks; reject shared hard-linked inodes."""
    try:
        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise UnsafeDestination(f"Destino enlazado o no regular: {name}")
    return info


def read_destination(descriptor, name, *, binary=False):
    info = regular_destination(descriptor, name)
    if info is None:
        return None
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                 dir_fd=descriptor)
    options = {} if binary else {'encoding': 'utf-8'}
    with os.fdopen(fd, 'rb' if binary else 'r', **options) as stream:
        actual = os.fstat(stream.fileno())
        if (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
            raise UnsafeDestination(f"Destino cambió durante la lectura: {name}")
        return stream.read()


def launcher_channel(content, channel):
    """Accept only this app/channel, allowing its historical missing option."""
    fields = {}
    section = None
    for line in content.splitlines():
        if line.startswith('['):
            section = line
        elif section == '[Desktop Entry]' and '=' in line:
            key, value = line.split('=', 1)
            if key in fields:
                raise UnsafeDestination(f"Campo duplicado de launcher: {key}")
            fields[key] = value
    if fields.get('Type') != 'Application' or fields.get('Name') != channel.title:
        raise UnsafeDestination('El launcher no corresponde al canal seleccionado')
    args = shlex.split(fields.get('Exec', ''))
    if len(args) < 2 or args[0] != 'python3' or not args[1].endswith('/bin/system_monitor.py'):
        raise UnsafeDestination('Exec no corresponde a NVIDIA System Monitor')
    selected, remaining = parse_channel_args(args[1:])
    explicit = len(remaining) != len(args[1:])
    if explicit and selected != channel:
        raise UnsafeDestination('Exec selecciona otro canal')
    return explicit


def publish(descriptor, name, content, *, preserve=False):
    """Publish a fresh inode; preserve=True atomically refuses overwriting config."""
    if regular_destination(descriptor, name) is not None and preserve:
        return
    temporary = '.nvidia-monitor-' + secrets.token_hex(16)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                 | os.O_CLOEXEC, 0o600, dir_fd=descriptor)
    try:
        options = {} if isinstance(content, bytes) else {'encoding': 'utf-8'}
        with os.fdopen(fd, 'wb' if isinstance(content, bytes) else 'w', **options) as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        regular_destination(descriptor, name)
        if preserve:
            try:
                os.link(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor,
                        follow_symlinks=False)
            except FileExistsError:
                regular_destination(descriptor, name)
        else:
            os.replace(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
    finally:
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass


def locations(channel):
    def xdg(variable, fallback):
        value = os.environ.get(variable)
        return Path(value) if value and os.path.isabs(value) else fallback

    apps = xdg('XDG_DATA_HOME', Path.home() / '.local/share') / 'applications'
    config = xdg('XDG_CONFIG_HOME', Path.home() / '.config') / channel.directory_name
    return apps, config


def refresh_database(descriptor):
    command = shutil.which('update-desktop-database')
    if command:
        # Pin the directory for this optional cache refresh as well.
        try:
            subprocess.run([command, f'/proc/self/fd/{descriptor}'], pass_fds=(descriptor,),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, timeout=CACHE_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            pass  # Publication already succeeded; optional caches are best effort.


def source_payloads(app_dir, channel):
    """Read every required source before publishing any destination."""
    app_dir = Path(app_dir)
    if not app_dir.is_absolute() or any(c in str(app_dir) for c in '"`$\\%\n\r'):
        raise UnsafeDestination('Ruta del proyecto no soportada por Exec')

    def read(relative, *, binary=False):
        source = app_dir / relative
        with directory_fd(source.parent) as descriptor:
            content = read_destination(descriptor, source.name, binary=binary)
        if content is None:
            raise FileNotFoundError(f'Fuente ausente: {source}')
        return content

    template = read(channel.desktop_id + '.in').replace('@APP_DIR@', str(app_dir))
    if not launcher_channel(template, channel):
        raise UnsafeDestination('La plantilla debe seleccionar su canal explícitamente')
    fields = dict(line.split('=', 1) for line in template.splitlines() if '=' in line)
    if fields.get('Icon') != channel.icon_name:
        raise UnsafeDestination('La plantilla debe seleccionar el icono de su canal')
    read('bin/system_monitor.py')
    config = read('config.json.example')
    icon = read('assets/icons/cl.mastro.NvidiaSystemMonitor.png', binary=True)
    return template, config, icon


def run(app_dir, channel_name=DEFAULT_CHANNEL, *, action='install'):
    channel = get_channel(channel_name)
    apps, config = locations(channel)
    icons = apps.parent / 'icons/hicolor/scalable/apps'
    name = channel.desktop_id  # No caller-provided filenames.
    icon_name = channel.icon_name + '.png'
    if action not in ('install', 'uninstall', 'check', 'validate'):
        raise ValueError(f'Acción no válida: {action}')
    payloads = source_payloads(app_dir, channel) if action in ('install', 'validate') else None
    with contextlib.ExitStack() as stack:
        # Pin and validate ALL selected destinations before the first write or
        # deletion. Missing directories are harmless for read-only/uninstall.
        def open_directory(path):
            try:
                return stack.enter_context(directory_fd(path, create=action == 'install'))
            except FileNotFoundError:
                if action == 'install':
                    raise
                return None

        apps_fd = open_directory(apps)
        existing = read_destination(apps_fd, name) if apps_fd is not None else None
        explicit = launcher_channel(existing, channel) if existing is not None else None
        if action == 'check':
            if existing is None:
                print(f'Launcher ausente: {apps / name}')
            elif channel.name == 'dev' and not explicit:
                print(f'Launcher DEV antiguo: {apps / name}; regenérelo con --channel dev')
                return 3
            else:
                print(f'Launcher compatible: {apps / name}')
            return 0

        icons_fd = open_directory(icons)
        if icons_fd is not None:
            regular_destination(icons_fd, icon_name)
        if action == 'uninstall':
            for descriptor, filename in ((apps_fd, name), (icons_fd, icon_name)):
                if descriptor is not None and regular_destination(descriptor, filename) is not None:
                    os.unlink(filename, dir_fd=descriptor)
            if apps_fd is not None:
                refresh_database(apps_fd)
            print(f'Launcher e icono eliminados para {channel.name}; preferencias conservadas')
            return 0

        config_fd = open_directory(config)
        if config_fd is not None:
            regular_destination(config_fd, 'config.json')
        if action == 'validate':
            return 0
        template, config_content, icon = payloads
        publish(config_fd, 'config.json', config_content, preserve=True)
        publish(icons_fd, icon_name, icon)
        publish(apps_fd, name, template)
        refresh_database(apps_fd)
        print(f'Launcher e icono instalados para {channel.name}: {apps / name}')
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channel', choices=('dev', 'stable'), default=DEFAULT_CHANNEL)
    parser.add_argument('--app-dir', type=Path, required=True)
    parser.add_argument('--action', choices=('install', 'uninstall', 'check', 'validate'), default='install')
    args = parser.parse_args(argv)
    try:
        return run(args.app_dir, args.channel, action=args.action)
    except (OSError, ValueError) as exc:
        print(f'Instalación rechazada: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
