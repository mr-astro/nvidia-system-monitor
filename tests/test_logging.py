import logging
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from src.utils.channel import DEV, STABLE
from src.utils.logger import setup_logging


class ChannelLogging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root)})
        patch.start()
        self.addCleanup(patch.stop)
        for channel in (STABLE, DEV):
            logger = logging.getLogger(channel.logger_name)
            saved = logger.handlers[:], logger.level, logger.propagate
            logger.handlers = []
            self.addCleanup(self._restore, logger, saved)

    @staticmethod
    def _restore(logger, saved):
        for handler in logger.handlers:
            handler.close()
        logger.handlers, logger.level, logger.propagate = saved

    def test_simultaneous_records_handlers_and_paths_are_isolated(self):
        stable, dev = setup_logging(), setup_logging(channel='dev')
        self.assertIsNot(stable, dev)
        self.assertIsNot(stable.handlers[0], dev.handlers[0])
        for logger, channel in ((stable, STABLE), (dev, DEV)):
            self.assertEqual(Path(logger.handlers[0].baseFilename),
                             self.root / channel.directory_name / 'monitor.log')
            self.assertFalse(logger.propagate)
            self.assertEqual(logger.handlers[0].maxBytes, 512 * 1024)
            self.assertEqual(logger.handlers[0].backupCount, 2)
        barrier = threading.Barrier(2)

        def write(logger, marker):
            barrier.wait()
            for index in range(100):
                logger.info('%s:%s', marker, index)

        threads = [threading.Thread(target=write, args=(stable, 'STABLE_ONLY')),
                   threading.Thread(target=write, args=(dev, 'DEV_ONLY'))]
        root_handler = mock.Mock(spec=logging.Handler)
        root_handler.level = logging.NOTSET
        with mock.patch.object(logging.getLogger(), 'handlers', [root_handler]):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
        root_handler.handle.assert_not_called()
        for logger in (stable, dev):
            logger.handlers[0].flush()
        stable_text = (self.root / STABLE.directory_name / 'monitor.log').read_text()
        dev_text = (self.root / DEV.directory_name / 'monitor.log').read_text()
        self.assertEqual(len(stable_text.splitlines()), 100)
        self.assertEqual(len(dev_text.splitlines()), 100)
        self.assertNotIn('DEV_ONLY', stable_text)
        self.assertNotIn('STABLE_ONLY', dev_text)

    def test_disabled_channel_does_not_disable_other_channel_or_duplicate_handlers(self):
        dev = setup_logging(False, channel='dev')
        stable = setup_logging()
        self.assertIsInstance(dev.handlers[0], logging.NullHandler)
        self.assertFalse((self.root / DEV.directory_name).exists())
        self.assertIs(setup_logging(), stable)
        self.assertIs(setup_logging(False, channel='dev'), dev)
        self.assertEqual(len(stable.handlers), 1)
        self.assertEqual(len(dev.handlers), 1)

    def test_dev_logging_does_not_touch_existing_stable_log(self):
        stable_path = self.root / STABLE.directory_name / 'monitor.log'
        stable_path.parent.mkdir()
        stable_path.write_text('historical stable log\n')
        before = stable_path.stat().st_mtime_ns
        dev = setup_logging(channel='dev')
        dev.info('DEV_ONLY')
        dev.handlers[0].flush()
        self.assertEqual(stable_path.read_text(), 'historical stable log\n')
        self.assertEqual(stable_path.stat().st_mtime_ns, before)
