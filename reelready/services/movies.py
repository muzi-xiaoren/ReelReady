"""Creating, linking and merging movie records across Douban / TMDB / IMDb ids."""

import logging
import calendar
from datetime import date

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .. import config
from ..http import make_client
from ..models import Event, Movie, MovieStatus, Stage, now
from ..settings import AppSettings
from ..sources.douban import DoubanClient, DoubanError, DoubanMovie, parse_douban_id
from ..sources.tmdb import TMDBClient, TMDBError, TMDBMovie, parse_imdb_id, parse_tmdb_id
from ..sources.movie_metadata import matching_titles
from .clients import make_douban, make_tmdb, tmdb_proxy
from .events import add_event

log = logging.getLogger(__name__)

# When two records turn out to be the same movie, the one in the "stronger" list survives.
_STATUS_PRIORITY = {
    MovieStatus.IGNORED: 3,
    MovieStatus.COMPLETED: 2,
    MovieStatus.MONITORING: 1,
    MovieStatus.CANDIDATE: 0,
}


class AddMovieError(Exception):
    pass


def min_year(settings: AppSettings) -> int:
    return collection_cutoff(settings).year


def collection_cutoff(settings: AppSettings, today: date | None = None) -> date:
    today = today or date.today()
    months = settings.collect.max_age_years * 12 + settings.collect.max_age_months
    year, month = divmod(today.year * 12 + today.month - 1 - months, 12)
    month += 1
    return date(year, month, min(today.day, calendar.monthrange(year, month)[1]))


def within_collection_window(settings: AppSettings, year: int | None, release_date: str | None = None) -> bool:
    cutoff = collection_cutoff(settings)
    if release_date:
        try:
            released = date.fromisoformat(release_date[:10])
            return cutoff <= released <= date.today()
        except ValueError:
            pass
    # Some collection feeds expose only the release year; avoid inventing a month.
    return year is not None and cutoff.year <= year <= date.today().year


def find_existing(
    session: Session,
    *,
    douban_id: str | None = None,
    tmdb_id: int | None = None,
    imdb_id: str | None = None,
) -> Movie | None:
    conditions = []
    if douban_id:
        conditions.append(Movie.douban_id == douban_id)
    if tmdb_id:
        conditions.append(Movie.tmdb_id == tmdb_id)
    if imdb_id:
        conditions.append(Movie.imdb_id == imdb_id)
    if not conditions:
        return None
    return session.scalars(select(Movie).where(or_(*conditions)).limit(1)).first()


def _merge(session: Session, keep: Movie, drop: Movie) -> Movie:
    """Fold one record into the other and delete it; the stronger list wins, then the older record."""
    rank = lambda m: (_STATUS_PRIORITY[MovieStatus(m.status)], -m.id)  # noqa: E731
    if rank(drop) > rank(keep):
        keep, drop = drop, keep
    fields = [
        "douban_id", "tmdb_id", "imdb_id", "original_title", "year", "poster_url", "overview",
        "douban_rating", "douban_votes", "tmdb_rating", "tmdb_votes", "digital_date",
    ]
    values = {f: getattr(drop, f) for f in fields}
    keep.stage = max(keep.stage, drop.stage)
    session.execute(update(Event).where(Event.movie_id == drop.id).values(movie_id=keep.id))
    session.delete(drop)
    # Release the unique ids held by ``drop`` before copying them over.
    session.flush()
    for name, value in values.items():
        if getattr(keep, name) in (None, "") and value not in (None, ""):
            setattr(keep, name, value)
    session.flush()
    return keep


def assign_ids(
    session: Session,
    movie: Movie,
    *,
    douban_id: str | None = None,
    tmdb_id: int | None = None,
    imdb_id: str | None = None,
) -> Movie:
    """Attach external ids to ``movie``, merging with any other record that already owns one."""
    for attr, value in (("douban_id", douban_id), ("tmdb_id", tmdb_id), ("imdb_id", imdb_id)):
        if not value or getattr(movie, attr):
            continue
        other = session.scalars(
            select(Movie).where(getattr(Movie, attr) == value, Movie.id != movie.id).limit(1)
        ).first()
        if other is not None:
            movie = _merge(session, movie, other)
        if not getattr(movie, attr):
            setattr(movie, attr, value)
    session.flush()
    return movie


def apply_douban(movie: Movie, item: DoubanMovie) -> bool:
    if movie.tmdb_id and not same_douban_movie(movie, item):
        log.info("Ignoring mismatched Douban metadata for movie %s", movie.id)
        return False
    movie.douban_rating = item.rating
    movie.douban_votes = item.votes
    if item.title:
        movie.title = item.title
    movie.original_title = movie.original_title or item.original_title
    movie.year = movie.year or item.year
    movie.poster_url = item.cover or movie.poster_url
    movie.overview = item.intro or movie.overview
    release_date = (movie.details or {}).get('release_date') if movie.tmdb_id else None
    merge_details(movie, item.details, release_date or item.release_date)
    return True


