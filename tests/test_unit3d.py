import unittest
from unittest.mock import patch

import httpx

from reelready.cookiecloud import detect_cookie_hosts
from reelready.sites.base import LoginExpired, MovieQuery, SiteConfig, SiteError
from reelready.sites.unit3d import Unit3DSite

LIST = '''<form action="/logout"></form><table><tbody><tr>
<td><a class="view-torrent" href="/torrents/123">Movie 1080p WEB-DL</a>
<span class="torrent-listings-subhead">中字</span></td>
<td class="torrent-listings-size"><span>4.5 GiB</span></td>
<td><a href="/torrents/123/peers"><span class="text-green">12</span></a></td>
</tr></tbody></table>'''


class Unit3DTests(unittest.TestCase):
    def setUp(self):
        self.site = Unit3DSite(SiteConfig(1, "unit3d", "Monikadesign", "https://monikadesign.uk", cookie="synthetic=1"))
        self.addCleanup(self.site.close)

    def response(self, html, path="/torrents"):
        return httpx.Response(200, text=html, request=httpx.Request("GET", "https://monikadesign.uk" + path))

    def test_login_and_imdb_search_parse_monika_layout(self):
        with patch.object(self.site, "_get", return_value=self.response(LIST)) as request:
            self.assertEqual(self.site.test(), "已登录")
            results = self.site.search(MovieQuery("tt1234567", "Movie", None, 2025))
            request.assert_called_with("torrents", perPage=100, imdbId="1234567")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].seeders, 12)
        self.assertEqual(results[0].size_bytes, int(4.5 * 1024 ** 3))
        self.assertEqual(results[0].imdb_id, "tt1234567")
        self.assertEqual(results[0].subtitle, "中字")

    def test_login_redirect_is_rejected(self):
        with patch.object(self.site, "_get", return_value=self.response('<input type="password">', "/login")):
            with self.assertRaises(LoginExpired):
                self.site.test()

    def test_unknown_page_is_not_reported_as_logged_in(self):
        with patch.object(self.site, "_get", return_value=self.response("<h1>Something changed</h1>")):
            with self.assertRaises(SiteError):
                self.site.test()

    def test_download_resolves_detail_link_and_checks_torrent(self):
        torrent = self.site._parse_list(LIST)[0]
        html = '<form action="/logout"></form><a href="/download_check/123/token">Download</a>'
        torrent_response = httpx.Response(200, content=b"d4:infodee", request=httpx.Request("GET", "https://monikadesign.uk/download/123/token"))
        with patch.object(self.site, "_get", side_effect=[self.response(html), torrent_response]) as request:
            self.assertEqual(self.site.download(torrent), b"d4:infodee")
            self.assertEqual(request.call_args.args, ("https://monikadesign.uk/download/123/token",))

    def test_download_does_not_send_cookie_to_external_link(self):
        torrent = self.site._parse_list(LIST)[0]
        html = '<form action="/logout"></form><a href="https://elsewhere.example/download/123">Download</a>'
        with patch.object(self.site, "_get", return_value=self.response(html)) as request:
            with self.assertRaises(SiteError):
                self.site.download(torrent)
            request.assert_called_once()

    def test_new_sites_require_login_cookies_and_qingwa_aliases_merge(self):
        data = {"cookie_data": {"cookies": [
            {"domain": "qingwapt.com", "name": "qw_session", "value": "synthetic"},
            {"domain": "www.qingwapt.com", "name": "qw_session", "value": "synthetic"},
            {"domain": "www.qingwa.pro", "name": "SITE_TOTAL_ID", "value": "synthetic"},
            {"domain": "monikadesign.uk", "name": "remember_web_test", "value": "synthetic"},
        ]}}
        self.assertEqual(detect_cookie_hosts(data), ["monikadesign.uk", "qingwapt.com"])
        data["cookie_data"]["cookies"][-1]["value"] = ""
        self.assertEqual(detect_cookie_hosts(data), ["qingwapt.com"])


if __name__ == "__main__":
    unittest.main()
