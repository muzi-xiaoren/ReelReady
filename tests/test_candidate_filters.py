import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reelready import config, db
from reelready.models import Event, Movie
from reelready.web.app import create_app


class CandidateFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        directory = Path(self.temp.name)
        self.engine = create_engine(f'sqlite:///{directory / "test.db"}', connect_args={'check_same_thread': False})
        self.patches = [patch.object(db, 'engine', self.engine),
                        patch.object(db, 'SessionLocal', sessionmaker(bind=self.engine, expire_on_commit=False)),
                        patch.object(config, 'DATA_DIR', directory), patch.object(config, 'DB_PATH', directory / 'test.db')]
        for item in self.patches:
            item.start()
        db.init_db()
        with db.session_scope() as session:
            rows = [
                Movie(title='Available', stage=2, last_checked_at=datetime.now(), year=2020, douban_rating=8, douban_votes=100, tmdb_rating=9, created_at=datetime(2026, 1, 1)),
                Movie(title='Dated', stage=1, digital_date='2099-01-01', year=2026, douban_rating=9, tmdb_rating=7, created_at=datetime(2026, 1, 2)),
                Movie(title='Unconfirmed', stage=0, last_checked_at=datetime.now(), year=2025, created_at=datetime(2026, 1, 3)),
                Movie(title='Unchecked', stage=0, year=2024, created_at=datetime(2026, 1, 4)),
                Movie(title='Ignored', stage=2, status='ignored'),
            ]
            session.add_all(rows)
            session.flush()
            self.available_id = rows[0].id
            self.unchecked_id = rows[3].id
        self.client = TestClient(create_app())

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def page(self, **params):
        response = self.client.get('/movies', params={'tab': 'candidate', **params})
        self.assertEqual(response.status_code, 200)
        return BeautifulSoup(response.text, 'html.parser')

    def titles(self, page):
        return [item.get_text(strip=True) for item in page.select('.movie-title')]

    def test_filters_are_exclusive_and_counts_exclude_other_lists(self):
        page = self.page()
        counts = {a['data-availability']: int(a.select_one('span').text) for a in page.select('[data-availability]')}
        self.assertEqual(counts, {'all': 4, 'available': 1, 'dated': 1, 'unconfirmed': 1, 'unchecked': 1})
        for category, title in [('available', 'Available'), ('dated', 'Dated'), ('unconfirmed', 'Unconfirmed'), ('unchecked', 'Unchecked')]:
            self.assertEqual(self.titles(self.page(availability=category)), [title])

    def test_sorting_keeps_null_ratings_last_and_has_stable_default(self):
        self.assertEqual(self.titles(self.page()), ['Unchecked', 'Unconfirmed', 'Dated', 'Available'])
        self.assertEqual(self.titles(self.page(sort='available')), ['Available', 'Dated', 'Unconfirmed', 'Unchecked'])
        self.assertEqual(self.titles(self.page(sort='douban'))[:2], ['Dated', 'Available'])
        self.assertEqual(self.titles(self.page(sort='tmdb'))[:2], ['Available', 'Dated'])
        self.assertEqual(self.titles(self.page(sort='year')), ['Dated', 'Unconfirmed', 'Unchecked', 'Available'])

    def test_search_filter_and_sort_are_combined_and_preserved_in_links(self):
        with db.session_scope() as session:
            session.get(Movie, self.available_id).title = 'Alice & Bob'
        page = self.page(q='Alice & Bob', availability='available', sort='douban')
        self.assertEqual(self.titles(page), ['Alice & Bob'])
        self.assertEqual(page.select_one('[name=q]')['value'], 'Alice & Bob')
        self.assertEqual(page.select_one('option[selected]')['value'], 'douban')
        for link in page.select('[data-availability]'):
            self.assertIn('q=Alice+%26+Bob', link['href'])
            self.assertIn('sort=douban', link['href'])

    def test_background_stage_change_moves_candidate_between_filters(self):
        before = self.client.get('/movies/revision').json()['revision']
        with db.session_scope() as session:
            row = session.get(Movie, self.unchecked_id)
            row.stage = 2
            row.last_checked_at = datetime.now()
        self.assertNotEqual(before, self.client.get('/movies/revision').json()['revision'])
        self.assertEqual(self.titles(self.page(availability='unchecked')), [])
        self.assertCountEqual(self.titles(self.page(availability='available')), ['Available', 'Unchecked'])

    def test_empty_filtered_view_offers_reset_and_invalid_options_fall_back(self):
        page = self.page(availability='dated', q='absent')
        self.assertIn('这个分类下暂无匹配的影片', page.get_text())
        self.assertIn('清除筛选', page.get_text())
        self.assertEqual(len(self.titles(self.page(availability='invalid', sort='invalid'))), 4)
        response = self.client.get('/movies?tab=ignored&availability=unchecked&sort=available')
        page = BeautifulSoup(response.text, 'html.parser')
        self.assertEqual(self.titles(page), ['Ignored'])
        self.assertFalse(page.select('.candidate-tools'))

    def test_event_groups_filter_before_limit_and_keep_exact_kind_filter(self):
        with db.session_scope() as session:
            session.add_all([Event(kind='candidate', title='Recent discovery', created_at=datetime(2026, 10, 24)) for _ in range(305)])
            session.add_all([Event(kind='download_failed', title='Download failure', urgent=True, created_at=datetime(2026, 10, 23)),
                             Event(kind='site_invalid', title='Site login failure', urgent=True, created_at=datetime(2026, 10, 22)),
                             Event(kind='downloaded', title='Submitted download', created_at=datetime(2026, 10, 21))])
        def titles(**params):
            page = BeautifulSoup(self.client.get('/events', params=params).text, 'html.parser')
            return [el.get_text(strip=True) for el in page.select('.event-title')]
        self.assertEqual(titles(group='errors'), ['Download failure', 'Site login failure'])
        self.assertEqual(titles(group='downloads'), ['Download failure', 'Submitted download'])
        self.assertEqual(titles(group='errors', kind='site_invalid'), ['Site login failure'])
        self.assertEqual(titles(group='movies', kind='site_invalid'), [])
        self.assertEqual(len(titles(group='invalid', kind='invalid')), 300)

    def test_management_pages_share_one_navigation_and_preserve_all_settings(self):
        for path in ['/movies', '/events', '/sites', '/settings']:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            page = BeautifulSoup(response.text, 'html.parser')
            self.assertEqual(len(page.select('#gallery-dock')), 1)
            self.assertEqual(len(page.select('[data-gallery-category]')), 5)
            self.assertEqual(len(page.select('#jobs')), 1)
            self.assertEqual(page.select_one('[data-gallery-category="candidate"] .count').text, '4')
        page = BeautifulSoup(self.client.get('/settings').text, 'html.parser')
        from reelready.settings import SECTIONS
        self.assertEqual({form['data-autosave'] for form in page.select('[data-autosave]')},
                         {'/settings/' + key for key, _ in SECTIONS})