def apply_tmdb(movie: Movie, item: TMDBMovie) -> None:
    movie.tmdb_rating = item.rating
    movie.tmdb_votes = item.votes
    # Douban titles are the canonical Chinese names; only fall back to TMDB's.
    if not movie.douban_id and item.title:
        movie.title = item.title
    movie.original_title = item.original_title or movie.original_title
    # Detail/search responses contain the primary release year; never keep a regional re-release year.
    movie.year = item.year or movie.year
    movie.poster_url = movie.poster_url or item.poster
    movie.overview = movie.overview or item.overview
    merge_details(movie, item.details, item.release_date)


def merge_details(movie: Movie, details: dict, release_date: str | None = None) -> None:
    values = dict(movie.details or {})
    details = dict(details)
    # Keep richer TMDB roles when Douban's actor list only provides names.
    if any(p.get('role') for p in values.get('cast') or []) and not any(p.get('role') for p in details.get('cast') or []):
        details.pop('cast', None)
    values.update({key: value for key, value in details.items() if value or isinstance(value, bool)})
    if release_date:
        values['release_date'] = release_date
    movie.details = values


def same_douban_movie(movie: Movie, item: DoubanMovie) -> bool:
    if movie.imdb_id and item.imdb_id:
        return movie.imdb_id == item.imdb_id
    release = str((movie.details or {}).get('release_date') or '')
    year = int(release[:4]) if release[:4].isdigit() else movie.year
    titles = [movie.title, movie.original_title or '', *((movie.details or {}).get('aliases') or [])]
    return matching_titles(titles, [item.title, item.original_title or '', *(item.details.get('aliases') or [])]) and (
        year is None or item.year is None or abs(year - item.year) <= 1
    )


def link_ids(session: Session, movie: Movie, tmdb: TMDBClient | None, douban: DoubanClient) -> Movie:
    """Best effort: fill in the missing TMDB / IMDb / Douban ids of a movie."""
    titles = [movie.original_title or "", movie.title, *((movie.details or {}).get('aliases') or [])]
    if tmdb is not None:
        try:
            if not movie.tmdb_id:
                found = tmdb.find_by_imdb(movie.imdb_id) if movie.imdb_id else tmdb.find(titles, movie.year)
                if found:
                    movie = assign_ids(session, movie, tmdb_id=found.id)
            if movie.tmdb_id and not movie.imdb_id:
                detail = tmdb.movie(movie.tmdb_id)
                apply_tmdb(movie, detail)
                movie = assign_ids(session, movie, imdb_id=detail.imdb_id)
        except TMDBError as exc:
            log.info("TMDB lookup failed for %s: %s", movie.title, exc)
    if not movie.douban_id:
        try:
            found = douban.find([movie.title, movie.original_title or "", *((movie.details or {}).get('aliases') or [])], movie.year)
            if found:
                detail = douban.detail(found.id)
                if same_douban_movie(movie, detail):
                    movie = assign_ids(session, movie, douban_id=found.id)
                    apply_douban(movie, detail)
        except DoubanError as exc:
            log.info("Douban lookup failed for %s: %s", movie.title, exc)
    if movie.douban_id and not movie.overview:
        try:
            apply_douban(movie, douban.detail(movie.douban_id))
        except DoubanError:
            log.info("Douban details unavailable for movie %s", movie.id)
    return movie


def poster_path(movie_id: int):
    return config.POSTER_DIR / f"{movie_id}.jpg"


def cache_poster(movie: Movie, settings: AppSettings) -> None:
    path = poster_path(movie.id)
    if path.exists() or not movie.poster_url:
        return
    url = movie.poster_url
    headers = {"User-Agent": settings.network.user_agent}
    proxy = None
    if "doubanio.com" in url:
        headers["Referer"] = "https://movie.douban.com/"
    elif "tmdb.org" in url:
        proxy = tmdb_proxy(settings)
    try:
        with make_client(proxy=proxy, headers=headers) as client:
            resp = client.get(url)
        if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image/"):
            path.write_bytes(resp.content)
    except httpx.HTTPError as exc:
        log.info("Poster download failed for %s: %s", movie.title, exc)


def ratings_text(movie: Movie) -> str:
    parts = []
    if movie.douban_rating:
        parts.append(f"豆瓣 {movie.douban_rating:.1f}（{movie.douban_votes or 0} 人）")
    if movie.tmdb_rating:
        parts.append(f"TMDB {movie.tmdb_rating:.1f}（{movie.tmdb_votes or 0} 票）")
    return " · ".join(parts) or "暂无评分"


