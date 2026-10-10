import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from reelready import config, db
from reelready.models import Event, Movie, MovieStatus, now
from reelready.services.collector import _collect_douban, _collect_tmdb, _new_candidate
from reelready.services.movies import apply_douban, apply_tmdb, restore_primary_year, same_douban_movie
from reelready.settings import AppSettings, load_settings
from reelready.sources.douban import DoubanClient, DoubanMovie
from reelready.sources.tmdb import TMDBClient, TMDBError, TMDBMovie
from reelready.web.app import create_app


class RereleaseCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.temp.name) / 'test.db'}")
        db.Base.metadata.create_all(self.engine)
        self.patches = [
            patch.object(db, 'SessionLocal', sessionmaker(bind=self.engine, expire_on_commit=False)),
            patch('reelready.services.collector.cache_poster'),
        ]
        for item in self.patches:
            item.start()
        self.settings = AppSettings()
        self.settings.collect.tmdb_regions = ['IT', 'US']
        self.recent = date.today().isoformat()
        self.tmdb = Mock()
        self.tmdb.now_playing.side_effect = lambda region: [TMDBMovie(
            396535, '釜山行', original_title='부산행', year=date.today().year,
            release_date=self.recent, rating=7.8, votes=9000,
            details={'collection_releases': [{'region': region, 'date': self.recent}]},
        )]
        self.tmdb.movie.return_value = TMDBMovie(
            396535, '釜山行', original_title='부산행', year=2016,
            release_date='2016-07-20', rating=7.8, votes=9000,
            imdb_id='tt5700672', overview='电影简介',
        )
        self.douban = Mock()
        self.douban.find.return_value = None

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.engine.dispose()
        self.temp.cleanup()

    def movies(self):
        with db.session_scope() as session:
            return list(session.scalars(select(Movie)))

    def events(self):
        with db.session_scope() as session:
            return list(session.scalars(select(Event)))

    def collect(self):
        return _collect_tmdb(self.settings, self.douban, self.tmdb, [])

    def test_default_rejects_recent_regional_rerelease_before_creating_movie_or_event(self):
        self.assertEqual(self.collect(), 0)
        self.assertEqual(self.movies(), [])
        self.assertEqual(self.events(), [])
        self.tmdb.movie.assert_called_once_with(396535)
        self.douban.find.assert_not_called()

    def test_enabled_keeps_primary_year_and_all_regional_dates_and_deduplicates(self):
        self.settings.collect.include_rereleases = True
        self.assertEqual(self.collect(), 1)
        movie = self.movies()[0]
        self.assertEqual(movie.year, 2016)
        self.assertEqual(movie.details['release_date'], '2016-07-20')
        self.assertTrue(movie.details['is_rerelease'])
        self.assertEqual([r['region'] for r in movie.details['collection_releases']], ['IT', 'US'])
        self.assertIn('(2016)', self.events()[0].title)
        self.assertIn('重映收集', self.events()[0].message)
        self.assertEqual(self.collect(), 0)
        self.assertEqual(len(self.movies()), 1)
        self.assertEqual(len(self.events()), 1)
        self.tmdb.movie.assert_called_once()

    def test_turning_switch_off_preserves_existing_monitored_movie(self):
        self.settings.collect.include_rereleases = True
        self.collect()
        with db.session_scope() as session:
            session.get(Movie, self.movies()[0].id).status = MovieStatus.MONITORING
        self.settings.collect.include_rereleases = False
        self.assertEqual(self.collect(), 0)
        self.assertEqual(self.movies()[0].status, MovieStatus.MONITORING)
        self.assertEqual(self.movies()[0].year, 2016)

    def test_unverified_primary_date_is_not_collected_when_detail_fails(self):
        self.settings.collect.include_rereleases = True
        self.tmdb.movie.side_effect = TMDBError('fixture unavailable')
        self.assertEqual(self.collect(), 0)
        self.assertEqual(self.movies(), [])
        self.assertEqual(self.events(), [])

    def test_new_movie_still_collected_and_future_primary_date_is_rejected(self):
        item = self.tmdb.movie.return_value
        item.year = date.today().year
        item.release_date = self.recent
        self.assertEqual(self.collect(), 1)
        self.assertFalse(self.movies()[0].details['is_rerelease'])
        with db.session_scope() as session:
            session.delete(session.get(Movie, self.movies()[0].id))
        item.release_date = (date.today() + timedelta(days=30)).isoformat()
        self.settings.collect.include_rereleases = True
        self.assertEqual(self.collect(), 0)
        self.assertEqual(self.movies(), [])

    def test_second_region_recent_date_is_not_lost_when_first_region_date_is_old(self):
        self.tmdb.now_playing.side_effect = lambda region: [TMDBMovie(
            396535, '釜山行', year=2016 if region == 'IT' else date.today().year,
            release_date='2016-07-20' if region == 'IT' else self.recent,
            rating=7.8, votes=9000,
        )]
        item = self.tmdb.movie.return_value
        item.year = date.today().year
        item.release_date = self.recent
        self.assertEqual(self.collect(), 1)

    def test_douban_now_showing_old_movie_obeys_switch(self):
        self.douban.now_showing.return_value = [DoubanMovie('1', '釜山行', year=2016, rating=8, votes=9000)]
        self.douban.detail.return_value = DoubanMovie(
            '1', '釜山行', year=2016, release_date='2016-07-20',
            rating=8, votes=9000, intro='简介',
        )
        self.assertEqual(_collect_douban(self.settings, self.douban, None), 0)
        self.assertEqual(self.events(), [])
        self.settings.collect.include_rereleases = True
        self.assertEqual(_collect_douban(self.settings, self.douban, None), 1)
        self.assertEqual(self.movies()[0].year, 2016)
        self.assertTrue(self.movies()[0].details['is_rerelease'])

    def test_final_linked_primary_date_is_rechecked(self):
        with db.session_scope() as session:
            movie = Movie(title='釜山行', year=date.today().year, details={'release_date': self.recent})
            session.add(movie)
            session.flush()
            movie_id = movie.id

        def old_primary(session, movie, tmdb, douban):
            apply_tmdb(movie, self.tmdb.movie.return_value)
            return movie

        with patch('reelready.services.collector.link_ids', side_effect=old_primary):
            self.assertFalse(_new_candidate(self.settings, movie_id, self.douban, self.tmdb))
        self.assertEqual(self.movies(), [])
        self.assertEqual(self.events(), [])

    def test_linking_does_not_attach_a_different_douban_imdb(self):
        self.settings.collect.include_rereleases = True
        self.douban.find.return_value = DoubanMovie('wrong', '釜山行', year=2016)
        self.douban.detail.return_value = DoubanMovie('wrong', '釜山行', year=2016, imdb_id='tt1111111')
        self.assertEqual(self.collect(), 1)
        self.assertIsNone(self.movies()[0].douban_id)

    def test_settings_page_renders_switch_and_autosave_persists_it(self):
        client = TestClient(create_app())  # Do not enter lifespan or start the scheduler.
        try:
            page = client.get('/settings')
            self.assertEqual(page.status_code, 200)
            self.assertIn('name="include_rereleases"', page.text)
            self.assertIn('收集重映影片', page.text)
            data = {}
            for key, value in self.settings.collect.model_dump().items():
                if isinstance(value, bool):
                    if value:
                        data[key] = 'on'
                else:
                    data[key] = ','.join(value) if isinstance(value, list) else str(value)
            data['include_rereleases'] = 'on'
            self.assertEqual(client.post('/settings/collect', data=data, headers={'HX-Request': 'true'}).status_code, 200)
            self.assertTrue(load_settings().collect.include_rereleases)
            data.pop('include_rereleases')
            self.assertEqual(client.post('/settings/collect', data=data, headers={'HX-Request': 'true'}).status_code, 200)
            self.assertFalse(load_settings().collect.include_rereleases)
        finally:
            client.close()

    def test_rerelease_details_show_primary_year_and_separate_regional_date(self):
        self.settings.collect.include_rereleases = True
        self.collect()
        movie_id = self.movies()[0].id
        with db.session_scope() as session:
            session.get(Movie, movie_id).metadata_checked_at = now()
        client = TestClient(create_app())
        try:
            page = client.get(f'/movies/{movie_id}')
            self.assertEqual(page.status_code, 200)
            self.assertIn('重映收集', page.text)
            self.assertIn('2016-07-20', page.text)
            self.assertIn('本次院线', page.text)
            self.assertIn('意大利 · ' + self.recent, page.text)
        finally:
            client.close()

    def test_startup_repairs_legacy_year_and_preserves_movie_state_with_backup(self):
        with db.session_scope() as session:
            movie = Movie(title='釜山行', year=date.today().year, source='tmdb', tmdb_id=396535,
                          status=MovieStatus.COMPLETED, downloaded={'title': 'existing download'},
                          details={'release_date': '2016-07-20', 'releases': [
                              {'date': '2016-07-20', 'region': 'KR'},
                              {'date': self.recent, 'region': 'IT'},
                          ]})
            session.add(movie)
            session.flush()
            movie_id = movie.id
        directory = Path(self.temp.name)
        with patch.object(db, 'engine', self.engine), patch.object(config, 'DATA_DIR', directory), patch.object(config, 'DB_PATH', directory / 'test.db'):
            db.init_db()
            db.init_db()  # Idempotent; a second start must not create another backup.
        saved = self.movies()[0]
        self.assertEqual(saved.id, movie_id)
        self.assertEqual(saved.year, 2016)
        self.assertEqual(saved.status, MovieStatus.COMPLETED)
        self.assertEqual(saved.downloaded, {'title': 'existing download'})
        self.assertTrue(saved.details['is_rerelease'])
        self.assertEqual(saved.details['collection_releases'], [{'date': self.recent, 'region': 'IT'}])
        backups = list(directory.glob('before-movie-year-fix-*.db'))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as backup:
            self.assertEqual(backup.execute('SELECT year FROM movies WHERE id=?', (movie_id,)).fetchone()[0], date.today().year)
        self.assertEqual(self.events(), [])
        client = TestClient(create_app())
        try:
            page = client.get('/movies?tab=completed')
            self.assertIn('2016', page.text)
            self.assertIn('釜山行', page.text)
        finally:
            client.close()

    def test_refresh_detaches_legacy_wrong_douban_link_and_restores_tmdb_title(self):
        from reelready.services.movie_metadata import refresh_metadata
        with db.session_scope() as session:
            movie = Movie(title='Wrong Short Film', year=2026, source='tmdb',
                          tmdb_id=396535, douban_id='wrong', imdb_id='tt5700672',
                          details={'aliases': ['Wrong Alias'], 'pubdates': ['2025'], 'durations': ['19分钟'], 'is_rerelease': True})
            session.add(movie)
            session.flush()
            movie_id = movie.id
        douban_context = MagicMock()
        douban_context.__enter__.return_value = self.douban
        self.douban.detail.return_value = DoubanMovie('wrong', 'Wrong Short Film', year=2025, imdb_id='tt1111111')
        with patch('reelready.services.movie_metadata.make_douban', return_value=douban_context), patch('reelready.services.movie_metadata.make_tmdb', return_value=self.tmdb), patch('reelready.services.pt.revalidate_cached_candidates'):
            refresh_metadata(movie_id)
        saved = self.movies()[0]
        self.assertEqual(saved.title, '釜山行')
        self.assertEqual(saved.year, 2016)
        self.assertIsNone(saved.douban_id)
        self.assertEqual(saved.details['sources'], ['TMDB'])
        self.assertTrue(saved.details['is_rerelease'])
        for field in ('aliases', 'pubdates', 'durations'):
            self.assertNotIn(field, saved.details)



