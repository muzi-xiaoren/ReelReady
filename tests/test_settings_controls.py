import unittest
from datetime import date
from unittest.mock import patch

from pydantic import ValidationError

from reelready.settings import AppSettings, CollectSettings, TorrentRules
from reelready.services.movies import collection_cutoff, within_collection_window
from reelready.sources.tmdb import _from_payload
from reelready.torrent_rules import TorrentInfo, parse, pick_best
from reelready.web import forms


class SettingsControlsTests(unittest.TestCase):
    def test_rerelease_switch_defaults_off_and_autosave_parses_both_states(self):
        section = CollectSettings()
        self.assertFalse(section.include_rereleases)
        view = next(v for v in forms.describe(section) if v.name == 'include_rereleases')
        self.assertEqual(view.kind, 'bool')
        self.assertTrue(forms.parse(section, {'include_rereleases': 'on'}).include_rereleases)
        self.assertFalse(forms.parse(CollectSettings(include_rereleases=True), {}).include_rereleases)

    def test_month_cutoff_handles_year_rollover_and_leap_day(self):
        settings = AppSettings()
        settings.collect.max_age_years = 0
        settings.collect.max_age_months = 1
        self.assertEqual(collection_cutoff(settings, date(2024, 3, 31)), date(2024, 2, 29))
        settings.collect.max_age_years = 1
        settings.collect.max_age_months = 2
        self.assertEqual(collection_cutoff(settings, date(2026, 1, 31)), date(2024, 11, 30))

    def test_date_boundary_and_year_only_fallback(self):
        settings = AppSettings(collect=CollectSettings(max_age_years=0, max_age_months=6))
        with patch('reelready.services.movies.date') as dates:
            dates.today.return_value = date(2026, 10, 8)
            dates.side_effect = date
            dates.fromisoformat.side_effect = date.fromisoformat
            self.assertTrue(within_collection_window(settings, 2026, '2026-04-08'))
            self.assertFalse(within_collection_window(settings, 2026, '2026-04-07'))
            self.assertFalse(within_collection_window(settings, 2026, '2026-10-09'))
            self.assertTrue(within_collection_window(settings, 2026))
            self.assertFalse(within_collection_window(settings, 2025))

    def test_tmdb_preserves_release_date(self):
        self.assertEqual(_from_payload({'id': 1, 'release_date': '2026-04-08'}).release_date, '2026-04-08')

    def test_invalid_month_is_rejected(self):
        for value in (-1, 12):
            with self.assertRaises(ValidationError):
                CollectSettings(max_age_months=value)

    def test_secret_mask_never_exposes_or_overwrites_secret(self):
        settings = AppSettings()
        settings.tmdb.api_key = 'synthetic-private-value'
        view = next(v for v in forms.describe(settings.tmdb) if v.name == 'api_key')
        self.assertEqual(view.value, forms.SECRET_MASK)
        for value in ('', forms.SECRET_MASK):
            self.assertEqual(forms.parse(settings.tmdb, {'api_key': value}).api_key, 'synthetic-private-value')
        self.assertEqual(forms.parse(settings.tmdb, {'api_key': 'replacement'}).api_key, 'replacement')

    def test_switches_allow_empty_and_preserve_extra_regions(self):
        section = CollectSettings(tmdb_regions=['AR', 'US'])
        view = next(v for v in forms.describe(section) if v.name == 'tmdb_regions')
        self.assertIn('AR', view.choices)
        self.assertEqual(forms.parse(section, {'tmdb_regions': ''}).tmdb_regions, [])
        self.assertEqual(forms.parse(section, {'tmdb_regions': 'hk,us,hk'}).tmdb_regions, ['HK', 'US'])

    def test_resolution_order_and_new_sizes(self):
        for text, expected in [('8K', 4320), ('1440p', 1440), ('576i', 576), ('480p', 480), ('1080i', 1080), ('UHD', 2160)]:
            torrent = TorrentInfo(1, 'test', text, 'Movie ' + text, seeders=3)
            self.assertEqual(parse(torrent).resolution, expected)
        rules = forms.parse(TorrentRules(), {'resolutions': '1080p,2160p'})
        hits = [TorrentInfo(1, 'test', '4k', 'Movie 2160p', seeders=9), TorrentInfo(1, 'test', 'hd', 'Movie 1080p', seeders=9)]
        self.assertEqual(pick_best(hits, rules)[0].torrent.torrent_id, 'hd')

    def test_seeders_win_across_accepted_quality_and_preferences(self):
        rules = TorrentRules()
        hits = [TorrentInfo(1, 'test', '4k', 'Movie 2160p WEB-DL HDR CHS', seeders=9), TorrentInfo(1, 'test', 'hd', 'Movie 1080p WEB-DL', seeders=100), TorrentInfo(1, 'test', 'cam', 'Movie 1080p HDCAM', seeders=200)]
        best, ranked = pick_best(hits, rules)
        self.assertEqual(best.torrent.torrent_id, 'hd')
        self.assertFalse(ranked[-1].ok)

    def test_explicit_resolution_beats_disc_source_uhd(self):
        self.assertEqual(parse(TorrentInfo(1, 'test', 'hd', 'Movie 1080p UHD BluRay')).resolution, 1080)

    def test_region_order_is_rendered_as_saved(self):
        section = CollectSettings(tmdb_regions=['JP', 'CN', 'US'])
        view = next(v for v in forms.describe(section) if v.name == 'tmdb_regions')
        self.assertEqual(list(view.choices)[:3], ['JP', 'CN', 'US'])
        section = forms.parse(section, {'tmdb_regions': 'JP,CN,US', 'tmdb_regions_order': 'DE,JP,CN,US'})
        view = next(v for v in forms.describe(section) if v.name == 'tmdb_regions')
        self.assertEqual(list(view.choices)[:4], ['DE', 'JP', 'CN', 'US'])
        self.assertEqual(section.tmdb_regions, ['JP', 'CN', 'US'])

    def test_no_matching_resolution_and_subtitle_is_preference(self):
        torrent = TorrentInfo(1, 'test', 'hd', 'Movie 1080p', seeders=3)
        self.assertIsNotNone(pick_best([torrent], TorrentRules())[0])
        best, ranked = pick_best([torrent], TorrentRules(resolutions=['720p']))
        self.assertIsNone(best)
        self.assertIn('不在允许范围', ranked[0].reason)


if __name__ == '__main__':
    unittest.main()
