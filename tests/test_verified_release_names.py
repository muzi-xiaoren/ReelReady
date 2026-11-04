"""Regressions from the monitored-library review, including the wrong Odyssey."""
import unittest

from reelready.services.pt import rank_for_movie
from reelready.settings import TorrentRules
from reelready.sites import MovieQuery
from reelready.torrent_rules import TorrentInfo


CASES = [
    ('tt36364878', '非穷尽列举', 'National Theatre Live: Inter Alia', 2025,
     'Inter Alia 2025 1080p NTatHome WEB-DL AAC2.0 H.264'),
    ('tt32820897', '鬼灭之刃：无限城篇 第一章 猗窝座再袭', 'Demon Slayer: Kimetsu no Yaiba Infinity Castle', 2025,
     'Gekijô-ban Kimetsu no Yaiba Mugen Jô-hen AKA Demon Slayer Kimetsu no Yaiba Infinity Castle 2025 BluRay 1080p x265 10bit DDP 5.1 MNHD-FRDS'),
    *[('tt32820897', '鬼灭之刃：无限城篇 第一章 猗窝座再袭', 'Demon Slayer: Kimetsu no Yaiba Infinity Castle', 2025, title) for title in (
        'Demon Slayer Kimetsu no Yaiba Infinity Castle I 2025 1080p NF WEB-DL x264 DDP5.1-ADWeb',
        'Demon Slayer Kimetsu No Yaiba Infinity Castle Part I 2025 1080p CR WEB-DL H.264 AAC-AilMWeb',
        'Demon Slayer Kimetsu no Yaiba Infinity Castle I 2025 1080p Viu WEB-DL H.264 AAC 2.0 2Audio-PTerWEB',
        'Demon Slayer Kimetsu no Yaiba Infinity Castle I 2025 1080p BluRay x265 3Audio DDP-FatFitness',
    )],
    *[('tt36592985', '小林家的龙丫头：怕寂寞的龙', "Miss Kobayashi's Dragon Maid: A Lonely Dragon Wants to Be Loved", 2025, title) for title in (
        "Miss Kobayashi's Dragon Maid: A lonely dragon wants to be loved AKA Kobayashi-san Chi no Meidoragon Samishigariya no Ryû 2025 1080p NF WEB-DL DDP 5.1 x264-Draken02",
        'Miss Kobayashis Dragon Maid A Lonely Dragon Wants to be Loved 2025 1080p NF WEB-DL x264 DDP5.1-ADWeb',
        'Eiga Kobayashi-san Chi no Maidragon Samishigariya no Ryuu 2025 BDRip 1080p H264 FLAC 2.0 6Audio-jsum@U2',
    )],
    ('tt0093342', '何处是我朋友的家', "Where Is the Friend's Home?", 1987,
     "Where Is the Friend's House 1987 1080p Criterion BluRay FLAC x264-SUPER"),
]


class VerifiedReleaseNamesTests(unittest.TestCase):
    def rank(self, case, *, source='metadata', imdb=None, subtitle=None, title=None, wait='', size=8):
        identity, chinese, original, year, release = case
        query = MovieQuery(identity, chinese, original, year)
        hit = TorrentInfo(1, 'fixture', '1', title or release,
                          subtitle=chinese if subtitle is None else subtitle,
                          imdb_id=imdb or identity, imdb_source=source,
                          seeders=10, size_bytes=size * 1024**3)
        return rank_for_movie([hit], TorrentRules(), query, wait)

    def test_all_ten_verified_releases_are_accepted(self):
        self.assertEqual(len(CASES), 10)
        for case in CASES:
            for source in ('metadata', 'detail'):
                with self.subTest(release=case[-1], source=source):
                    self.assertIsNotNone(self.rank(case, source=source)[0])

    def test_search_inferred_and_unknown_provenance_cannot_authorize_download(self):
        for case in CASES:
            for source in ('search', 'unknown'):
                with self.subTest(release=case[-1], source=source):
                    self.assertIsNone(self.rank(case, source=source)[0])

    def test_verified_id_still_requires_some_title_evidence(self):
        self.assertIsNone(self.rank(CASES[0], title='Another Movie 2025 1080p WEB-DL', subtitle='')[0])
        self.assertIsNone(self.rank(CASES[-1], title='The Koker Trilogy 1987-1994 BluRay 1080p x265', subtitle='乡村三部曲合集')[0])

    def test_same_name_same_year_wrong_odyssey_and_parent_id_extras_stay_rejected(self):
        case = ('tt33764258', '奥德赛', 'The Odyssey', 2026, 'The Odyssey 2026 1080p AMZN WEB-DL H.264')
        best, rows = self.rank(case, imdb='tt41605854')
        self.assertIsNone(best)
        self.assertIn('同名影片', rows[0].reason)
        for title in ('The Odyssey Making of an Epic 2026 1080p WEB-DL',
                      'The Odyssey Trailer 2026 1080p WEB-DL'):
            self.assertIsNone(self.rank(case, title=title)[0])
        self.assertIsNone(self.rank(case, title='Galaxymphony III The Final Odyssey 2025 1080p BluRay x264',
                                   subtitle='银河交响曲：终极奥德赛')[0])

    def test_identity_does_not_bypass_source_specs_or_release_gate(self):
        case = CASES[0]
        for suffix, expected in (('720p BluRay x264', '清晰度'),
                                 ('1080p BluRay REMUX AVC', 'Remux'),
                                 ('1080p HDCAM', '枪版')):
            best, rows = self.rank(case, title=f'Inter Alia 2025 {suffix}')
            self.assertIsNone(best)
            self.assertIn(expected, rows[0].reason)
        self.assertIsNone(self.rank(case, size=40)[0])
        self.assertIsNone(self.rank(case, wait='等待上线确认')[0])

    def test_bracketed_release_keeps_source_type_requirement(self):
        case = ('tt5598102', '脸庞，村庄', 'Visages villages', 2017,
                '[其他][脸庞，村庄][Visages villages 2017 1080p x265 DTS-HD MA 5.1-CHD][中英字幕]')
        best, rows = self.rank(case)
        self.assertIsNone(best)
        self.assertIn('片源类型不明', rows[0].reason)
        self.assertIsNotNone(self.rank(case, title=case[-1].replace('1080p x265', '1080p BluRay x265'))[0])


if __name__ == '__main__':
    unittest.main()
