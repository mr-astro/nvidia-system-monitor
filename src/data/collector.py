"""Background data collection.

All blocking work (subprocesses, sysfs reads) happens in a worker thread, so
the GTK main loop never waits on nvidia-smi, lsblk or sensors. The GUI only
reads the latest immutable-by-convention snapshot.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from src.data.errors import SourceState, freshness
from src.data.cpu import CpuMetrics
from src.data.ram import RamMetrics

ERROR_LOG_INTERVAL = 60.0


@dataclass
class Snapshot:
    sequence: int = 0
    timestamp: float = 0.0
    cpu: Optional[CpuMetrics] = None
    gpus: list = field(default_factory=list)
    gpu_available: bool = False
    cuda_version: str = "N/D"
    ram: Optional[RamMetrics] = None
    disks: list = field(default_factory=list)
    sensors: list = field(default_factory=list)
    errors: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)
    acquisition_started_at: float = 0.0


class Collector:
    """Polls the monitors periodically. A monitor passed as None is disabled."""

    def __init__(
        self,
        cpu=None,
        gpu=None,
        ram=None,
        storage=None,
        sensors=None,
        interval: float = 1.0,
        sensors_interval: float = 5.0,
        logger=None,
        clock=time.monotonic,
    ):
        self.cpu = cpu
        self.gpu = gpu
        self.ram = ram
        self.storage = storage
        if storage is not None:
            storage.refresh_interval = interval
        self.sensors = sensors
        self.interval = interval
        self.sensors_interval = sensors_interval
        self.logger = logger
        self._clock = clock

        self._lock = threading.Lock()
        self._snapshot = Snapshot()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sequence = 0
        self._last_logged: dict = {}
        self._sensors_cache: list = []
        self._sensors_at: Optional[float] = None
        self._states = {}
        self._errors = {}

    # ---- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hw-collector", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def latest(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def _run(self) -> None:
        while not self._stop.is_set():
            started = self._clock()
            snapshot = self.collect_once()
            with self._lock:
                self._snapshot = snapshot
            elapsed = self._clock() - started
            self._stop.wait(max(0.05, self.interval - elapsed))

    # ---- collection -----------------------------------------------------

    def _log_error(self, name: str, message: str) -> None:
        """Log a failure when it is new or at most once per ERROR_LOG_INTERVAL."""
        if self.logger is None:
            return
        now = self._clock()
        last = self._last_logged.get(name)
        if last and last[0] == message and now - last[1] < ERROR_LOG_INTERVAL:
            return
        self._last_logged[name] = (message, now)
        self.logger.error("Fallo recolectando '%s': %s", name, message)

    def _guard(self, name: str, errors: dict, fn, default):
        try:
            return fn()
        except Exception as exc:  # one failing source must not stop the others
            message = f"{type(exc).__name__}: {exc}"
            errors[name] = message
            self._log_error(name, message)
            return default

    def _sample(self, name, monitor, fn, default, cadence):
        started = self._clock()
        errors = {}
        value = self._guard(name, errors, fn, default)
        diagnostics = getattr(monitor, "errors", {})
        errors.update(diagnostics if isinstance(diagnostics, dict) else {})
        error = errors.get(name)
        previous = self._states.get(name, SourceState())
        self._states[name] = SourceState(
            previous.acquired_at if error else started,
            previous.completed_at if error else self._clock(),
            freshness(cadence, getattr(monitor, "timeout", 0.0)), error,
        )
        self._errors.pop(name, None)
        self._errors.update(errors)
        # CUDA and storage diagnostics may have their own cache cadence.
        if name == "gpu" and "cuda" not in errors:
            self._errors.pop("cuda", None)
        for key, message in errors.items():
            self._log_error(key, message)
        return value

    def collect_once(self) -> Snapshot:
        self._sequence += 1
        snap = Snapshot(sequence=self._sequence, acquisition_started_at=self._clock())
        if self.cpu is not None:
            snap.cpu = self._sample("cpu", self.cpu, self.cpu.get_metrics, None, self.interval)
        if self.ram is not None:
            snap.ram = self._sample("ram", self.ram, self.ram.get_metrics, None, self.interval)
        if self.gpu is not None:
            snap.gpu_available = bool(getattr(self.gpu, "available", True))
            snap.gpus = self._sample("gpu", self.gpu, self.gpu.get_metrics, [], self.interval)
            if snap.gpus:
                snap.cuda_version = self._guard("cuda", self._errors, self.gpu.get_cuda_version, "N/D")
                diagnostics = getattr(self.gpu, "errors", {})
                if isinstance(diagnostics, dict) and "cuda" in diagnostics:
                    self._errors["cuda"] = diagnostics["cuda"]
                    self._log_error("cuda", diagnostics["cuda"])
                elif snap.cuda_version != "N/D":
                    self._errors.pop("cuda", None)
        if self.storage is not None:
            # Storage reports independently timed topology, usage and temperatures.
            for key in list(self._errors):
                if key == "topology" or key.startswith(("usage:", "smart:", "temperature:", "udisks:")):
                    self._errors.pop(key)
            snap.disks = self._sample("storage", self.storage, self.storage.get_metrics, [], self.interval)
            states = getattr(self.storage, "sources", {})
            if isinstance(states, dict):
                for key in list(self._states):
                    if key == "topology" or key.startswith(("usage:", "smart:", "temperature:", "udisks:")):
                        self._states.pop(key)
                self._states.update(states)
        if self.sensors is not None:
            now = self._clock()
            if self._sensors_at is None or now - self._sensors_at >= self.sensors_interval:
                value = self._sample("sensors", self.sensors, self.sensors.get_readings,
                                     self._sensors_cache, max(self.interval, self.sensors_interval))
                if not self._states["sensors"].error:
                    self._sensors_cache = value
                self._sensors_at = self._clock()
            snap.sensors = self._sensors_cache
        snap.errors = dict(self._errors)
        snap.sources = dict(self._states)
        snap.timestamp = self._clock()  # publication, never a measurement timestamp
        return snap
