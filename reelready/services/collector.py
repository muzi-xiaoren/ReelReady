"""Collect highly rated movies that are currently in cinemas."""

import logging
from datetime import date

from ..db import session_scope
from ..models import Movie, MovieStatus, now
from ..settings import AppSettings
from ..sources.douban import DoubanClient, DoubanError, DoubanMovie
from ..sources.tmdb import TMDBClient, TMDBError, TMDBMovie
from .clients import make_douban, make_tmdb
from .events import add_event
from .checker import apply_availability
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
            if settings.collect.recent_enabled:
                try:
                    recent = _collect_recent(settings, douban, tmdb, notes)
                    created += recent
                    notes.append(f"近期高分补充 {recent} 部")
                except DoubanError as exc:
                    notes.append(f"近期高分补充失败: {exc}")
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


def _new_candidate(settings: AppSettings, movie_id: int, douban: DoubanClient, tmdb: TMDBClient | None,
                   tmdb_info: TMDBMovie | None = None, douban_info: DoubanMovie | None = None) -> bool:
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
        tmdb_id, douban_id = movie.tmdb_id, movie.douban_id
    # Reuse full collection responses; only query missing source metadata once.
    if tmdb_info is None and tmdb is not None and tmdb_id:
        try:
            tmdb_info = tmdb.movie(tmdb_id, settings.check.provider_regions)
        except TMDBError:
            log.info('Initial availability lookup unavailable for movie %s', movie_id)
    if douban_info is None and douban_id:
        try:
            douban_info = douban.detail(douban_id)
        except DoubanError:
            log.info('Initial Douban availability unavailable for movie %s', movie_id)
    with session_scope() as session:
        movie = session.get(Movie, movie_id)
        if movie is not None and movie.status == MovieStatus.CANDIDATE:
            apply_availability(session, movie, tmdb_info, douban_info)
    if movie is not None:
        cache_poster(movie, settings)
    return True


def _collect_douban(settings: AppSettings, douban: DoubanClient, tmdb: TMDBClient | None) -> int:
    return _collect_douban_items(settings, douban, tmdb, douban.now_showing(), now_showing=True)


def _collect_recent(settings: AppSettings, douban: DoubanClient, tmdb: TMDBClient | None,
                    notes: list[str] | None = None) -> int:
    from .collection_history import DiscoveryHistory
    history = DiscoveryHistory(settings)
    try:
        _collect_douban_items(settings, douban, tmdb, history.due_retries(),
                             now_showing=False, report=history.record)
        # Persist successful retries even if the first listing request fails.
        history.save()
        items = douban.recent_high_rated(history.first_year, history.last_year,
                                        settings.collect.douban_min_rating,
                                        progress=history.progress, checkpoint=history.save)
        _collect_douban_items(settings, douban, tmdb, history.feed_items(items),
                             now_showing=False, report=history.record)
        history.state.pop('last_error', None)
    except DoubanError as exc:
        history.state['last_error'] = {'at': now().isoformat(), 'reason': str(exc)[:300]}
        if notes is not None:
            notes.append(f'近期补充部分失败: {exc}')
        else:
            raise
    finally:
        history.save()
    if notes is not None:
        notes.append(history.summary())
    return history.added


def _collect_douban_items(settings: AppSettings, douban: DoubanClient, tmdb: TMDBClient | None, items, *, now_showing: bool, report=None) -> int:
    created = 0
    def record(item, outcome, reason):
        if report:
            report(item, outcome, reason)
    for item in items:
        # Missing feed statistics are unknown, not proof that a movie fails.
        if ((item.rating is not None and item.rating < settings.collect.douban_min_rating) or
                (item.votes is not None and item.votes < settings.collect.douban_min_votes)):
            record(item, 'filtered', '列表评分或评价人数未达到设置门槛')
            continue
        with session_scope() as session:
            if find_existing(session, douban_id=item.id) is not None:
                upsert_candidate_douban(session, item)
                record(item, 'existing', '已有影片记录，保留原分类')
                continue
        try:
            detail = douban.detail(item.id)
        except DoubanError as exc:
            log.info("Skipping unverified Douban candidate %s: %s", item.id, exc)
            record(item, 'retry', f'详情请求失败: {str(exc)[:300]}；下轮到期后重试')
            continue
        if not _douban_qualifies(settings, detail, now_showing=now_showing):
            reason = '详情评分或评价人数未达到设置门槛'
            if not within_collection_window(settings, detail.year, detail.release_date):
                reason = '上映日期不在收集范围内'
            record(detail, 'filtered', reason)
            continue
        detail.details['is_rerelease'] = not within_collection_window(settings, detail.year, detail.release_date)
        detail.details['collection_now_showing'] = now_showing
        with session_scope() as session:
            movie, is_new = upsert_candidate_douban(session, detail)
            movie_id = movie.id
        if is_new and _new_candidate(settings, movie_id, douban, tmdb, douban_info=detail):
            created += 1
            record(detail, 'added', '已加入待确认')
        else:
            record(detail, 'merged', '已合并到已有影片，或关联后的上映日期不符合范围')
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
            detail = tmdb.movie(item.id, settings.check.provider_regions)
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
        if is_new and _new_candidate(settings, movie_id, douban, tmdb, tmdb_info=detail):
            created += 1
    return created
