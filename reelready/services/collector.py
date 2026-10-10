"""Collect highly rated movies that are currently in cinemas."""

import logging
from datetime import date

from ..db import session_scope
from ..models import Movie, MovieStatus
from ..settings import AppSettings
from ..sources.douban import DoubanClient, DoubanError, DoubanMovie
from ..sources.tmdb import TMDBClient, TMDBError, TMDBMovie
from .clients import make_douban, make_tmdb
from .events import add_event
from .movies import (
    cache_poster,
    find_existing,
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


def _released(year: int | None, release_date: str | None) -> bool:
    if release_date:
        try:
            return date.fromisoformat(release_date[:10]) <= date.today()
        except ValueError:
            pass
    return year is not None and year <= date.today().year


def _recent_regional_release(settings: AppSettings, item: TMDBMovie) -> bool:
    return any(
        within_collection_window(settings, None, entry.get('date'))
        for entry in item.details.get('collection_releases') or []
    )


def _douban_qualifies(settings: AppSettings, item: DoubanMovie, *, now_showing: bool = False) -> bool:
    c = settings.collect
    return (
        (within_collection_window(settings, item.year, item.release_date) or (
            c.include_rereleases and now_showing and _released(item.year, item.release_date)
        ))
        and item.rating is not None
        and item.rating >= c.douban_min_rating
        and (item.votes or 0) >= c.douban_min_votes
    )


def _tmdb_qualifies(settings: AppSettings, item: TMDBMovie) -> bool:
    c = settings.collect
    return (
        (within_collection_window(settings, item.year, item.release_date) or (
            c.include_rereleases and _released(item.year, item.release_date) and _recent_regional_release(settings, item)
        ))
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
        details = movie.details or {}
        # Linking can replace provisional feed data; check the final primary date again.
        if not within_collection_window(settings, movie.year, details.get('release_date')) and not (
            settings.collect.include_rereleases and details.get('is_rerelease')
            and _released(movie.year, details.get('release_date'))
        ):
            session.delete(movie)
            return False
        message = ratings_text(movie)
        if details.get('is_rerelease'):
            message += f" · 重映收集（首映年份 {movie.year or '未知'}）"
        add_event(
            session,
            "candidate",
            f"《{movie.title}》{f'({movie.year})' if movie.year else ''}",
            message,
            movie_id=movie.id,
        )
    cache_poster(movie, settings)
    return True


def _collect_douban(settings: AppSettings, douban: DoubanClient, tmdb: TMDBClient | None) -> int:
    created = 0
    for item in douban.now_showing():
        if item.rating is None or item.rating < settings.collect.douban_min_rating or (item.votes or 0) < settings.collect.douban_min_votes:
            continue
        with session_scope() as session:
            if find_existing(session, douban_id=item.id) is not None:
                upsert_candidate_douban(session, item)
                continue
        try:
            detail = douban.detail(item.id)
        except DoubanError as exc:
            log.info("Skipping unverified Douban candidate %s: %s", item.id, exc)
            continue
        if not _douban_qualifies(settings, detail, now_showing=True):
            continue
        detail.details['is_rerelease'] = not within_collection_window(settings, detail.year, detail.release_date)
        detail.details['collection_now_showing'] = True
        with session_scope() as session:
            movie, is_new = upsert_candidate_douban(session, detail)
            movie_id = movie.id
        if is_new and _new_candidate(settings, movie_id, douban, tmdb):
            created += 1
    return created


def _collect_tmdb(settings: AppSettings, douban: DoubanClient, tmdb: TMDBClient, notes: list[str]) -> int:
    seen: dict[int, TMDBMovie] = {}
    for region in settings.collect.tmdb_regions:
        try:
            for item in tmdb.now_playing(region.strip().upper()):
                releases = item.details.get('collection_releases') or [{'region': region.upper(), 'date': item.release_date}]
                previous = seen.get(item.id)
                if previous is None:
                    item.details['collection_releases'] = list(releases)
                    seen[item.id] = item
                else:
                    merged = previous.details['collection_releases']
                    merged.extend(entry for entry in releases if entry not in merged)
        except TMDBError as exc:
            notes.append(f"TMDB {region} 失败: {exc}")
            break
    created = 0
    for item in seen.values():
        if item.rating is None or item.rating < settings.collect.tmdb_min_rating or (item.votes or 0) < settings.collect.tmdb_min_votes:
            continue
        if not (within_collection_window(settings, item.year, item.release_date) or _recent_regional_release(settings, item)):
            continue
        with session_scope() as session:
            if find_existing(session, tmdb_id=item.id) is not None:
                upsert_candidate_tmdb(session, item)
                continue
        # Fetch once per new movie, outside a DB transaction. The list's date is regional.
        try:
            detail = tmdb.movie(item.id)
        except TMDBError as exc:
            log.info("Skipping unverified TMDB candidate %s: %s", item.id, exc)
            continue
        detail.details['collection_releases'] = item.details['collection_releases']
        if not _tmdb_qualifies(settings, detail):
            continue
        detail.details['is_rerelease'] = not within_collection_window(settings, detail.year, detail.release_date)
        with session_scope() as session:
            movie, is_new = upsert_candidate_tmdb(session, detail)
            movie_id = movie.id
        if is_new and _new_candidate(settings, movie_id, douban, tmdb):
            created += 1
    return created
