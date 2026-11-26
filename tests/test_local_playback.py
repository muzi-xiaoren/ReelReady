import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reelready import config, db
from reelready.downloader import torrent_info_hash
from reelready.models import Movie
from reelready.services import playback, protocol
from reelready.settings import AppSettings, PlaybackSettings, load_settings, save_settings
from reelready.web.app import create_app


def bencode(value) -> bytes:
    if isinstance(value, int):
        return b'i%de' % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return b'%d:%s' % (len(value), value)
    if isinstance(value, list):
        return b'l' + b''.join(bencode(item) for item in value) + b'e'
    return b'd' + b''.join(bencode(key) + bencode(value[key]) for key in sorted(value)) + b'e'


class TorrentHashTests(unittest.TestCase):
    def test_hashes_raw_info_dictionary(self):
        info = {'name': 'Movie.2024.2160p.mkv', 'length': 123, 'piece length': 16384, 'pieces': b'\x00' * 20}
        content = bencode({'announce': 'https://tracker.example/a', 'created by': 'x', 'info': info})
        self.assertEqual(torrent_info_hash(content), hashlib.sha1(bencode(info)).hexdigest())

    def test_invalid_content_returns_none(self):
        for content in (b'', b'<html>login</html>', b'd4:info', b'd3:fooi1ee'):
            self.assertIsNone(torrent_info_hash(content))


class PathMappingTests(unittest.TestCase):
    def test_maps_docker_prefix_to_windows_path(self):
        settings = PlaybackSettings(path_from='/downloads/', path_to='D:\\Movies\\')
        self.assertEqual(settings.host_path('/downloads/Dune/Dune.mkv'), 'D:\\Movies\\Dune\\Dune.mkv')
        self.assertEqual(settings.host_path('/downloads'), 'D:\\Movies')
        self.assertEqual(settings.host_path('/downloads2/a.mkv'), '/downloads2/a.mkv')
        self.assertEqual(settings.host_path('/other/a.mkv'), '/other/a.mkv')

    def test_without_mapping_keeps_path(self):
        self.assertEqual(PlaybackSettings().host_path('D:\\Movies\\a.mkv'), 'D:\\Movies\\a.mkv')

    def test_windows_prefix_is_case_insensitive(self):
        settings = PlaybackSettings(path_from='d:\\downloads', path_to='\\\\nas\\movies')
        self.assertEqual(settings.host_path('D:\\Downloads\\a b\\c.mkv'), '\\\\nas\\movies\\a b\\c.mkv')


class LegacyMatchTests(unittest.TestCase):
    def test_exact_name_and_size_disambiguation(self):
        record = {'title': 'Dune Part Two 2024 2160p WEB-DL', 'size_bytes': 10_000}
        rows = [{'name': 'Dune.Part.Two.2024.2160p.WEB-DL', 'size': 10_050, 'hash': 'a'},
                {'name': 'Other.Movie.2024', 'size': 10_000, 'hash': 'b'}]
        self.assertEqual(playback.match_legacy(record, rows)['hash'], 'a')
        rows.append({'name': 'Dune.Part.Two.2024.2160p.WEB-DL', 'size': 50_000, 'hash': 'c'})
        self.assertEqual(playback.match_legacy(record, rows)['hash'], 'a')

    def test_cross_seeded_copies_share_one_location(self):
        record = {'title': 'Dogville 2003 GER BluRay 1080p x265 10bit DDP5.1 MNHD-FRDS', 'size_bytes': 12_870}
        rows = [{'name': '狗镇.Dogville.2003.GER.BluRay.1080p.x265.10bit.DDP5.1.MNHD-FRDS', 'size': 12_870,
                 'hash': h, 'progress': p, 'content_path': '/downloads/狗镇.Dogville'} for h, p in (('a', 0.5), ('b', 1), ('c', 1))]
        self.assertIn(playback.match_legacy(record, rows)['hash'], ('b', 'c'))
        rows[2]['content_path'] = '/elsewhere/狗镇.Dogville'
        self.assertIsNone(playback.match_legacy(record, rows))

    def test_loose_match_needs_size(self):
        record = {'title': '沙丘2 Dune.Part.Two.2024.2160p.WEB-DL [中字]', 'size_bytes': 10_000}
        rows = [{'name': 'Dune.Part.Two.2024.2160p.WEB-DL', 'size': 99_000, 'hash': 'a'}]
        self.assertIsNone(playback.match_legacy(record, rows))
        rows[0]['size'] = 10_000
        self.assertEqual(playback.match_legacy(record, rows)['hash'], 'a')


class PlaybackDbTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name)
        self.engine = create_engine(f'sqlite:///{path / "test.db"}', connect_args={'check_same_thread': False})
        self.patches = [patch.object(db, 'engine', self.engine),
            patch.object(db, 'SessionLocal', sessionmaker(bind=self.engine, expire_on_commit=False)),
            patch.object(config, 'DB_PATH', path / 'test.db'), patch.object(config, 'DATA_DIR', path)]
        for p in self.patches:
            p.start()
        db.init_db()
        with db.session_scope() as session:
            session.add(Movie(id=1, title='沙丘2', status='completed',
                              downloaded={'title': 'Dune.2', 'size_bytes': 100, 'hash': 'AAA'}))
            session.add(Movie(id=2, title='旧记录', status='completed',
                              downloaded={'title': 'Old.Movie.2023.1080p', 'size_bytes': 500}))
            session.add(Movie(id=3, title='没下载', status='monitoring'))
        settings = AppSettings()
        settings.playback.path_from = '/downloads'
        settings.playback.path_to = 'D:\\Movies'
        save_settings(settings)
        self.rows = [
            {'hash': 'aaa', 'name': 'Dune.2', 'progress': 0.634, 'state': 'downloading', 'content_path': '/downloads/Dune.2.mkv'},
            {'hash': 'bbb', 'name': 'Old.Movie.2023.1080p', 'size': 500, 'progress': 1, 'state': 'stalledUP',
             'content_path': '/downloads/Old.Movie.2023.1080p'},
        ]
        self.client = TestClient(create_app())

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def fake_qb(self):
        qb = MagicMock()
        qb.__enter__.return_value = qb
        qb.torrents.return_value = self.rows
        return patch.object(playback, 'QBittorrent', return_value=qb), qb

    def test_status_reports_progress_paths_and_remembers_legacy_hash(self):
        patcher, qb = self.fake_qb()
        with patcher:
            status = playback.local_status(load_settings(), [1, 2, 3])
        qb.torrents.assert_called_once_with()
        self.assertEqual(status[1]['state'], 'downloading')
        self.assertEqual(status[1]['message'], '下载中 63%')
        self.assertEqual(status[1]['path'], 'D:\\Movies\\Dune.2.mkv')
        self.assertEqual(status[2]['state'], 'ready')
        self.assertNotIn(3, status)
        with db.session_scope() as session:
            self.assertEqual(session.get(Movie, 2).downloaded['hash'], 'bbb')
        patcher, qb = self.fake_qb()
        with patcher:
            playback.local_status(load_settings(), [1, 2])
        qb.torrents.assert_called_once_with(['aaa', 'bbb'])

    def test_missing_torrent(self):
        self.rows = []
        patcher, _ = self.fake_qb()
        with patcher:
            self.assertEqual(playback.local_status(load_settings(), [1])[1]['state'], 'missing')

    def test_status_route_and_handler_endpoint(self):
        patcher, _ = self.fake_qb()
        with patcher:
            body = self.client.get('/playback/status?ids=1,2,x').json()
            self.assertEqual(body['movies']['2']['state'], 'ready')
            info = self.client.get('/playback/movies/2').json()
            missing = self.client.get('/playback/movies/3').json()
        self.assertEqual(info['path'], r'D:\Movies\Old.Movie.2023.1080p')
        self.assertEqual(info['player'], 'system')
        self.assertEqual(missing['state'], 'not_downloaded')

    def test_reg_downloads(self):
        response = self.client.get('/playback/reelready-protocol.reg')
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment; filename="reelready-protocol.reg"', response.headers['content-disposition'])
        text = response.content.decode('utf-16')
        self.assertTrue(text.startswith('Windows Registry Editor Version 5.00'))
        self.assertIn('http://testserver/playback/movies/', text)
        uninstall = self.client.get('/playback/reelready-protocol-uninstall.reg').content.decode('utf-16')
        self.assertIn(r'[-HKEY_CURRENT_USER\Software\Classes\reelready]', uninstall)

    def test_pages_render_playback_controls(self):
        with patch('reelready.web.routes.queue_metadata', return_value=False):
            detail = self.client.get('/movies/1').text
        self.assertIn('data-playback="1"', detail)
        settings_page = self.client.get('/settings').text
        self.assertIn('/playback/reelready-protocol.reg', settings_page)
        self.assertIn('id="playback"', settings_page)
        cards = self.client.get('/movies?tab=completed').text
        self.assertIn('data-playback-action="play"', cards)


class ProtocolTests(unittest.TestCase):
    def test_script_cannot_break_out_of_registry_command(self):
        script = protocol.handler_script('http://localhost:8765/')
        self.assertNotIn('"', script)
        self.assertNotIn('%', script)
        self.assertIn("'http://localhost:8765/playback/movies/'", script)
        command = protocol.handler_command()
        self.assertTrue(command.startswith(r'"C:\Windows\System32\conhost.exe" --headless '))
        self.assertLess(len(command), 300)
        # The link is only ever appended after a comment marker.
        self.assertTrue(command.endswith(' #" "%1"'))

    def test_rejects_unsafe_server_address(self):
        for server in ("http://x'; calc #", 'javascript:alert(1)', 'http://a/%USERPROFILE%', 'http://a/$(calc)'):
            with self.assertRaises(ValueError):
                protocol.handler_script(server)

    def test_reg_file_escapes_command(self):
        text = protocol.install_reg('http://localhost:8765').decode('utf-16')
        line = next(item for item in text.splitlines() if item.startswith('@="\\"C:'))
        value = line[3:-1].replace('\\"', '"').replace('\\\\', '\\')
        self.assertEqual(value, protocol.handler_command())
        script_line = next(item for item in text.splitlines() if item.startswith('"Script"="'))
        script = script_line[10:-1].replace('\\"', '"').replace('\\\\', '\\')
        self.assertEqual(script, protocol.handler_script('http://localhost:8765'))
        self.assertIn('"URL Protocol"=""', text)
        self.assertIn('\r\n', text)

    @unittest.skipUnless(sys.platform == 'win32', 'needs Windows PowerShell')
    def test_powershell_parses_script(self):
        script = protocol.handler_script('http://localhost:8765') + ' # reelready://play/1'
        check = ("[Console]::InputEncoding=[Text.Encoding]::UTF8; $e=$null; [void][System.Management.Automation.Language.Parser]::ParseInput("
                 "[Console]::In.ReadToEnd(),[ref]$null,[ref]$e); if($e.Count){$e | ForEach-Object {$_.Message}; exit 1}")
        result = subprocess.run([protocol.POWERSHELL, '-NoProfile', '-NonInteractive', '-Command', check],
                                input=script.encode('utf-8'), capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout.decode('utf-8', 'replace') + result.stderr.decode('utf-8', 'replace'))


if __name__ == '__main__':
    unittest.main()
