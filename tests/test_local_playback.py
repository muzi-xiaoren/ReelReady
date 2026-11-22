import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from reelready import config, db, local_helper
from reelready.downloader import torrent_info_hash
from reelready.models import Movie
from reelready.services import playback
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

    def test_status_route_and_helper_endpoint_token(self):
        patcher, _ = self.fake_qb()
        with patcher:
            body = self.client.get('/playback/status?ids=1,2,x').json()
            self.assertEqual(body['movies']['2']['state'], 'ready')
            self.assertEqual(self.client.get('/playback/movies/2').status_code, 403)
            self.assertEqual(self.client.get('/playback/movies/2', headers={'X-ReelReady-Token': 'wrong'}).status_code, 403)
            token = load_settings().playback.helper_token
            info = self.client.get('/playback/movies/2', headers={'X-ReelReady-Token': token}).json()
        self.assertEqual(info['path'], 'D:\\Movies\\Old.Movie.2023.1080p')
        self.assertEqual(info['player'], 'system')

    def test_helper_download_is_preconfigured(self):
        settings = load_settings()
        response = self.client.get('/playback/helper.py')
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response.headers['content-disposition'])
        namespace: dict = {}
        exec(compile(response.text, 'reelready-helper.py', 'exec'), namespace)
        self.assertEqual(namespace['TOKEN'], settings.playback.helper_token)
        self.assertEqual(namespace['PORT'], settings.playback.helper_port)
        self.assertEqual(namespace['SERVER'], 'http://testserver')

    def test_pages_render_playback_controls(self):
        with patch('reelready.web.routes.queue_metadata', return_value=False):
            detail = self.client.get('/movies/1').text
        self.assertIn('data-playback="1"', detail)
        self.assertIn('data-playback-token=', detail)
        settings_page = self.client.get('/settings').text
        self.assertIn('/playback/helper.py', settings_page)
        self.assertIn('id="playback"', settings_page)
        cards = self.client.get('/movies?tab=completed').text
        self.assertIn('data-playback-action="play"', cards)


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_pick_video_prefers_largest_non_sample(self):
        folder = self.root / 'Movie'
        folder.mkdir()
        (folder / 'sample.mkv').write_bytes(b'x' * 50)
        (folder / 'movie.mkv').write_bytes(b'x' * 20)
        (folder / 'readme.nfo').write_bytes(b'x' * 99)
        self.assertEqual(local_helper.pick_video(folder).name, 'movie.mkv')
        with self.assertRaises(local_helper.HelperError):
            local_helper.pick_video(folder / 'readme.nfo')

    def test_open_movie_rules(self):
        video = self.root / 'a.mkv'
        video.write_bytes(b'x')
        info = {'state': 'downloading', 'path': str(video), 'message': '下载中 10%', 'player': 'system'}
        with patch.object(local_helper, 'fetch_movie', return_value=info), \
             patch.object(local_helper, 'open_folder') as folder, patch.object(local_helper, 'play', return_value='PotPlayer') as play:
            self.assertEqual(local_helper.open_movie('s', 't', 1, 'folder'), '已打开所在文件夹')
            folder.assert_called_once_with(video)
            with self.assertRaisesRegex(local_helper.HelperError, '下载中'):
                local_helper.open_movie('s', 't', 1, 'play')
            info['state'] = 'ready'
            self.assertIn('PotPlayer', local_helper.open_movie('s', 't', 1, 'play'))
            play.assert_called_once_with(video, 'system', '')
            info['path'] = str(self.root / 'gone.mkv')
            with self.assertRaisesRegex(local_helper.HelperError, '路径映射'):
                local_helper.open_movie('s', 't', 1, 'play')

    def test_custom_player_requires_existing_path(self):
        with self.assertRaises(local_helper.HelperError):
            local_helper.find_player('custom', '')
        with self.assertRaises(local_helper.HelperError):
            local_helper.find_player('vlc', str(self.root / 'missing.exe'))
        exe = self.root / 'player.exe'
        exe.write_bytes(b'')
        self.assertEqual(local_helper.find_player('custom', f'"{exe}"'), str(exe))

    def test_http_server_checks_origin_and_token(self):
        server = 'http://localhost:8765'
        local_helper.Handler.config = {'server': server, 'token': 'secret', 'origins': local_helper.allowed_origins(server)}
        httpd = local_helper.ThreadingHTTPServer(('127.0.0.1', 0), local_helper.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{httpd.server_address[1]}'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def call(path, method='GET', headers=None, body=None):
            request = urllib.request.Request(base + path, method=method, headers=headers or {}, data=body)
            try:
                with opener.open(request, timeout=5) as response:
                    return response.status, response.headers, response.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.headers, exc.read()

        try:
            status, headers, _ = call('/open', 'OPTIONS', {'Origin': 'http://127.0.0.1:8765'})
            self.assertEqual(status, 204)
            self.assertEqual(headers['Access-Control-Allow-Origin'], 'http://127.0.0.1:8765')
            self.assertEqual(call('/open', 'OPTIONS', {'Origin': 'https://evil.example'})[0], 403)
            self.assertEqual(call('/ping', headers={'X-ReelReady-Token': 'nope'})[0], 403)
            self.assertEqual(call('/ping', headers={'X-ReelReady-Token': 'secret', 'Origin': 'https://evil.example'})[0], 403)
            status, _, body = call('/ping', headers={'X-ReelReady-Token': 'secret', 'Origin': 'http://localhost:8765'})
            self.assertEqual((status, json.loads(body)['ok']), (200, True))
            with patch.object(local_helper, 'open_movie', return_value='已打开所在文件夹') as opened:
                status, _, body = call('/open', 'POST', {'X-ReelReady-Token': 'secret', 'Content-Type': 'application/json'},
                                       json.dumps({'movie_id': 7, 'action': 'folder'}).encode())
            self.assertEqual(status, 200)
            opened.assert_called_once_with(server, 'secret', 7, 'folder')
            status, _, _ = call('/open', 'POST', {'X-ReelReady-Token': 'secret'}, json.dumps({'movie_id': 7, 'action': 'rm'}).encode())
            self.assertEqual(status, 400)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == '__main__':
    unittest.main()
