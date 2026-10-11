import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from reelready import db
from reelready.models import Event, JobState
from reelready.notifier import NotifyError
from reelready.scheduler import JOBS, Scheduler, _is_due
from reelready.settings import AppSettings


class DigestRetryPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.temp.name) / 'test.db'}")
        db.Base.metadata.create_all(self.engine)
        self.session_patch = patch.object(db, 'SessionLocal', sessionmaker(bind=self.engine, expire_on_commit=False))
        self.session_patch.start()
        self.settings = AppSettings()
        self.settings.email.enabled = True
        self.settings.email.digest_hour = 12
        with db.session_scope() as session:
            session.add(Event(kind='candidate', title='测试电影', urgent=False, notified=False))

    def tearDown(self):
        self.session_patch.stop()
        self.engine.dispose()
        self.temp.cleanup()

    def test_failure_persists_retry_and_success_does_not_resend(self):
        current = datetime(2026, 10, 11, 12)
        with patch('reelready.scheduler.load_settings', return_value=self.settings), \
             patch('reelready.scheduler.notify_urgent'), \
             patch('reelready.scheduler.now', return_value=current), \
             patch('reelready.services.events.send_mail', side_effect=NotifyError('connection refused')), \
             self.assertLogs('reelready.scheduler', level='ERROR'):
            Scheduler()._execute(JOBS['digest'], None, record=True)
        with db.session_scope() as session:
            state = session.get(JobState, 'digest')
            self.assertEqual(state.last_status, 'error')
            self.assertEqual(state.retry_at, current + timedelta(minutes=5))
            self.assertFalse(session.scalar(select(Event)).notified)
            self.assertTrue(_is_due(JOBS['digest'], self.settings, state.last_run_at, state.retry_at, state))
        # A new Scheduler represents a restarted container; state comes from SQLite.
        with patch('reelready.scheduler.load_settings', return_value=self.settings), \
             patch('reelready.scheduler.notify_urgent'), \
             patch('reelready.scheduler.now', return_value=current + timedelta(minutes=5)), \
             patch('reelready.services.events.send_mail') as send:
            scheduler = Scheduler()
            scheduler._execute(JOBS['digest'], None, record=True)
            scheduler._execute(JOBS['digest'], None, record=True)
            self.assertEqual(send.call_count, 1)
        with db.session_scope() as session:
            self.assertTrue(session.scalar(select(Event)).notified)
            state = session.get(JobState, 'digest')
            self.assertEqual(state.last_status, 'ok')
            self.assertIsNone(state.retry_at)
            self.assertEqual(state.retry_count, 0)
