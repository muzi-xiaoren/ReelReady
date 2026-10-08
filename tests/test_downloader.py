import unittest
from unittest.mock import patch

from reelready.downloader import QBittorrent
from reelready.settings import QBittorrentSettings


class DockerDownloaderTests(unittest.TestCase):
    def test_loopback_uses_host_only_inside_docker(self):
        for host in ('localhost', '127.0.0.1', '[::1]'):
            for docker in (True, False):
                url = f'http://{host}:8080/qb/'
                with patch('reelready.downloader.Path.exists', return_value=docker), patch('reelready.downloader.make_client') as client:
                    qb = QBittorrent(QBittorrentSettings(url=url))
                    self.assertEqual(qb.base, 'http://host.docker.internal:8080/qb' if docker else url.rstrip('/'))
                    self.assertFalse(client.call_args.kwargs['trust_env'])
                    self.assertEqual(client.call_args.kwargs['headers']['Referer'], qb.base)
                    qb.close()

    def test_remote_address_unchanged(self):
        with patch('reelready.downloader.Path.exists', return_value=True), patch('reelready.downloader.make_client'):
            with QBittorrent(QBittorrentSettings(url='https://downloader.example:443/qb')) as qb:
                self.assertEqual(qb.base, 'https://downloader.example:443/qb')
