"""Read-only ATA/NVMe SMART from UDisks2, on the collector thread.

The ObjectManager snapshot is shared with temperature acquisition. Attribute
reads are bracketed by matching physical identity and SMART properties, under
one bounded D-Bus deadline. No SmartUpdate or interactive authorization is used.
"""
from dataclasses import replace
import time

from src.data.errors import SourceState
from src.data.smart import AtaAttribute, SmartMetrics
from src.data.udisks import ATA, BLOCK, DRIVE, NVME, UDisksTemperature, _NAME


def _uint(data, key, maximum=2**64 - 1):
    if key not in data:
        return None
    value = data[key]
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"UDisks2: campo {key} inválido")
    return value


def _supported(interface, properties):
    if interface == NVME:
        if properties.get('State') != 'live':
            raise ValueError('UDisks2: controlador NVMe no disponible')
    elif interface != ATA or properties.get('SmartSupported') is not True or properties.get('SmartEnabled') is not True:
        raise ValueError('UDisks2: SMART no disponible o deshabilitado')


def parse_properties(interface, properties, attributes):
    """Decode documented units only; never infer ATA wear from an ID/name."""
    _supported(interface, properties)
    temperature = properties.get('SmartTemperature')
    if temperature is not None:
        if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                or not 0 <= temperature <= 1273.15):
            raise ValueError('UDisks2: temperatura inválida')
        temperature = temperature - 273.15 if temperature > 0 else None
    warnings = []
    if interface == NVME:
        if not isinstance(attributes, dict):
            raise ValueError('UDisks2: controlador o atributos NVMe no disponibles')
        critical = properties.get('SmartCriticalWarning')
        if critical is not None and (not isinstance(critical, (list, tuple))
                                     or any(not isinstance(item, str) or not item for item in critical)):
            raise ValueError('UDisks2: SmartCriticalWarning inválido')
        if critical:
            warnings.extend('Advertencia crítica NVMe: ' + value for value in critical)
        used = _uint(attributes, 'percent_used', 255)
        spare = _uint(attributes, 'avail_spare', 100)
        media = _uint(attributes, 'media_errors')
        entries = _uint(attributes, 'num_err_log_entries')
        unsafe = _uint(attributes, 'unsafe_shutdowns')
        hours = _uint(properties, 'SmartPowerOnHours')
        if all(value is None for value in (critical, used, spare, media, entries, unsafe, hours, temperature)):
            raise ValueError('UDisks2: sin atributos SMART disponibles')
        if used is not None and used >= 100:
            warnings.append('Percentage Used NVMe ha alcanzado el 100% de resistencia estimada')
        if media:
            warnings.append(f'Errores de integridad de medios NVMe: {media}')
        if entries:
            warnings.append(f'Entradas del registro de errores NVMe: {entries}')
        return SmartMetrics(health='unknown' if critical is None else 'failed' if critical else 'passed',
                            percentage_used=used, available_spare=spare, unsafe_shutdowns=unsafe,
                            media_errors=media, error_log_entries=entries, temperature_c=temperature,
                            power_on_hours=hours or None, warnings=tuple(warnings), source='udisks2-nvme')
    failing = properties.get('SmartFailing')
    if failing is not None and type(failing) is not bool:
        raise ValueError('UDisks2: SmartFailing inválido')
    seconds = _uint(properties, 'SmartPowerOnSeconds')
    for key in ('SmartNumAttributesFailing', 'SmartNumAttributesFailedInThePast', 'SmartNumBadSectors'):
        value = properties.get(key)
        if value is not None and (type(value) is not int or value < -1 or value > 2**63 - 1):
            raise ValueError(f'UDisks2: campo {key} inválido')
        if value is not None and value > 0:
            warnings.append(f'{key}: {value}')
    if not isinstance(attributes, (list, tuple)):
        raise ValueError('UDisks2: tabla de atributos ATA inválida')
    rows, crc = [], None
    for row in attributes:
        if (not isinstance(row, (list, tuple)) or len(row) != 9
                or not isinstance(row[1], str) or not row[1] or not isinstance(row[8], dict)):
            raise ValueError('UDisks2: atributo ATA malformado')
        bounds = {0: (0, 255), 2: (0, 65535), 3: (-1, 2**31-1), 4: (-1, 2**31-1),
                  5: (-1, 2**31-1), 6: (-2**63, 2**63-1), 7: (0, 2**31-1)}
        if any(type(row[index]) is not int or not low <= row[index] <= high
               for index, (low, high) in bounds.items()):
            raise ValueError('UDisks2: valor ATA inválido')
        if any(item.id == row[0] for item in rows):
            raise ValueError('UDisks2: identificador ATA duplicado')
        rows.append(AtaAttribute(*row[:8]))
        # Require corroborating name AND documented dimensionless unit. The
        # neighbouring vendor attribute 231 is deliberately never mapped.
        if row[0] == 199 and row[1] == 'udma-crc-error-count' and row[7] == 1 and row[6] >= 0:
            crc = row[6]
    if crc:
        warnings.append(f'Contador histórico CRC de interfaz SATA: {crc}')
    if failing:
        warnings.append('SMART indica fallo de salud')
    if failing is None and seconds is None and temperature is None and not rows:
        raise ValueError('UDisks2: sin atributos SMART disponibles')
    return SmartMetrics(health='unknown' if failing is None else 'failed' if failing else 'passed',
                        power_on_hours=seconds // 3600 if seconds else None,
                        temperature_c=temperature, warnings=tuple(warnings), source='udisks2-ata',
                        interface_crc_errors=crc, ata_attributes=tuple(rows))