def approve(movie: Movie) -> None:
    movie.status = MovieStatus.MONITORING
    movie.approved_at = now()


def add_manual(session: Session, ref: str, settings: AppSettings) -> Movie:
    """Add a movie from a Douban / TMDB / IMDb link or id straight into monitoring."""
    ref = ref.strip()
    douban_id = parse_douban_id(ref)
    tmdb_id = parse_tmdb_id(ref)
    imdb_id = parse_imdb_id(ref)
    if not (douban_id or tmdb_id or imdb_id):
        if ref.isdigit():
            douban_id = ref
        else:
            raise AddMovieError("请输入豆瓣 / TMDB / IMDb 的链接或编号")

    existing = find_existing(session, douban_id=douban_id, tmdb_id=tmdb_id, imdb_id=imdb_id)
    if existing is not None:
        if existing.status in (MovieStatus.CANDIDATE, MovieStatus.IGNORED):
            approve(existing)
        return existing

    tmdb = make_tmdb(settings)
    if (tmdb_id or imdb_id) and tmdb is None:
        raise AddMovieError("添加 TMDB / IMDb 条目需要先在设置里填写 TMDB API Key")
    try:
        with make_douban() as douban:
            movie = _fetch_new(douban_id, tmdb_id, imdb_id, tmdb, douban)
            if movie.year and movie.year < min_year(settings):
                raise AddMovieError(f"《{movie.title}》是 {movie.year} 年的片，暂不支持老片")
            existing = find_existing(
                session, douban_id=movie.douban_id, tmdb_id=movie.tmdb_id, imdb_id=movie.imdb_id
            )
            if existing is not None:
                approve(existing)
                return existing
            session.add(movie)
            session.flush()
            movie = link_ids(session, movie, tmdb, douban)
    finally:
        if tmdb is not None:
            tmdb.close()

    # Linking may have merged this into a record the user had ignored; a manual add wins.
    if movie.status != MovieStatus.COMPLETED:
        approve(movie)
    add_event(
        session, "added", f"《{movie.title}》已加入监测", ratings_text(movie), movie_id=movie.id, notify=False
    )
    session.flush()
    cache_poster(movie, settings)
    return movie


def _fetch_new(
    douban_id: str | None,
    tmdb_id: int | None,
    imdb_id: str | None,
    tmdb: TMDBClient | None,
    douban: DoubanClient,
) -> Movie:
    movie = Movie(title="", source="manual", status=MovieStatus.MONITORING, approved_at=now())
    try:
        if douban_id:
            apply_douban(movie, douban.detail(douban_id))
            movie.douban_id = douban_id
            return movie
        if imdb_id and not tmdb_id:
            found = tmdb.find_by_imdb(imdb_id)
            if found is None:
                raise AddMovieError("TMDB 上找不到这个 IMDb 编号")
            tmdb_id = found.id
        detail = tmdb.movie(tmdb_id)
    except (DoubanError, TMDBError) as exc:
        raise AddMovieError(str(exc)) from exc
    apply_tmdb(movie, detail)
    movie.tmdb_id = tmdb_id
    movie.imdb_id = detail.imdb_id
    return movie


def upsert_candidate_douban(session: Session, item: DoubanMovie) -> tuple[Movie, bool]:
    existing = find_existing(session, douban_id=item.id, imdb_id=item.imdb_id)
    if existing is not None:
        existing = assign_ids(session, existing, douban_id=item.id, imdb_id=item.imdb_id)
        existing.douban_rating = item.rating
        existing.douban_votes = item.votes
        return existing, False
    movie = Movie(title=item.title, source="douban", douban_id=item.id, imdb_id=item.imdb_id)
    apply_douban(movie, item)
    session.add(movie)
    session.flush()
    return movie, True


def upsert_candidate_tmdb(session: Session, item: TMDBMovie) -> tuple[Movie, bool]:
    existing = find_existing(session, tmdb_id=item.id, imdb_id=item.imdb_id)
    if existing is not None:
        existing = assign_ids(session, existing, tmdb_id=item.id, imdb_id=item.imdb_id)
        existing.tmdb_rating = item.rating
        existing.tmdb_votes = item.votes
        return existing, False
    movie = Movie(title=item.title, source="tmdb", tmdb_id=item.id, imdb_id=item.imdb_id)
    apply_tmdb(movie, item)
    session.add(movie)
    session.flush()
    return movie, True


def mark_downloaded(movie: Movie, info: dict) -> None:
    movie.status = MovieStatus.COMPLETED
    movie.stage = Stage.DOWNLOADED
    movie.downloaded = info
    movie.completed_at = now()
