"""Collect highly rated movies that are currently in cinemas."""

import logging

from ..db import session_scope
from ..models import Movie, MovieStatus
from ..settings import AppSettings
from ..sources.douban import DoubanClient, DoubanError, DoubanMovie
from ..sources.tmdb import TMDBClient, TMDBError, TMDBMovie
from .clients import make_douban, make_tmdb
from .events import add_event
from .movies import (
    cache_poster,
    link_ids,
    within_collection_window,
    ratings_text,
    upsert_candidate_douban,
    upsert_candidate_tmdb,
)

log = logging.getLogger(__name__)


def run_collect(settings: AppSettings) -> str:
    created = 0
    notes: list[str] = []
    tmdb = make_tmdb(settings)
    try:
        with make_douban() as douban:
            if settings.collect.douban_enabled:
                try:
                    created += _collect_douban(settings, douban, tmdb)
                except DoubanError as exc:
                    notes.append(f"豆瓣失败: {exc}")
            if settings.collect.tmdb_enabled:
                if tmdb is None:
                    notes.append("未配置 TMDB API Key，跳过 TMDB")
                else:
                    created += _collect_tmdb(settings, douban, tmdb, notes)
    finally:
        if tmdb is not None:
            tmdb.close()
    return "；".join([f"新增 {created} 部待确认影片", *notes])


def _douban_qualifies(settings: AppSettings, item: DoubanMovie) -> bool:
    c = settings.collect
    return (
        within_collection_window(settings, item.year, item.release_date)
        and item.rating is not None
        and item.rating >= c.douban_min_rating
        and (item.votes or 0) >= c.douban_min_votes
    )


def _tmdb_qualifies(settings: AppSettings, item: TMDBMovie) -> bool:
    c = settings.collect
    return (
        within_collection_window(settings, item.year, item.release_date)
        and item.rating is not None
        and item.rating >= c.tmdb_min_rating
        and (item.votes or 0) >= c.tmdb_min_votes
    )


def _new_candidate(settings: AppSettings, movie_id: int, douban: DoubanClient, tmdb: TMDBClient | None) -> bool:
    """Link ids of a freshly collected movie; return False if it merged into an existing record."""
    with session_scope() as session:
        movie = session.get(Movie, movie_id)
        movie = link_ids(session, movie, tmdb, douban)
        if movie.id != movie_id or movie.status != MovieStatus.CANDIDATE:
            return False
        add_event(
            session,
            "candidate",
            f"《{movie.title}》{f'({movie.year})' if movie.year else ''}",
            ratings_text(movie),
            movie_id=movie.id,
        )
    cache_poster(movie, settings)
    return True


def _collect_douban(settings: AppSettings, douban: DoubanClient, tmdb: TMDBClient | None) -> int:
    created = 0
    for item in douban.now_showing():
        if not _douban_qualifies(settings, item):
            continue
        with session_scope() as session:
            movie, is_new = upsert_candidate_douban(session, item)
            movie_id = movie.id
        if is_new and _new_candidate(settings, movie_id, douban, tmdb):
            created += 1
    return created


def _collect_tmdb(settings: AppSettings, douban: DoubanClient, tmdb: TMDBClient, notes: list[str]) -> int:
    seen: dict[int, TMDBMovie] = {}
    for region in settings.collect.tmdb_regions:
        try:
            for item in tmdb.now_playing(region.strip().upper()):
                seen.setdefault(item.id, item)
        except TMDBError as exc:
            notes.append(f"TMDB {region} 失败: {exc}")
            break
    created = 0
    for item in seen.values():
        if not _tmdb_qualifies(settings, item):
            continue
        with session_scope() as session:
            movie, is_new = upsert_candidate_tmdb(session, item)
            movie_id = movie.id
        if is_new and _new_candidate(settings, movie_id, douban, tmdb):
            created += 1
    return created
