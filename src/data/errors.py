"""Collection diagnostics and monotonic acquisition times (no GTK dependency)."""
from dataclasses import dataclass
import subprocess


@dataclass(frozen=True)
class SourceState:
    acquired_at: float | None = None
    completed_at: float | None = None
    max_age: float = 0.0
    error: str | None = None

    def age(self, now):
        return None if self.acquired_at is None else max(0.0, now - self.acquired_at)

    def stale(self, now):
        age = self.age(now)
        return bool(self.error) or (age is not None and age > self.max_age)


def freshness(cadence, timeout=0.0):
    """One scheduled interval plus bounded collection and scheduling tolerance."""
    return cadence + timeout + max(1.0, cadence * 0.25)


def command_error(command, *, exception=None, result=None):
    if isinstance(exception, subprocess.TimeoutExpired):
        return f"{command}: timeout ({exception.timeout:g} s)"
    if exception is not None:
        return f"{command}: {type(exception).__name__}: {str(exception)[:400]}"
    detail = ' '.join((getattr(result, 'stderr', '') or getattr(result, 'stdout', '') or '').split())[:400]
    return f"{command}: salida {result.returncode}" + (f" — {detail}" if detail else '')
