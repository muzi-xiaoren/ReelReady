import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from reelready import db
from reelready.models import Event, Movie, MovieStatus, Stage
from reelready.services.collector import _collect_recent, run_collect
from reelready.services.checker import monitored_ids, _check_one
from reelready.services.movies import add_manual
from reelready.services.pt import download, download_candidate
from reelready.settings import AppSettings
from reelready.sites import SiteError
from reelready.sources.douban import DoubanClient, DoubanMovie
from reelready.web.app import create_app


class RecentWatchedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.temp.name) / 'test.db'}")
        db.Base.metadata.create_all(self.engine)
        self.patches = [patch.object(db, 'SessionLocal', sessionmaker(bind=self.engine, expire_on_commit=False)),
                        patch('reelready.services.collector.cache_poster'),
                        patch('reelready.services.collector.link_ids', side_effect=lambda s,m,*args: m)]
        for p in self.patches:
            p.start()
        self.settings = AppSettings()
        self.douban = Mock()
        self.item = DoubanMovie('37116446', '给阿嬷的情书', year=date.today().year,
                                release_date=(date.today()-timedelta(days=30)).isoformat(),
                                rating=9.3, votes=1000000)
        self.douban.recent_high_rated.return_value = [self.item]
        self.douban.detail.return_value = self.item

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.engine.dispose()
        self.temp.cleanup()

    def test_off_cinema_high_score_collected_once_as_candidate(self):
        self.assertEqual(_collect_recent(self.settings,self.douban,None),1)
        self.assertEqual(_collect_recent(self.settings,self.douban,None),0)
        with db.session_scope() as s:
            rows = list(s.scalars(select(Movie)))
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0].status,MovieStatus.CANDIDATE)
            self.assertFalse(rows[0].details['collection_now_showing'])
            self.assertEqual(len(list(s.scalars(select(Event)))),1)

    def test_recent_keeps_blacklist_and_watched_and_does_not_refetch_details(self):
        for status in (MovieStatus.WATCHED,MovieStatus.IGNORED):
            with db.session_scope() as s:
                row = s.scalar(select(Movie))
                if row is None:
                    row = Movie(title=self.item.title,douban_id=self.item.id)
                    s.add(row)
                row.status = status
            self.assertEqual(_collect_recent(self.settings,self.douban,None),0)
            with db.session_scope() as s:
                self.assertEqual(s.scalar(select(Movie)).status,status)
        self.douban.detail.assert_not_called()

    def test_recent_rechecks_exact_date_votes_and_rating(self):
        self.settings.collect.include_rereleases = True
        for changes in ({'release_date':(date.today()-timedelta(days=500)).isoformat()},
                        {'release_date':(date.today()+timedelta(days=20)).isoformat()},
                        {'votes':10},{'rating':6}):
            fields = dict(self.item.__dict__)
            fields.update(changes)
            self.douban.detail.return_value = DoubanMovie(**fields)
            self.assertEqual(_collect_recent(self.settings,self.douban,None),0)
        with db.session_scope() as s:
            self.assertIsNone(s.scalar(select(Movie)))

    def test_disabled_recent_skips_its_source(self):
        self.settings.collect.recent_enabled = False
        self.settings.collect.douban_enabled = False
        self.settings.collect.tmdb_enabled = False
        with patch('reelready.services.collector.make_douban') as make, patch('reelready.services.collector.make_tmdb',return_value=None):
            run_collect(self.settings)
        make.return_value.__enter__.return_value.recent_high_rated.assert_not_called()

    def test_recent_switch_is_visible_and_saved(self):
        client=TestClient(create_app())
        self.assertEqual(client.get('/settings').text.count('name="recent_enabled"'),1)
        self.assertEqual(client.post('/settings/collect',data={'recent_enabled':'on'},headers={'HX-Request':'true'}).status_code,200)
        from reelready.settings import load_settings
        self.assertTrue(load_settings().collect.recent_enabled)
        client.post('/settings/collect',data={},headers={'HX-Request':'true'})
        self.assertFalse(load_settings().collect.recent_enabled)
        client.close()

    def test_old_database_adds_watched_column_without_changing_blacklist(self):
        with db.session_scope() as s:
            s.add(Movie(title='旧黑名单',status=MovieStatus.IGNORED))
        with self.engine.begin() as connection:
            connection.exec_driver_sql('ALTER TABLE movies DROP COLUMN watched_from_status')
        with patch.object(db,'engine',self.engine):
            db.init_db()
            db.init_db()
        with db.session_scope() as s:
            row=s.scalar(select(Movie))
            self.assertEqual(row.status,MovieStatus.IGNORED)
            self.assertIsNone(row.watched_from_status)

    def test_mark_watched_from_each_list_and_restore_preserves_download(self):
        client = TestClient(create_app())  # No scheduler or lifespan.
        with patch('reelready.web.routes.scheduler.trigger') as trigger:
            for previous in (MovieStatus.CANDIDATE,MovieStatus.MONITORING,MovieStatus.COMPLETED,MovieStatus.IGNORED):
                with db.session_scope() as s:
                    row = Movie(title='已看影片',status=previous,stage=Stage.DOWNLOADED if previous==MovieStatus.COMPLETED else Stage.IN_CINEMA,
                                downloaded={'title':'既有下载'} if previous==MovieStatus.COMPLETED else None,
                                douban_id=str(100+len(list(s.scalars(select(Movie))))))
                    s.add(row)
                    s.flush()
                    identity = row.id
                for _ in range(2):
                    self.assertEqual(client.post(f'/movies/{identity}/watched',headers={'HX-Request':'true'}).status_code,200)
                with db.session_scope() as s:
                    row = s.get(Movie,identity)
                    self.assertEqual(row.status,MovieStatus.WATCHED)
                    self.assertEqual(row.watched_from_status,previous)
                self.assertNotIn(identity,monitored_ids())
                page=client.get('/movies?tab=watched')
                self.assertIn('已看过',page.text)
                self.assertIn(f'/movies/{identity}/restore',page.text)
                self.assertEqual(client.post(f'/movies/{identity}/restore',headers={'HX-Request':'true'}).status_code,200)
                with db.session_scope() as s:
                    row=s.get(Movie,identity)
                    self.assertEqual(row.status,previous)
                    if previous==MovieStatus.COMPLETED:
                        self.assertEqual(row.downloaded,{'title':'既有下载'})
            trigger.assert_not_called()
        client.close()

    def test_watched_blocks_stale_download_and_check_and_manual_readd(self):
        with db.session_scope() as s:
            row=Movie(title='已经看过',status=MovieStatus.WATCHED,douban_id='123456',last_pt_candidates=[])
            s.add(row)
            s.flush()
            identity=row.id
            self.assertEqual(add_manual(s,'123456',self.settings).status,MovieStatus.WATCHED)
        with patch('reelready.services.pt._download') as external:
            self.assertFalse(download(self.settings,identity,Mock(),Mock()))
            external.assert_not_called()
        with self.assertRaisesRegex(SiteError,'恢复影片'):
            download_candidate(self.settings,identity,0)
        tmdb,douban=Mock(),Mock()
        self.assertFalse(_check_one(self.settings,identity,tmdb,douban))
        tmdb.movie.assert_not_called()
        douban.detail.assert_not_called()


