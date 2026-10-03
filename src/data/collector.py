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

    def collect_once(self) -> Snapshot:
        errors: dict = {}
        self._sequence += 1
        snap = Snapshot(sequence=self._sequence, timestamp=self._clock(), errors=errors)

        if self.cpu is not None:
            snap.cpu = self._guard("cpu", errors, self.cpu.get_metrics, None)
        if self.ram is not None:
            snap.ram = self._guard("ram", errors, self.ram.get_metrics, None)
        if self.gpu is not None:
            snap.gpu_available = bool(getattr(self.gpu, "available", True))
            snap.gpus = self._guard("gpu", errors, self.gpu.get_metrics, [])
            if snap.gpus:
                snap.cuda_version = self._guard("cuda", errors, self.gpu.get_cuda_version, "N/D")
        if self.storage is not None:
            snap.disks = self._guard("storage", errors, self.storage.get_metrics, [])
        if self.sensors is not None:
            now = self._clock()
            if self._sensors_at is None or now - self._sensors_at >= self.sensors_interval:
                self._sensors_cache = self._guard(
                    "sensors", errors, self.sensors.get_readings, self._sensors_cache
                )
                self._sensors_at = now
            snap.sensors = self._sensors_cache
        return snap
