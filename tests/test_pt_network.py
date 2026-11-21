import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

import httpx
from pydantic import ValidationError

from reelready.services.clients import pt_proxy
from reelready.services.pt import _fetch_torrent, _download, open_sites
from reelready.settings import AppSettings, PTSettings
from reelready.sites import LoginExpired, SiteError
from reelready.torrent_rules import TorrentInfo
from reelready.web import forms


def network_error():
    try:
        raise httpx.ReadTimeout('read timed out')
    except httpx.ReadTimeout as cause:
        error = SiteError('下载种子失败')
        error.__cause__ = cause
        return error


class PTNetworkTests(unittest.TestCase):
    def setUp(self):
        self.settings = AppSettings()
        self.site = Mock(name='site')
        self.site.name = 'fixture'
        self.hit = TorrentInfo(1, 'fixture', '123', 'Movie 2026 1080p WEB-DL')

    def test_proxy_switch_and_missing_address(self):
        self.settings.network.proxy_url = 'http://proxy.test:7897'
        self.assertIsNone(pt_proxy(self.settings))
        self.settings.pt.use_proxy = True
        self.assertEqual(pt_proxy(self.settings), self.settings.network.proxy_url)
        self.settings.network.proxy_url = ''
        self.assertIsNone(pt_proxy(self.settings))

    def test_open_sites_passes_selected_proxy_to_adapters(self):
        self.settings.network.proxy_url = 'http://proxy.test:7897'
        session = Mock()
        session.scalars.return_value = [Mock()]
        for enabled in (False, True):
            self.settings.pt.use_proxy = enabled
            with patch('reelready.services.pt.session_scope', return_value=nullcontext(session)), \
                 patch('reelready.services.pt.snapshot', return_value='fixture'), \
                 patch('reelready.services.pt.build_site', return_value=self.site) as build:
                self.assertEqual(open_sites(self.settings), [self.site])
                build.assert_called_once_with('fixture', proxy=self.settings.network.proxy_url if enabled else None)

    def test_transient_fetch_regenerates_link_and_succeeds_within_limit(self):
        self.site.download.side_effect = [network_error(), network_error(), b'torrent']
        with patch('reelready.services.pt.time.sleep') as sleep:
            self.assertEqual(_fetch_torrent(self.settings, self.site, self.hit), b'torrent')
        self.assertEqual(self.site.download.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_transient_fetch_exhaustion_and_disabled_retries(self):
        for retries in (0, 1, 3):
            self.settings.pt.download_retries = retries
            self.site.download.reset_mock()
            self.site.download.side_effect = network_error()
            with patch('reelready.services.pt.time.sleep') as sleep, self.assertRaises(SiteError):
                _fetch_torrent(self.settings, self.site, self.hit)
            self.assertEqual(self.site.download.call_count, retries + 1)
            self.assertEqual(sleep.call_count, retries)

    def test_authentication_and_invalid_file_errors_are_not_retried(self):
        for error in (LoginExpired('expired'), SiteError('HTTP 403'), SiteError('not a torrent')):
            self.site.download.reset_mock()
            self.site.download.side_effect = error
            with patch('reelready.services.pt.time.sleep') as sleep, self.assertRaises(SiteError):
                _fetch_torrent(self.settings, self.site, self.hit)
            self.site.download.assert_called_once()
            sleep.assert_not_called()

    def test_qb_submission_timeout_is_not_retried(self):
        from reelready.downloader import DownloaderError
        self.site.download.return_value = b'torrent'
        qb = Mock()
        qb.add_torrent.side_effect = DownloaderError('submission timed out')
        session = Mock()
        session.get.return_value.title = 'Fixture'
        with patch('reelready.services.pt.QBittorrent', return_value=nullcontext(qb)), \
             patch('reelready.services.pt.session_scope', return_value=nullcontext(session)), \
             patch('reelready.services.pt.add_event') as event:
            self.assertFalse(_download(self.settings, 1, self.site, self.hit))
        self.site.download.assert_called_once()
        qb.add_torrent.assert_called_once()
        self.assertEqual(event.call_args.args[1], 'download_failed')

    def test_settings_render_parse_and_validate_retry_limit(self):
        section = forms.parse(PTSettings(), {'use_proxy': 'on', 'download_retries': '3'})
        self.assertTrue(section.use_proxy)
        self.assertEqual(section.download_retries, 3)
        self.assertIn('download_retries', {view.name for view in forms.describe(section)})
        for value in (-1, 4):
            with self.assertRaises(ValidationError):
                PTSettings(download_retries=value)


if __name__ == '__main__':
    unittest.main()
