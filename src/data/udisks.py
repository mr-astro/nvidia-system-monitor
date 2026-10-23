"""Optional, read-only UDisks2 SMART temperatures through Gio's system D-Bus.

No SMART update, device operation or authentication is requested. One bounded
ObjectManager read serves all disks; SMART age is independent of that read.
"""
from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, replace

from src.data.errors import SourceState, freshness
from src.data.identity import identifier, wwn

BLOCK = "org.freedesktop.UDisks2.Block"
ATA = "org.freedesktop.UDisks2.Drive.Ata"
NVME = "org.freedesktop.UDisks2.NVMe.Controller"
DRIVE = "org.freedesktop.UDisks2.Drive"
SERVICE = "org.freedesktop.UDisks2"
ROOT = "/org/freedesktop/UDisks2"
_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class TemperatureReading:
    value: float | None
    state: SourceState


class UDisksTemperature:
    def __init__(self, timeout=2.0, poll_interval=10.0, failure_backoff=300.0,
                 smart_interval=600.0, clock=time.monotonic, wall_clock=time.time,
                 fetch=None):
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.failure_backoff = failure_backoff
        self.max_age = freshness(smart_interval, timeout)
        self._clock, self._wall_clock = clock, wall_clock
        self._fetch = fetch or self._managed_objects
        self._connection = None
        self._next_read = None
        self._objects = {}
        self._error = None
        # Device -> (persistent identity, SmartUpdated, reading).
        self._samples = {}
        self._contexts = set()
        self._refresh_requested = False

    def prepare(self, contexts):
        if contexts - self._contexts:
            self._refresh_requested = True
        for old in self._contexts - contexts:
            self._samples.pop(old, None)
        self._contexts = contexts

    def invalidate(self, context):
        self._samples.pop(context, None)
        self._contexts.discard(context)

    def rebind(self, old, new):
        """Enrich corroboration for an unchanged kernel attachment, not a new disk."""
        if old in self._samples:
            _, updated, reading = self._samples.pop(old)
            self._samples[new] = (new, updated, reading)
        if old in self._contexts:
            self._contexts.remove(old)
            self._contexts.add(new)

    @staticmethod
    def _model_matches(block, expected, actual):
        if expected == actual:
            return True
        # Linux's SCSI model field is 16 bytes; ATA IDENTIFY/UDisks may expose
        # the full model. Only recognise this exact, directional truncation on
        # a whole sd disk. It never supplies physical evidence by itself.
        if not expected.isascii() or len(expected) != 16 or not actual.startswith(expected):
            return False
        raw = block.get('Device')
        if not isinstance(raw, (bytes, bytearray, list, tuple)):
            return False
        try:
            device = bytes(raw).rstrip(b'\0')
        except (ValueError, TypeError):
            return False
        return re.fullmatch(rb'/dev/sd[a-z]+', device) is not None

    @staticmethod
    def _matches(block, drive, context):
        expected = context.identity
        checks = []
        actual_wwn = wwn(drive.get('WWN'))
        actual_serial = identifier(drive.get('Serial'))
        actual_model = identifier(drive.get('Model'))
        if expected.wwn and actual_wwn:
            checks.append(expected.wwn == actual_wwn)
        if expected.serial and actual_serial:
            checks.append(expected.serial == actual_serial)
        if expected.model and actual_model:
            # Model alone is not physical evidence; it can only contradict a
            # corroborated WWN/serial, never establish a match by itself.
            if not UDisksTemperature._model_matches(block, expected.model, actual_model):
                return False
        block_id = block.get('Id', '')
        if expected.wwn and isinstance(block_id, str) and block_id.startswith('by-id-wwn-'):
            checks.append(expected.wwn == wwn(block_id.removeprefix('by-id-')))
        return bool(checks) and all(checks)

    def _call(self, path, interface, method, parameters, signature):
        # Lazy import keeps this optional source usable without GUI imports.
        from gi.repository import Gio, GLib

        cancel = Gio.Cancellable()
        budget = min(self.timeout, getattr(self, '_deadline', float('inf')) - time.monotonic())
        if budget <= 0:
            raise TimeoutError("tiempo límite de lectura SMART D-Bus")
        timer = threading.Timer(budget, cancel.cancel)
        timer.daemon = True
        started = time.monotonic()
        timer.start()
        try:
            if self._connection is None or self._connection.is_closed():
                address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SYSTEM, cancel)
                self._connection = Gio.DBusConnection.new_for_address_sync(
                    address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                    | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, cancel,
                )
                self._connection.set_exit_on_close(False)
            remaining = budget - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("tiempo límite de conexión D-Bus")
            response = self._connection.call_sync(
                SERVICE, path, interface, method,
                parameters, GLib.VariantType.new(signature),
                Gio.DBusCallFlags.NO_AUTO_START, max(1, int(remaining * 1000)), cancel,
            )
            return response.unpack()[0]
        finally:
            timer.cancel()

    def _managed_objects(self):
        return self._call(ROOT, "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
                          None, "(a{oa{sa{sv}}})")

    @staticmethod
    def _validate_objects(objects):
        if not isinstance(objects, dict) or any(
            not isinstance(path, str) or not isinstance(interfaces, dict)
            or any(not isinstance(properties, dict) for properties in interfaces.values())
            for path, interfaces in objects.items()
        ):
            raise ValueError("respuesta ObjectManager malformada")
        return objects

    def _refresh(self):
        now = self._clock()
        if self._next_read is not None and now < self._next_read and (not self._refresh_requested or self._error):
            return
        self._refresh_requested = False
        try:
            objects = self._validate_objects(self._fetch())
        except Exception as exc:
            self._error = f"UDisks2: {type(exc).__name__}: {str(exc)[:400]}"
            self._next_read = self._clock() + self.failure_backoff
        else:
            self._objects = objects
            self._error = None
            self._next_read = self._clock() + self.poll_interval

    def _unavailable(self, name, error, retain=True):
        previous = self._samples.get(name) if retain else None
        reading = previous[2] if previous else TemperatureReading(None, SourceState(max_age=self.max_age))
        return TemperatureReading(reading.value, replace(reading.state, error=error))

    def read(self, name, context=None):
        key = name if context is None else context
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            return self._unavailable(key, "UDisks2: nombre de dispositivo no válido", retain=False)
        if context is not None and (not context.identity.cacheable or not context.identity.physical):
            return self._unavailable(key, 'UDisks2: asociación física o sesión no verificable', retain=False)
        if context is not None and context not in self._contexts:
            self.prepare(self._contexts | {context})
        self._refresh()
        if self._error:
            return self._unavailable(key, self._error)
        matches = []
        for interfaces in self._objects.values():
            block = interfaces.get(BLOCK, {})
            raw_device = block.get("Device", [])
            if not isinstance(raw_device, (bytes, bytearray, list, tuple)):
                continue
            try:
                device = bytes(raw_device).rstrip(b"\0").decode("utf-8")
            except (TypeError, ValueError, UnicodeError):
                continue
            # Match a whole-disk object, never an adjacent partition or mapper.
            if device == "/dev/" + name and "org.freedesktop.UDisks2.Partition" not in interfaces:
                matches.append(block)
        if len(matches) != 1:
            self._samples.pop(key, None)
            return self._unavailable(key, "UDisks2: dispositivo ausente o identidad ambigua", retain=False)
        block = matches[0]
        drive = block.get("Drive")
        identity = (block.get("Id"), drive)
        if not isinstance(identity[0], str) or not identity[0] or not isinstance(drive, str) or drive == "/":
            self._samples.pop(key, None)
            return self._unavailable(key, "UDisks2: identidad física no verificable", retain=False)
        if context is not None:
            if not self._matches(block, self._objects.get(drive, {}).get('org.freedesktop.UDisks2.Drive', {}), context):
                self._samples.pop(key, None)
                return self._unavailable(key, 'UDisks2: ObjectManager no corresponde a la identidad actual', retain=False)
            # Physical evidence is corroborated above; a D-Bus path/name change
            # must not reanchor the same physical SMART measurement.
            identity = context
        previous = self._samples.get(key)
        if previous and previous[0] != identity:
            self._samples.pop(key)
            previous = None
        interfaces = self._objects.get(drive, {})
        ata = interfaces.get(NVME, interfaces.get(ATA, {}))
        if NVME in interfaces and ata.get('State') != 'live':
            return self._unavailable(key, "UDisks2: controlador NVMe no disponible")
        if NVME not in interfaces and (ata.get("SmartSupported") is not True or ata.get("SmartEnabled") is not True):
            return self._unavailable(key, "UDisks2: SMART no disponible o deshabilitado")
        updated, kelvin = ata.get("SmartUpdated"), ata.get("SmartTemperature")
        wall = self._wall_clock()
        if (isinstance(updated, bool) or not isinstance(updated, int) or updated <= 0
                or (updated > wall + 1 and not (previous and updated == previous[1])) or isinstance(kelvin, bool)
                or not isinstance(kelvin, (int, float)) or not math.isfinite(kelvin)
                or kelvin <= 0):
            return self._unavailable(key, "UDisks2: temperatura o SmartUpdated no válidos")
        if previous and updated < previous[1]:
            return self._unavailable(key, "UDisks2: SmartUpdated retrocedió")
        # Anchor once per actual SMART measurement, never once per D-Bus read.
        measured = previous[2].state.acquired_at if previous and updated == previous[1] else (
            self._clock() - max(0.0, wall - updated)
        )
        reading = TemperatureReading(kelvin - 273.15, SourceState(measured, measured, self.max_age))
        self._samples[key] = (identity, updated, reading)
        return reading
