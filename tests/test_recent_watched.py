import tempfile
import unittest
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from reelready import db
from reelready.models import Event, Movie, MovieStatus, Setting, Stage
from reelready.services.collector import _collect_recent, run_collect
from reelready.services.checker import monitored_ids, _check_one
from reelready.services.movies import add_manual
from reelready.services.pt import download, download_candidate
from reelready.settings import AppSettings
from reelready.sites import SiteError
from reelready.sources.douban import DoubanClient, DoubanError, DoubanMovie
from reelready.services.collection_history import DiscoveryHistory, STATE_KEY
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

    def test_cursors_survive_runs_and_find_movie_beyond_first_150(self):
        client = DoubanClient.__new__(DoubanClient)
        def page(path, **params):
            return {'items': [dict(id=f"{params['tags']}-{i}", title='列表影片', year=params['tags'],
                                   rating={'value': 8.5 if i == 175 else 6, 'count': 10000})
                              for i in range(params['start'], params['start'] + 50)]}
        client._get = Mock(side_effect=page)
        client.detail = Mock(side_effect=lambda identity: DoubanMovie(
            **dict(self.item.__dict__, id=identity)))
        self.assertEqual(_collect_recent(self.settings, client, None), 0)
        self.assertEqual(client._get.call_count, 6)
        # Each run reloads its cursor from the DB rather than re-reading page one.
        self.assertEqual(_collect_recent(self.settings, client, None), 2)
        self.assertEqual(client._get.call_args_list[6].kwargs['start'], 150)
        with db.session_scope() as s:
            state = s.get(Setting, STATE_KEY).value
            self.assertEqual(len(list(s.scalars(select(Movie)))), 2)
            self.assertTrue(all(entry['offset'] == 300 for entry in state['progress']['years'].values()))

    def test_failed_detail_is_saved_and_retried_when_due_without_feed_entry(self):
        self.douban.detail.side_effect = DoubanError('synthetic timeout')
        self.assertEqual(_collect_recent(self.settings, self.douban, None), 0)
        with db.session_scope() as s:
            state = s.get(Setting, STATE_KEY).value
            self.assertEqual(state['retry'][self.item.id]['attempts'], 1)
            self.assertEqual(state['audit'][self.item.id]['outcome'], 'retry')
        # A repeated feed entry does not bypass the retry delay.
        self.assertEqual(_collect_recent(self.settings, self.douban, None), 0)
        self.assertEqual(self.douban.detail.call_count, 1)
        with db.session_scope() as s:
            row = s.get(Setting, STATE_KEY)
            state = deepcopy(row.value)
            state['retry'][self.item.id]['retry_at'] = '2000-01-01T00:00:00'
            row.value = state
        self.douban.recent_high_rated.return_value = []
        self.douban.detail.side_effect = None
        self.assertEqual(_collect_recent(self.settings, self.douban, None), 1)
        with db.session_scope() as s:
            state = s.get(Setting, STATE_KEY).value
            self.assertFalse(state['retry'])
            self.assertEqual(state['audit'][self.item.id]['outcome'], 'added')
            self.assertEqual(state['audit'][self.item.id]['previous'][-1]['outcome'], 'retry')
            self.assertIn('synthetic timeout', state['audit'][self.item.id]['previous'][-1]['reason'])
        self.assertEqual(self.douban.detail.call_count, 2)

    def test_partial_list_failure_keeps_add_count_and_resume_position(self):
        client = DoubanClient.__new__(DoubanClient)
        def page(path, **params):
            if params['start']:
                raise DoubanError('synthetic list timeout')
            return {'items': [dict(id=str(i), title='列表影片', year=params['tags'],
                                   rating={'value': 8.5 if i == 1 else 6, 'count': 10000}) for i in range(50)]}
        client._get = Mock(side_effect=page)
        client.detail = Mock(return_value=DoubanMovie(**dict(self.item.__dict__, id='1')))
        notes = []
        self.assertEqual(_collect_recent(self.settings, client, None, notes), 1)
        with db.session_scope() as s:
            state = s.get(Setting, STATE_KEY).value
            self.assertEqual(state['progress']['years'][str(date.today().year)]['offset'], 50)
            self.assertEqual(state['progress']['next_year'], date.today().year)
            self.assertTrue(state['last_error'])
        self.assertTrue(any('部分失败' in note for note in notes))
        _collect_recent(self.settings, client, None, [])
        self.assertEqual(client._get.call_args.kwargs['start'], 50)
        self.assertEqual(client.detail.call_count, 1)

    def test_decisions_distinguish_threshold_date_and_existing(self):
        low = DoubanMovie('low', '低分影片', rating=6, votes=10000)
        old = DoubanMovie(**dict(self.item.__dict__, id='old', release_date='2000-01-01'))
        self.douban.recent_high_rated.return_value = [low, old, self.item]
        self.douban.detail.side_effect = lambda identity: old if identity == 'old' else self.item
        _collect_recent(self.settings, self.douban, None)
        _collect_recent(self.settings, self.douban, None)
        with db.session_scope() as s:
            audit = s.get(Setting, STATE_KEY).value['audit']
            self.assertIn('门槛', audit['low']['reason'])
            self.assertIn('上映日期', audit['old']['reason'])
            self.assertEqual(audit[self.item.id]['outcome'], 'existing')

    def test_missing_feed_statistics_are_verified_from_details(self):
        self.douban.recent_high_rated.return_value = [DoubanMovie(self.item.id, self.item.title)]
        self.assertEqual(_collect_recent(self.settings, self.douban, None), 1)
        self.douban.detail.assert_called_once_with(self.item.id)

    def test_history_is_bounded_and_changed_threshold_restarts_scan(self):
        history = DiscoveryHistory(self.settings)
        with patch('reelready.services.collection_history.AUDIT_LIMIT', 2):
            for i in range(3):
                history.record(DoubanMovie(str(i), '影片'), 'filtered', '测试门槛')
        self.assertEqual(list(history.audit), ['1', '2'])
        history.progress['years'] = {str(date.today().year): {'offset': 150}}
        history.save()
        self.settings.collect.douban_min_rating = 8
        self.assertEqual(DiscoveryHistory(self.settings).progress, {})

    def test_retries_have_backoff_and_bounded_batch_and_keep_decision_history(self):
        history = DiscoveryHistory(self.settings)
        for _ in range(8):
            history.record(self.item, 'retry', '测试失败')
        entry = history.retry[self.item.id]
        self.assertEqual(entry['attempts'], 8)
        from datetime import datetime
        self.assertGreater(datetime.fromisoformat(entry['retry_at']), datetime.now() + timedelta(hours=23))
        self.assertEqual(len(history.audit[self.item.id]['previous']), 3)
        self.assertIn('min_rating', history.audit[self.item.id]['criteria'])
        for i in range(25):
            item = DoubanMovie(str(i), '重试影片')
            history.record(item, 'retry', '失败')
            history.retry[item.id]['retry_at'] = '2000-01-01T00:00:00'
        self.assertEqual(len(history.due_retries()), 20)

    def test_recent_source_label_does_not_claim_current_cinema(self):
        _collect_recent(self.settings, self.douban, None)
        with db.session_scope() as s:
            identity = s.scalar(select(Movie.id))
        client = TestClient(create_app())
        with patch('reelready.web.routes.queue_metadata'):
            self.assertIn('来源：豆瓣近期高分', client.get(f'/movies/{identity}').text)
        client.close()

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
    def test_page_cursor_commits_only_after_page_is_consumed(self):
        client = DoubanClient.__new__(DoubanClient)
        client._get = Mock(return_value={'items': [dict(id=str(i), title='电影') for i in range(50)]})
        progress = {}
        iterator = client.recent_high_rated(2026, 2026, 7, progress=progress)
        next(iterator)
        self.assertEqual(progress['years']['2026']['offset'], 0)
        iterator.close()
        self.assertEqual(progress['years']['2026']['offset'], 0)

    def test_repeated_page_across_runs_resets_cursor(self):
        client = DoubanClient.__new__(DoubanClient)
        client._get = Mock(return_value={'items': [dict(id=str(i), title='电影') for i in range(50)]})
        progress = {'years': {'2026': {'offset': 150, 'last_ids': [str(i) for i in range(50)]}}}
        self.assertEqual(list(client.recent_high_rated(2026, 2026, 7, progress=progress)), [])
        self.assertEqual(progress['years']['2026']['offset'], 0)
        self.assertEqual(client._get.call_count, 1)

    def test_many_years_share_request_budget_and_rotate(self):
        client = DoubanClient.__new__(DoubanClient)
        client._get = Mock(side_effect=lambda path, **p: {'items': [dict(id=f"{p['tags']}-{p['start']}-{i}", title='电影') for i in range(50)]})
        progress = {}
        list(client.recent_high_rated(2000, 2026, 7, progress=progress))
        self.assertEqual(client._get.call_count, 6)
        list(client.recent_high_rated(2000, 2026, 7, progress=progress))
        self.assertEqual(client._get.call_args_list[6].kwargs['tags'], '2024')

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
