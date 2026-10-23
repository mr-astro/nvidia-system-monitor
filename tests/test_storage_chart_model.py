"""Chart proportions use only validated physical extents, never filesystem usage."""
from dataclasses import replace
import math
import unittest

from src.data.errors import SourceState
from src.data.storage_layout import build_layout
from src.gui.storage_chart_model import PartitionColors, prepare_chart
from tests.storage_fixtures import disk, part, presence


def layout_for(node=None, source=SourceState(10, 11, 20)):
    node = node or disk(children=[part(start=10, sectors=100),
                                 part('sda2', start=200, sectors=300, number='8:2')])
    return build_layout(node, presence(node), source)


class ChartModels(unittest.TestCase):
    def setUp(self):
        self.colors = PartitionColors()

    def chart(self, layout=None, now=11):
        return prepare_chart(layout or layout_for(), now, self.colors)

    def test_offsets_and_legend_preserve_physical_order(self):
        layout = layout_for()
        chart = self.chart(layout)
        self.assertTrue(chart.available)
        self.assertEqual(len(chart.segments), len(layout.regions))
        for segment, region in zip(chart.segments, layout.regions):
            self.assertEqual(segment.size_bytes, region.size_bytes)
            self.assertAlmostEqual(segment.start, -math.pi / 2 + math.tau * region.start_bytes / layout.capacity_bytes)
            self.assertAlmostEqual(segment.end, -math.pi / 2 + math.tau * region.end_bytes / layout.capacity_bytes)
            self.assertIn('del disco', segment.text)
        self.assertAlmostEqual(sum(s.end - s.start for s in chart.segments), math.tau)
        self.assertEqual(chart.segments[1].percentage, '10,00%')

    def test_only_verified_gaps_are_unpartitioned(self):
        chart = self.chart()
        self.assertEqual(sum(s.size_bytes for s in chart.segments if s.title == 'Sin particionar'), 600 * 512)
        invalid = layout_for(disk(children=[part(start=None)]))
        self.assertEqual(self.chart(invalid).segments, ())

    def test_no_layout_has_explicit_unavailable_state(self):
        chart = prepare_chart(None, 11, self.colors)
        self.assertFalse(chart.available)
        self.assertEqual(chart.capacity, 'N/D')
        self.assertEqual(chart.segments, ())
        self.assertIn('verificada', chart.reason)

    def test_invalid_stale_error_and_unmeasured_geometry_have_no_sectors(self):
        layout = layout_for()
        invalid = replace(layout, state='unavailable', reason='Solapamiento')
        cases = [(invalid, 11, 'Solapamiento'), (layout, 31, 'obsoleta'),
                 (replace(layout, source=SourceState(error='lsblk falló')), 11, 'lsblk falló'),
                 (replace(layout, source=SourceState()), 11, 'Sin adquisición')]
        for item, now, reason in cases:
            with self.subTest(reason=reason):
                chart = self.chart(item, now)
                self.assertFalse(chart.available)
                self.assertEqual(chart.segments, ())
                self.assertIn(reason, chart.reason)

    def test_empty_disk_has_one_real_unpartitioned_region(self):
        chart = self.chart(layout_for(disk()))
        self.assertEqual(len(chart.segments), 1)
        self.assertEqual(chart.segments[0].title, 'Sin particionar')
        self.assertEqual(chart.segments[0].percentage, '100,00%')

    def test_direct_filesystem_is_whole_device_never_an_invented_partition(self):
        chart = self.chart(layout_for(disk(fstype='btrfs', pttype=None)))
        self.assertEqual(len(chart.segments), 1)
        self.assertIn('Dispositivo completo', chart.segments[0].title)
        self.assertNotIn('Sin particionar', chart.segments[0].title)

    def test_small_regions_are_not_enlarged_or_rounded_to_zero_in_legend(self):
        node = disk(size=512 * 10**9, children=[part(start=2, sectors=1)])
        layout = layout_for(node)
        segment = self.chart(layout).segments[1]
        self.assertEqual(segment.percentage, '<0,01%')
        self.assertAlmostEqual(segment.end - segment.start, math.tau / 10**9)
        self.assertEqual(self.chart(layout).segments[-1].percentage, '>99,99%')

    def test_partuuid_survives_rename_resize_and_temporary_staleness(self):
        layout = layout_for()
        before = self.chart(layout).segments[1]
        self.chart(layout, 100)
        region = layout.regions[1]
        renamed = replace(region, partition=replace(region.partition, name='sdc9', number=9))
        after = self.chart(replace(layout, regions=(layout.regions[0], renamed, *layout.regions[2:]))).segments[1]
        self.assertEqual(before.key, after.key)
        self.assertEqual(before.color, after.color)
        self.assertEqual(after.title, 'sdc9')

    def test_extended_container_is_not_an_additional_sector(self):
        node = disk(pttype='dos', children=[
            part(start=2, sectors=98, parttype='0x83'),
            part('sda2', start=100, sectors=900, number='8:2', parttype='0x0f', partn=2),
            part('sda5', start=110, sectors=890, number='8:5', parttype='0x83', partn=5)])
        layout = layout_for(node)
        chart = self.chart(layout)
        self.assertTrue(chart.available)
        self.assertNotIn('sda2', [s.title for s in chart.segments])
        self.assertIn('Reserva de partición extendida', [s.title for s in chart.segments])
        self.assertEqual(sum(s.size_bytes for s in chart.segments), node['size'])

    def test_nested_logical_layers_do_not_create_extra_segments(self):
        child = part(children=[dict(name='crypt', type='crypt', size=450000, children=[
            dict(name='vg-root', type='lvm', size=250000, fstype='btrfs', mountpoints=['/', '/home'])])])
        chart = self.chart(layout_for(disk(children=[child])))
        self.assertEqual([s.title for s in chart.segments], ['Sin particionar', 'sda1'])

    def test_refreshing_source_time_does_not_change_prepared_visual_model(self):
        layout = layout_for()
        self.assertEqual(self.chart(layout), self.chart(replace(layout, source=SourceState(11, 12, 20))))

    def test_palette_has_eight_distinct_initial_assignments_and_is_bounded(self):
        self.assertEqual(len({self.colors.slot(('partition', i)) for i in range(8)}), 8)
        for i in range(1000):
            self.colors.slot(('partition', i))
        self.assertEqual(len(self.colors.slots), 256)
