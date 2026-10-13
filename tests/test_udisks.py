"""Read-only D-Bus fixtures; no host device identities or live SMART calls."""
import copy
import types
import unittest
from unittest import mock

from src.data.udisks import ATA, BLOCK, UDisksTemperature


def objects(name="sda", updated=1000, kelvin=310.15, identity="synthetic-disk-A"):
    return {
        "/block/example": {BLOCK: {"Device": list(("/dev/" + name).encode()) + [0],
                                     "Drive": "/drive/example", "Id": identity}},
        "/drive/example": {ATA: {"SmartSupported": True, "SmartEnabled": True,
                                  "SmartUpdated": updated, "SmartTemperature": kelvin}},
    }


class UDisksReadings(unittest.TestCase):
    def setUp(self):
        self.now, self.wall = [10.0], [1000.0]
        self.data = objects()
        self.fetch = mock.Mock(side_effect=lambda: copy.deepcopy(self.data))
        self.provider = UDisksTemperature(fetch=self.fetch, clock=lambda: self.now[0],
                                          wall_clock=lambda: self.wall[0])

    def advance(self, seconds):
        self.now[0] += seconds
        self.wall[0] += seconds

    def test_kelvin_conversion_and_actual_measurement_age(self):
        self.data['/drive/example'][ATA]['SmartUpdated'] = 995
        reading = self.provider.read('sda')
        self.assertAlmostEqual(reading.value, 37)
        self.assertEqual(reading.state.age(10), 5)
        self.assertEqual(reading.state.completed_at, 5)
        self.assertFalse(reading.state.stale(10))

    def test_repeated_reads_and_wall_clock_jumps_never_rejuvenate(self):
        first = self.provider.read('sda')
        self.advance(11)
        second = self.provider.read('sda')
        self.assertEqual(second.state.acquired_at, first.state.acquired_at)
        self.wall[0] -= 500
        self.advance(11)
        third = self.provider.read('sda')
        self.assertEqual(third.state.age(self.now[0]), 22)
        self.assertIsNone(third.state.error)
        self.wall[0] += 1500
        self.advance(11)
        self.assertEqual(self.provider.read('sda').state.age(self.now[0]), 33)

    def test_new_timestamp_recovers_and_older_timestamp_is_rejected(self):
        self.provider.read('sda')
        self.advance(11)
        self.data['/drive/example'][ATA].update(SmartUpdated=1011, SmartTemperature=312.15)
        second = self.provider.read('sda')
        self.assertAlmostEqual(second.value, 39)
        self.assertEqual(second.state.age(self.now[0]), 0)
        self.advance(11)
        self.data['/drive/example'][ATA]['SmartUpdated'] = 1000
        third = self.provider.read('sda')
        self.assertAlmostEqual(third.value, 39)
        self.assertEqual(third.state.age(self.now[0]), 11)
        self.assertIn('retrocedió', third.state.error)

    def test_own_cadence_and_stopped_smart_updates_expire(self):
        first = self.provider.read('sda')
        self.advance(599)
        self.assertFalse(self.provider.read('sda').state.stale(self.now[0]))
        self.advance(first.state.max_age - 599 + 1)
        self.assertTrue(self.provider.read('sda').state.stale(self.now[0]))
        self.assertEqual(self.provider.read('sda').state.acquired_at, first.state.acquired_at)

    def test_one_cached_request_serves_several_devices(self):
        other = objects(name='sdb', identity='synthetic-disk-B')
        self.data['/block/other'] = other['/block/example']
        self.assertAlmostEqual(self.provider.read('sda').value, 37)
        self.assertAlmostEqual(self.provider.read('sdb').value, 37)
        self.fetch.assert_called_once()

    def test_timeout_disconnect_and_permissions_persist_until_retry_success(self):
        for error in (TimeoutError('timeout'), OSError('bus disconnected'), PermissionError('denied')):
            with self.subTest(error=error):
                self.setUp()
                first = self.provider.read('sda')
                self.advance(11)
                self.fetch.side_effect = error
                failed = self.provider.read('sda')
                self.assertEqual(failed.value, first.value)
                self.assertEqual(failed.state.acquired_at, first.state.acquired_at)
                self.advance(50)
                self.assertIn(str(error), self.provider.read('sda').state.error)
                self.assertEqual(self.fetch.call_count, 2)
                self.advance(251)
                self.fetch.side_effect = lambda: copy.deepcopy(self.data)
                recovered = self.provider.read('sda')
                self.assertIsNone(recovered.state.error)
                self.assertEqual(recovered.state.acquired_at, first.state.acquired_at)

    def test_service_absence_and_bad_object_manager_data(self):
        for result in (None, [], {'/bad': []}, {'/bad': {ATA: []}}):
            with self.subTest(result=result):
                provider = UDisksTemperature(fetch=lambda: result)
                reading = provider.read('sda')
                self.assertIsNone(reading.value)
                self.assertIn('malformada', reading.state.error)
        self.fetch.side_effect = ImportError('gi unavailable')
        self.assertIsNone(self.provider.read('sda').value)

    def test_invalid_temperature_timestamps_and_disabled_smart_are_not_measurements(self):
        for change in ({'SmartTemperature': 0}, {'SmartTemperature': float('nan')},
                       {'SmartTemperature': float('inf')}, {'SmartTemperature': True},
                       {'SmartUpdated': 0}, {'SmartUpdated': 1002}, {'SmartUpdated': True},
                       {'SmartUpdated': '1000'}, {'SmartEnabled': False}, {'SmartSupported': False}):
            with self.subTest(change=change):
                data = objects()
                data['/drive/example'][ATA].update(change)
                provider = UDisksTemperature(fetch=lambda: data, wall_clock=lambda: 1000)
                reading = provider.read('sda')
                self.assertIsNone(reading.value)
                self.assertIsNone(reading.state.acquired_at)
                self.assertIsNotNone(reading.state.error)

    def test_rename_disappearance_and_same_name_replacement_invalidate_old_reading(self):
        self.provider.read('sda')
        self.advance(11)
        self.data = objects(name='sdc')
        self.assertIsNone(self.provider.read('sda').value)
        self.assertAlmostEqual(self.provider.read('sdc').value, 37)
        self.advance(11)
        self.data = objects(name='sdc', identity='synthetic-disk-B', updated=0)
        self.assertIsNone(self.provider.read('sdc').value)

    def test_partition_ambiguous_and_unverifiable_devices_are_rejected(self):
        self.data['/block/example']['org.freedesktop.UDisks2.Partition'] = {}
        self.assertIsNone(self.provider.read('sda').value)
        self.advance(11)
        self.data = objects()
        self.data['/block/duplicate'] = copy.deepcopy(self.data['/block/example'])
        self.assertIn('ambigua', self.provider.read('sda').state.error)
        self.advance(11)
        self.data = objects(identity='')
        self.assertIn('no verificable', self.provider.read('sda').state.error)
        self.assertIsNone(self.provider.read('../sda').value)


