"""Fetch full metadata without holding a database transaction during network I/O."""
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
import logging

from sqlalchemy import select

from ..db import session_scope
from ..models import Movie, now
from ..settings import load_settings
from ..sources.douban import DoubanError
from ..sources.tmdb import TMDBError
from .clients import make_douban, make_tmdb
from .movies import apply_douban, apply_tmdb

log = logging.getLogger(__name__)
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='movie-metadata')
_pending = set()
_lock = Lock()


def queue_metadata(movie_id: int) -> bool:
    with _lock:
        if movie_id in _pending or len(_pending) >= 16:
            return False
        _pending.add(movie_id)
        _pool.submit(_run, movie_id)
        return True


def metadata_pending(movie_id: int) -> bool:
    with _lock:
        return movie_id in _pending


def refresh_metadata(movie_id: int) -> None:
    with session_scope() as session:
        row = session.get(Movie, movie_id)
        if not row:
            return
        douban_id, tmdb_id, imdb_id, title, year = row.douban_id, row.tmdb_id, row.imdb_id, row.title, row.year
    settings = load_settings()
    douban_info = tmdb_info = None
    notes = []
    if douban_id:
        try:
            with make_douban() as client:
                douban_info = client.detail(douban_id)
        except DoubanError:
            notes.append('豆瓣资料暂时无法获取')
    client = make_tmdb(settings)
    if client:
        try:
            if not tmdb_id:
                imdb_id = imdb_id or (douban_info.imdb_id if douban_info else None)
                found = client.find_by_imdb(imdb_id) if imdb_id else client.find([title], year)
                if found and (imdb_id or (found.year == year and title in (found.title, found.original_title))):
                    tmdb_id = found.id
            if tmdb_id:
                tmdb_info = client.movie(tmdb_id)
        except TMDBError:
            notes.append('TMDB 资料暂时无法获取')
        finally:
            client.close()
    elif not douban_info:
        notes.append('可在设置中填写 TMDB API Key，补充演职员与各地区上映日期')
    with session_scope() as session:
        row = session.get(Movie, movie_id)
        if not row:
            return
        if tmdb_info:
            apply_tmdb(row, tmdb_info)
            if not row.tmdb_id and not session.scalar(select(Movie.id).where(Movie.tmdb_id == tmdb_id, Movie.id != movie_id)):
                row.tmdb_id = tmdb_id
        if douban_info:
            apply_douban(row, douban_info)
        details = dict(row.details or {})
        details['sources'] = [source for source, item in [('豆瓣', douban_info), ('TMDB', tmdb_info)] if item]
        details['message'] = '；'.join(notes) or ('资料已更新' if douban_info or tmdb_info else '暂未匹配到详细资料，可稍后重试')
        row.details = details
        row.metadata_checked_at = now()
    from .pt import revalidate_cached_candidates
    revalidate_cached_candidates(movie_ids=[movie_id])


def _run(movie_id: int) -> None:
    try:
        refresh_metadata(movie_id)
    except Exception:
        log.warning('Movie metadata refresh failed for %s', movie_id)
        with session_scope() as session:
            row = session.get(Movie, movie_id)
            if row:
                row.details = dict(row.details or {}, message='资料更新失败，请稍后重试；已保存的信息仍保留')
                row.metadata_checked_at = now()
    finally:
        with _lock:
            _pending.discard(movie_id)
