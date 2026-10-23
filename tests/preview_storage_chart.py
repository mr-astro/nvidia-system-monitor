"""Interactive synthetic GTK preview. Never constructs monitors or queries hardware.

Run from the DEV root: python3 -B -m tests.preview_storage_chart --case all
"""
import argparse
from dataclasses import replace

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

from src.data.collector import Snapshot
from src.data.errors import SourceState
from src.data.storage import DiskMetrics, PartitionMetrics
from src.data.storage_layout import build_layout
from src.data.storage_presentation import StoragePresentation
from src.gui.app import build_css
from src.gui.storage import StoragePanel
from src.utils.config import DEFAULT_CONFIG
from src.utils.disk_aliases import DiskIdentity
from tests.storage_fixtures import disk, part, presence


CASES = ('partitions', 'empty', 'direct', 'tiny', 'stale', 'invalid', 'single',
         'multiple', 'btrfs', 'ntfs', 'usage_stale', 'usage_error', 'usage_missing', 'ambiguous', 'root',
         'normal', 'attention', 'critical')
COLOR_CASES = ('normal', 'attention', 'critical', 'ntfs')
USAGE_CASES = ('root', 'single', 'multiple', 'btrfs', 'ntfs', 'usage_stale', 'usage_error', 'usage_missing', 'ambiguous',
               'normal', 'attention', 'critical')


def sample(case, index):
    name = f'sd{chr(97 + index)}'
    capacity = 2**40
    children = [part(name + '1', parent=name, start=2048, sectors=2**21, partn=1),
                part(name + '2', parent=name, start=2048 + 2**21, sectors=3 * 2**29,
                     number=f'8:{index * 16 + 2}', partn=2, fstype='crypto_LUKS')]
    node = disk(name, f'8:{index * 16}', size=capacity, children=children)
    title = 'Disco simulado · particiones y huecos físicos'
    if case == 'empty':
        node.update(size=128 * 2**30, children=[])
        title = 'Disco simulado · sin particiones'
    elif case == 'direct':
        node.update(size=512 * 2**30, children=[], pttype=None, fstype='btrfs')
        title = 'Disco simulado · Btrfs en dispositivo completo'
    elif case == 'tiny':
        node['children'] = [part(name + '1', parent=name, start=2048, sectors=1)]
        title = 'Disco simulado · partición de 512 B'
    elif case == 'stale':
        title = 'Disco simulado · topología obsoleta'
    elif case == 'invalid':
        node['children'][0]['start'] = None
        title = 'Disco simulado · geometría incompleta'
    elif case in USAGE_CASES:
        node['children'][1]['fstype'] = 'btrfs' if case in {'btrfs', 'root', 'normal', 'attention', 'critical'} else 'ext4'
        title = {'single': 'Disco simulado · un filesystem medible',
                 'multiple': 'Disco simulado · varios filesystems',
                 'btrfs': 'Disco simulado · Btrfs compartido',
                 'ntfs': 'Disco simulado · NTFS sin montar',
                 'usage_stale': 'Disco simulado · uso obsoleto',
                 'usage_error': 'Disco simulado · error de lectura',
                 'usage_missing': 'Disco simulado · uso no disponible',
                 'ambiguous': 'Disco simulado · lecturas contradictorias',
                 'root': 'Disco simulado · raíz y otros filesystems',
                 'normal': 'Disco simulado · utilización normal',
                 'attention': 'Disco simulado · atención',
                 'critical': 'Disco simulado · crítico'}[case]
        if case == 'ntfs':
            node['children'] = [part(name + '1', parent=name, start=2048, sectors=2**30, fstype='ntfs')]
    source = SourceState(10, 11, 20)
    layout = build_layout(node, presence(node), source)
    if case == 'stale':
        layout = replace(layout, source=SourceState(0, 0, 5))
    partitions = [PartitionMetrics(p['name'], fstype=p['fstype'], size_bytes=p['size'])
                  for p in node['children']]
    metric = DiskMetrics(name, model=title, kind='SSD SATA', transport='sata',
                         size_bytes=node['size'], temperature_c=35 + index,
                         temperature_source='temperature:' + name, partitions=partitions,
                         layout=layout)
    if case == 'direct':
        metric.direct_filesystem = PartitionMetrics(name, fstype='btrfs', size_bytes=node['size'],
                                                    mountpoints=['/datos'], used_bytes=128 * 2**30,
                                                    free_bytes=384 * 2**30, usage_percent=25)
    elif case in USAGE_CASES and case != 'ntfs':
        fs = metric.partitions[1]
        fs.mountpoints = ['/datos']
        fs.used_bytes, fs.free_bytes, fs.usage_percent = 576 * 2**30, 192 * 2**30, 75
        if case in {'multiple', 'root', 'normal', 'attention', 'critical'}:
            other = metric.partitions[0]
            other.mountpoints = ['/arranque']
            other.used_bytes, other.free_bytes, other.usage_percent = 2**30 // 4, 3 * 2**30 // 4, 25
            if case in {'root', 'normal', 'attention', 'critical'}:
                fs.mountpoints = ['/', '/home']
                other.fstype, other.mountpoints = 'vfat', ['/boot/efi']
                if case != 'root':
                    percent = {'normal': 40, 'attention': 75, 'critical': 95}[case]
                    fs.used_bytes = fs.size_bytes * percent // 100
                    fs.free_bytes, fs.usage_percent = fs.size_bytes - fs.used_bytes, percent
        elif case == 'btrfs':
            fs.mountpoints = ['/']
            metric.partitions.append(replace(fs, mountpoints=['/home']))
        elif case == 'ambiguous':
            metric.partitions.append(replace(fs, mountpoints=['/otro'], used_bytes=192 * 2**30,
                                             free_bytes=576 * 2**30, usage_percent=25))
        elif case == 'usage_missing':
            fs.used_bytes = fs.free_bytes = fs.usage_percent = None
    return metric


