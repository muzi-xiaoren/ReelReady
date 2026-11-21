"""Explicit personal-evaluation UI actions. External writes require a preview token."""

import copy
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from ..db import session_scope
from ..models import Movie
from ..services import ratings
from ..settings import load_settings, update_section

router = APIRouter(prefix='/ratings')
_lock = threading.Lock()
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='rating-sync')
_plans = {}
_auth = {}
_TTL = 1800


class EvaluationInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rating: StrictInt | None = Field(default=None, ge=1, le=5)
    review: str | None = Field(default=None, max_length=10000)
    mark_watched: StrictBool = False


class PreviewInput(BaseModel):
    movie_id: StrictInt | None = Field(default=None, ge=1)
    platform: Literal['tmdb', 'douban'] = 'tmdb'


class ConfirmInput(BaseModel):
    choices: dict[str, str] = Field(default_factory=dict, max_length=100000)


@router.get('/movies/{movie_id}')
def get_evaluation(movie_id: int):
    with session_scope() as session:
        movie = session.get(Movie, movie_id)
        if movie is None:
            raise HTTPException(404, '影片已不存在')
        return ratings.evaluation(movie)


@router.post('/movies/{movie_id}')
def save_evaluation(movie_id: int, values: EvaluationInput):
    try:
        return ratings.save_evaluation(movie_id, values.model_dump(exclude_unset=True))
    except ratings.RatingError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get('/accounts')
def accounts():
    settings = load_settings()
    from ..services.douban_ratings import credentials
    try:
        credentials()
        douban_ready = True
    except Exception:
        douban_ready = False
    return dict(tmdb_connected=bool(settings.tmdb.session_id), tmdb_name=settings.tmdb.account_name,
                tmdb_configured=bool(settings.tmdb.api_key), douban_ready=douban_ready)


@router.get('/local')
def get_local(movie_id: int | None = None):
    return ratings.local_items(movie_id)


@router.post('/tmdb/connect')
def connect(request: Request):
    try:
        with ratings.account_client(require_session=False) as account:
            result = account.request('GET', '/authentication/token/new', authenticated=False)
        token = result.get('request_token')
        if not isinstance(token, str) or not token:
            raise ratings.RatingError('TMDB 未返回授权令牌')
    except ratings.RatingError as exc:
        raise HTTPException(400, str(exc)) from None
    state = secrets.token_urlsafe(24)
    with _lock:
        _expire(_auth)
        if len(_auth) >= 16:
            raise HTTPException(429, '待授权请求过多，请稍后重试')
        _auth[state] = dict(created=time.monotonic(), token=token)
    callback = str(request.url_for('tmdb_callback')) + '?' + urlencode({'state': state})
    return {'url': 'https://www.themoviedb.org/authenticate/' + token + '?' + urlencode({'redirect_to': callback})}


@router.get('/tmdb/callback', name='tmdb_callback')
def callback(state: str = '', request_token: str = '', approved: str = ''):
    with _lock:
        _expire(_auth)
        pending = _auth.pop(state, None)
    if not pending or not secrets.compare_digest(pending['token'], request_token) or approved != 'true':
        return RedirectResponse('/settings?rating_auth=cancelled#tmdb', status_code=303)
    try:
        with ratings.account_client(require_session=False) as account:
            result = account.request('POST', '/authentication/session/new',
                body={'request_token': request_token}, authenticated=False)
            session_id = result.get('session_id')
            if not isinstance(session_id, str) or not session_id:
                raise ratings.RatingError('账户授权未完成')
            account.session_id = session_id
            identity = account.request('GET', '/account')
        update_section('tmdb', lambda section: section.model_copy(update={
            'session_id': session_id, 'account_name': str(identity.get('username') or 'TMDB 用户')}))
    except ratings.RatingError:
        return RedirectResponse('/settings?rating_auth=failed#tmdb', status_code=303)
    return RedirectResponse('/settings?rating_auth=connected#tmdb', status_code=303)


@router.post('/tmdb/disconnect')
def disconnect():
    update_section('tmdb', lambda section: section.model_copy(update={'session_id': '', 'account_name': ''}))
    return {'ok': True}


def _expire(mapping):
    for key in list(mapping):
        if time.monotonic() - mapping[key]['created'] > _TTL and mapping[key].get('state') not in ('reading', 'writing'):
            del mapping[key]


def _run(plan_id, items=None, choices=None):
    try:
        with _lock:
            platform = _plans[plan_id]['platform']
        with ratings.account_client(platform=platform) as account:
            with _lock:
                if account.credential_key != _plans[plan_id]['account']:
                    raise ratings.RatingError('同步账户已改变，请重新预览')
                rows = copy.deepcopy(_plans[plan_id]['rows'])
            rows = ratings.compare_items(items, account) if items is not None else ratings.apply_items(rows, choices, account)
        with _lock:
            _plans[plan_id].update(rows=rows, state='ready' if items is not None else 'done')
    except Exception as exc:
        with _lock:
            _plans[plan_id].update(state='error', error=str(exc) if isinstance(exc, ratings.RatingError) else '同步未完成，请重新预览后重试')


@router.post('/preview')
def preview(values: PreviewInput):
    try:
        with ratings.account_client(platform=values.platform) as account:
            identity = account.credential_key
    except ratings.RatingError as exc:
        raise HTTPException(400, str(exc)) from None
    items = ratings.local_items(values.movie_id)
    with _lock:
        _expire(_plans)
        if any(p['state'] in ('reading', 'writing') for p in _plans.values()):
            raise HTTPException(409, '已有同步任务正在运行，请等待完成')
        if len(_plans) >= 8:
            del _plans[next(iter(_plans))]
        plan_id = secrets.token_urlsafe(24)
        _plans[plan_id] = dict(created=time.monotonic(), state='reading', rows=[], error='',
            account=identity, platform=values.platform, count=len(items), movie_id=values.movie_id)
    _worker.submit(_run, plan_id, items=items)
    return {'id': plan_id}


@router.get('/plans/{plan_id}')
def get_plan(plan_id: str):
    with _lock:
        _expire(_plans)
        plan = _plans.get(plan_id)
        if not plan:
            raise HTTPException(404, '预览已过期，请重新预览')
        return {key: copy.deepcopy(plan[key]) for key in ('state', 'rows', 'error', 'count')}


@router.get('/active')
def active_plan():
    with _lock:
        for key, plan in _plans.items():
            if plan['state'] in ('reading', 'writing'):
                return dict(id=key, movie_id=plan['movie_id'], platform=plan['platform'])
    return None


@router.post('/plans/{plan_id}/confirm')
def confirm(plan_id: str, values: ConfirmInput):
    with _lock:
        _expire(_plans)
        plan = _plans.get(plan_id)
        if not plan:
            raise HTTPException(404, '预览已过期，请重新预览')
        if plan['state'] != 'ready':
            raise HTTPException(409, '该预览已执行或仍在处理中，请重新预览')
        keys = {row['key'] for row in plan['rows']}
        if any(key not in keys or action not in ('add', 'overwrite', 'skip') for key, action in values.choices.items()):
            raise HTTPException(400, '同步选择无效')
        plan['state'] = 'writing'
    _worker.submit(_run, plan_id, choices=values.choices)
    return {'ok': True}
