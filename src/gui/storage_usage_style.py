"""Inner-ring appearance only: reuse the existing configurable level classifier."""
from dataclasses import dataclass

from src.utils.config import DEFAULT_CONFIG
from src.utils.formatting import usage_level


@dataclass(frozen=True)
class UsageStyle:
    level: str | None
    label: str


def usage_style(usage, thresholds=None):
    thresholds = thresholds if thresholds is not None else DEFAULT_CONFIG['storage_usage']['thresholds']
    level = (usage_level(usage.fraction * 100, thresholds['medium_percent'], thresholds['high_percent'])
             if usage.state == 'current' and usage.fraction is not None else None)
    return UsageStyle(level, {None: 'Desconocido', 'low': 'Normal',
                              'medium': 'Atención', 'high': 'Crítico'}[level])