class MovieIdentityTests(unittest.TestCase):
    def test_legacy_repair_requires_a_valid_cached_tmdb_theatrical_date(self):
        for tmdb_id, year, primary, releases in (
            (None, 2026, '2016-07-20', [{'date': '2016-07-20'}]),
            (396535, 2026, 'not-a-date', [{'date': 'not-a-date'}]),
            (396535, 2026, '2016-07-20', []),
            (396535, 2016, '2026-07-20', [{'date': '2026-07-20'}]),
        ):
            with self.subTest(tmdb_id=tmdb_id, year=year, primary=primary):
                movie = Movie(title='电影', tmdb_id=tmdb_id, year=year, details={'release_date': primary, 'releases': releases})
                self.assertFalse(restore_primary_year(movie))
                self.assertEqual(movie.year, year)

    def test_douban_search_rejects_similar_short_film_even_with_close_year(self):
        with DoubanClient() as client:
            with patch.object(client, 'search', return_value=[
                DoubanMovie('wrong', '부자산행', year=2025),
                DoubanMovie('correct', '부산행', year=2016),
            ]):
                self.assertIsNone(client.find(['釜山行', '부산행'], 2026))
                self.assertEqual(client.find(['釜山行', '부산행'], 2016).id, 'correct')

    def test_tmdb_search_requires_title_and_allows_year_tolerance(self):
        with TMDBClient('fixture') as client:
            with patch.object(client, 'search', return_value=[
                TMDBMovie(1, 'Other Film', year=2016),
                TMDBMovie(2, 'Train to Busan', year=2017),
            ]):
                self.assertEqual(client.find(['Train-to-Busan'], 2016).id, 2)

    def test_full_detail_corrects_regional_year(self):
        movie = Movie(title='釜山行', year=2026, details={'is_rerelease': True})
        apply_tmdb(movie, TMDBMovie(396535, '釜山行', year=2016, release_date='2016-07-20', details={'is_rerelease': False}))
        self.assertEqual(movie.year, 2016)
        self.assertFalse(movie.details['is_rerelease'])

    def test_wrong_linked_metadata_cannot_overwrite_movie(self):
        movie = Movie(
            title='釜山行', original_title='부산행', year=2026, tmdb_id=396535,
            imdb_id='tt5700672', details={'release_date': '2016-07-20', 'runtime': 118},
        )
        wrong = DoubanMovie('38407676', '부자산행', year=2025, details={'genres': ['短片']})
        self.assertFalse(apply_douban(movie, wrong))
        self.assertEqual(movie.title, '釜山行')
        self.assertNotIn('genres', movie.details)
        self.assertFalse(same_douban_movie(movie, DoubanMovie('wrong', '釜山行', year=2016, imdb_id='tt1111111')))
        self.assertTrue(same_douban_movie(movie, DoubanMovie('correct', 'Train to Busan', year=2016, imdb_id='tt5700672')))

    def test_matching_douban_regional_date_does_not_replace_primary_date(self):
        movie = Movie(title='釜山行', year=2016, tmdb_id=396535, imdb_id='tt5700672', details={'release_date': '2016-07-20'})
        self.assertTrue(apply_douban(movie, DoubanMovie(
            'correct', '釜山行', year=2016, imdb_id='tt5700672', release_date='2026-09-02',
            details={'release_date': '2026-09-02', 'pubdates': ['2026-09-02(意大利)']},
        )))
        self.assertEqual(movie.year, 2016)
        self.assertEqual(movie.details['release_date'], '2016-07-20')


if __name__ == '__main__':
    unittest.main()
