"""Personal evaluations and explicit, compare-before-write TMDB synchronization."""

from contextlib import contextmanager, nullcontext
from typing import Any

import httpx
from sqlalchemy import or_, select

from ..db import session_scope
from ..models import Movie, MovieStatus
from ..settings import load_settings
from ..sources.tmdb import API
from .clients import make_tmdb


class RatingError(Exception):
    pass


def validate_evaluation(rating, review):
    if rating is not None and (type(rating) is not int or not 1 <= rating <= 5):
        raise RatingError('请选择 1 至 5 星，或清除评分')
    if review is not None and (not isinstance(review, str) or len(review) > 10000):
        raise RatingError('评论最多 10000 字')


def evaluation(movie):
    return dict(id=movie.id, title=movie.title, rating=movie.personal_rating,
                review=movie.personal_review or '', douban_url=movie.douban_url,
                tmdb_id=movie.tmdb_id)


def save_evaluation(movie_id, values):
    # Share the download guard only when the user explicitly pauses monitoring.
    from .pt import _download_lock
    with (_download_lock if values.get('mark_watched') else nullcontext()), session_scope() as session:
        movie = session.get(Movie, movie_id)
        if movie is None:
            raise RatingError('影片已不存在')
        rating = values.get('rating', movie.personal_rating)
        review = values.get('review', movie.personal_review)
        validate_evaluation(rating, review)
        movie.personal_rating = rating
        movie.personal_review = (review or '').strip() or None
        if values.get('mark_watched') and movie.status != MovieStatus.WATCHED:
            movie.watched_from_status = movie.status
            movie.status = MovieStatus.WATCHED
        return evaluation(movie)


@contextmanager
def account_client(require_session=True, platform='tmdb'):
    if platform == 'douban':
        from .douban_ratings import account_client as douban_account
        with douban_account() as account:
            yield account
        return
    settings = load_settings()
    if require_session and not settings.tmdb.session_id:
        raise RatingError('请先连接你的 TMDB 账户')
    client = make_tmdb(settings)
    if client is None:
        raise RatingError('请先在设置中填写 TMDB API Key')
    try:
        yield TMDBAccount(client, settings.tmdb.session_id)
    finally:
        client.close()


class TMDBAccount:
    platform = 'tmdb'

    def __init__(self, client, session_id):
        self.client = client
        self.session_id = session_id

    @property
    def credential_key(self):
        return self.session_id

    def request(self, method, path, *, body=None, authenticated=True):
        params = dict(self.client._params)
        if authenticated:
            params['session_id'] = self.session_id
        try:
            response = self.client._client.request(method, API + path, params=params, json=body, follow_redirects=False)
            if response.is_redirect:
                raise RatingError('TMDB 返回意外跳转，已停止同步')
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get('success') is False:
                raise RatingError('TMDB 未接受请求，请检查账户授权后重试')
            return data
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            raise RatingError(f'TMDB 请求失败（HTTP {code}），请检查授权或稍后重试') from None
        except (httpx.HTTPError, ValueError):
            # Never expose session IDs or API keys embedded in request URLs.
            raise RatingError('TMDB 连接失败，请检查代理或网络后重试') from None

    def rating(self, movie_id):
        data = self.request('GET', f'/movie/{movie_id}/account_states')
        if 'rated' not in data:
            raise RatingError('TMDB 未返回个人评分，已停止同步')
        rated = data['rated']
        if rated is False:
            return None
        value = rated.get('value') if isinstance(rated, dict) else None
        if type(value) not in (float, int) or not 0.5 <= value <= 10:
            raise RatingError('TMDB 个人评分格式异常，已停止同步')
        return value

    def write_rating(self, movie_id, value):
        if load_settings().tmdb.session_id != self.session_id:
            raise RatingError('同步账户已断开或改变，请重新连接并预览')
        self.request('POST', f'/movie/{movie_id}/rating', body={'value': value})


def local_items(movie_id=None):
    # Select only personal fields rather than loading cached torrent/metadata JSON.
    query = select(Movie.id, Movie.title, Movie.tmdb_id, Movie.douban_id, Movie.personal_rating, Movie.personal_review)
    query = query.where(or_(Movie.personal_rating.is_not(None), Movie.personal_review.is_not(None)))
    if movie_id is not None:
        query = query.where(Movie.id == movie_id)
    with session_scope() as session:
        return [dict(row._mapping) for row in session.execute(query.order_by(Movie.id))]


def compare_items(items, account):
    if account.platform == 'douban':
        from .douban_ratings import compare_items as compare_douban
        return compare_douban(items, account)
    rows = []
    for item in items:
        mid = item['id']
        if item['personal_rating'] is not None:
            row: dict[str, Any] = dict(key=f'{mid}:rating', movie_id=mid, title=item['title'],
                field='rating', local=item['personal_rating'] * 2, remote=None,
                tmdb_id=item['tmdb_id'], status='new', result='')
            if not item['tmdb_id']:
                row.update(status='error', result='缺少 TMDB 影片编号，未同步')
            else:
                try:
                    row['remote'] = account.rating(item['tmdb_id'])
                    row['status'] = 'new' if row['remote'] is None else (
                        'same' if row['local'] == row['remote'] else 'conflict')
                except RatingError as exc:
                    row.update(status='error', result=str(exc))
            rows.append(row)
        if item['personal_review']:
            rows.append(dict(key=f'{mid}:review', movie_id=mid, title=item['title'], field='review',
                local=item['personal_review'], remote=None, status='unsupported',
                result='TMDB 暂未提供评论写入接口，评论仅保存在本地'))
    return rows


def apply_items(rows, choices, account):
    if account.platform == 'douban':
        from .douban_ratings import apply_items as apply_douban
        return apply_douban(rows, choices, account)
    for row in rows:
        action = choices.get(row['key'], 'add' if row['status'] == 'new' else 'skip')
        if row['status'] not in ('new', 'conflict') or action == 'skip':
            if not row['result']:
                row['result'] = '已相同，无需同步' if row['status'] == 'same' else '已跳过'
            continue
        if action != ('add' if row['status'] == 'new' else 'overwrite'):
            row['result'] = '操作无效，已跳过'
            continue
        try:
            with session_scope() as session:
                movie = session.get(Movie, row['movie_id'])
                valid = movie is not None and movie.tmdb_id == row['tmdb_id'] and (
                    movie.personal_rating is not None and movie.personal_rating * 2 == row['local'])
            if not valid:
                raise RatingError('本地评价已改变，请重新预览')
            remote = account.rating(row['tmdb_id'])
            if remote == row['local']:
                row['result'] = '已相同，无需同步'
                continue
            if remote != row['remote']:
                raise RatingError('平台评分已改变，请重新预览后决定是否覆盖')
            account.write_rating(row['tmdb_id'], row['local'])
            row['result'] = '同步成功'
        except RatingError as exc:
            row['result'] = str(exc)
    return rows
