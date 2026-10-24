"""Detect when monitored movies get a digital release date or land on streaming platforms."""

import logging
import re

from sqlalchemy import select

from ..db import session_scope
from ..models import Movie, MovieStatus, Stage, now
from ..settings import AppSettings
from ..sources.douban import DoubanClient, DoubanError, DoubanMovie
from ..sources.tmdb import TMDBClient, TMDBError, TMDBMovie
from .clients import make_douban, make_tmdb
from .events import add_event
from .movies import apply_tmdb, assign_ids, link_ids, same_douban_movie

log = logging.getLogger(__name__)

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def monitored_ids(movie_ids: list[int] | None = None) -> list[int]:
    with session_scope() as session:
        query = select(Movie.id).where(Movie.status == MovieStatus.MONITORING)
        if movie_ids:
            query = query.where(Movie.id.in_(movie_ids))
        return list(session.scalars(query.order_by(Movie.id)))


def run_check(settings: AppSettings, movie_ids: list[int] | None = None) -> str:
    ids = monitored_ids(movie_ids)
    if not ids:
        return "没有监测中的影片"
    advanced = 0
    errors = 0
    tmdb = make_tmdb(settings)
    try:
        with make_douban() as douban:
            for movie_id in ids:
                try:
                    if _check_one(settings, movie_id, tmdb, douban):
                        advanced += 1
                except Exception:
                    errors += 1
                    log.exception("Check failed for movie %s", movie_id)
    finally:
        if tmdb is not None:
            tmdb.close()
    from .pt import revalidate_cached_candidates
    revalidate_cached_candidates(movie_ids=ids)
    summary = f"检测 {len(ids)} 部，{advanced} 部状态有更新"
    return f"{summary}，{errors} 部出错" if errors else summary


def _check_one(settings: AppSettings, movie_id: int, tmdb: TMDBClient | None, douban: DoubanClient) -> bool:
    with session_scope() as session:
        movie = session.get(Movie, movie_id)
        if movie is None or movie.status != MovieStatus.MONITORING:
            return False
        if not (movie.tmdb_id and movie.douban_id):
            movie = link_ids(session, movie, tmdb, douban)

        digital_dates: list[str] = []
        streaming: list[str] = []
        if tmdb is not None and movie.tmdb_id:
            try:
                detail = tmdb.movie(movie.tmdb_id, settings.check.provider_regions)
                apply_tmdb(movie, detail)
                movie = assign_ids(session, movie, imdb_id=detail.imdb_id)
                if detail.digital_date:
                    digital_dates.append(detail.digital_date)
                streaming.extend(detail.providers)
            except TMDBError as exc:
                log.info("TMDB check failed for %s: %s", movie.title, exc)
        if movie.douban_id:
            try:
                info = douban.detail(movie.douban_id)
                movie.douban_rating = info.rating
                movie.douban_votes = info.votes
                streaming[:0] = [f"国内 · {name}" for name in info.vendors]
                if info.pre_playable_date and (m := _DATE.search(info.pre_playable_date)):
                    digital_dates.append(m.group(0))
            except DoubanError as exc:
                log.info("Douban check failed for %s: %s", movie.title, exc)

        if digital_dates:
            movie.digital_date = min(digital_dates)
        if streaming:
            movie.streaming = streaming
        movie.last_checked_at = now()
        return _advance(session, movie)


def apply_availability(session, movie: Movie, tmdb_info: TMDBMovie | None = None,
                       douban_info: DoubanMovie | None = None) -> bool:
    """Use fetched metadata without approving a candidate or creating notifications."""
    valid_tmdb = isinstance(tmdb_info, TMDBMovie) and movie.tmdb_id == tmdb_info.id
    valid_douban = isinstance(douban_info, DoubanMovie) and movie.douban_id == douban_info.id and (
        not movie.tmdb_id or same_douban_movie(movie, douban_info))
    if not (valid_tmdb or valid_douban):
        return False
    dates, providers = [], []
    if valid_tmdb:
        if tmdb_info.digital_date:
            dates.append(tmdb_info.digital_date)
        providers.extend(tmdb_info.providers)
    if valid_douban:
        providers[:0] = [f'国内 · {name}' for name in douban_info.vendors]
        if douban_info.pre_playable_date and (match := _DATE.search(douban_info.pre_playable_date)):
            dates.append(match.group())
    if dates:
        movie.digital_date = min(dates)
    if providers:
        movie.streaming = list(dict.fromkeys(providers))
    movie.last_checked_at = now()
    _advance(session, movie, notify=False)
    return True


def _advance(session, movie: Movie, *, notify: bool = True) -> bool:
    from .pt import release_wait_reason

    if movie.stage >= Stage.DOWNLOADED:
        return False
    target = Stage.IN_CINEMA
    if (movie.streaming or movie.digital_date) and not release_wait_reason(movie):
        target = Stage.STREAMING
    elif movie.digital_date:
        target = Stage.DATED
    if target <= movie.stage:
        if movie.stage == Stage.STREAMING and target < Stage.STREAMING:
            # Correct an old preorder-as-streaming classification without an alert.
            movie.stage = target
        return False
    movie.stage = target
    if not notify:
        return True
    if target == Stage.STREAMING:
        platforms = "、".join(movie.streaming[:6])
        more = f" 等 {len(movie.streaming)} 个平台" if len(movie.streaming) > 6 else ""
        message = f"可在 {platforms}{more} 观看" if platforms else f"已到数字版日期 {movie.digital_date}"
        add_event(session, "streaming", f"《{movie.title}》已上线", message, movie_id=movie.id)
    else:
        add_event(
            session, "dated", f"《{movie.title}》已定档", f"预计 {movie.digital_date} 上线数字版", movie_id=movie.id
        )
    return True