class RecentSourceTests(unittest.TestCase):
    def test_discovery_sends_year_tags_and_bounds_pagination(self):
        client=DoubanClient.__new__(DoubanClient)
        client._get=Mock(side_effect=lambda path,**p: {'items':[{'id':f"{p['tags']}-{p['start']}-{i}",'title':'电影','year':p['tags'],'type':'movie','rating':{'value':8.1,'count':6000}} for i in range(50)]})
        items=list(client.recent_high_rated(2025,2026,7))
        self.assertEqual(len(items),300)
        self.assertEqual(client._get.call_count,6)
        self.assertEqual(client._get.call_args_list[0].kwargs['tags'],'2026')
        self.assertEqual(client._get.call_args_list[-1].kwargs['start'],100)

    def test_repeated_pages_stop_without_duplicate_ids(self):
        client=DoubanClient.__new__(DoubanClient)
        client._get=Mock(return_value={'items':[{'id':str(i),'title':'电影','year':2026} for i in range(50)]})
        self.assertEqual(len(list(client.recent_high_rated(2026,2026,7))),50)
        self.assertEqual(client._get.call_count,2)

    def test_pubdates_supply_full_release_date(self):
        client=DoubanClient.__new__(DoubanClient)
        client._get=Mock(return_value={'id':'1','title':'电影','pubdate':['2026-05-01(中国大陆)','2026-04-30(电影节)']})
        self.assertEqual(client.detail('1').release_date,'2026-04-30')
