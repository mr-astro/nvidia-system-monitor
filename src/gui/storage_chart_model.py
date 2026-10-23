"""Prepared physical chart data. No GTK, Cairo, I/O or filesystem telemetry."""
from dataclasses import dataclass
import hashlib
import math

from src.utils.formatting import fmt_bytes


@dataclass(frozen=True)
class Segment:
    key: tuple
    title: str
    size_bytes: int
    percentage: str
    start: float
    end: float
    color: int

    @property
    def text(self):
        return f"{self.title} · {fmt_bytes(self.size_bytes).replace('.', ',')} · {self.percentage} del disco"


@dataclass(frozen=True)
class ChartModel:
    capacity: str = 'N/D'
    segments: tuple[Segment, ...] = ()
    available: bool = False
    reason: str = 'Sin geometría física verificada'


class PartitionColors:
    """Stable assignments within a card, including temporary unavailable states.

    Eight categorical colors; beyond eight simultaneous partitions colors repeat,
    with the textual identity always present. The registry is bounded per card.
    """
    def __init__(self):
        self.slots = {}

    def slot(self, key):
        if key in self.slots:
            return self.slots[key]
        initial = int.from_bytes(hashlib.sha256(repr(key).encode()).digest()[:4], 'big') % 8
        used = set(self.slots.values())
        slot = next(((initial + offset) % 8 for offset in range(8)
                     if (initial + offset) % 8 not in used), initial)
        if len(self.slots) < 256:
            self.slots[key] = slot
        return slot


def prepare_chart(layout, now, colors):
    """Use validated exclusive byte offsets verbatim, never normalize or inflate."""
    if layout is None:
        return ChartModel()
    capacity = fmt_bytes(layout.capacity_bytes).replace('.', ',')
    if not layout.available or not layout.current(now):
        reason = (layout.reason or layout.source.error or
                  ('Sin adquisición válida de topología' if layout.source.acquired_at is None
                   else 'Topología obsoleta; esperando una distribución vigente'))
        return ChartModel(capacity=capacity, reason=reason)
    segments = []
    for region in layout.regions:
        part = region.partition
        if part is not None:
            identity = ('uuid', part.partuuid.lower()) if part.partuuid else (
                ('number', part.number) if part.number is not None else ('name', part.name))
            key, title = ('partition', *identity), part.name
            color = colors.slot(key)
        else:
            key = (region.kind, region.start_bytes, region.end_bytes)
            title = {'unpartitioned': 'Sin particionar',
                     'extended_reserved': 'Reserva de partición extendida',
                     'whole_device': 'Dispositivo completo (sin particiones)'}[region.kind]
            color = {'unpartitioned': 8, 'extended_reserved': 9, 'whole_device': 0}[region.kind]
        percent = region.size_bytes * 100 / layout.capacity_bytes
        if 0 < percent < 0.01:
            percentage = '<0,01%'
        elif 99.99 < percent < 100:
            percentage = '>99,99%'
        else:
            percentage = f'{percent:.2f}%'.replace('.', ',')
        segments.append(Segment(key, title, region.size_bytes, percentage,
                                -math.pi / 2 + math.tau * region.start_bytes / layout.capacity_bytes,
                                -math.pi / 2 + math.tau * region.end_bytes / layout.capacity_bytes, color))
    return ChartModel(capacity, tuple(segments), True, '')
