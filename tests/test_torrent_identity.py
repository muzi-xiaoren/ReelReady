import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reelready import db
from reelready.models import Movie, MovieStatus, Stage
from reelready.services.checker import _advance
from reelready.services.pt import (rank_for_movie, verify_candidates, release_wait_reason,
                                    revalidate_cached_candidates, download, run_pt_scan, _Target)
from reelready.settings import AppSettings
from reelready.sites.base import MovieQuery, SiteConfig, SiteError, detail_imdb
from reelready.sites.nexusphp import NexusPHPSite
from reelready.sites.unit3d import Unit3DSite
from reelready.torrent_rules import TorrentInfo
from reelready.web.app import create_app


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.settings = AppSettings()
        self.settings.pt.request_delay_seconds = 0
        self.query = MovieQuery('tt33764258', '奥德赛', 'The Odyssey', 2026)

    def hit(self, torrent_id='199315', imdb=None, source='metadata', seeders=184):
        return TorrentInfo(2,'CarPT',torrent_id,'The Odyssey 2026 1080p AMZN WEB-DL H.264',
                           seeders=seeders,imdb_id=imdb,imdb_source=source)

    def test_same_name_same_year_with_different_imdb_is_rejected(self):
        torrent=self.hit(imdb='tt41605854',source='detail')
        best,rows=rank_for_movie([torrent],self.settings.rules,self.query,'等待上线确认')
        self.assertIsNone(best)
        self.assertIn('tt41605854',rows[0].reason)
        self.assertIn('tt33764258',rows[0].reason)
        self.assertIn('同名影片',rows[0].reason)

    def test_missing_or_search_inferred_imdb_cannot_authorize_download(self):
        for torrent in (self.hit(),self.hit(imdb=self.query.imdb_id,source='search')):
            best,rows=rank_for_movie([torrent],self.settings.rules,self.query,'')
            self.assertIsNone(best)
            self.assertIn('影片身份未核实',rows[0].reason)
        best,_=rank_for_movie([self.hit(imdb=self.query.imdb_id,source='detail')],self.settings.rules,self.query,'')
        self.assertIsNotNone(best)

    def test_details_verification_falls_back_to_the_correct_identity(self):
        site=Mock(config=SimpleNamespace(id=2))
        def verify(torrent):
            torrent.imdb_id='tt41605854' if torrent.torrent_id=='wrong' else self.query.imdb_id
            torrent.imdb_source='detail'
        site.verify_identity.side_effect=verify
        wrong=self.hit('wrong',seeders=300)
        correct=self.hit('correct',seeders=200)
        verify_candidates([wrong,correct],[site],self.settings,self.query)
        best,rows=rank_for_movie([wrong,correct],self.settings.rules,self.query,'')
        self.assertEqual(best.torrent.torrent_id,'correct')
        self.assertFalse(rows[-1].ok)
        self.assertEqual(site.verify_identity.call_count,2)

    def test_unavailable_details_are_not_silently_accepted_and_work_is_bounded(self):
        site=Mock(config=SimpleNamespace(id=2))
        site.verify_identity.side_effect=SiteError('fixture failed')
        hits=[self.hit(str(i)) for i in range(20)]
        verify_candidates(hits,[site],self.settings,self.query)
        self.assertEqual(site.verify_identity.call_count,10)
        self.assertIsNone(rank_for_movie(hits,self.settings.rules,self.query,'')[0])
        self.assertIn('暂时无法访问',hits[0].identity_error)

    def test_buy_only_does_not_override_a_future_date_or_no_date(self):
        for digital in (None,(date.today()+timedelta(days=30)).isoformat()):
            movie=Movie(title='奥德赛',digital_date=digital,streaming=['US · Fandango At Home(买)'])
            self.assertIn('预售',release_wait_reason(movie))
        movie.digital_date=date.today().isoformat()
        self.assertEqual(release_wait_reason(movie),'')
        movie.digital_date=(date.today()+timedelta(days=30)).isoformat()
        movie.streaming=['US · Service(订阅)']
        self.assertEqual(release_wait_reason(movie),'')

    def test_preorder_cannot_emit_streaming_transition(self):
        movie=Movie(title='奥德赛',stage=Stage.DATED,digital_date=(date.today()+timedelta(days=30)).isoformat(),streaming=['US · Fandango At Home(买)'])
        with patch('reelready.services.checker.add_event') as event:
            self.assertFalse(_advance(Mock(),movie))
            event.assert_not_called()
            self.assertEqual(movie.stage,Stage.DATED)
            movie.stage=Stage.STREAMING
            self.assertFalse(_advance(Mock(),movie))
            self.assertEqual(movie.stage,Stage.DATED)

    def test_detail_parser_ignores_sidebar_but_rejects_ambiguous_description(self):
        html='<div id="descr">IMDb链接 https://www.imdb.com/title/tt41605854/</div><aside>tt33764258</aside>'
        self.assertEqual(detail_imdb(html),'tt41605854')
        self.assertIsNone(detail_imdb('<div id="descr">tt41605854 tt33764258</div>'))
        self.assertIsNone(detail_imdb('<script>tt33764258</script> No identity'))

    def test_site_details_replace_inferred_identity_without_fetching_a_torrent(self):
        for cls,kind in ((NexusPHPSite,'nexusphp'),(Unit3DSite,'unit3d')):
            site=cls(SiteConfig(2,kind,'fixture','https://fixture.example',cookie='synthetic=1'))
            try:
                torrent=self.hit(imdb=self.query.imdb_id,source='search')
                with patch.object(site,'_get_page',return_value='<div id="descr">https://www.imdb.com/title/tt41605854/</div>') as page, patch.object(site,'download') as download_file:
                    site.verify_identity(torrent)
                    self.assertEqual(torrent.imdb_id,'tt41605854')
                    self.assertEqual(torrent.imdb_source,'detail')
                    self.assertEqual(page.call_count,1)
                    download_file.assert_not_called()
            finally:
                site.close()


class CachedIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.engine=create_engine(f"sqlite:///{Path(self.temp.name)/'test.db'}")
        db.Base.metadata.create_all(self.engine)
        self.session_patch=patch.object(db,'SessionLocal',sessionmaker(bind=self.engine,expire_on_commit=False))
        self.session_patch.start()
        self.settings=AppSettings()
        self.settings.pt.early_search=True
        torrent=TorrentInfo(2,'CarPT','199315','The Odyssey 2026 1080p WEB-DL',seeders=184,imdb_id='tt41605854',imdb_source='detail')
        with db.session_scope() as s:
            row=Movie(title='奥德赛',original_title='The Odyssey',year=2026,imdb_id='tt33764258',status=MovieStatus.COMPLETED,stage=Stage.DOWNLOADED,
                      metadata_checked_at=date.today(),last_pt_candidates=[dict(torrent.__dict__,ok=True,reason='',tags=[])],downloaded={'title':'historical submission'})
            s.add(row)
            s.flush()
            self.id=row.id

    def tearDown(self):
        self.session_patch.stop()
        self.engine.dispose()
        self.temp.cleanup()

    def test_completed_cache_is_rejected_and_appears_under_collapsed_results(self):
        self.assertEqual(revalidate_cached_candidates(self.settings,[self.id]),1)
        with db.session_scope() as s:
            row=s.get(Movie,self.id)
            self.assertEqual(row.status,MovieStatus.COMPLETED)
            self.assertEqual(row.downloaded,{'title':'historical submission'})
            self.assertFalse(row.last_pt_candidates[0]['ok'])
        client=TestClient(create_app())
        with patch('reelready.web.routes.queue_metadata'), patch('reelready.web.routes.load_settings',return_value=self.settings):
            page=client.get(f'/movies/{self.id}')
        self.assertEqual(page.status_code,200)
        self.assertIn('不符合条件的种子',page.text)
        self.assertIn('tt41605854',page.text)
        self.assertNotIn('<details class="torrent-rejected" open',page.text)
        client.close()

    def test_stale_manual_download_is_blocked_before_site_download_and_qb(self):
        with db.session_scope() as s:
            row=s.get(Movie,self.id)
            row.status=MovieStatus.MONITORING
            row.streaming=['可看平台']
            torrent=TorrentInfo(**{k:v for k,v in row.last_pt_candidates[0].items() if k in TorrentInfo.__dataclass_fields__})
        site=Mock(config=SimpleNamespace(id=2))
        with patch('reelready.services.pt._download') as external:
            self.assertFalse(download(self.settings,self.id,site,torrent))
        external.assert_not_called()
        site.download.assert_not_called()
        with db.session_scope() as s:
            self.assertFalse(s.get(Movie,self.id).last_pt_candidates[0]['ok'])
