"""SMART presentation only: no acquisition, GTK, wall clock or device I/O."""
from dataclasses import dataclass


@dataclass(frozen=True)
class SmartView:
    state: str
    status: str
    health: str
    wear: str
    hours: str
    freshness: str
    warnings_title: str
    warnings: tuple[str, ...]
    error: str | None
    detail: str
    failed: bool


_HEALTH = {'passed': 'Sin fallo declarado', 'failed': 'Fallo declarado', 'unknown': 'N/D'}
_STATUS = {'valid': 'Vigente', 'unknown': 'Desconocido', 'stale': 'Obsoleto', 'error': 'Error de consulta'}


def prepare_smart(reading, now):
    """Project one immutable measurement without asserting historical validity.

    Percentage Used must come from an acquisition path that validates NVMe
    fields. ATA attributes and unrecognised sources never provide wear.
    """
    age = reading.state.age(now)
    current = reading.state.current(now)
    used = reading.percentage_used
    wear = (f'{used}%' if reading.source in ('udisks2-nvme', 'smartctl')
            and type(used) is int and 0 <= used <= 255 else 'N/D')
    hours = (f'{reading.power_on_hours} h' if type(reading.power_on_hours) is int
             and reading.power_on_hours >= 0 else 'N/D')
    health = _HEALTH.get(reading.health, 'N/D')
    if reading.state.error:
        state = 'error'
    elif age is not None and not current:
        state = 'stale'
    elif not current or (health, wear, hours) == ('N/D', 'N/D', 'N/D'):
        state = 'unknown'
    else:
        state = 'valid'
    if age is None:
        freshness = 'Sin lectura válida'
    elif current:
        freshness = f'hace {age:.0f} s · caduca en {max(0, reading.state.max_age - age):.0f} s'
    else:
        expiry = 'caducada' if age > reading.state.max_age else 'no vigente'
        freshness = f'Lectura anterior · hace {age:.0f} s · {expiry}'
    source = {'udisks2-ata': 'UDisks2 · ATA', 'udisks2-nvme': 'UDisks2 · NVMe',
              'udisks2': 'UDisks2', 'smartctl': 'smartctl'}.get(reading.source)
    if source:
        freshness = source + ' · ' + freshness

    warnings = list(dict.fromkeys(reading.warnings)) if age is not None else []
    crc = reading.interface_crc_errors
    if age is not None and type(crc) is int and crc > 0:
        previous = f'Contador histórico CRC de interfaz SATA: {crc}'
        warnings = [warning for warning in warnings if warning != previous]
        warnings.append(f'CRC acumulado de interfaz SATA: {crc}. Es histórico; no declara un fallo actual.')

    fields = [f'Salud SMART: {health}', f'Percentage Used (desgaste NVMe): {wear}',
              f'Horas de funcionamiento: {hours}']
    detail = [f'Fuente SMART: {reading.source or "N/D"}', _STATUS[state], freshness]
    if reading.state.error:
        detail.append(reading.state.error)
    if age is not None:
        detail.append('Lectura SMART' if current else 'Última lectura (anterior)')
        detail.extend(fields)
        detail.append(f'Caducidad: {reading.state.max_age:g} s desde la medición')
        if warnings:
            detail.append('Advertencias: ' + ' · '.join(warnings))
        for value, title, suffix in (
            (reading.media_errors, 'Errores de integridad de medios NVMe', ''),
            (reading.error_log_entries, 'Entradas del registro de errores NVMe', ''),
            (reading.available_spare, 'Reserva disponible NVMe', '%'),
            (reading.unsafe_shutdowns, 'Apagados inseguros NVMe (contador histórico)', ''),
            (crc, 'CRC de interfaz SATA (contador histórico)', ''),
        ):
            if value is not None:
                detail.append(f'{title}: {value}{suffix}')
        if reading.critical_warning is not None:
            detail.append(f'Critical Warning NVMe: 0x{reading.critical_warning:02x}')
        for attribute in reading.ata_attributes:
            detail.append(f'Atributo ATA {attribute.id}: {attribute.name} · normalizado {attribute.value}'
                          ' · sin interpretación de desgaste')
        if reading.exit_status is not None:
            detail.append(f'Código de salida smartctl: {reading.exit_status}')
    detail.append('Percentage Used estima resistencia consumida; no mide espacio ocupado ni vida restante garantizada.')
    return SmartView(state, _STATUS[state], health if current else 'N/D', wear if current else 'N/D',
                     hours if current else 'N/D', freshness,
                     'Advertencias' if current else 'Advertencias de la lectura anterior',
                     tuple(warnings), reading.state.error, '\n'.join(detail), current and reading.health == 'failed')
