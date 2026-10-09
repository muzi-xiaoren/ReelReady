import unittest
from unittest.mock import patch

from reelready.scheduler import Scheduler, JOBS


class SchedulerQueueTests(unittest.TestCase):
    def test_duplicate_requests_and_capacity(self):
        scheduler = Scheduler()
        self.assertTrue(scheduler.trigger('check', [2, 1]))
        self.assertFalse(scheduler.trigger('check', [1, 2, 2]))
        self.assertFalse(scheduler.trigger('unknown'))
        with patch('reelready.scheduler.MAX_PENDING_REQUESTS', 1):
            self.assertFalse(scheduler.trigger('pt_scan', [1]))
        self.assertEqual(scheduler._queue.qsize(), 1)

    def test_active_task_cannot_be_queued_again(self):
        scheduler = Scheduler()
        results = []
        with patch.object(scheduler, '_execute_job', side_effect=lambda *args: results.append(scheduler.trigger('check', [1]))):
            scheduler._execute(JOBS['check'], [1], record=False)
        self.assertEqual(results, [False])
        self.assertTrue(scheduler.trigger('check', [1]))

    def test_finished_queue_request_releases_pending_marker(self):
        scheduler = Scheduler()
        scheduler.trigger('check', [1])
        with patch('reelready.scheduler.STARTUP_DELAY_SECONDS', 0), patch.object(scheduler, '_run_due'), patch.object(scheduler, '_execute_job', side_effect=lambda *args: scheduler._stop.set()):
            scheduler._loop()
        self.assertFalse(scheduler._pending_requests)
        self.assertIsNone(scheduler._active_request)