class UDisksTransport(unittest.TestCase):
    def test_transport_only_reads_objects_without_autostart_and_is_bounded(self):
        from gi.repository import Gio
        connection = mock.Mock()
        connection.is_closed.return_value = False
        connection.call_sync.return_value.unpack.return_value = (objects(),)
        provider = UDisksTemperature(timeout=.1)
        provider._connection = connection
        with mock.patch('src.data.udisks.threading.Timer') as timer:
            self.assertEqual(provider._managed_objects(), objects())
        args = connection.call_sync.call_args.args
        self.assertEqual(args[2:4], ('org.freedesktop.DBus.ObjectManager', 'GetManagedObjects'))
        self.assertEqual(args[6], Gio.DBusCallFlags.NO_AUTO_START)
        self.assertLessEqual(args[7], 100)
        timer.assert_called_once()
        timer.return_value.start.assert_called_once()
        timer.return_value.cancel.assert_called_once()

    def test_connection_cancellation_is_cleaned_up_on_error(self):
        from gi.repository import Gio
        provider = UDisksTemperature(timeout=.1)
        with mock.patch.object(Gio, 'dbus_address_get_for_bus_sync', side_effect=TimeoutError('bus timeout')), \
                mock.patch('src.data.udisks.threading.Timer') as timer:
            reading = provider.read('sda')
        self.assertIn('bus timeout', reading.state.error)
        timer.return_value.cancel.assert_called_once()


if __name__ == '__main__':
    unittest.main()
