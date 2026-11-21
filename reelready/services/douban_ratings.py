"""The authenticated Douban interest form. Never interpret a blocked page as empty."""

import hashlib
import re
import time
from contextlib import contextmanager
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from ..cookiecloud import cookie_header_for
from ..http import make_client
from ..settings import load_settings
from .sites import load_cookiecloud

ORIGIN = 'https://movie.douban.com'


def credentials():
    from .ratings import RatingError
    settings = load_settings()
    data, _ = load_cookiecloud(settings)
    header = cookie_header_for(data or {}, ORIGIN) or ''
    cookie = SimpleCookie()
    try:
        cookie.load(header)
    except Exception:
        raise RatingError('豆瓣登录信息无效，请重新登录并同步 CookieCloud') from None
    if not cookie.get('dbcl2'):
        raise RatingError('请先在浏览器登录豆瓣，并同步 CookieCloud')
    return header, cookie['ck'].value if cookie.get('ck') else '', settings.network.user_agent


@contextmanager
def account_client():
    header, ck, user_agent = credentials()
    with make_client(proxy=None, trust_env=False, headers={
        'Cookie': header, 'User-Agent': user_agent, 'Referer': ORIGIN + '/',
        'Origin': ORIGIN}, timeout=20) as client:
        yield DoubanAccount(client, ck, hashlib.sha256(header.encode()).hexdigest())


