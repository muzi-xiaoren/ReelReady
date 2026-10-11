import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from reelready.models import JobState
from reelready.settings import AppSettings
from reelready.scheduler import Scheduler, JOBS, _is_due, next_run, _record_digest_retry


class SchedulerQueueTests(unittest.TestCase):
    def test_digest_three_retries_then_next_daily_slot(self):
        settings = AppSettings()
        settings.email.enabled = True
        settings.email.digest_hour = 12
        current = datetime(2026, 10, 11, 12)
        state = JobState(name='digest', retry_count=0)
        for delay in (5, 15, 30):
            _record_digest_retry(state, settings, 'error', current)
            state.last_run_at = current
            expected = current + timedelta(minutes=delay)
            self.assertEqual(state.retry_at, expected)
            self.assertFalse(_is_due(JOBS['digest'], settings, current, expected - timedelta(seconds=1), state))
            self.assertTrue(_is_due(JOBS['digest'], settings, current, expected, state))
            with patch('reelready.scheduler.now', return_value=current):
                self.assertEqual(next_run(JOBS['digest'], settings, current, state), expected)
            current = expected
        _record_digest_retry(state, settings, 'error', current)
        state.last_run_at = current
        self.assertEqual(state.retry_count, 3)
        self.assertIsNone(state.retry_at)
        self.assertFalse(_is_due(JOBS['digest'], settings, current, current + timedelta(hours=1), state))
        tomorrow = datetime(2026, 10, 12, 12)
        self.assertTrue(_is_due(JOBS['digest'], settings, current, tomorrow, state))
        _record_digest_retry(state, settings, 'error', tomorrow)
        self.assertEqual(state.retry_count, 0)
        self.assertEqual(state.retry_at, tomorrow + timedelta(minutes=5))

    def test_digest_retry_survives_reload_and_clears_on_success(self):
        settings = AppSettings()
        settings.email.enabled = True
        settings.email.digest_hour = 12
        current = datetime(2026, 10, 11, 12, 5)
        state = JobState(name='digest', last_run_at=current - timedelta(minutes=5),
                         retry_count=0, retry_at=current)
        self.assertTrue(_is_due(JOBS['digest'], settings, state.last_run_at, current, state))
        _record_digest_retry(state, settings, 'ok', current)
        state.last_run_at = current
        self.assertIsNone(state.retry_at)
        self.assertEqual(state.retry_count, 0)
        self.assertFalse(_is_due(JOBS['digest'], settings, current, current + timedelta(minutes=30), state))

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