class UDisksSMART(UDisksTemperature):
    def __init__(self, *args, attributes=None, attribute_interval=60.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.attribute_interval = max(10.0, attribute_interval)
        self._attributes = attributes or self._get_attributes
        self._health_samples = {}
        self._health_next = {}
        self._health_properties = {}

    def prepare(self, contexts):
        super().prepare(contexts)
        for mapping in (self._health_samples, self._health_next, self._health_properties):
            for old in mapping.keys() - contexts:
                mapping.pop(old)

    def invalidate(self, context):
        super().invalidate(context)
        self._health_samples.pop(context, None)
        self._health_next.pop(context, None)
        self._health_properties.pop(context, None)

    def rebind(self, old, new):
        super().rebind(old, new)
        for mapping in (self._health_samples, self._health_next, self._health_properties):
            if old in mapping:
                mapping[new] = mapping.pop(old)

    def _get_attributes(self, path, interface):
        from gi.repository import GLib
        parameters = GLib.Variant('(a{sv})', ({'auth.no_user_interaction': GLib.Variant('b', True)},))
        signature = '(a{sv})' if interface == NVME else '(a(ysqiiixia{sv}))'
        return self._call(path, interface, 'SmartGetAttributes', parameters, signature)

    def _locate(self, objects, name, context):
        matches = []
        for interfaces in objects.values():
            block = interfaces.get(BLOCK, {})
            raw = block.get('Device')
            if not isinstance(raw, (bytes, bytearray, list, tuple)):
                continue
            try:
                device = bytes(raw).rstrip(b'\0').decode('utf-8')
            except (ValueError, TypeError, UnicodeError):
                continue
            if device == '/dev/' + name and 'org.freedesktop.UDisks2.Partition' not in interfaces:
                matches.append(block)
        if len(matches) != 1:
            raise ValueError('UDisks2: dispositivo ausente o identidad ambigua')
        block = matches[0]
        path = block.get('Drive')
        if not isinstance(path, str) or not path.startswith('/') or path == '/':
            raise ValueError('UDisks2: ruta de Drive no verificable')
        interfaces = objects.get(path, {})
        if not self._matches(block, interfaces.get(DRIVE, {}), context):
            raise ValueError('UDisks2: ObjectManager no corresponde a la identidad actual')
        interface = NVME if NVME in interfaces else ATA
        if interface not in interfaces:
            raise ValueError('UDisks2: interfaz SMART no disponible')
        return path, interface, interfaces[interface]

    def _timestamp(self, properties, previous):
        updated = _uint(properties, 'SmartUpdated')
        if not updated or (updated > self._wall_clock() + 1 and not (previous and updated == previous[0])):
            raise ValueError('UDisks2: SmartUpdated no válido')
        if previous and updated < previous[0]:
            raise ValueError('UDisks2: SmartUpdated retrocedió')
        return updated

    def _failure(self, context, error, retain=True):
        previous = self._health_samples.get(context) if retain else None
        reading = previous[1] if previous else SmartMetrics(source='udisks2', state=SourceState(max_age=self.max_age))
        return replace(reading, state=replace(reading.state, error=error))

    def read_smart(self, name, context=None):
        if (not isinstance(name, str) or name in ('.', '..') or not _NAME.fullmatch(name)
                or context is None or not context.identity.cacheable or not context.identity.physical):
            return self._failure(context, 'UDisks2: asociación física o sesión no verificable', retain=False)
        self._deadline = time.monotonic() + self.timeout
        try:
            if context not in self._contexts:
                self.prepare(self._contexts | {context})
            self._refresh()
            if self._error:
                return self._failure(context, self._error)
            # Resolve even on cache hits: paths alone cannot corroborate a disk.
            try:
                path, interface, properties = self._locate(self._objects, name, context)
            except ValueError as exc:
                self.invalidate(context)
                return self._failure(context, str(exc), retain=False)
            previous = self._health_samples.get(context)
            updated = self._timestamp(properties, previous)
            _supported(interface, properties)
            if previous and self._clock() < self._health_next.get(context, 0):
                if (not previous[1].state.error and
                        self._health_properties.get(context) != (path, interface, properties)):
                    return self._failure(context, 'UDisks2: propiedades SMART cambiaron; renovación pendiente')
                return previous[1]
            attributes = self._attributes(path, interface)
            if time.monotonic() >= self._deadline:
                raise TimeoutError('tiempo límite de lectura SMART D-Bus')
            after = self._validate_objects(self._fetch())
            if time.monotonic() >= self._deadline:
                raise TimeoutError('tiempo límite de lectura SMART D-Bus')
            after_path, after_interface, after_properties = self._locate(after, name, context)
            if (path, interface, properties) != (after_path, after_interface, after_properties):
                raise ValueError('UDisks2: identidad o propiedades SMART cambiaron durante la lectura')
            reading = parse_properties(interface, properties, attributes)
            measured = previous[1].state.acquired_at if previous and updated == previous[0] else None
            if measured is None:
                measured = self._clock() - max(0.0, self._wall_clock() - updated)
            reading = replace(reading, state=SourceState(measured, measured, self.max_age))
            self._objects = after
            self._next_read = self._clock() + self.poll_interval
            self._health_samples[context] = (updated, reading)
            self._health_properties[context] = (path, interface, properties)
            self._health_next[context] = self._clock() + self.attribute_interval
            return reading
        except Exception as exc:
            reading = self._failure(context, f'UDisks2: {type(exc).__name__}: {str(exc)[:400]}')
            previous = self._health_samples.get(context)
            self._health_samples[context] = (previous[0] if previous else 0, reading)
            self._health_next[context] = self._clock() + self.failure_backoff
            return reading
        finally:
            del self._deadline
