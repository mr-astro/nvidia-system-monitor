"""Scoped filesystem usage for the inner ring. No GTK, I/O or disk aggregation."""
from dataclasses import dataclass, replace
import math

from src.data.storage import NON_FILESYSTEM_TYPES
from src.utils.formatting import fmt_bytes


@dataclass(frozen=True)
class UsageModel:
    state: str = 'unavailable'
    scope: str = 'Interior · Uso no disponible'
    summary: str = 'Uso no disponible'
    reason: str = 'Sin una medición verificable'
    details: str = 'El anillo interior nunca representa utilización global del disco.'
    fraction: float | None = None
    root_selected: bool = False


def _numbers(part):
    values = (part.used_bytes, part.free_bytes, part.usage_percent)
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
               and math.isfinite(v) and v >= 0 for v in values):
        return False
    denominator = part.used_bytes + part.free_bytes
    return (denominator > 0 and math.isfinite(denominator) and part.usage_percent <= 100
            and abs(100 * part.used_bytes / denominator - part.usage_percent) <= 0.050001)


def prepare_usage(disk, sources, errors, now):
    """Choose one measured scope, never infer shared identity from equal readings.

    The monitor's df-style percentage uses used + available (not physical SIZE).
    Repeated technical nodes can be coalesced only if their readings agree.
    Btrfs UUID/membership/qgroup data are not published here: no totals across
    distinct nodes, devices or subvolumes can be justified by this presentation.
    """
    direct = disk.direct_filesystem
    candidates = ([direct] if direct is not None and direct.fstype not in NON_FILESYSTEM_TYPES
                  else disk.partitions)
    groups, conflicts = {}, set()
    for part in candidates:
        if part.fstype in NON_FILESYSTEM_TYPES:
            continue
        previous = groups.get(part.name)
        if previous is not None:
            if (previous.fstype, previous.used_bytes, previous.free_bytes, previous.usage_percent) != (
                    part.fstype, part.used_bytes, part.free_bytes, part.usage_percent):
                conflicts.add(part.name)
                part = replace(previous, mountpoints=list(dict.fromkeys(previous.mountpoints + part.mountpoints)))
            part = replace(part, mountpoints=list(dict.fromkeys(previous.mountpoints + part.mountpoints)))
        groups[part.name] = part
    mounted = [p for p in groups.values() if any(m.startswith('/') for m in p.mountpoints)]

    def state_for(part):
        return sources.get('usage:' + part.name)

    def error_for(part):
        state = state_for(part)
        return errors.get('usage:' + part.name) or (state.error if state else None)

    roots = [p for p in mounted if '/' in p.mountpoints]
    if len(roots) > 1:
        return UsageModel(state='ambiguous', scope='Interior · FS /',
                          summary='Uso no disponible', reason='Montaje raíz contradictorio · ver detalles',
                          details='Varios nodos distintos declaran /; no se elige ni suma ninguno.')
    root = roots[0] if roots else None
    if conflicts and (root is None or root.name in conflicts):
        return UsageModel(state='ambiguous', scope='Interior · FS /' if root is not None else 'Interior · uso de filesystem',
                          summary='Uso ambiguo · ver detalles',
                          reason='Lecturas contradictorias para ' + ', '.join(sorted(conflicts)))
    backing = disk.order.root
    if root is None and backing.available and disk.name in backing.disks:
        return UsageModel(scope='Interior · FS /', summary='Uso no disponible',
                          reason='Filesystem raíz sin montaje verificable',
                          details='El respaldo físico de / está identificado, pero no su lectura de filesystem. '
                          'No se sustituye por el uso de otro montaje.')
    fresh = [p for p in mounted if _numbers(p) and state_for(p)
             and state_for(p).acquired_at is not None and not state_for(p).stale(now) and not error_for(p)]
    if root is None and (len(fresh) > 1 or (not fresh and len(mounted) > 1)):
        readings = []
        for part in mounted:
            state = state_for(part)
            status = ('error' if error_for(part) else 'obsoleto' if state and state.stale(now)
                      else 'reciente' if part in fresh else 'no disponible')
            percent = f'{part.usage_percent:.1f}%' if _numbers(part) else 'N/D'
            readings.append(f'{part.name} · {part.fstype} · {percent} ({status})')
        return UsageModel(state='multiple', summary='Varios filesystems · ver detalles',
                          reason='Sin porcentaje agregado', details='\n'.join(readings) +
                          '\nNo se suman sistemas de archivos ni se deduce utilización física.')
    part = root if root is not None else fresh[0] if fresh else mounted[0] if mounted else next(iter(groups.values()), None)
    if part is None:
        return UsageModel()
    mounts = tuple(dict.fromkeys(m for m in part.mountpoints if m.startswith('/')))
    location = (mounts[0] if len(mounts) == 1 else
                f'{len(mounts)} montajes' + (' compartidos' if part.fstype == 'btrfs' else ''))
    if root is not None:
        scope = f'Interior · FS / · {part.name} · {part.fstype}'
        if len(mounts) > 1:
            scope += ' · ' + location
    elif mounts:
        scope = f'Interior · FS {part.name} · {part.fstype} · {location}'
    else:
        scope = 'Interior · Uso no disponible · sin montar'
    state = state_for(part)
    age = state.age(now) if state else None
    measured = f'hace {age:.0f} s' if age is not None else 'sin medición válida'
    details = (f'Lectura publicada para {part.name} ({part.fstype})\n'
               f'Montajes: {", ".join(mounts) or "Sin montar"}\n'
               'Alcance: filesystem publicado, nunca utilización global del disco.\n'
               'Denominador: usado + disponible según statvfs/df; no capacidad física.\n')
    if part.fstype == 'btrfs':
        details += ('Una lectura compartida; no se suman subvolúmenes. No atribuye uso '
                    'individual a subvolúmenes ni a miembros físicos de Btrfs.\n')
    if root is not None:
        details += 'Selección prioritaria: filesystem asociado a /. Los demás filesystems no se agregan.\n'
        if conflicts:
            details += 'Otros nodos con lecturas contradictorias: ' + ', '.join(sorted(conflicts)) + '\n'
    elif len(groups) > 1:
        details += 'Solo este filesystem tiene una lectura reciente verificable; los demás están en detalles.\n'
    if _numbers(part):
        details += (f'Última lectura: {part.usage_percent:.1f}% · '
                    f'{fmt_bytes(part.used_bytes)} usados · {fmt_bytes(part.free_bytes)} disponibles\n')
    details += measured
    error = error_for(part)
    if error:
        return UsageModel('error', scope, 'Error de uso', 'Error de adquisición · ver detalles',
                          details + '\n' + error, root_selected=root is not None)
    if not mounts:
        return UsageModel('unavailable', scope, 'Uso no disponible', 'Sin montar; sin uso verificable', details)
    if state is not None and state.acquired_at is not None and state.stale(now):
        return UsageModel('stale', scope, 'Uso obsoleto', 'Última medición · ' + measured, details,
                          root_selected=root is not None)
    if state is None or state.acquired_at is None or not _numbers(part):
        return UsageModel('unavailable', scope, 'Uso no disponible', 'Sin una medición coherente y verificable', details,
                          root_selected=root is not None)
    percent = part.usage_percent
    summary = f'{percent:.1f}% usado · {100 - percent:.1f}% disponible'.replace('.', ',')
    note = 'Reciente · ' + measured + (' · revisar detalles' if conflicts else '')
    return UsageModel('current', scope, summary, note, details, percent / 100,
                      root_selected=root is not None)
