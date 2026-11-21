import copy
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from reelready import config, db
from reelready.models import Movie
from reelready.services import ratings, douban_ratings
from reelready.services.backup import export_data, import_data, BackupError
from reelready.settings import AppSettings, load_settings, save_settings
from reelready.web.app import create_app
from reelready.web import ratings as routes


def interest_html(rating='', review='', private=True):
    return {'interest_status': 'P', 'html': f'''<form class="a_interest_form" method="POST"
      action="https://movie.douban.com/j/subject/42/interest">
      <input name="interest" value="collect" checked><input name="rating" value="{rating}">
      <input name="tags" value="纪录片"><input name="private" type="checkbox" {'checked' if private else ''}>
      <textarea name="comment" maxlength="350">{review}</textarea></form>'''}


class PersonalRatingTests(unittest.TestCase):
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
            session.add(Movie(id=1, title='测试影片', tmdb_id=10, douban_id='42', status='monitoring'))
        self.client = TestClient(create_app())
        self.headers = {'HX-Request': 'true'}
        self.account = Mock(platform='tmdb', credential_key='synthetic-session')
        self.account.rating.return_value = None
        routes._plans.clear()
        routes._auth.clear()

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def post(self, path, values):
        return self.client.post('/ratings' + path, json=values, headers=self.headers)

    def save(self, **fields):
        return ratings.save_evaluation(1, fields)

    def rows(self):
        return ratings.compare_items(ratings.local_items(), self.account)

    def test_local_star_keeps_review_and_does_not_mark_watched_or_download(self):
        self.save(review='喜欢这部纪录片')
        with patch('reelready.services.pt.download') as download:
            result = self.post('/movies/1', {'rating': 4})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['review'], '喜欢这部纪录片')
        with db.session_scope() as session:
            self.assertEqual(session.get(Movie, 1).status, 'monitoring')
        download.assert_not_called()

    def test_explicit_watched_preserves_original_status(self):
        self.save(rating=3, mark_watched=True)
        self.save(rating=4, mark_watched=True)
        with db.session_scope() as session:
            movie = session.get(Movie, 1)
            self.assertEqual((movie.status, movie.watched_from_status), ('watched', 'monitoring'))

    def test_validation_and_csrf(self):
        for fields in ({'rating': 0}, {'rating': 6}, {'rating': True}, {'rating': '4'},
                       {'rating': 4.5}, {'review': 'x' * 10001}, {'mark_watched': 'yes'}, {'unknown': 1}):
            self.assertEqual(self.post('/movies/1', fields).status_code, 422)
        self.assertEqual(self.client.post('/ratings/movies/1', json={'rating': 4}).status_code, 403)
        self.assertEqual(self.post('/movies/999', {'rating': 4}).status_code, 400)

    def test_clear_local_never_calls_platform(self):
        self.save(rating=5, review='不错')
        self.post('/movies/1', {'rating': None, 'review': ''})
        self.assertEqual(ratings.local_items(), [])
        self.account.write_rating.assert_not_called()

    def test_saving_rating_never_waits_for_download_lock(self):
        with patch('reelready.services.pt._download_lock') as lock:
            self.save(rating=4, review='本地保存')
            lock.__enter__.assert_not_called()
            self.save(mark_watched=True)
            lock.__enter__.assert_called_once()

    def test_global_includes_all_categories_only_personal_fields(self):
        self.save(rating=4)
        with db.session_scope() as session:
            for n, status in enumerate(['watched', 'ignored', 'candidate', 'completed'], 2):
                session.add(Movie(id=n, title=status, status=status, personal_review='短评'))
            session.add(Movie(id=8, title='只有公共评分', tmdb_rating=9.9))
        self.assertEqual(len(ratings.local_items()), 5)
        self.assertEqual(len(ratings.local_items(1)), 1)
        self.assertNotIn('details', ratings.local_items()[0])

    def test_backup_and_migration_preserve_personal_fields(self):
        self.save(rating=4, review='值得一看')
        payload = export_data()
        with db.session_scope() as session:
            session.delete(session.get(Movie, 1))
        import_data(json.dumps(payload).encode())
        with db.session_scope() as session:
            movie = session.scalar(select(Movie))
            self.assertEqual((movie.personal_rating, movie.personal_review), (4, '值得一看'))
        db.init_db()
        db.init_db()
        with db.session_scope() as session:
            self.assertEqual(session.scalar(select(Movie)).personal_rating, 4)
        payload['movies'][0]['personal_rating'] = 9
        with self.assertRaises(BackupError):
            import_data(json.dumps(payload).encode())

    def test_old_database_gets_nullable_columns_without_losing_movies(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql('ALTER TABLE movies DROP COLUMN personal_rating')
            connection.exec_driver_sql('ALTER TABLE movies DROP COLUMN personal_review')
        db.init_db()
        with db.session_scope() as session:
            movie = session.get(Movie, 1)
            self.assertEqual(movie.title, '测试影片')
            self.assertIsNone(movie.personal_rating)

    def test_conflicts_default_to_skip_and_empty_platform_is_add(self):
        self.save(rating=4, review='保存在本地')
        self.account.rating.return_value = 6
        rows = self.rows()
        self.assertEqual([row['status'] for row in rows], ['conflict', 'unsupported'])
        ratings.apply_items(rows, {}, self.account)
        self.account.write_rating.assert_not_called()
        self.account.rating.return_value = None
        rows = self.rows()
        ratings.apply_items(rows, {}, self.account)
        self.account.write_rating.assert_called_once_with(10, 8)

    def test_remote_same_is_skipped_and_read_failure_never_adds(self):
        self.save(rating=4)
        self.account.rating.return_value = 8
        rows = self.rows()
        self.assertEqual(rows[0]['status'], 'same')
        ratings.apply_items(rows, {}, self.account)
        self.account.write_rating.assert_not_called()
        self.account.rating.side_effect = ratings.RatingError('读取失败')
        rows = self.rows()
        self.assertEqual(rows[0]['status'], 'error')
        ratings.apply_items(rows, {rows[0]['key']: 'overwrite'}, self.account)
        self.account.write_rating.assert_not_called()

    def test_recheck_stops_changed_local_remote_or_identity(self):
        self.save(rating=4)
        for change in ('local', 'remote', 'identity'):
            rows = self.rows()
            if change == 'local':
                self.save(rating=5)
            elif change == 'remote':
                self.account.rating.return_value = 6
            else:
                with db.session_scope() as session:
                    session.get(Movie, 1).tmdb_id = 99
            ratings.apply_items(rows, {}, self.account)
            self.account.write_rating.assert_not_called()
            self.save(rating=4)
            self.account.rating.return_value = None
            with db.session_scope() as session:
                session.get(Movie, 1).tmdb_id = 10

    def test_tmdb_http_errors_and_malformed_account_state_do_not_leak_secrets(self):
        for response in (httpx.Response(401), httpx.Response(200, json={}),
                         httpx.Response(200, json={'rated': True}), httpx.Response(302, headers={'Location':'https://example.com'})):
            def handle(request):
                return response
            with httpx.Client(transport=httpx.MockTransport(handle)) as client:
                account = ratings.TMDBAccount(Mock(_client=client, _params={'api_key':'secret-key'}), 'secret-session')
                with self.assertRaises(ratings.RatingError) as error:
                    account.rating(10)
                self.assertNotIn('secret', str(error.exception))

    def test_tmdb_write_is_blocked_after_disconnect(self):
        account = ratings.TMDBAccount(Mock(), 'old-session')
        with self.assertRaises(ratings.RatingError):
            account.write_rating(10, 8)
        account.client._client.request.assert_not_called()

    def test_single_use_confirmation_and_server_owned_preview(self):
        self.save(rating=4)
        @contextmanager
        def fake_account(**kwargs):
            yield self.account
        with patch.object(ratings, 'account_client', fake_account), patch.object(routes._worker, 'submit') as submit:
            plan_id = self.post('/preview', {}).json()['id']
            self.assertEqual(submit.call_count, 1)
            routes._run(plan_id, items=ratings.local_items())
            plan = self.client.get('/ratings/plans/' + plan_id).json()
            self.assertEqual(plan['state'], 'ready')
            self.assertNotIn('account', plan)
            self.assertEqual(self.post('/plans/' + plan_id + '/confirm', {'choices': {'999:rating':'overwrite'}}).status_code, 400)
            self.assertEqual(self.post('/plans/' + plan_id + '/confirm', {}).status_code, 200)
            active = self.client.get('/ratings/active').json()
            self.assertEqual((active['id'], active['platform'], active['movie_id']), (plan_id, 'tmdb', None))
            self.assertEqual(self.post('/plans/' + plan_id + '/confirm', {}).status_code, 409)
            routes._run(plan_id, choices={})
            self.account.write_rating.assert_called_once_with(10, 8)
            self.assertIsNone(self.client.get('/ratings/active').json())

    def test_switching_account_invalidates_preview_without_any_publish(self):
        routes._plans['plan'] = dict(platform='tmdb', account='previous-account', state='ready', rows=[])
        @contextmanager
        def fake_account(**kwargs):
            yield self.account
        with patch.object(ratings, 'account_client', fake_account):
            routes._run('plan', choices={})
        self.assertEqual(routes._plans['plan']['state'], 'error')
        self.account.write_rating.assert_not_called()

    def test_tmdb_authorization_callback_is_bound_to_unexpired_token(self):
        self.account.request.side_effect = [{'request_token': 'synthetic-token'},
            {'session_id': 'synthetic-session'}, {'username': '测试账户'}]
        @contextmanager
        def fake_account(**kwargs):
            yield self.account
        with patch.object(ratings, 'account_client', fake_account):
            result = self.post('/tmdb/connect', {})
            self.assertIn('https://www.themoviedb.org/authenticate/', result.json()['url'])
            state = next(iter(routes._auth))
            self.client.get('/ratings/tmdb/callback', params={'state': state, 'request_token':'synthetic-token', 'approved':'true'}, follow_redirects=False)
        self.assertEqual(load_settings().tmdb.account_name, '测试账户')
        account_response = self.client.get('/ratings/accounts').text
        self.assertNotIn('synthetic-session', account_response)
        self.post('/tmdb/disconnect', {})
        self.assertFalse(load_settings().tmdb.session_id)

    def test_invalid_callback_never_creates_session(self):
        routes._auth['state'] = {'created':0, 'token':'synthetic-token'}
        with patch.object(ratings, 'account_client') as account:
            response = self.client.get('/ratings/tmdb/callback?state=state&request_token=wrong&approved=true', follow_redirects=False)
        self.assertIn('cancelled', response.headers['location'])
        account.assert_not_called()

    def test_douban_fields_are_compared_separately_and_skip_preserves_remote_review(self):
        self.save(rating=4, review='本地短评')
        account = Mock(platform='douban')
        account.read.return_value = dict(rating=2, review='平台短评', tags='纪录片', private=True, max_length=350)
        rows = ratings.compare_items(ratings.local_items(), account)
        self.assertEqual([r['status'] for r in rows], ['conflict', 'conflict'])
        ratings.apply_items(rows, {'1:rating':'overwrite', '1:review':'skip'}, account)
        account.write.assert_called_once_with('42', 4, '平台短评', account.read.return_value)

    def test_douban_new_review_preserves_remote_rating_and_batches_one_post(self):
        self.save(rating=4, review='本地短评')
        account = Mock(platform='douban')
        account.read.return_value = dict(rating=2, review='', tags='', private=False, max_length=350)
        rows = ratings.compare_items(ratings.local_items(), account)
        ratings.apply_items(rows, {'1:rating':'skip'}, account)
        account.write.assert_called_once_with('42', 2, '本地短评', account.read.return_value)
        account.reset_mock()
        account.read.return_value['rating'] = None
        rows = ratings.compare_items(ratings.local_items(), account)
        ratings.apply_items(rows, {}, account)
        account.write.assert_called_once_with('42', 4, '本地短评', account.read.return_value)

    def test_douban_long_review_and_remote_change_are_never_posted(self):
        self.save(review='长' * 351)
        account = Mock(platform='douban')
        account.read.return_value = dict(rating=None, review='', tags='', private=False, max_length=350)
        rows = ratings.compare_items(ratings.local_items(), account)
        self.assertEqual(rows[0]['status'], 'error')
        ratings.apply_items(rows, {}, account)
        account.write.assert_not_called()
        self.save(review='短评')
        rows = ratings.compare_items(ratings.local_items(), account)
        account.read.return_value['review'] = '刚在豆瓣改过'
        ratings.apply_items(rows, {}, account)
        account.write.assert_not_called()

    def test_douban_preserves_latest_skipped_field(self):
        self.save(rating=4, review='本地短评')
        account = Mock(platform='douban')
        account.read.return_value = dict(rating=2, review='之前的评论', tags='', private=False, max_length=350)
        rows = ratings.compare_items(ratings.local_items(), account)
        account.read.return_value = dict(rating=2, review='用户刚改的评论', tags='新标签', private=True, max_length=350)
        ratings.apply_items(rows, {'1:rating':'overwrite', '1:review':'skip'}, account)
        account.write.assert_called_once_with('42', 4, '用户刚改的评论', account.read.return_value)

    def test_douban_form_sanitizes_submission_preserves_private_tags_no_broadcast(self):
        calls = []
        def handle(request):
            calls.append(request)
            if request.url.path == '/mine':
                return httpx.Response(200, text='<div class="nav-user-account"></div><a href="https://www.douban.com/accounts/logout">退出</a>')
            return httpx.Response(200, json=interest_html('3', '原短评') if request.method == 'GET' else {'r':0})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client, patch('reelready.services.douban_ratings.time.sleep'), patch.object(douban_ratings, 'credentials', return_value=('header','ck-value','UA')):
            import hashlib
            account = douban_ratings.DoubanAccount(client, 'ck-value', hashlib.sha256(b'header').hexdigest())
            previous = account.read('42')
            self.assertEqual((previous['rating'], previous['review']), (3, '原短评'))
            account.write('42', 4, previous['review'], previous)
        from urllib.parse import parse_qs
        data = parse_qs(calls[-1].content.decode())
        self.assertEqual(data['private'], ['on'])
        self.assertEqual(data['tags'], ['纪录片'])
        self.assertNotIn('share-shuo', data)
        self.assertEqual(data['comment'], ['原短评'])
        self.assertEqual(data['interest'], ['collect'])
        self.assertEqual({r.url.host for r in calls}, {'movie.douban.com'})

    def test_douban_login_block_and_html_changes_never_mean_empty(self):
        for code, html in [(418, ''), (200, '<a class="nav-login">登录</a>')]:
            with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(code, text=html))) as client:
                account = douban_ratings.DoubanAccount(client, 'ck', 'fingerprint')
                with self.assertRaises(ratings.RatingError):
                    account.read('42')
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'html':'login'}))) as client:
            account = douban_ratings.DoubanAccount(client, 'ck', 'fingerprint')
            account.login_verified = True
            with self.assertRaises(ratings.RatingError):
                account.read('42')

    def test_douban_refreshes_missing_csrf_token_from_authenticated_get(self):
        calls = []
        def handle(request):
            calls.append(request)
            if request.url.path == '/mine':
                return httpx.Response(200, headers={'Set-Cookie':'ck=refreshed-token; Path=/; Domain=.douban.com'},
                    text='<div class="nav-user-account"></div><a href="/accounts/logout">退出</a>')
            return httpx.Response(200, json=interest_html() if request.method == 'GET' else {'r':0})
        import hashlib
        with httpx.Client(transport=httpx.MockTransport(handle), headers={'Cookie':'dbcl2=synthetic'}) as client, patch.object(douban_ratings, 'credentials', return_value=('dbcl2=synthetic','','UA')), patch('reelready.services.douban_ratings.time.sleep'):
            account = douban_ratings.DoubanAccount(client, '', hashlib.sha256(b'dbcl2=synthetic').hexdigest())
            remote = account.read('42')
            self.assertTrue(account.login_verified)
            account.write('42', 4, '', remote)
        self.assertIn('ck=refreshed-token', calls[-1].headers['Cookie'])
        self.assertIn(b'ck=refreshed-token', calls[-1].content)

    def test_douban_real_fragment_controls_outside_form_are_parsed(self):
        payload = interest_html('4', '原来的短评')
        payload['html'] = payload['html'].replace('<input name="tags"', '</form><input name="tags"').replace('</textarea></form>', '</textarea>')
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
            account = douban_ratings.DoubanAccount(client, 'ck', 'fingerprint')
            account.login_verified = True
            value = account.read('42')
            self.assertEqual((value['rating'], value['review'], value['private'], value['tags']), (4, '原来的短评', True, '纪录片'))
