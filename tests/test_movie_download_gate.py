import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from reelready.models import Movie
from reelready.services.pt import release_wait_reason, rank_for_movie
from reelready.services.pt import run_pt_scan, _Target
from reelready.settings import AppSettings
from reelready.settings import TorrentRules
from reelready.sites import MovieQuery
from reelready.torrent_rules import TorrentInfo, matches_movie, parse


class MovieDownloadGateTests(unittest.TestCase):
    def setUp(self):
        self.query = MovieQuery('tt1234567', '奥德赛', 'The Odyssey', 2026)
        self.rules = TorrentRules()

    def hit(self, title, subtitle='', imdb_id='tt1234567'):
        return TorrentInfo(1, 'fixture', '1', title, subtitle=subtitle, seeders=10, imdb_id=imdb_id)

    def test_parent_imdb_does_not_accept_making_of(self):
        hit = self.hit('The Odyssey: The Making of an Epic 2026 1080p AMZN WEB-DL H.264', '奥德赛：史诗的诞生')
        self.assertFalse(matches_movie(hit, imdb_id=self.query.imdb_id, titles=self.query.titles, year=self.query.year))
        best, ranked = rank_for_movie([hit], self.rules, self.query, '')
        self.assertIsNone(best)
        self.assertIn('影片不匹配', ranked[0].reason)

    def test_subtitle_cannot_make_concert_match(self):
        hit = self.hit('Galaxymphony III The Final Odyssey 2025 1080p BluRay x264', '银河交响曲：终极奥德赛')
        self.assertFalse(matches_movie(hit, imdb_id=self.query.imdb_id, titles=self.query.titles, year=self.query.year))

    def test_digital_date_in_future_blocks_apparently_good_web_release(self):
        movie = Movie(title='奥德赛', digital_date=(date.today() + timedelta(days=30)).isoformat(), streaming=[])
        wait = release_wait_reason(movie)
        hit = self.hit('The Odyssey 2026 1080p AMZN WEB-DL H.264')
        best, ranked = rank_for_movie([hit], self.rules, self.query, wait)
        self.assertIsNone(best)
        self.assertIn('等待上线确认', ranked[0].reason)

    def test_platform_or_reached_date_provides_release_evidence(self):
        hit = self.hit('The Odyssey 2026 1080p WEB-DL H.264')
        for movie in [Movie(title='a', streaming=['平台'], digital_date=None), Movie(title='a', streaming=[], digital_date=date.today().isoformat())]:
            self.assertEqual(release_wait_reason(movie), '')
            self.assertIsNotNone(rank_for_movie([hit], self.rules, self.query, '')[0])

    def test_unknown_source_and_subtitle_cam_are_rejected(self):
        unknown = self.hit('The Odyssey 2026 1080p x264')
        self.assertIn('片源类型不明', rank_for_movie([unknown], self.rules, self.query, '')[1][0].reason)
        cam = self.hit('The Odyssey 2026 1080p WEB-DL H.264', '屏摄 枪版')
        self.assertTrue(parse(cam).bad_source)
        self.assertIsNone(rank_for_movie([cam], self.rules, self.query, '')[0])

    def test_web_rip_is_not_misclassified_as_web_dl(self):
        self.assertEqual(parse(self.hit('The Odyssey 2026 1080p WEBRip x264')).source, 'WEBRip')

    def test_no_date_is_unconfirmed_and_target_documentary_can_match(self):
        self.assertIn('等待上线确认', release_wait_reason(Movie(title='a', streaming=[], digital_date=None)))
        hit = self.hit('The Odyssey The Making of an Epic 2026 1080p WEB-DL')
        self.assertTrue(matches_movie(hit, imdb_id='tt1234567', titles=['The Odyssey The Making of an Epic'], year=2026))

    def test_scan_never_calls_downloader_without_release_evidence(self):
        site = Mock()
        site.config = SimpleNamespace(id=1)
        site.search.return_value = [self.hit('The Odyssey 2026 1080p WEB-DL H.264')]
        target = _Target(19, '奥德赛', self.query, '等待上线确认')
        with patch('reelready.services.pt._targets', return_value=[target]), patch('reelready.services.pt.open_sites', return_value=[site]), patch('reelready.services.pt._mark_site_ok'), patch('reelready.services.pt._save_scan') as save, patch('reelready.services.pt.download') as download:
            settings = AppSettings()
            settings.pt.early_search = True
            summary = run_pt_scan(settings)
        download.assert_not_called()
        self.assertIn('开始下载 0 部', summary)
        self.assertFalse(save.call_args.args[1][0].ok)
        site.close.assert_called_once()

    def test_early_search_off_never_opens_sites_for_unreleased_movie(self):
        target = _Target(19, '奥德赛', self.query, '等待上线确认')
        with patch('reelready.services.pt._targets', return_value=[target]), patch('reelready.services.pt.open_sites') as sites:
            summary = run_pt_scan(AppSettings())
        sites.assert_not_called()
        self.assertIn('提前搜索已关闭', summary)

    def test_early_search_off_still_searches_released_movie(self):
        target = _Target(19, '奥德赛', self.query, '')
        site = Mock()
        site.config = SimpleNamespace(id=1)
        site.search.return_value = []
        with patch('reelready.services.pt._targets', return_value=[target]), patch('reelready.services.pt.open_sites', return_value=[site]), patch('reelready.services.pt._mark_site_ok'), patch('reelready.services.pt._save_scan'):
            run_pt_scan(AppSettings())
        site.search.assert_called_once()

    def test_combined_titles_and_unbracketed_group_prefix(self):
        for title in ('奥德赛.The.Odyssey.2026.2160p.WEB-DL', 'The.Odyssey.奥德赛.2026.2160p.WEB-DL', 'FRDS.The.Odyssey.2026.2160p.WEB-DL'):
            for imdb in ('tt1234567', None):
                self.assertTrue(matches_movie(self.hit(title, imdb_id=imdb), imdb_id=self.query.imdb_id, titles=self.query.titles, year=2026), title)

    def test_numeric_film_titles_are_not_release_years(self):
        for title, names, year in [('Blade.Runner.2049.2017.2160p.BluRay', ['银翼杀手2049', 'Blade Runner 2049'], 2017), ('1917.2019.1080p.BluRay', ['1917'], 2019)]:
            self.assertTrue(matches_movie(self.hit(title, imdb_id=None), imdb_id=None, titles=names, year=year))

    def test_year_tolerance_requires_title_and_imdb_provenance(self):
        for difference in (-1, 1):
            self.assertTrue(matches_movie(self.hit(f'The Odyssey {2026+difference} 1080p WEB-DL', imdb_id=None), imdb_id=None, titles=self.query.titles, year=2026))
        verified = self.hit('The Odyssey 2020 1080p WEB-DL')
        self.assertTrue(matches_movie(verified, imdb_id=self.query.imdb_id, titles=self.query.titles, year=2026))
        verified.imdb_source = 'search'
        self.assertFalse(matches_movie(verified, imdb_id=self.query.imdb_id, titles=self.query.titles, year=2026))
        self.assertFalse(matches_movie(self.hit('Another Movie 2026 1080p WEB-DL'), imdb_id=self.query.imdb_id, titles=self.query.titles, year=2026))

    def test_subtitle_year_accepts_matching_chinese_or_english_main_title(self):
        for title in ('奥德赛 2160p WEB-DL', 'The Odyssey 2160p WEB-DL'):
            for subtitle in ('2026', '上映年份：2026年 · 中字', '[2026-07-17]'):
                with self.subTest(title=title, subtitle=subtitle):
                    hit = self.hit(title, subtitle, imdb_id=None)
                    best, _ = rank_for_movie([hit], self.rules, self.query, '')
                    self.assertIsNotNone(best)

    def test_subtitle_year_keeps_existing_one_year_tolerance(self):
        for year in (2025, 2027):
            hit = self.hit('奥德赛 2160p WEB-DL', f'年份：{year}', imdb_id=None)
            self.assertTrue(matches_movie(hit, imdb_id=self.query.imdb_id, titles=self.query.titles, year=2026))

    def test_inferred_imdb_still_requires_year_evidence(self):
        hit = self.hit('奥德赛 2160p WEB-DL', '2026')
        hit.imdb_source = 'search'
        self.assertTrue(matches_movie(hit, imdb_id=self.query.imdb_id, titles=self.query.titles, year=2026))
        hit.subtitle = ''
        self.assertFalse(matches_movie(hit, imdb_id=self.query.imdb_id, titles=self.query.titles, year=2026))
        hit.imdb_source = 'metadata'
        self.assertTrue(matches_movie(hit, imdb_id=self.query.imdb_id, titles=self.query.titles, year=2026))

    def test_subtitle_year_cannot_override_title_year_or_wrong_main_title(self):
        for title, subtitle in (
            ('奥德赛 2020 2160p WEB-DL', '2026'),
            ('奥德赛2020 2160p WEB-DL', '2026'),
            ('另一部电影 2160p WEB-DL', '奥德赛 2026'),
            ('奥德赛2 2160p WEB-DL', '奥德赛 2026'),
            ('新奥德赛 2160p WEB-DL', '2026'),
            ('奥德赛外传 2160p WEB-DL', '2026'),
            ('Another Movie 2160p WEB-DL', 'The Odyssey 2026'),
            ('2160p WEB-DL', '奥德赛 2026'),
        ):
            with self.subTest(title=title):
                hit = self.hit(title, subtitle, imdb_id=None)
                self.assertFalse(matches_movie(hit, imdb_id=self.query.imdb_id, titles=self.query.titles, year=2026))
        hit = self.hit('奥德赛2026 2160p WEB-DL', '', imdb_id=None)
        self.assertTrue(matches_movie(hit, imdb_id=self.query.imdb_id, titles=self.query.titles, year=2026))

    def test_missing_wrong_ambiguous_or_identifier_year_is_rejected(self):
        for subtitle in ('', '中字', '2020', '20260', 'x2026', '2026MB', '2020 / 2026'):
            with self.subTest(subtitle=subtitle):
                hit = self.hit('奥德赛 2160p WEB-DL', subtitle, imdb_id=None)
                self.assertFalse(matches_movie(hit, imdb_id=self.query.imdb_id, titles=self.query.titles, year=2026))

    def test_subtitle_numeric_movie_title_is_not_a_release_year(self):
        for subtitle, expected in (('银翼杀手2049 / Blade Runner 2049', False), ('银翼杀手2049 / Blade Runner 2049 · 2017', True)):
            hit = self.hit('Blade Runner 2049 2160p BluRay', subtitle, imdb_id=None)
            self.assertEqual(matches_movie(hit, imdb_id=None, titles=['银翼杀手2049', 'Blade Runner 2049'], year=2017), expected)
        hit = self.hit('1917 1080p BluRay', '1917', imdb_id=None)
        self.assertFalse(matches_movie(hit, imdb_id=None, titles=['1917'], year=2019))
        hit.subtitle = '1917 · 上映年份2019年'
        self.assertTrue(matches_movie(hit, imdb_id=None, titles=['1917'], year=2019))

    def test_subtitle_year_does_not_bypass_extras_cam_or_release_gate(self):
        for subtitle in ('2026 制作特辑', '2026 屏摄 枪版'):
            hit = self.hit('奥德赛 2160p WEB-DL', subtitle, imdb_id=None)
            self.assertIsNone(rank_for_movie([hit], self.rules, self.query, '')[0])
        hit = self.hit('奥德赛 2160p WEB-DL', '2026', imdb_id=None)
        best, ranked = rank_for_movie([hit], self.rules, self.query, '等待上线确认')
        self.assertIsNone(best)
        self.assertIn('等待上线确认', ranked[0].reason)
