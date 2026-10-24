import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from reelready import config, db
from reelready.models import Event, Movie, MovieStatus, Stage
from reelready.services.checker import apply_availability
from reelready.services.collector import _new_candidate
from reelready.services.movie_metadata import queue_candidate_backfill, refresh_metadata
from reelready.services.movies import infer_rerelease
from reelready.settings import AppSettings
from reelready.sources.tmdb import TMDBError, TMDBMovie
from reelready.web.app import create_app


class CandidateAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        directory = Path(self.temp.name)
        self.engine = create_engine(f'sqlite:///{directory / "test.db"}', connect_args={'check_same_thread': False})
        self.patches = [
            patch.object(db, 'engine', self.engine),
            patch.object(db, 'SessionLocal', sessionmaker(bind=self.engine, expire_on_commit=False)),
            patch.object(config, 'DATA_DIR', directory),
            patch.object(config, 'DB_PATH', directory / 'test.db'),
        ]
        for item in self.patches:
            item.start()
        db.init_db()
        self.client = TestClient(create_app())  # No lifespan or scheduled jobs.
        with db.session_scope() as session:
            row = Movie(title='Fixture', tmdb_id=212162, year=date.today().year, source='tmdb')
            session.add(row)
            session.flush()
            self.movie_id = row.id

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def movie(self):
        with db.session_scope() as session:
            return session.get(Movie, self.movie_id)

    def apply(self, **kwargs):
        with db.session_scope() as session:
            row = session.get(Movie, self.movie_id)
            return apply_availability(session, row, TMDBMovie(212162, 'Fixture', **kwargs))

    def test_unchecked_candidate_does_not_claim_cinema_or_active_monitoring(self):
        self.assertEqual(self.movie().stage_label, '待检测')
        response = self.client.get(f'/movies/{self.movie_id}')
        self.assertEqual(response.status_code, 200)
        self.assertIn('上线状态待检测', response.text)
        self.assertNotIn('持续监测中', response.text)
        self.assertNotIn('院线中', response.text)

    def test_past_digital_date_marks_available_without_approval_or_event(self):
        self.apply(digital_date=(date.today() - timedelta(days=1)).isoformat())
        row = self.movie()
        self.assertEqual(row.stage, Stage.STREAMING)
        self.assertEqual(row.status, MovieStatus.CANDIDATE)
        self.assertIsNotNone(row.last_checked_at)
        with db.session_scope() as session:
            self.assertEqual(list(session.scalars(select(Event))), [])
        response = self.client.get(f'/movies/{row.id}')
        self.assertIn('已查到上线信息', response.text)
        self.assertIn('待你确认后才会自动监测', response.text)

    def test_future_buy_only_preorder_is_not_available(self):
        self.apply(digital_date=(date.today() + timedelta(days=30)).isoformat(), providers=['US · Amazon(买)'])
        self.assertEqual(self.movie().stage, Stage.DATED)

    def test_successful_lookup_without_evidence_does_not_claim_cinema(self):
        self.apply()
        row = self.movie()
        self.assertEqual(row.stage, Stage.IN_CINEMA)
        self.assertEqual(row.stage_label, '暂未确认上线')
        self.assertFalse(row.availability_pending)

    def test_wrong_source_identity_does_not_count_as_lookup(self):
        with db.session_scope() as session:
            row = session.get(Movie, self.movie_id)
            self.assertFalse(apply_availability(session, row, TMDBMovie(999, 'Wrong', providers=['US · X(订阅)'])))
        self.assertTrue(self.movie().availability_pending)

    def test_collection_reuses_detail_and_never_approves_candidate(self):
        tmdb, douban = Mock(), Mock()
        info = TMDBMovie(212162, 'Fixture', digital_date=date.today().isoformat())
        with patch('reelready.services.collector.link_ids', side_effect=lambda session, row, *args: row), patch('reelready.services.collector.cache_poster'):
            self.assertTrue(_new_candidate(AppSettings(), self.movie_id, douban, tmdb, tmdb_info=info))
        tmdb.movie.assert_not_called()
        douban.detail.assert_not_called()
        self.assertEqual(self.movie().status, MovieStatus.CANDIDATE)
        self.assertEqual(self.movie().stage, Stage.STREAMING)
        with db.session_scope() as session:
            self.assertEqual([e.kind for e in session.scalars(select(Event))], ['candidate'])

    def test_failed_initial_lookup_remains_pending(self):
        tmdb = Mock()
        tmdb.movie.side_effect = TMDBError('offline')
        with patch('reelready.services.collector.link_ids', side_effect=lambda session, row, *args: row), patch('reelready.services.collector.cache_poster'):
            _new_candidate(AppSettings(), self.movie_id, Mock(), tmdb)
        self.assertTrue(self.movie().availability_pending)

    def test_metadata_refresh_updates_availability_without_approval(self):
        tmdb = Mock()
        tmdb.movie.return_value = TMDBMovie(212162, 'Fixture', providers=['US · X(订阅)'])
        with patch('reelready.services.movie_metadata.make_tmdb', return_value=tmdb), patch('reelready.services.pt.revalidate_cached_candidates') as revalidate:
            refresh_metadata(self.movie_id)
        tmdb.movie.assert_called_once_with(212162, AppSettings().check.provider_regions)
        revalidate.assert_called_once_with(movie_ids=[self.movie_id])
        self.assertEqual(self.movie().stage, Stage.STREAMING)
        self.assertEqual(self.movie().status, MovieStatus.CANDIDATE)

    def rerelease_details(self, region='JP'):
        return {'release_date': '2013-10-26', 'releases': [
            {'date': '2013-10-26', 'region': 'JP'},
            {'date': date.today().isoformat(), 'region': region},
        ]}

    def test_same_region_rerelease_repaired_idempotently_without_changing_status(self):
        with db.session_scope() as session:
            row = session.get(Movie, self.movie_id)
            row.details = self.rerelease_details()
            row.year = 2013
        db.init_db()
        db.init_db()
        row = self.movie()
        self.assertTrue(row.details['is_rerelease'])
        self.assertEqual(row.year, 2013)
        self.assertEqual(row.status, MovieStatus.CANDIDATE)
        self.assertEqual(row.stage, Stage.IN_CINEMA)
        self.assertIn('重映', self.client.get('/movies?tab=candidate').text)
        self.assertIn('重映收集', self.client.get(f'/movies/{row.id}').text)

    def test_different_region_first_release_is_not_inferred_as_rerelease(self):
        row = Movie(details=self.rerelease_details(region='TW'), created_at=datetime.now())
        self.assertFalse(infer_rerelease(row))

    def test_backfill_runs_without_opening_detail_and_skips_recent_failures(self):
        with patch('reelready.services.movie_metadata.queue_metadata', return_value=True) as queue:
            self.assertTrue(queue_candidate_backfill())
        queue.assert_called_once_with(self.movie_id)
        with db.session_scope() as session:
            session.get(Movie, self.movie_id).metadata_checked_at = datetime.now()
        with patch('reelready.services.movie_metadata.queue_metadata') as queue:
            self.assertFalse(queue_candidate_backfill())
        queue.assert_not_called()

    def test_backfill_never_queues_monitored_watched_or_checked_candidates(self):
        for status in (MovieStatus.MONITORING, MovieStatus.WATCHED, MovieStatus.IGNORED, MovieStatus.COMPLETED):
            with db.session_scope() as session:
                session.get(Movie, self.movie_id).status = status
            with patch('reelready.services.movie_metadata.queue_metadata') as queue:
                self.assertFalse(queue_candidate_backfill())
            queue.assert_not_called()
        with db.session_scope() as session:
            row = session.get(Movie, self.movie_id)
            row.status = MovieStatus.CANDIDATE
            row.last_checked_at = datetime.now()
        with patch('reelready.services.movie_metadata.queue_metadata') as queue:
            self.assertFalse(queue_candidate_backfill())
        queue.assert_not_called()

    def test_backfill_does_not_queue_more_while_another_lookup_is_pending(self):
        with patch('reelready.services.movie_metadata._pending', {999}), patch('reelready.services.movie_metadata.queue_metadata') as queue:
            self.assertFalse(queue_candidate_backfill())
        queue.assert_not_called()

    def test_scheduler_tick_backfills_without_manual_visit(self):
        from reelready.scheduler import Scheduler
        with patch('reelready.scheduler._is_due', return_value=False), patch('reelready.services.movie_metadata.queue_candidate_backfill') as backfill:
            Scheduler()._run_due()
        backfill.assert_called_once_with()

    def test_local_revision_is_stable_and_tracks_updates_and_deletions_without_external_calls(self):
        from sqlalchemy import event
        statements = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(self.engine, 'before_cursor_execute', capture)
        try:
            with patch('reelready.web.routes.queue_metadata') as queue:
                initial = self.client.get('/movies/revision')
                self.assertEqual(initial.status_code, 200)
                self.assertEqual(initial.headers['cache-control'], 'no-store')
                version = initial.json()['revision']
                self.assertEqual(version, self.client.get('/movies/revision').json()['revision'])
                self.assertEqual(version, self.client.get(f'/movies/revision?movie_id={self.movie_id}').json()['revision'])
            queue.assert_not_called()
        finally:
            event.remove(self.engine, 'before_cursor_execute', capture)
        self.assertFalse(any(column in q for q in statements for column in ('movies.details', 'movies.overview', 'movies.last_pt_candidates')))
        with db.session_scope() as session:
            session.get(Movie, self.movie_id).title = 'Changed without sleeping'
        updated = self.client.get('/movies/revision').json()['revision']
        self.assertNotEqual(version, updated)
        with db.session_scope() as session:
            session.delete(session.get(Movie, self.movie_id))
        self.assertNotEqual(updated, self.client.get('/movies/revision').json()['revision'])

    def test_detail_live_region_contains_progress_and_information(self):
        with patch('reelready.web.routes.metadata_pending', return_value=True), patch('reelready.web.routes.queue_metadata'):
            html = self.client.get(f'/movies/{self.movie_id}').text
        self.assertLess(html.index('id="movie-live-view"'), html.index('aria-label="影片进度"'))
        self.assertLess(html.index('aria-label="影片进度"'), html.index('id="movie-information"'))
        self.assertIn('data-pending="true"', html)
        self.assertNotIn('hx-select="#movie-information"', html)


if __name__ == '__main__':
    unittest.main()
