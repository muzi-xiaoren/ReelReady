import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from reelready import config, db
from reelready.models import Movie, Site
from reelready.services import site_tests
from reelready.services.sites import add_site, test_site
from reelready.settings import load_settings
from reelready.web.app import create_app


class SiteManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        directory = Path(self.temp.name)
        engine = create_engine(f"sqlite:///{directory / 'test.db'}", connect_args={"check_same_thread": False, "timeout": 30})
        self.patches = [
            patch.object(db, "engine", engine),
            patch.object(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False)),
            patch.object(config, "DATA_DIR", directory),
            patch.object(config, "DB_PATH", directory / "test.db"),
        ]
        for item in self.patches:
            item.start()
        self.engine = engine
        db.init_db()
        self.settings = load_settings()
        self.client = TestClient(create_app())  # No lifespan/scheduler or external requests.

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def add(self, url="https://tjupt.org"):
        return add_site(self.settings, kind="nexusphp", name="test", base_url=url, cookie="synthetic=1")

    def test_concurrent_adds_and_aliases_create_only_one_site(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.add(), range(16)))
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(len({site.id for site, _ in results}), 1)
        site, created = self.add("http://www.tjupt.org/")
        self.assertFalse(created)
        self.assertEqual(site.id, results[0][0].id)

    def test_http_add_returns_while_test_is_blocked_and_drops_repeats(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def blocked_test(settings, site_id):
            entered.set()
            release.wait(5)
            with db.session_scope() as session:
                session.get(Site, site_id).status = "ok"
            finished.set()
            return True, "ok"

        with patch.object(site_tests, "test_site", side_effect=blocked_test) as check:
            try:
                started = time.monotonic()
                response = self.client.post("/sites/add", data={"kind": "nexusphp", "base_url": "https://tjupt.org", "cookie": "synthetic=1"}, headers={"HX-Request": "true"})
                self.assertEqual(response.status_code, 200)
                self.assertLess(time.monotonic() - started, 1)
                self.assertNotIn("HX-Refresh", response.headers)
                self.assertEqual(response.headers["HX-Trigger-After-Settle"], "sitesChanged")
                self.assertTrue(entered.wait(1))
                site_id = self.add()[0].id
                self.assertEqual(site_tests.queue_site_test(site_id), "pending")
                self.client.post("/sites/add", data={"kind": "nexusphp", "base_url": "https://www.tjupt.org"}, headers={"HX-Request": "true"})
                html = self.client.get("/sites").text
                self.assertIn("测试中…", html)
                self.assertIn("every 2s", html)
                self.assertIn('id="connected-sites"', html)
                with db.session_scope() as session:
                    self.assertEqual(len(list(session.scalars(select(Site)))), 1)
                check.assert_called_once()
            finally:
                release.set()
                self.assertTrue(finished.wait(2))
                deadline = time.monotonic() + 2
                while site_tests._pending and time.monotonic() < deadline:
                    time.sleep(.01)
        self.assertNotIn("every 2s", self.client.get("/sites").text)

    def test_old_connection_result_does_not_overwrite_new_cookie(self):
        site, _ = self.add()
        def change_cookie():
            with db.session_scope() as session:
                row = session.get(Site, site.id)
                row.cookie = "updated=1"
                row.status = "unknown"
            return "old connection succeeded"
        with patch("reelready.services.sites.build_site") as build:
            build.return_value.test.side_effect = change_cookie
            ok, message = test_site(self.settings, site.id)
        self.assertFalse(ok)
        with db.session_scope() as session:
            self.assertEqual(session.get(Site, site.id).status, "unknown")

    def test_other_site_aliases_render_once_with_all_domains(self):
        hosts = ["bwtorrents.tv", "bwtorrents.cc", "bwtorrents.xyz", "bwtorrents.us"]
        data = {"cookie_data": {host: [{"domain": host, "name": "visitor", "value": "synthetic"}] for host in hosts}}
        with patch("reelready.web.routes.load_cookiecloud", return_value=(data, None)), patch("reelready.web.routes.detected_sites", return_value=[]):
            html = self.client.get("/sites").text
        self.assertEqual(html.count("<strong>BWT</strong>"), 1)
        self.assertIn("4 个域名", html)
        for host in hosts:
            self.assertIn(host, html)

    def test_rousipro_requires_api_key_and_uses_default_address(self):
        response = self.client.post("/sites/add", data={"kind": "rousipro"}, headers={"HX-Request": "true"})
        self.assertIn("API Key", response.headers["HX-Trigger"])
        with db.session_scope() as session:
            self.assertEqual(len(list(session.scalars(select(Site)))), 0)
        with patch("reelready.web.routes.queue_site_test", return_value="queued"):
            response = self.client.post("/sites/add", data={"kind": "rousipro", "api_key": "synthetic"}, headers={"HX-Request": "true"})
        self.assertNotIn("synthetic", response.headers["HX-Trigger"])
        with db.session_scope() as session:
            site = session.scalar(select(Site))
            self.assertEqual(site.base_url, "https://rousi.pro")
            self.assertFalse(site.use_cookiecloud)
        html = self.client.get("/sites").text
        self.assertIn("Rousi Pro (API Key)", html)
        self.assertIn('placeholder="留空保持不变"', html)

    def test_queue_has_backpressure(self):
        site, _ = self.add()
        with patch.object(site_tests, "MAX_PENDING", 0):
            self.assertEqual(site_tests.queue_site_test(site.id), "busy")
        with db.session_scope() as session:
            self.assertEqual(session.get(Site, site.id).status, "unknown")

    def test_settings_controls_render_masked_secrets_and_region_names(self):
        from reelready.settings import save_settings
        self.settings.tmdb.api_key = 'synthetic-private-value'
        save_settings(self.settings)
        html = self.client.get('/settings').text
        self.assertNotIn('synthetic-private-value', html)
        self.assertIn('value="••••••••••••"', html)
        self.assertIn('中国大陆', html)
        self.assertIn('data-order="up"', html)
        self.assertIn('name="max_age_months"', html)

    def test_movie_detail_renders_dates_and_cast_and_queues_old_movie(self):
        with db.session_scope() as session:
            row = Movie(title='Metadata example', douban_id='synthetic', overview='剧情介绍', details={'release_date': '2026-08-11', 'cast': [{'name': '演员甲', 'role': '主角'}], 'genres': ['剧情']})
            session.add(row)
        with patch('reelready.web.routes.queue_metadata') as queue, patch('reelready.web.routes.metadata_pending', return_value=True):
            html = self.client.get(f'/movies/{row.id}').text
        queue.assert_called_once_with(row.id)
        self.assertIn('2026-08-11', html)
        self.assertIn('演员甲', html)
        self.assertIn('主角', html)
        self.assertIn('hx-trigger="every 2s"', html)

    def test_early_search_switch_hides_cached_torrents_and_blocks_manual_search(self):
        from datetime import date, timedelta
        from reelready.models import now
        from reelready.settings import save_settings
        with db.session_scope() as session:
            row = Movie(title='unreleased', digital_date=(date.today() + timedelta(days=10)).isoformat(), metadata_checked_at=now(), last_pt_summary='old results', last_pt_candidates=[{'title': 'Cached movie 1080p WEB-DL', 'site_name': 'fixture', 'size_bytes': 1234, 'seeders': 1, 'ok': False, 'reason': '等待', 'tags': []}])
            session.add(row)
        html = self.client.get(f'/movies/{row.id}').text
        self.assertNotIn('Cached movie 1080p WEB-DL', html)
        self.assertNotIn('old results', html)
        with patch('reelready.web.routes.scheduler.trigger') as trigger:
            self.client.post(f'/movies/{row.id}/search', headers={'HX-Request': 'true'})
        trigger.assert_not_called()
        self.settings.pt.early_search = True
        save_settings(self.settings)
        self.assertIn('Cached movie 1080p WEB-DL', self.client.get(f'/movies/{row.id}').text)
        self.settings.pt.early_search = False
        save_settings(self.settings)
        with db.session_scope() as session:
            session.get(Movie, row.id).digital_date = date.today().isoformat()
        self.assertIn('Cached movie 1080p WEB-DL', self.client.get(f'/movies/{row.id}').text)

    def test_metadata_fetch_preserves_existing_details_when_sources_fail(self):
        from reelready.services.movie_metadata import refresh_metadata
        from reelready.sources.douban import DoubanError
        with db.session_scope() as session:
            row = Movie(title='old', douban_id='synthetic', overview='已保存简介', details={'runtime': 120})
            session.add(row)
        with patch('reelready.services.movie_metadata.make_douban') as source, patch('reelready.services.movie_metadata.make_tmdb', return_value=None):
            source.return_value.__enter__.return_value.detail.side_effect = DoubanError('offline')
            refresh_metadata(row.id)
        with db.session_scope() as session:
            row = session.get(Movie, row.id)
            self.assertEqual(row.overview, '已保存简介')
            self.assertEqual(row.details['runtime'], 120)
            self.assertIsNotNone(row.metadata_checked_at)

    def test_manual_sites_are_grouped_without_duplicate_adds(self):
        with patch("reelready.web.routes.queue_site_test", return_value="queued") as check:
            for category in ("custom", "standard"):
                response = self.client.post("/sites/add", data={"kind": "nexusphp", "name": "Manual example", "base_url": "https://manual.example", "cookie": "synthetic=1", "category": category}, headers={"HX-Request": "true"})
                self.assertEqual(response.status_code, 200)
        check.assert_called_once()
        with db.session_scope() as session:
            rows = list(session.scalars(select(Site)))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].category, "custom")
        html = self.client.get("/sites").text
        self.assertIn('class="site-name">Manual example', html)

    def test_hide_and_restore_survive_reload_and_cookie_changes(self):
        from reelready.services.sites import hidden_discovery_sites
        data = {"cookie_data": {"tjupt.org": [{"domain": "tjupt.org", "name": "c_secure_pass", "value": "synthetic"}]}}
        with patch("reelready.web.routes.load_cookiecloud", return_value=(data, None)), patch("reelready.web.routes.detected_sites", return_value=["tjupt.org"]):
            response = self.client.post("/sites/discovery/visibility", data={"host": "tjupt.org"}, headers={"HX-Request": "true"})
            self.assertEqual(response.status_code, 200)
            html = self.client.get("/sites").text
            self.assertNotIn('aria-label="添加 北洋园PT', html)
            self.assertIn('aria-label="恢复 北洋园PT"', html)
        with patch("reelready.web.routes.load_cookiecloud", return_value=(None, None)):
            response = self.client.post("/sites/discovery/visibility", data={"host": "www.tjupt.org", "hidden": "false"}, headers={"HX-Request": "true"})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(hidden_discovery_sites(), {})
        with patch("reelready.web.routes.load_cookiecloud", return_value=(data, None)), patch("reelready.web.routes.detected_sites", return_value=["tjupt.org"]):
            self.assertIn('aria-label="添加 北洋园PT', self.client.get("/sites").text)

    def test_hide_catalog_aliases_and_concurrent_changes(self):
        from reelready.services.sites import set_discovery_hidden, hidden_discovery_sites, discovery_identity
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda host: set_discovery_hidden(host, True), ["bwtorrents.tv", "bwtorrents.cc", "tjupt.org", "hdfans.org"]))
        hidden = hidden_discovery_sites()
        self.assertEqual(len(hidden), 3)
        self.assertEqual(discovery_identity("bwtorrents.tv"), discovery_identity("bwtorrents.cc"))
        data = {"cookie_data": {host: [{"domain": host, "name": "visitor", "value": "synthetic"}] for host in ["bwtorrents.tv", "bwtorrents.cc"]}}
        with patch("reelready.web.routes.load_cookiecloud", return_value=(data, None)), patch("reelready.web.routes.detected_sites", return_value=[]):
            html = self.client.get("/sites").text
        self.assertEqual(html.count("<strong>BWT</strong>"), 1)
        self.assertNotIn('class="site-disclosure other-sites"', html)

    def test_old_database_gets_category_without_losing_sites(self):
        site, _ = self.add()
        with self.engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE sites DROP COLUMN category")
        db.init_db()
        with db.session_scope() as session:
            row = session.get(Site, site.id)
            self.assertEqual(row.category, "standard")
            self.assertEqual(row.cookie, "synthetic=1")

    def test_concurrent_settings_groups_preserve_each_other(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.client.post, '/settings/pt', data={'interval_hours': '7', 'request_delay_seconds': '8'}, headers={'HX-Request': 'true'}), pool.submit(self.client.post, '/settings/check', data={'provider_regions': 'JP,CN,US', 'interval_hours': '9'}, headers={'HX-Request': 'true'})]
            for future in futures:
                self.assertEqual(future.result().status_code, 200)
        saved = load_settings()
        self.assertEqual(saved.pt.interval_hours, 7)
        self.assertEqual(saved.check.interval_hours, 9)
        self.assertEqual(saved.check.provider_regions, ['JP', 'CN', 'US'])

    def test_rejected_torrents_collapsed_with_original_download_indexes(self):
        from bs4 import BeautifulSoup
        with db.session_scope() as session:
            movie = Movie(title='UI grouping fixture', digital_date='2020-01-01', last_pt_candidates=[
                {'title': 'rejected first', 'ok': False, 'reason': 'wrong source', 'seeders': 10, 'size_bytes': 1},
                {'title': 'accepted second', 'ok': True, 'seeders': 9, 'size_bytes': 1},
                {'title': 'rejected third', 'ok': False, 'reason': 'too large', 'seeders': 8, 'size_bytes': 1},
            ])
            session.add(movie)
            session.flush()
            movie_id = movie.id
        response = self.client.get(f'/movies/{movie_id}')
        self.assertEqual(response.status_code, 200)
        page = BeautifulSoup(response.text, 'html.parser')
        collapsed = page.select_one('details.torrent-rejected')
        self.assertNotIn('open', collapsed.attrs)
        self.assertIn('rejected first', collapsed.get_text())
        self.assertNotIn('accepted second', collapsed.get_text())
        self.assertEqual([button['hx-post'] for button in collapsed.select('button[hx-post]')], [f'/movies/{movie_id}/download/0', f'/movies/{movie_id}/download/2'])
        self.assertIn(f'/movies/{movie_id}/download/1', response.text)
        self.assertLess(response.text.index('detail-progress'), response.text.index('detail-hero'))

    def test_rule_save_reranks_cache_without_visiting_sites(self):
        from reelready.torrent_rules import TorrentInfo
        from dataclasses import asdict
        with db.session_scope() as session:
            movie = Movie(title='Movie', year=2026, digital_date='2020-01-01', last_pt_candidates=[dict(asdict(TorrentInfo(1, 'fixture', '123', 'Movie 2026 1080p WEB-DL', seeders=10)), ok=True)])
            session.add(movie)
            session.flush()
            movie_id = movie.id
        with patch('reelready.services.pt.open_sites') as sites, patch('reelready.services.pt.download') as download:
            self.client.post('/settings/rules', data={'resolutions': '2160p', 'max_size_gb': '30', 'min_seeders': '1'}, headers={'HX-Request': 'true'})
        with db.session_scope() as session:
            movie = session.get(Movie, movie_id)
            self.assertFalse(movie.last_pt_candidates[0]['ok'])
            self.assertIn('清晰度', movie.last_pt_candidates[0]['reason'])
        sites.assert_not_called()
        download.assert_not_called()

    def test_manual_download_selects_identity_after_cached_reordering(self):
        from reelready.services.pt import download_candidate
        from reelready.torrent_rules import TorrentInfo
        from dataclasses import asdict
        from unittest.mock import Mock
        with db.session_scope() as session:
            movie = Movie(title='Movie', last_pt_candidates=[asdict(TorrentInfo(1, 'fixture', 'B', 'second', seeders=100)), asdict(TorrentInfo(1, 'fixture', 'A', 'first', seeders=10))])
            session.add(movie)
            session.flush()
            movie_id = movie.id
        with patch('reelready.services.pt.open_sites', return_value=[Mock()]), patch('reelready.services.pt.download', return_value=True) as download:
            download_candidate(load_settings(), movie_id, 0, site_id=1, torrent_id='A')
        self.assertEqual(download.call_args.args[3].torrent_id, 'A')

    def test_old_browser_download_without_identity_does_not_submit(self):
        with patch('reelready.web.routes.download_candidate') as download:
            response = self.client.post('/movies/1/download/0', headers={'HX-Request': 'true'})
        self.assertIn('刷新', json.loads(response.headers['HX-Trigger'])['toast']['message'])
        download.assert_not_called()

    def test_notifications_commit_each_send_before_later_unexpected_failure(self):
        from reelready.models import Event
        from reelready.services.events import notify_urgent
        self.settings.email.enabled = True
        with db.session_scope() as session:
            session.add_all([Event(kind='downloaded', title='First 已开始下载', urgent=True), Event(kind='downloaded', title='Second', urgent=True)])
        with patch('reelready.services.events.send_mail', side_effect=[None, RuntimeError('fixture')]) as send:
            with self.assertRaises(RuntimeError):
                notify_urgent(self.settings)
        self.assertIn('事件发生时间', send.call_args_list[0].args[2])
        self.assertIn('下载任务已提交', send.call_args_list[0].args[1])
        with patch('reelready.services.events.send_mail') as retry:
            notify_urgent(self.settings)
        self.assertEqual(retry.call_count, 1)
        self.assertIn('Second', retry.call_args.args[1])

    def test_concurrent_notifications_send_event_once(self):
        from reelready.models import Event
        from reelready.services.events import notify_urgent
        self.settings.email.enabled = True
        with db.session_scope() as session:
            session.add(Event(kind='downloaded', title='fixture', urgent=True))
        with patch('reelready.services.events.send_mail', side_effect=lambda *args: time.sleep(0.05)) as send:
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _: notify_urgent(self.settings), range(2)))
        self.assertEqual(send.call_count, 1)

    def test_completed_movie_is_not_submitted_again(self):
        from reelready.models import MovieStatus
        from reelready.services.pt import download
        from reelready.torrent_rules import TorrentInfo
        from unittest.mock import Mock
        with db.session_scope() as session:
            movie = Movie(title='fixture', status=MovieStatus.COMPLETED)
            session.add(movie)
            session.flush()
            movie_id = movie.id
        site = Mock()
        with patch('reelready.services.pt.QBittorrent') as qb:
            self.assertFalse(download(self.settings, movie_id, site, TorrentInfo(1, 'fixture', '1', 'fixture')))
        site.download.assert_not_called()
        qb.assert_not_called()

    def test_event_retention_blank_and_positive_days(self):
        response = self.client.post('/settings/events', data={'retention_days': '30'}, headers={'HX-Request': 'true'})
        self.assertEqual(load_settings().events.retention_days, 30)
        self.client.post('/settings/events', data={'retention_days': ''}, headers={'HX-Request': 'true'})
        self.assertIsNone(load_settings().events.retention_days)
        for value in ('0', '-1', '1.5'):
            response = self.client.post('/settings/events', data={'retention_days': value}, headers={'HX-Request': 'true'})
            self.assertIn('保存失败', json.loads(response.headers['HX-Trigger'])['toast']['message'])
            self.assertIsNone(load_settings().events.retention_days)

    def test_event_cleanup_respects_retention_and_keeps_movies(self):
        from datetime import timedelta
        from reelready.models import Event, now
        from reelready.services.events import cleanup_events
        with db.session_scope() as session:
            movie = Movie(title='fixture')
            session.add(movie)
            session.flush()
            movie_id = movie.id
            session.add_all([Event(kind='downloaded', title='old', movie_id=movie_id, created_at=now()-timedelta(days=31)), Event(kind='downloaded', title='recent', movie_id=movie_id)])
        cleanup_events(self.settings)
        with db.session_scope() as session:
            self.assertEqual(len(session.scalars(select(Event)).all()), 2)
        self.settings.events.retention_days = 30
        cleanup_events(self.settings)
        with db.session_scope() as session:
            self.assertEqual([e.title for e in session.scalars(select(Event))], ['recent'])
            self.assertIsNotNone(session.get(Movie, movie_id))

    def test_manual_event_delete_removes_only_selected_record(self):
        from reelready.models import Event
        with db.session_scope() as session:
            event = Event(kind='downloaded', title='fixture', urgent=True)
            session.add(event)
            session.flush()
            event_id = event.id
        self.assertIn('删除动态', self.client.get('/events').text)
        response = self.client.post(f'/events/{event_id}/delete', headers={'HX-Request': 'true'})
        self.assertEqual(response.status_code, 200)
        with db.session_scope() as session:
            self.assertIsNone(session.get(Event, event_id))

    def test_connected_site_poll_does_not_load_cookiecloud(self):
        self.add()
        with patch('reelready.web.routes.load_cookiecloud') as cookies:
            response = self.client.get('/sites/connected')
        cookies.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="connected-sites"', response.text)
        self.assertIn('hx-get="/sites/connected"', response.text)
        self.assertNotIn('<html', response.text)

    def test_site_page_decrypts_snapshot_only_once(self):
        with patch('reelready.web.routes.load_cookiecloud', return_value=({}, None)) as direct, patch('reelready.services.sites.load_cookiecloud') as repeated:
            response = self.client.get('/sites')
        self.assertEqual(response.status_code, 200)
        direct.assert_called_once()
        repeated.assert_not_called()

    def test_movie_cards_do_not_load_large_detail_columns(self):
        from sqlalchemy import event
        with db.session_scope() as session:
            session.add(Movie(title='fixture', details={'cast': ['x'*1000]*50}, overview='story', last_pt_candidates=[{'title': 'x'*1000}]*30))
        statements = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(self.engine, 'before_cursor_execute', capture)
        try:
            response = self.client.get('/movies?tab=candidate')
        finally:
            event.remove(self.engine, 'before_cursor_execute', capture)
        self.assertEqual(response.status_code, 200)
        self.assertIn('fixture', response.text)
        movie_queries = [statement for statement in statements if 'FROM movies' in statement]
        self.assertTrue(movie_queries)
        self.assertFalse(any('movies.details' in q or 'movies.last_pt_candidates' in q or 'movies.overview' in q for q in movie_queries))

    def test_duplicate_migration_backs_up_and_preserves_movie_references(self):
        site, _ = self.add()
        with db.session_scope() as session:
            duplicate = Site(kind="nexusphp", name="duplicate", base_url="https://www.tjupt.org", cookie="synthetic=2")
            session.add(duplicate)
            session.flush()
            session.add(Movie(title="test", last_pt_candidates=[{"site_id": duplicate.id, "title": "torrent"}], downloaded={"site_id": duplicate.id}))
        db.init_db()
        with db.session_scope() as session:
            self.assertEqual(len(list(session.scalars(select(Site)))), 1)
            movie = session.scalar(select(Movie))
            self.assertEqual(movie.last_pt_candidates[0]["site_id"], site.id)
            self.assertEqual(movie.downloaded["site_id"], site.id)
        self.assertEqual(len(list(config.DATA_DIR.glob("before-site-dedup-*.db"))), 1)


if __name__ == "__main__":
    unittest.main()
