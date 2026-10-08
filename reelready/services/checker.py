"""Detect when monitored movies get a digital release date or land on streaming platforms."""

import logging
import re

from sqlalchemy import select

from ..db import session_scope
from ..models import Movie, MovieStatus, Stage, now
from ..settings import AppSettings
from ..sources.douban import DoubanClient, DoubanError
from ..sources.tmdb import TMDBClient, TMDBError
from .clients import make_douban, make_tmdb
from .events import add_event
from .movies import apply_tmdb, assign_ids, link_ids

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
    summary = f"检测 {len(ids)} 部，{advanced} 部状态有更新"
    return f"{summary}，{errors} 部出错" if errors else summary


def _check_one(settings: AppSettings, movie_id: int, tmdb: TMDBClient | None, douban: DoubanClient) -> bool:
    with session_scope() as session:
        movie = session.get(Movie, movie_id)
        if movie is None:
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


def _advance(session, movie: Movie) -> bool:
    if movie.stage >= Stage.DOWNLOADED:
        return False
    target = Stage.IN_CINEMA
    if movie.streaming:
        target = Stage.STREAMING
    elif movie.digital_date:
        target = Stage.DATED
    if target <= movie.stage:
        return False
    movie.stage = target
    if target == Stage.STREAMING:
        platforms = "、".join(movie.streaming[:6])
        more = f" 等 {len(movie.streaming)} 个平台" if len(movie.streaming) > 6 else ""
        add_event(session, "streaming", f"《{movie.title}》已上线", f"可在 {platforms}{more} 观看", movie_id=movie.id)
    else:
        add_event(
            session, "dated", f"《{movie.title}》已定档", f"预计 {movie.digital_date} 上线数字版", movie_id=movie.id
        )
    return True
