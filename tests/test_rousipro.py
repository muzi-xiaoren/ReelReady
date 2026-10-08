import unittest
from unittest.mock import patch

import httpx

from reelready.sites.base import LoginExpired, MovieQuery, SiteConfig, SiteError
from reelready.sites.rousipro import RousiProSite
from reelready.torrent_rules import TorrentInfo


class RousiProTests(unittest.TestCase):
    def setUp(self):
        self.site = RousiProSite(SiteConfig(1, "rousipro", "Rousi Pro", "https://rousi.pro", api_key="synthetic"))
        self.addCleanup(self.site.close)

    def test_cookie_does_not_replace_api_key(self):
        with self.assertRaises(LoginExpired):
            RousiProSite(SiteConfig(1, "rousipro", "Rousi Pro", "https://rousi.pro", cookie="synthetic=1"))

    def test_profile_and_title_search_use_v1_api(self):
        with patch.object(self.site, "_api", side_effect=[
            {"id": 1, "username": "synthetic"},
            {"torrents": [{"id": 123, "title": "Movie 1080p WEB-DL", "size": 1024, "seeders": 3}]},
        ]) as api:
            self.assertEqual(self.site.test(), "已连接 Rousi Pro")
            result = self.site.search(MovieQuery("tt1234567", "电影", "Movie", 2026))
            api.assert_called_with("api/v1/torrents", keyword="Movie", page=1, page_size=100)
        self.assertEqual(result[0].torrent_id, "123")
        self.assertIsNone(result[0].imdb_id)  # No fabricated IMDb attribution for title hits.

    def test_download_uses_fresh_signed_url(self):
        torrent = TorrentInfo(1, "Rousi Pro", "123", "Movie")
        response = httpx.Response(200, content=b"d4:infodee")
        with patch.object(self.site, "_api", return_value={"download_url": "/download/123?signed=synthetic"}) as api, patch.object(self.site, "_get", return_value=response) as get:
            self.assertEqual(self.site.download(torrent), b"d4:infodee")
            api.assert_called_once_with("api/v1/torrents/123")
            get.assert_called_once_with("/download/123?signed=synthetic")

    def test_missing_download_permissions_are_reported_without_purchase(self):
        with patch.object(self.site, "_api", return_value={"download_url": "", "price": 10}), patch.object(self.site, "_get") as get:
            with self.assertRaises(SiteError):
                self.site.download(TorrentInfo(1, "Rousi Pro", "123", "Movie"))
            get.assert_not_called()

    def test_external_download_links_cannot_receive_api_key(self):
        with patch.object(self.site._client, "get") as get:
            with self.assertRaises(SiteError):
                self.site._get("https://elsewhere.example/download/123")
            get.assert_not_called()
        self.assertFalse(self.site._client.follow_redirects)

    def test_api_authentication_and_payload_errors(self):
        for status, error in [(401, LoginExpired), (403, SiteError)]:
            with patch.object(self.site._client, "get", return_value=httpx.Response(status)):
                with self.assertRaises(error):
                    self.site.test()
        with patch.object(self.site, "_get", return_value=httpx.Response(200, json={"code": 200, "data": {"id": 1, "username": "synthetic"}})):
            self.assertEqual(self.site.test(), "已连接 Rousi Pro")
        with patch.object(self.site, "_get", return_value=httpx.Response(200, json={"code": 200, "data": {}})):
            with self.assertRaises(SiteError):
                self.site.test()


if __name__ == "__main__":
    unittest.main()
