"""A6: deterministic synthetic I/O, attachment changes and bounded concurrency."""
import copy
import json
import threading
import time
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.data.collector import Collector
from src.data.errors import SourceState
from src.data.identity import DeviceIdentity, Presence
from src.data.storage import DiskMetrics
from src.data.storage_presentation import (
    AliasRequest, StorageAliasWorker, VisualDevice, presentation_request,
)
from src.utils.disk_aliases import DiskAliasResolver
from tests.test_storage import fake_monitor


def physical(letter='a', sequence=1):
    return DeviceIdentity(diskseq=sequence, wwn='500' + letter,
                          serial='synthetic-' + letter, model='Synthetic')


def request(generation=1, name='sda', identity=None, session=1):
    return AliasRequest((generation,), (VisualDevice(name, Presence(identity or physical(), session)),))


def settled(worker, req, sequence=1):
    worker.request(req, sequence)
    with worker._condition:
        ready = worker._condition.wait_for(
            lambda: worker._result is not None and worker._active is None and worker._pending is None,
            timeout=3)
    if not ready:
        raise AssertionError('synthetic alias worker did not finish')
    return worker.request(req, sequence)


class AliasWorker(unittest.TestCase):
    def setUp(self):
        self.main = threading.get_ident()
        self.now = [10.0]
        self.live = {'sda': physical()}
        self.aliases = {'wwn-500a': 'Alias A'}
        self.links = {'wwn-500a': 'sda'}
        self.calls = []
        self.read_hook = None
        self.resolve_hook = None
        self.config_error = None
        self.raw_json = None
        for patcher in (
            mock.patch.object(Path, 'resolve', lambda path, **kw: self.resolve(path, **kw)),
            mock.patch.object(Path, 'read_text', lambda path, **kw: self.read(path, **kw)),
            mock.patch.object(Path, 'iterdir', lambda path: self.entries(path)),
            mock.patch.object(Path, 'is_symlink', lambda path: self.symlink(path)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def record(self, operation):
        self.assertNotEqual(threading.get_ident(), self.main, 'alias I/O on foreground thread')
        self.calls.append((operation, threading.get_ident()))

    def read(self, path, **kwargs):
        self.record('json')
        if self.read_hook:
            self.read_hook()
        if self.config_error:
            raise self.config_error
        return self.raw_json if self.raw_json is not None else json.dumps({'aliases': self.aliases})

    def resolve(self, path, strict=False):
        self.record('resolve')
        if path == Path('/synthetic/dev'):
            return path
        if self.resolve_hook:
            self.resolve_hook()
        return Path('/synthetic/dev') / self.links[path.name]

    def entries(self, path):
        self.record('iterdir')
        return iter(Path('/synthetic/by-id') / name for name in list(self.links))

    def symlink(self, path):
        self.record('is_symlink')
        return True

    def identify(self, name):
        self.record('identity')
        return self.live.get(name, DeviceIdentity(error='synthetic absent'))

    def worker(self, **kwargs):
        kwargs.setdefault('clock', lambda: self.now[0])
        kwargs.setdefault('identity_reader', self.identify)
        worker = StorageAliasWorker('/synthetic/config.json', '/synthetic/by-id', '/synthetic/dev',
                                    channel='dev', **kwargs)
        def cleanup():
            worker.stop()
            if worker._thread and worker._thread.ident:
                worker._thread.join(3)
                self.assertFalse(worker._thread.is_alive())
        self.addCleanup(cleanup)
        return worker

    def block(self):
        entered, release = threading.Event(), threading.Event()
        def wait():
            entered.set()
            if not release.wait(5):
                raise TimeoutError('synthetic I/O timeout')
        # Unblock before worker cleanup even if a test assertion fails.
        return entered, release, wait

    def test_construction_is_lexical_and_all_io_has_one_worker_owner(self):
        worker = self.worker()
        self.assertEqual(self.calls, [])
        result = settled(worker, request())
        self.assertEqual(dict(result.identities)['sda'].alias, 'Alias A')
        self.assertEqual({thread for _, thread in self.calls}, {worker._thread.ident})
        self.assertIsNone(result.state.error)
        with self.assertRaises(FrozenInstanceError):
            result.state = SourceState()
        with self.assertRaises(FrozenInstanceError):
            result.identities[0][1].alias = 'modified'

    def test_json_absent_malformed_and_inaccessible_are_safe(self):
        worker = self.worker()
        for generation, error, raw in ((1, FileNotFoundError(), None),
                                        (2, None, '{bad JSON'),
                                        (3, PermissionError('synthetic denied'), None)):
            with self.subTest(generation=generation):
                self.config_error, self.raw_json = error, raw
                result = settled(worker, request(generation))
                self.assertIsNone(dict(result.identities)['sda'].alias)
                self.assertEqual(bool(result.state.error), generation != 1)
        self.config_error = self.raw_json = None
        result = settled(worker, request(4))
        self.assertEqual(dict(result.identities)['sda'].alias, 'Alias A')
        self.assertIsNone(result.state.error)

    def test_slow_json_never_blocks_request_and_queue_is_replaceable(self):
        worker = self.worker()
        entered, release, self.read_hook = self.block()
        self.addCleanup(release.set)
        first = worker.request(request(1), 1)
        self.assertIsNone(dict(first.identities)['sda'].alias)
        self.assertTrue(entered.wait(2))
        durations = []
        maximum = 0
        for generation in range(2, 1002):
            started = time.perf_counter()
            worker.request(request(generation), generation)
            durations.append(time.perf_counter() - started)
            maximum = max(maximum, worker.pending_count)
        self.assertEqual(maximum, 1)
        self.assertLess(max(durations), .1)
        self.assertEqual(sum(op == 'json' for op, _ in self.calls), 1)
        release.set()
        result = settled(worker, request(1001), 1001)
        self.assertEqual(result.generation, (1001,))
        self.assertEqual(sum(op == 'json' for op, _ in self.calls), 2)
        print(f'A6 queue stress: 1000 submissions, pending max={maximum}, request max={max(durations)*1000:.3f} ms')

    def test_slow_symlink_result_for_removed_device_is_discarded(self):
        worker = self.worker()
        entered, release, self.resolve_hook = self.block()
        self.addCleanup(release.set)
        worker.request(request(), 1)
        self.assertTrue(entered.wait(2))
        empty = AliasRequest((2,), ())
        result = worker.request(empty, 2)
        self.assertEqual(result.identities, ())
        self.assertEqual(worker.pending_count, 0)
        release.set()
        with worker._condition:
            self.assertTrue(worker._condition.wait_for(lambda: worker._active is None, 2))
            self.assertIsNone(worker._result)

    def test_replacement_during_resolution_rejects_old_alias(self):
        worker = self.worker()
        self.resolve_hook = lambda: self.live.update(sda=physical('b', 2))
        result = settled(worker, request())
        self.assertIsNone(dict(result.identities)['sda'].alias)
        self.assertIn('cambió', result.state.error)
        self.resolve_hook = None
        self.links = {'wwn-500b': 'sda'}
        self.aliases = {'wwn-500b': 'Alias B'}
        new = settled(worker, request(2, identity=physical('b', 2), session=2), 2)
        self.assertEqual(dict(new.identities)['sda'].alias, 'Alias B')
        self.assertNotEqual(dict(result.identities)['sda'].key, dict(new.identities)['sda'].key)

    def test_contradictory_wwn_link_and_changed_session_before_scan_are_rejected(self):
        worker = self.worker()
        self.links = {'wwn-500b': 'sda'}
        result = settled(worker, request())
        self.assertIn('cambió', result.state.error)
        self.assertIsNone(dict(result.identities)['sda'].alias)
        self.live['sda'] = physical('b', 2)
        result = settled(worker, request(2))
        self.assertIn('no verificable', result.state.error)

    def test_rename_preserves_session_key_and_reconnection_changes_it(self):
        worker = self.worker()
        first = dict(settled(worker, request()).identities)['sda']
        self.live = {'sdc': physical()}
        self.links = {'wwn-500a': 'sdc'}
        renamed = dict(settled(worker, request(2, 'sdc')).identities)['sdc']
        self.assertEqual(first, renamed)
        self.live['sdc'] = physical(sequence=2)
        reconnected = dict(settled(worker, request(3, 'sdc', physical(sequence=2), 2)).identities)['sdc']
        self.assertNotEqual(first.key, reconnected.key)
        self.assertEqual(first.expansion_key, reconnected.expansion_key)

    def test_old_completion_cannot_overwrite_latest_even_after_aba_request(self):
        worker = self.worker()
        entered, release, self.read_hook = self.block()
        self.addCleanup(release.set)
        worker.request(request(1), 1)
        self.assertTrue(entered.wait(2))
        worker.request(request(2), 2)
        worker.request(request(1), 3)
        self.assertEqual(worker.pending_count, 1)
        release.set()
        result = settled(worker, request(1), 3)
        self.assertEqual(result.generation, (1,))
        self.assertEqual(sum(op == 'json' for op, _ in self.calls), 2)

    def test_retry_error_keeps_measurement_age_then_recovers_without_topology_change(self):
        worker = self.worker()
        first = settled(worker, request())
        self.now[0] = 26
        self.config_error = PermissionError('synthetic denied')
        failed = settled(worker, request())
        self.assertEqual(failed.state.acquired_at, first.state.acquired_at)
        self.assertEqual(failed.state.completed_at, first.state.completed_at)
        self.assertIn('denied', failed.state.error)
        self.now[0] = 42
        self.config_error = None
        recovered = settled(worker, request())
        self.assertEqual(recovered.state.acquired_at, 42)
        self.assertIsNone(recovered.state.error)
        self.assertIsNone(first.state.error)

    def test_pending_topology_refresh_keeps_compatible_error_until_recovery(self):
        worker = self.worker()
        self.config_error = PermissionError('synthetic denied')
        failed = settled(worker, request(1))
        entered, release, self.read_hook = self.block()
        self.addCleanup(release.set)
        self.config_error = None
        pending = worker.request(request(2), 2)
        self.assertTrue(entered.wait(2))
        self.assertEqual(pending.state, failed.state)
        self.assertIsNone(dict(pending.identities)['sda'].alias)
        release.set()
        recovered = settled(worker, request(2), 2)
        self.assertIsNone(recovered.state.error)
        self.assertEqual(dict(recovered.identities)['sda'].alias, 'Alias A')

    def test_stop_is_nonblocking_cancels_pending_and_discards_active_result(self):
        worker = self.worker()
        entered, release, self.read_hook = self.block()
        self.addCleanup(release.set)
        worker.request(request(1), 1)
        self.assertTrue(entered.wait(2))
        worker.request(request(2), 2)
        started = time.perf_counter()
        worker.stop()
        duration = time.perf_counter() - started
        self.assertLess(duration, .05)
        self.assertEqual(worker.pending_count, 0)
        release.set()
        worker._thread.join(2)
        self.assertFalse(worker._thread.is_alive())
        self.assertIsNone(worker._result)
        self.assertEqual(sum(op == 'json' for op, _ in self.calls), 1)
        self.assertIn('detenido', worker.request(request(3), 3).state.error)
        print(f'A6 blocked-worker stop: {duration*1000:.3f} ms')

    def test_resolver_construction_failure_and_thread_start_failure_have_diagnostics(self):
        factory = mock.Mock(side_effect=[OSError('synthetic construction'), None])
        worker = self.worker(resolver_factory=factory)
        result = settled(worker, request())
        self.assertIn('synthetic construction', result.state.error)
        factory.side_effect = lambda *args, **kwargs: DiskAliasResolver(*args, **kwargs)
        self.assertIsNone(settled(worker, request(2)).state.error)
        another = self.worker()
        with mock.patch.object(threading.Thread, 'start', side_effect=RuntimeError('no thread')):
            result = another.request(request(), 1)
        self.assertIn('no thread', result.state.error)

    def test_unknown_identity_fallback_is_not_continued_by_name(self):
        worker = self.worker()
        req = AliasRequest((1,), (VisualDevice('sda'),))
        first = settled(worker, req, 1)
        second = worker.request(req, 2)
        self.assertNotEqual(dict(first.identities)['sda'].key, dict(second.identities)['sda'].key)
        self.assertIsNone(dict(first.identities)['sda'].expansion_key)
        self.assertIsNone(dict(first.identities)['sda'].alias)

    def test_channels_are_lexical_and_separate_without_io(self):
        with mock.patch.dict('os.environ', {'XDG_CONFIG_HOME': '/synthetic/config'}):
            stable = StorageAliasWorker(channel='stable')
            dev = StorageAliasWorker(channel='dev')
        self.assertEqual(stable.config_path, Path('/synthetic/config/nvidia-system-monitor/storage-aliases.json'))
        self.assertEqual(dev.config_path, Path('/synthetic/config/nvidia-system-monitor-dev/storage-aliases.json'))
        self.assertEqual(self.calls, [])

    def test_blocked_aliases_do_not_delay_hardware_publication_or_change_a3(self):
        worker = self.worker()
        entered, release, self.read_hook = self.block()
        self.addCleanup(release.set)
        monitor = fake_monitor(identity_reader=lambda name, node: self.live[name])
        nodes = [{'name': 'sda', 'type': 'disk', 'fstype': 'ext4', 'size': 10**12,
                  'mountpoints': ['/synthetic/mount']}]
        mounts = {'/synthetic/mount': ('10', '8:0', '/', 'ext4', '/dev/sda')}
        stat = SimpleNamespace(f_blocks=1000, f_bfree=400, f_bavail=300, f_frsize=4096)
        cpu = SimpleNamespace(get_metrics=mock.Mock(return_value=None))
        collector = Collector(storage=monitor, storage_aliases=worker, cpu=cpu)
        with mock.patch.object(monitor, '_run_lsblk', side_effect=lambda: copy.deepcopy(nodes)), \
                mock.patch.object(monitor, '_read_mountinfo', return_value=mounts), \
                mock.patch.object(monitor, '_temperature', return_value=38), \
                mock.patch('src.data.storage.os.statvfs', return_value=stat):
            first = collector.collect_once()
            self.assertTrue(entered.wait(2))
            before = first.sources['usage:sda']
            durations = []
            for _ in range(1000):
                started = time.perf_counter()
                snap = collector.collect_once()
                durations.append(time.perf_counter()-started)
            self.assertEqual(snap.sequence, 1001)
            self.assertEqual(cpu.get_metrics.call_count, 1001)
            self.assertEqual(snap.disks[0].temperature_c, 38)
            self.assertEqual(snap.disks[0].direct_filesystem.used_bytes, 600*4096)
            self.assertEqual(snap.disks[0].partitions, [])
            self.assertEqual(first.sources['usage:sda'], before)
            self.assertLess(max(durations), .1)
            self.assertEqual(worker.pending_count, 0)
            # Adapter returns a copy containing A2's frozen evidence.
            bindings = monitor.presentation_bindings()
            bindings.clear()
            self.assertIn('sda', monitor.presentation_bindings())
            print(f'A6 collector stress: 1000 publications, max={max(durations)*1000:.3f} ms, mean={sum(durations)/len(durations)*1000:.3f} ms')

    def test_real_collector_thread_keeps_publishing_while_alias_io_is_blocked(self):
        worker = self.worker()
        entered, release, self.read_hook = self.block()
        self.addCleanup(release.set)
        reached = threading.Event()
        monitor = SimpleNamespace(
            get_metrics=lambda: [DiskMetrics('sda')], errors={},
            sources={'topology': SourceState(0, 0, 20)},
            presentation_bindings=lambda: {'sda': Presence(physical(), 1)})
        class SignallingCollector(Collector):
            def collect_once(self):
                snapshot = super().collect_once()
                if snapshot.sequence >= 4:
                    reached.set()
                return snapshot
        collector = SignallingCollector(storage=monitor, storage_aliases=worker, interval=.05)
        collector.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(reached.wait(2), 'hardware publication waited for alias I/O')
            published = collector.latest()
            self.assertGreaterEqual(published.sequence, 3)
            self.assertEqual(published.disks[0].name, 'sda')
            self.assertEqual(dict(published.storage_presentation.identities)['sda'].key, 'presence:1')
            self.assertFalse(release.is_set())
        finally:
            collector.stop(timeout=0)
            release.set()
            collector._thread.join(2)
        self.assertFalse(collector._thread.is_alive())


if __name__ == '__main__':
    unittest.main()