class DoubanAccount:
    platform = 'douban'

    def __init__(self, client, ck, credential_key):
        self.client, self.ck, self.credential_key = client, ck, credential_key
        self.last_request = 0
        self.login_verified = False

    def ensure_login(self):
        from .ratings import RatingError
        if self.login_verified:
            return
        try:
            response = self.client.get(ORIGIN + '/mine', follow_redirects=False)
            self.last_request = time.monotonic()
            location = urlsplit(response.headers.get('location', ''))
            own_profile = response.status_code in (301, 302, 303) and location.hostname in (None, 'movie.douban.com') and location.path.startswith('/people/')
            soup = BeautifulSoup(response.text, 'html.parser') if response.status_code == 200 else None
            if not own_profile and not (soup is not None and soup.select_one('.nav-user-account') and soup.select_one('a[href*="accounts/logout"]')):
                raise RatingError('豆瓣登录已失效或需要验证，请在浏览器登录后同步 CookieCloud')
            # CookieCloud may omit an expired ck. Douban issues a fresh CSRF token
            # on this authenticated GET; bind the outgoing header and form to it.
            fresh_ck = response.cookies.get('ck')
            if fresh_ck:
                self.ck = fresh_ck
                parts = [part.strip() for part in self.client.headers.get('Cookie', '').split(';')
                         if part.strip() and part.partition('=')[0].strip() != 'ck']
                self.client.headers['Cookie'] = '; '.join([*parts, 'ck=' + fresh_ck])
            if not self.ck:
                raise RatingError('豆瓣未返回防伪令牌，请在浏览器重新登录后同步 CookieCloud')
            self.login_verified = True
        except httpx.HTTPError:
            raise RatingError('无法验证豆瓣登录，未读取或覆盖任何评价') from None

    @staticmethod
    def path(movie_id):
        from .ratings import RatingError
        if not re.fullmatch(r'\d+', str(movie_id)):
            raise RatingError('豆瓣影片编号无效')
        return f'/j/subject/{movie_id}/interest'

    def request(self, method, movie_id, data=None):
        from .ratings import RatingError
        try:
            time.sleep(max(0, .8 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            response = self.client.request(method, ORIGIN + self.path(movie_id), data=data,
                headers={'Referer': f'{ORIGIN}/subject/{movie_id}/'}, follow_redirects=False)
            if response.is_redirect:
                raise RatingError('豆瓣要求重新登录或验证，请在浏览器完成后同步 CookieCloud')
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise RatingError('豆瓣返回异常，未同步')
            if result.get('r') not in (None, 0) or result.get('error'):
                raise RatingError('豆瓣未接受请求，请在浏览器检查登录或验证码')
            return result
        except httpx.HTTPStatusError as exc:
            raise RatingError(f'豆瓣请求失败（HTTP {exc.response.status_code}），请在浏览器完成登录或验证后重试') from None
        except (httpx.HTTPError, ValueError):
            raise RatingError('豆瓣连接失败，未读取或覆盖任何评价') from None

    def read(self, movie_id):
        from .ratings import RatingError
        self.ensure_login()
        data = self.request('GET', movie_id)
        if not isinstance(data.get('html'), str) or 'interest_status' not in data:
            raise RatingError('豆瓣未返回个人评价表单，请重新登录后同步 CookieCloud')
        soup = BeautifulSoup(data['html'], 'html.parser')
        form = soup.select_one('form.a_interest_form')
        if form is None or len(soup.select('form')) != 1 or form.get('action') != ORIGIN + self.path(movie_id) or form.get('method', '').upper() != 'POST':
            raise RatingError('豆瓣评价表单已变化，已停止同步')
        # The real fragment closes its form before #advtags. These controls are
        # siblings, so require a single verified form and unique typed controls.
        controls = []
        for selector in ('input[name="rating"]', 'textarea[name="comment"]',
                         'input[name="tags"]', 'input[name="private"]'):
            matches = soup.select(selector)
            if len(matches) != 1:
                raise RatingError('豆瓣评价字段不完整或重复，已停止同步')
            controls.append(matches[0])
        rating, comment, tags, privacy = controls
        value = rating.get('value', '')
        if value not in ('', '0', '1', '2', '3', '4', '5'):
            raise RatingError('豆瓣个人评分异常，已停止同步')
        interest = form.select_one('input[name="interest"][checked]')
        interest = interest.get('value') if interest else None
        if interest not in (None, 'wish', 'collect', 'do'):
            raise RatingError('豆瓣观看状态异常，已停止同步')
        maximum = comment.get('maxlength') or '350'
        if not str(maximum).isdigit():
            raise RatingError('豆瓣短评长度规则已变化，已停止同步')
        return dict(rating=int(value) if value not in ('', '0') else None,
            review=comment.get_text(), tags=tags.get('value', ''), private=privacy.has_attr('checked'),
            private_value=privacy.get('value', 'on'),
            foldcollect=(soup.select_one('input[name="foldcollect"]') or {}).get('value', ''),
            interest=interest, max_length=int(maximum))

    def write(self, movie_id, rating, review, previous):
        from .ratings import RatingError
        # Do not send an old login's plan using a newly synced account.
        header, _, _ = credentials()
        if hashlib.sha256(header.encode()).hexdigest() != self.credential_key:
            raise RatingError('豆瓣登录信息已改变，请重新预览')
        if len(review) > previous['max_length']:
            raise RatingError(f'豆瓣短评最多 {previous["max_length"]} 字，请缩短后重试')
        payload = dict(ck=self.ck, interest='collect', rating=rating or '', comment=review,
                      tags=previous['tags'], foldcollect=previous.get('foldcollect', ''))
        if previous['private']:
            payload['private'] = previous.get('private_value', 'on')
        # Omitting share-shuo prevents posting a separate broadcast.
        result = self.request('POST', movie_id, payload)
        if result.get('r') != 0:
            raise RatingError('豆瓣未确认提交结果，请重新预览核对；不会自动重复发布')


def compare_items(items, account):
    from .ratings import RatingError
    rows = []
    for item in items:
        try:
            if not item['douban_id']:
                raise RatingError('缺少豆瓣影片编号，未同步')
            remote = account.read(item['douban_id'])
            error = ''
        except RatingError as exc:
            remote, error = {}, str(exc)
        for field, value in [('rating', item['personal_rating']), ('review', item['personal_review'])]:
            if value in (None, ''):
                continue
            old = remote.get(field)
            row = dict(key=f'{item["id"]}:{field}', movie_id=item['id'], title=item['title'], field=field,
                local=value, remote=old, douban_id=item['douban_id'], result=error, platform='douban',
                status='error' if error else 'new' if old in (None, '') else 'same' if old == value else 'conflict')
            if field == 'review' and not error and len(value) > remote['max_length']:
                row.update(status='error', result=f'豆瓣短评最多 {remote["max_length"]} 字，未截断或提交')
            rows.append(row)
    return rows


def apply_items(rows, choices, account):
    from ..db import session_scope
    from ..models import Movie
    from .ratings import RatingError
    groups = {}
    for row in rows:
        action = choices.get(row['key'], 'add' if row['status'] == 'new' else 'skip')
        if row['status'] not in ('new', 'conflict') or action != ('add' if row['status'] == 'new' else 'overwrite'):
            if not row['result']:
                row['result'] = '已相同，无需同步' if row['status'] == 'same' else '已跳过'
            continue
        groups.setdefault(row['movie_id'], []).append(row)
    for mid, selected in groups.items():
        try:
            remote = account.read(selected[0]['douban_id'])
            changed = []
            desired = dict(remote)
            with session_scope() as session:
                movie = session.get(Movie, mid)
                for row in selected:
                    field = row['field']
                    if movie is None or movie.douban_id != row['douban_id'] or getattr(movie, 'personal_' + field) != row['local']:
                        raise RatingError('本地评价已改变，请重新预览')
                    if remote[field] == row['local']:
                        row['result'] = '已相同，无需同步'
                        continue
                    if (remote[field] or None) != (row['remote'] or None):
                        raise RatingError('豆瓣评价已改变，请重新预览后决定是否覆盖')
                    desired[field] = row['local']
                    changed.append(row)
            if changed:
                account.write(selected[0]['douban_id'], desired['rating'], desired['review'], remote)
                for row in changed:
                    row['result'] = '同步成功'
        except RatingError as exc:
            for row in selected:
                if not row['result']:
                    row['result'] = str(exc)
    return rows