def make_preview(case='all', theme='dark', width=960, height=850):
    selected = (COLOR_CASES if case == 'colors' else ('root', 'ntfs', 'ntfs') if case == 'fedora' else
                CASES if case == 'all' else USAGE_CASES if case == 'usage' else (case,))
    metrics = [sample(name, i) for i, name in enumerate(selected)]
    if case == 'fedora':
        # Explicit synthetic scenario, not hardware detection or alias inference.
        for metric, model, kind in zip(metrics, ('Dahua C970', 'Samsung PM9C1b', 'Kingston A400'),
                                       ('NVMe', 'NVMe', 'SSD SATA')):
            metric.model, metric.kind = model + ' · SIMULADO', kind
    sources = {'topology': SourceState(10, 11, 20)}
    sources.update({'temperature:' + d.name: SourceState(10, 11, 20) for d in metrics})
    sources.update({'usage:' + d.name: SourceState(10, 11, 20) for d in metrics if d.direct_filesystem})
    for selected_case, metric in zip(selected, metrics):
        for fs in metric.partitions:
            if fs.mountpoints:
                sources['usage:' + fs.name] = (SourceState(0, 0, 5) if selected_case == 'usage_stale' else
                                               SourceState(10, 11, 20, 'statvfs: error simulado') if selected_case == 'usage_error'
                                               else SourceState(10, 11, 20))
    snap = Snapshot(sequence=1, timestamp=10, disks=metrics, sources=sources,
                    storage_presentation=StoragePresentation(identities=tuple(
                        (d.name, DiskIdentity('synthetic:' + d.name, 'Alias que no sustituye al modelo')) for d in metrics)))
    window = Gtk.Window(title='NVIDIA System Monitor DEV · DATOS SIMULADOS',
                        default_width=width, default_height=height)
    window.add_css_class('monitor-window')
    if theme == 'dark':
        window.add_css_class('monitor-dark')
    root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    for method in (root.set_margin_top,root.set_margin_bottom,root.set_margin_start,root.set_margin_end):
        method(12)
    caption = Gtk.Label(label='DEV · ALMACENAMIENTO · DATOS SIMULADOS', xalign=0)
    caption.add_css_class('monitor-channel')
    root.append(caption)
    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroller.set_vexpand(True)
    panel = StoragePanel(usage_thresholds=DEFAULT_CONFIG['storage_usage']['thresholds'])
    panel.update(snap, 11)
    scroller.set_child(panel)
    root.append(scroller)
    window.set_child(root)
    provider = Gtk.CssProvider()
    provider.load_from_data(build_css(DEFAULT_CONFIG['color_scheme'], theme == 'dark',
                            storage_colors=DEFAULT_CONFIG['storage_usage']['color_scheme']).encode())
    Gtk.StyleContext.add_provider_for_display(window.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    # Retain provider/panel for the capture harness and visual inspection.
    window.preview_provider, window.preview_panel = provider, panel
    return window


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=('all', 'usage', 'fedora', 'colors', *CASES), default='usage')
    parser.add_argument('--theme', choices=('dark', 'system'), default='dark')
    parser.add_argument('--width', type=int, default=960)
    parser.add_argument('--height', type=int, default=850)
    args = parser.parse_args()
    app = Gtk.Application(application_id='cl.mastro.NvidiaSystemMonitor.ChartPreview')
    def activate(application):
        window = make_preview(args.case, args.theme, args.width, args.height)
        application.add_window(window)
        window.present()
    app.connect('activate', activate)
    app.run([])


if __name__ == '__main__':
    main()
