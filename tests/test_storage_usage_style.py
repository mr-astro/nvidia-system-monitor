from dataclasses import replace
import unittest

from src.gui.storage_usage_model import UsageModel
from src.gui.storage_usage_style import usage_style


class UsageStyles(unittest.TestCase):
    def test_exact_boundaries_and_measured_extremes(self):
        for percent, level, text in ((0, 'low', 'Normal'), (69.99, 'low', 'Normal'),
                                     (70, 'medium', 'Atención'), (78, 'medium', 'Atención'),
                                     (84.99, 'medium', 'Atención'), (85, 'high', 'Crítico'),
                                     (100, 'high', 'Crítico')):
            with self.subTest(percent=percent):
                style = usage_style(UsageModel(state='current', fraction=percent / 100))
                self.assertEqual((style.level, style.label), (level, text))

    def test_missing_stale_error_ambiguous_and_multiple_are_unknown(self):
        for state in ('unavailable', 'stale', 'error', 'ambiguous', 'multiple'):
            self.assertEqual(usage_style(UsageModel(state=state)).label, 'Desconocido')
            self.assertIsNone(usage_style(UsageModel(state=state, fraction=.75)).level)
        self.assertIsNone(usage_style(UsageModel(state='current')).level)

    def test_custom_thresholds_reuse_existing_classifier(self):
        usage = UsageModel(state='current', fraction=.75)
        self.assertEqual(usage_style(usage, {'medium_percent': 80, 'high_percent': 95}).level, 'low')
        self.assertEqual(usage_style(usage, {'medium_percent': 50, 'high_percent': 75}).level, 'high')

    def test_age_and_scope_do_not_change_valid_level(self):
        usage = UsageModel(state='current', fraction=.75, reason='Reciente · hace 1 s')
        self.assertEqual(usage_style(usage), usage_style(replace(usage, reason='Reciente · hace 4 s', scope='FS /')))
