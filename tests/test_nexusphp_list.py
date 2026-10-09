import unittest
from unittest.mock import patch
from reelready.sites.base import SiteConfig
from reelready.sites.base import MovieQuery
from reelready.sites.nexusphp import NexusPHPSite


class NexusPHPListTests(unittest.TestCase):
    def setUp(self):
        self.site = NexusPHPSite(SiteConfig(1, 'nexusphp', 'fixture', 'https://fixture.example', cookie='synthetic=1'))
        self.addCleanup(self.site.close)

    def test_cover_before_title_uses_title_link(self):
        html = '<table class="torrents"><tr><td>Name</td><td>Size</td></tr><tr><td><a href="details.php?id=1"><img src="cover.jpg"></a><a href="details.php?id=1">Movie 2026 1080p WEB-DL</a></td><td>4 GB</td></tr></table>'
        rows = self.site._parse_list(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].title, 'Movie 2026 1080p WEB-DL')

    def test_empty_icon_link_does_not_create_fake_torrent(self):
        html = '<table class="torrents"><tr><td>Name</td></tr><tr><td><a href="details.php?id=53605"><img src="icon.jpg"></a></td></tr></table>'
        self.assertEqual(self.site._parse_list(html), [])

    def test_imdb_search_does_not_fabricate_verified_metadata(self):
        html = '<table class="torrents"><tr><td>Name</td></tr><tr><td><a href="details.php?id=1">Movie 2026 1080p WEB-DL</a></td></tr></table>'
        with patch.object(self.site, '_get_page', return_value=html):
            result = self.site.search(MovieQuery('tt1234567', 'Movie', None, 2026))
        self.assertEqual(result[0].imdb_source, 'search')
