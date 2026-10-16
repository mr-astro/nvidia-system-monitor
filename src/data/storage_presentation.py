"""Single-owner alias I/O, independent of both hardware polling and GTK.

Only the replaceable pending request and completed immutable result cross the
condition lock. Filesystem I/O, including resolver construction, never does.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from src.data.errors import SourceState, freshness
from src.data.identity import Presence, probe, wwn
from src.utils.channel import DEFAULT_CHANNEL, get_channel
from src.utils.disk_aliases import DiskAliasResolver, DiskIdentity
from src.utils.paths import config_dir


class _Superseded(Exception):
    pass


@dataclass(frozen=True)
class VisualDevice:
    name: str
    presence: Presence | None = None


@dataclass(frozen=True)
class AliasRequest:
    generation: tuple
    devices: tuple[VisualDevice, ...]


@dataclass(frozen=True)
class StoragePresentation:
    generation: tuple = ()
    identities: tuple[tuple[str, DiskIdentity], ...] = ()
    state: SourceState = SourceState()


def presentation_request(disks, topology, bindings):
    devices = tuple(VisualDevice(d.name, bindings.get(d.name)) for d in disks)
    generation = (topology.acquired_at if topology else None,
                  tuple((d.name, d.model, d.size_bytes) for d in disks), devices)
    return AliasRequest(generation, devices)


def fallback_identities(request, sequence):
    """Session continuity and verified physical expansion never depend on alias I/O."""
    result = []
    for position, device in enumerate(request.devices):
        presence = device.presence
        if presence and presence.identity.usable:
            key = f"presence:{presence.generation}"
            physical = presence.identity.physical
            expansion = repr(physical) if physical else None
        else:
            # Without evidence, a later snapshot may contain a different disk
            # at the same name. Do not inherit its card or expansion state.
            key, expansion = f"unverified:{sequence}:{position}:{device.name}", None
        result.append((device.name, DiskIdentity(key, expansion_key=expansion)))
    return tuple(result)


class StorageAliasWorker:
    def __init__(self, config_path=None, by_id_dir="/dev/disk/by-id", device_dir="/dev", *,
                 channel=DEFAULT_CHANNEL, sys_dir="/sys", clock=time.monotonic,
                 retry_interval=15.0, resolver_factory=DiskAliasResolver, identity_reader=None):
        get_channel(channel)
        # Lexical paths only. DiskAliasResolver (and Path.resolve) belong to _run.
        self.config_path = Path(config_path) if config_path is not None else config_dir(channel) / "storage-aliases.json"
        self._resolver_args = (self.config_path, by_id_dir, device_dir)
        self._channel = channel
        self._factory = resolver_factory
        self._identify = identity_reader or (lambda name: probe(name, {}, sys_dir))
        self._clock, self._retry = clock, retry_interval
        self._condition = threading.Condition()
        self._thread = None
        self._closed = False
        self._desired = None
        self._pending = None
        self._active = None
        self._result = None
        self._state = None
        self._ticket = 0
        self._retry_at = 0.0
        self._start_error = None

    def start(self):
        with self._condition:
            if self._closed or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="storage-aliases", daemon=True)
            try:
                self._thread.start()
            except RuntimeError as exc:
                self._closed = True
                self._start_error = f"Trabajador de alias: {exc}"

    def stop(self):
        """Signal only: a blocked filesystem syscall must never delay GTK shutdown."""
        with self._condition:
            self._closed = True
            self._desired = self._pending = self._result = None
            self._state = None
            self._condition.notify_all()

    @property
    def pending_count(self):
        with self._condition:
            return int(self._pending is not None)

    def request(self, request, sequence):
        self.start()
        with self._condition:
            if self._closed:
                return StoragePresentation(request.generation, fallback_identities(request, sequence),
                                           SourceState(error=self._start_error or "Trabajador de alias detenido"))
            if self._desired is None or self._desired[1] != request:
                if self._desired is None or self._desired[1].devices != request.devices:
                    self._state = None
                self._ticket += 1
                self._desired = (self._ticket, request)
                self._pending = self._desired if request.devices else None
                self._result = None
                self._condition.notify()
            elif (self._result is not None and self._active is None and self._pending is None
                  and self._clock() >= self._retry_at):
                # Retry failures and refresh configuration even with cached topology.
                self._ticket += 1
                self._desired = (self._ticket, request)
                self._pending = self._desired
                self._condition.notify()
            result = self._result
            state = self._state
        identities = fallback_identities(request, sequence)
        if result is None:
            return StoragePresentation(request.generation, identities, state or SourceState())
        aliases = dict(result.identities)
        return replace(result, identities=tuple(
            (name, replace(identity, alias=aliases[name].alias if name in aliases else None))
            for name, identity in identities))

    def _check(self, work):
        with self._condition:
            if self._closed or self._desired != work:
                raise _Superseded()

    def _run(self):
        resolver = None
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                if self._closed:
                    return
                work = self._pending
                self._active, self._pending = work, None
                previous_state = self._state
            ticket, request = work
            started = self._clock()
            error = None
            identities = ()
            try:
                if resolver is None:
                    resolver = self._factory(*self._resolver_args, channel=self._channel)
                self._check(work)
                before = {}
                for device in request.devices:
                    self._check(work)
                    expected = device.presence
                    current = self._identify(device.name) if expected and expected.identity.cacheable else None
                    if current and expected.identity.same_attachment(current):
                        before[device.name] = current
                    else:
                        error = "Identidad de alias no verificable o sesión cambiada"
                self._check(work)
                candidates = resolver.refresh(tuple(d for d in request.devices if d.name in before),
                                              (request.generation, ticket))
                self._check(work)
                error = resolver.error or error
                verified = []
                for device in request.devices:
                    self._check(work)
                    if device.name not in before:
                        continue
                    current = self._identify(device.name)
                    identity = candidates[device.name]
                    expected = device.presence.identity
                    contradictory = (identity.key.startswith('id:wwn-') and expected.wwn
                                     and wwn(identity.key[3:]) != expected.wwn)
                    if (not before[device.name].same_attachment(current) or contradictory):
                        error = "La identidad o sesión cambió durante la resolución de alias"
                        continue
                    verified.append((device.name, identity))
                identities = tuple(verified)
            except _Superseded:
                with self._condition:
                    self._active = None
                    self._condition.notify_all()
                continue
            except Exception as exc:
                error = f"Resolución de alias: {type(exc).__name__}: {str(exc)[:400]}"
            completed = self._clock()
            # A failed scan retains the age of the last compatible measurement.
            if error:
                state = replace(previous_state or SourceState(),
                                max_age=freshness(self._retry), error=error)
            else:
                state = SourceState(started, completed, freshness(self._retry))
            result = StoragePresentation(request.generation, identities, state)
            with self._condition:
                self._active = None
                if self._closed:
                    return
                if self._desired == work:
                    self._result = result
                    self._state = state
                    self._retry_at = completed + self._retry
                self._condition.notify_all()
