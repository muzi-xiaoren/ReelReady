import base64
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import sessionmaker

from reelready import config, db
from reelready.cookiecloud import _passphrase
from reelready.models import CookieCloudBlob, Event, JobState, Movie, Setting, Site
from reelready.services.backup import BackupError, export_data, import_data
from reelready.services.sites import HIDDEN_SITES_KEY, load_cookiecloud
from reelready.settings import AppSettings, load_settings, save_settings
from reelready.web.app import create_app


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        directory = Path(self.temp.name)
        self.engine = create_engine(f'sqlite:///{directory / "test.db"}', connect_args={'check_same_thread': False})
        self.patches = [patch.object(db, 'engine', self.engine),
                        patch.object(db, 'SessionLocal', sessionmaker(bind=self.engine, expire_on_commit=False)),
                        patch.object(config, 'DATA_DIR', directory), patch.object(config, 'DB_PATH', directory / 'test.db')]
        for item in self.patches:
            item.start()
        db.init_db()
        self.settings = AppSettings()
        self.settings.email.password = 'synthetic-email-password'
        self.settings.tmdb.api_key = 'synthetic-tmdb-key'
        self.settings.qbittorrent.password = 'synthetic-qb-password'
        self.settings.network.proxy_url = 'http://127.0.0.1:7897'
        self.settings.collect.tmdb_regions = ['JP', 'CN']
        save_settings(self.settings)
        cc = self.settings.cookiecloud
        self.cookie_data = {'cookie_data': {'fixture.example': [{'name': 'uid', 'domain': '.fixture.example', 'value': 'synthetic'}]}}
        cipher = AES.new(_passphrase(cc.uuid, cc.password), AES.MODE_CBC, iv=bytes(16))
        encrypted = base64.b64encode(cipher.encrypt(pad(json.dumps(self.cookie_data).encode(), AES.block_size))).decode()
        with db.session_scope() as session:
            session.add_all([
                Setting(key=HIDDEN_SITES_KEY, value={'host:hidden.example': 'hidden.example'}),
                CookieCloudBlob(uuid=cc.uuid, encrypted=encrypted, crypto_type='aes-128-cbc-fixed'),
                Site(id=7, identity_key='nexusphp:fixture.example', kind='nexusphp', name='Fixture', base_url='https://fixture.example', cookie='uid=synthetic', api_key='synthetic-site-key', enabled=False, category='custom', use_cookiecloud=False),
                Movie(id=12, title='Fixture movie', tmdb_id=12345, status='completed', stage=3,
                      downloaded={'site_id': 7, 'site': 'Fixture', 'title': 'synthetic torrent'},
                      last_pt_candidates=[{'site_id': 7, 'title': 'synthetic torrent', 'torrent_id': '42'}]),
                JobState(name='check', last_run_at=datetime(2026, 10, 20, 10), last_status='ok', last_message='checked'),
            ])
            session.flush()
            session.add(Event(movie_id=12, kind='downloaded', title='Fixture download', notified=False))
        self.client = TestClient(create_app())

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def clear(self):
        with self.engine.begin() as connection:
            for table in reversed(db.Base.metadata.sorted_tables):
                connection.execute(delete(table))

    def encode(self, data):
        return json.dumps(data, ensure_ascii=False).encode()

    def test_full_backup_restores_all_configuration_on_fresh_install(self):
        payload = export_data('full')
        self.clear()
        with patch('reelready.notifier.send_mail') as mail, patch('reelready.services.pt.download') as download:
            stats = import_data(self.encode(payload), 'full')
        mail.assert_not_called()
        download.assert_not_called()
        restored = load_settings()
        self.assertEqual(restored.model_dump(), self.settings.model_dump())
        self.assertEqual(load_cookiecloud(restored)[0], self.cookie_data)
        with db.session_scope() as session:
            self.assertEqual(session.get(Setting, HIDDEN_SITES_KEY).value, {'host:hidden.example': 'hidden.example'})
            site = session.scalar(select(Site))
            self.assertEqual((site.cookie, site.api_key, site.enabled, site.category, site.use_cookiecloud), ('uid=synthetic', 'synthetic-site-key', False, 'custom', False))
            movie = session.scalar(select(Movie))
            self.assertEqual(movie.downloaded['site_id'], site.id)
            self.assertEqual(movie.last_pt_candidates[0]['site_id'], site.id)
            self.assertEqual((movie.status, movie.stage), ('completed', 3))
            event = session.scalar(select(Event))
            self.assertEqual(event.movie_id, movie.id)
            self.assertTrue(event.notified)
            self.assertEqual(session.get(JobState, 'check').last_run_at, datetime(2026, 10, 20, 10))
        self.assertEqual((stats['created'], stats['sites'], stats['jobs']), (1, 1, 1))

    def test_site_and_movie_ids_remap_and_same_site_configuration_is_updated(self):
        payload = export_data('full')
        self.clear()
        with db.session_scope() as session:
            session.add_all([
                Site(id=7, kind='nexusphp', name='Unrelated', base_url='https://other.example', identity_key='nexusphp:other.example', cookie='keep'),
                Site(id=21, kind='nexusphp', name='Old fixture', base_url='https://www.fixture.example/', identity_key='nexusphp:fixture.example', cookie='replace'),
                Movie(id=12, title='Unrelated movie', tmdb_id=999),
            ])
        import_data(self.encode(payload), 'full')
        second = import_data(self.encode(payload), 'full')
        with db.session_scope() as session:
            self.assertEqual(session.get(Site, 7).cookie, 'keep')
            self.assertEqual(session.get(Site, 21).cookie, 'uid=synthetic')
            movie = session.scalar(select(Movie).where(Movie.tmdb_id == 12345))
            self.assertNotEqual(movie.id, 12)
            self.assertEqual(movie.downloaded['site_id'], 21)
            self.assertEqual(movie.last_pt_candidates[0]['site_id'], 21)
            self.assertEqual(session.scalar(select(Event)).movie_id, movie.id)
            self.assertEqual(session.scalar(select(func.count()).select_from(Event)), 1)
            self.assertEqual(session.scalar(select(func.count()).select_from(Site)), 2)
        self.assertEqual((second['created'], second['merged'], second['events']), (0, 1, 0))

    def test_records_only_export_omits_configuration(self):
        payload = export_data()
        self.assertEqual(payload['scope'], 'records')
        for key in ('settings', 'sites', 'cookiecloud', 'jobs'):
            self.assertNotIn(key, payload)
        for secret in ('synthetic-email-password', 'synthetic-qb-password', 'synthetic-tmdb-key', 'synthetic-site-key'):
            self.assertNotIn(secret, json.dumps(payload))

    def test_records_only_import_of_full_file_keeps_current_configuration(self):
        payload = export_data('full')
        self.clear()
        current = AppSettings()
        current.email.password = 'keep-current'
        save_settings(current)
        with db.session_scope() as session:
            session.add(Site(id=7, kind='nexusphp', name='Unrelated', base_url='https://other.example', identity_key='nexusphp:other.example', cookie='keep'))
        import_data(self.encode(payload), 'records')
        self.assertEqual(load_settings().model_dump(), current.model_dump())
        with db.session_scope() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Site)), 1)
            self.assertEqual(session.get(Site, 7).cookie, 'keep')
            movie = session.scalar(select(Movie))
            self.assertEqual(movie.last_pt_candidates, [])
            self.assertIsNone(movie.downloaded['site_id'])
            self.assertIsNone(session.get(CookieCloudBlob, self.settings.cookiecloud.uuid))

    def test_legacy_record_backup_remains_importable_in_full_mode(self):
        payload = export_data()
        payload['version'] = 1
        del payload['scope']
        self.clear()
        stats = import_data(self.encode(payload), 'full')
        self.assertEqual(stats, {'created': 1, 'merged': 0, 'events': 1})

    def test_invalid_late_record_rolls_back_configuration_and_preserves_backup(self):
        payload = export_data('full')
        next(row for row in payload['settings'] if row['key'] == 'app')['value']['email']['password'] = 'do-not-restore'
        payload['events'][0]['created_at'] = 'invalid'
        with self.assertRaises(BackupError):
            import_data(self.encode(payload), 'full')
        self.assertEqual(load_settings().email.password, 'synthetic-email-password')
        backups = list(config.DATA_DIR.glob('before-import-*.db'))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as connection:
            stored = json.loads(connection.execute("SELECT value FROM settings WHERE key='app'").fetchone()[0])
        self.assertEqual(stored['email']['password'], 'synthetic-email-password')

    def test_corrupt_full_configuration_is_rejected_without_exposing_values(self):
        payload = export_data('full')
        next(row for row in payload['settings'] if row['key'] == 'app')['value']['collect']['max_age_months'] = 'synthetic-sensitive-value'
        with self.assertRaises(BackupError) as error:
            import_data(self.encode(payload), 'full')
        self.assertNotIn('synthetic-sensitive-value', str(error.exception))
        self.assertEqual(list(config.DATA_DIR.glob('before-import-*.db')), [])
        del payload['sites']
        with self.assertRaises(BackupError):
            import_data(self.encode(payload), 'full')

    def test_http_full_export_and_multipart_import_use_selected_scope(self):
        response = self.client.get('/data/export?scope=full')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertIn('full-backup', response.headers['content-disposition'])
        payload = response.content
        self.clear()
        response = self.client.post('/data/import', data={'scope': 'full'}, files={'file': ('backup.json', payload, 'application/json')}, headers={'HX-Request': 'true'})
        self.assertEqual(response.status_code, 200)
        message = json.loads(response.headers['HX-Trigger'])['toast']['message']
        self.assertIn('已恢复全部设置', message)
        self.assertEqual(load_settings().email.password, 'synthetic-email-password')
        self.assertEqual(self.client.get('/data/export?scope=invalid').status_code, 400)

    def test_repeated_import_of_movie_without_external_ids_does_not_duplicate(self):
        with db.session_scope() as session:
            session.add(Movie(title='Unlinked manual movie', status='watched', year=2020))
        payload = export_data('full')
        self.clear()
        import_data(self.encode(payload), 'full')
        stats = import_data(self.encode(payload), 'full')
        self.assertEqual(stats['created'], 0)
        with db.session_scope() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Movie)), 2)

    def test_settings_has_distinct_cloud_and_data_backup_sections(self):
        self.settings.backup.github_token = 'synthetic-cloud-token'
        save_settings(self.settings)
        response = self.client.get('/settings')
        self.assertEqual(response.status_code, 200)
        page = BeautifulSoup(response.text, 'html.parser')
        self.assertEqual(page.select_one('#backup h2').text, '云备份')
        self.assertEqual(page.select_one('#data-backup h2').text, '导入导出')
        self.assertEqual(len(page.select('#backup')), 1)
        self.assertEqual(page.select_one('.settings-nav a[href="#backup"]').text, '云备份')
        self.assertEqual(page.select_one('.settings-nav a[href="#data-backup"]').text, '导入导出')
        self.assertNotIn('synthetic-cloud-token', response.text)

    def test_cloud_ui_has_all_fields_once_and_disabled_targets_keep_saved_credentials(self):
        self.settings.backup.github_token = 'synthetic-keep-token'
        self.settings.backup.webdav_password = 'synthetic-keep-password'
        self.settings.backup.github_repo = 'fixture/private-backups'
        save_settings(self.settings)
        page = BeautifulSoup(self.client.get('/settings').text, 'html.parser')
        form = page.select_one('[data-cloud-backup]')
        ids = [element['id'] for element in page.select('[id]')]
        self.assertEqual(len(ids), len(set(ids)))
        controls = form.select('input[name],select[name]')
        names = [control['name'] for control in controls]
        self.assertCountEqual(names, list(type(self.settings.backup).model_fields))
        self.assertTrue(form.select_one('#cloud-github-body').has_attr('hidden'))
        self.assertTrue(form.select_one('#cloud-webdav-body').has_attr('hidden'))
        self.assertTrue(form.select_one('[data-backup-run]').has_attr('disabled'))
        data = {}
        for control in controls:
            if control.get('type') == 'checkbox':
                if control.has_attr('checked'):
                    data[control['name']] = 'on'
            elif control.name == 'select':
                data[control['name']] = control.select_one('option[selected]')['value']
            else:
                data[control['name']] = control.get('value', '')
        data['scope'] = 'full'
        # The browser disables only the irrelevant interval while scheduling is off.
        data.pop('interval_hours')
        with patch('reelready.web.routes.scheduler.trigger') as trigger:
            response = self.client.post('/settings/backup', data=data, headers={'HX-Request': 'true'})
        self.assertEqual(json.loads(response.headers['HX-Trigger'])['toast']['message'], '设置已保存')
        trigger.assert_not_called()
        restored = load_settings().backup
        self.assertEqual(restored.github_token, 'synthetic-keep-token')
        self.assertEqual(restored.webdav_password, 'synthetic-keep-password')
        self.assertEqual(restored.github_repo, 'fixture/private-backups')
        self.assertEqual(restored.scope, 'full')
        self.assertFalse(restored.github_enabled)
        self.assertFalse(restored.auto_enabled)

    def test_cloud_upload_uses_selected_scope_and_skips_export_without_targets(self):
        from reelready.services.cloud_backup import run_cloud_backup
        for scope in ('records', 'full'):
            settings = AppSettings()
            settings.backup.github_enabled = settings.backup.webdav_enabled = True
            settings.backup.scope = scope
            with patch('reelready.services.cloud_backup.export_data', return_value={'scope': scope}) as export, patch('reelready.services.cloud_backup._upload_github', return_value=0) as github, patch('reelready.services.cloud_backup._upload_webdav', return_value=0) as webdav:
                run_cloud_backup(settings)
            export.assert_called_once_with(scope)
            self.assertEqual(json.loads(github.call_args.args[-1])['scope'], scope)
            self.assertEqual(github.call_args.args[-1], webdav.call_args.args[-1])
        with patch('reelready.services.cloud_backup.export_data') as export:
            run_cloud_backup(AppSettings())
        export.assert_not_called()

    def test_compact_job_panel_still_shows_failures_and_contains_all_tasks(self):
        with db.session_scope() as session:
            session.add(JobState(name='pt_scan', last_run_at=datetime.now(), last_status='error', last_message='Synthetic downloader unavailable'))
        page = BeautifulSoup(self.client.get('/movies').text, 'html.parser')
        panel = page.select_one('#jobs')
        self.assertEqual(panel.name, 'details')
        self.assertFalse(panel.has_attr('open'))
        self.assertIn('1 项异常', panel.select_one('summary').get_text())
        self.assertIn('搜索 PT 并下载', panel.select_one('summary').get_text())
        self.assertEqual(len(panel.select('.job-grid .job')), 6)
        self.assertIn('Synthetic downloader unavailable', panel.get_text())
        self.assertIsNotNone(page.select_one('.movie-add-panel > summary'))
        fragment = BeautifulSoup(self.client.get('/jobs').text, 'html.parser')
        self.assertIsNotNone(fragment.select_one('details#jobs'))
