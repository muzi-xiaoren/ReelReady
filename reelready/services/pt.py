"""Search PT sites for monitored movies and hand the best torrent to qBittorrent."""

import logging
import threading
import time
from datetime import date
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import load_only

from ..db import session_scope
from ..downloader import DownloaderError, QBittorrent
from ..models import Movie, MovieStatus, Site, Setting, now
from ..settings import AppSettings, SETTINGS_KEY
from ..sites import BaseSite, LoginExpired, MovieQuery, SiteError, build_site, snapshot
from ..torrent_rules import GB, Evaluated, TorrentInfo, matches_movie, pick_best
from .checker import monitored_ids
from .events import add_event
from .movies import mark_downloaded

log = logging.getLogger(__name__)

# How many search hits are kept on the movie for display / manual download.
KEEP_CANDIDATES = 30
_download_lock = threading.Lock()


@dataclass
class _Target:
    id: int
    title: str
    query: MovieQuery
    release_wait: str = ""


def release_wait_reason(movie: Movie) -> str:
    if movie.streaming:
        return ""
    if movie.digital_date:
        try:
            if date.fromisoformat(movie.digital_date) <= date.today():
                return ""
            return f"等待上线确认：数字版预计 {movie.digital_date}，暂无可看平台"
        except ValueError:
            pass
    return "等待上线确认：暂无有效数字版日期或可看平台"


def rank_for_movie(hits: list[TorrentInfo], rules, query: MovieQuery, release_wait: str):
    _, ranked = pick_best(hits, rules)
    for item in ranked:
        if not matches_movie(item.torrent, imdb_id=query.imdb_id, titles=query.titles, year=query.year):
            item.ok = False
            item.reason = "影片不匹配：标题、年份或内容类型与目标电影不符"
        elif item.ok and not item.parsed.source:
            item.ok = False
            item.reason = "片源类型不明：未识别到 WEB-DL、WEBRip、BluRay、HDTV 或 DVD"
        elif item.ok and release_wait:
            item.ok = False
            item.reason = release_wait
    accepted = [item for item in ranked if item.ok]
    rejected = [item for item in ranked if not item.ok]
    return (accepted[0] if accepted else None), accepted + rejected


def mark_site_invalid(site_id: int, message: str) -> None:
    with session_scope() as session:
        site = session.get(Site, site_id)
        if site is None:
            return
        was_invalid = site.status == "invalid"
        site.status = "invalid"
        site.status_message = message
        site.last_checked_at = now()
        if not was_invalid:
            add_event(
                session,
                "site_invalid",
                f"站点「{site.name}」登录失效",
                f"{message}\n在浏览器里重新登录该站点，CookieCloud 同步后会自动恢复。",
                urgent=True,
            )


def _mark_site_ok(site_id: int) -> None:
    with session_scope() as session:
        site = session.get(Site, site_id)
        if site is not None and site.status != "ok":
            site.status = "ok"
            site.status_message = None
        if site is not None:
            site.last_checked_at = now()


def open_sites(settings: AppSettings, site_ids: list[int] | None = None) -> list[BaseSite]:
    with session_scope() as session:
        query = select(Site).where(Site.enabled.is_(True), Site.status.notin_(("invalid", "testing")))
        if site_ids:
            query = select(Site).where(Site.id.in_(site_ids))
        configs = [snapshot(s, settings.network.user_agent) for s in session.scalars(query)]
    sites: list[BaseSite] = []
    for config in configs:
        try:
            sites.append(build_site(config))
        except LoginExpired as exc:
            mark_site_invalid(config.id, str(exc))
        except SiteError as exc:
            log.warning("Cannot open site %s: %s", config.name, exc)
    return sites


def _targets(movie_ids: list[int] | None) -> list[_Target]:
    ids = monitored_ids(movie_ids)
    with session_scope() as session:
        movies = session.scalars(select(Movie).options(load_only(Movie.id, Movie.title, Movie.original_title, Movie.imdb_id, Movie.year, Movie.details, Movie.streaming, Movie.digital_date)).where(Movie.id.in_(ids)).order_by(Movie.id)) if ids else []
        return [
            _Target(m.id, m.title, _movie_query(m), release_wait_reason(m)) for m in movies
        ]


def _movie_query(movie: Movie) -> MovieQuery:
    return MovieQuery(movie.imdb_id, movie.title, movie.original_title, movie.year, (movie.details or {}).get('aliases') or [])


def run_pt_scan(settings: AppSettings, movie_ids: list[int] | None = None) -> str:
    targets = _targets(movie_ids)
    if not targets:
        return "没有需要搜索的影片"
    skipped = []
    if not settings.pt.early_search:
        skipped = [target for target in targets if target.release_wait]
        targets = [target for target in targets if not target.release_wait]
    if not targets:
        return f"提前搜索已关闭，{len(skipped)} 部影片等待上线确认，未访问 PT 站点"
    sites = open_sites(settings)
    if not sites:
        return "没有可用的站点"
    delay = max(settings.pt.request_delay_seconds, 0)
    downloaded = 0
    try:
        first = True
        for target in targets:
            hits: list[TorrentInfo] = []
            errors: list[str] = []
            for site in list(sites):
                if not first:
                    time.sleep(delay)
                first = False
                try:
                    found = site.search(target.query)
                    _mark_site_ok(site.config.id)
                except LoginExpired as exc:
                    mark_site_invalid(site.config.id, str(exc))
                    sites.remove(site)
                    site.close()
                    continue
                except SiteError as exc:
                    errors.append(f"{site.name}: {exc}")
                    continue
                hits.extend(found)
            best, ranked = rank_for_movie(hits, settings.rules, target.query, target.release_wait)
            _save_scan(target.id, ranked, errors)
            if best is not None:
                site = next((s for s in sites if s.config.id == best.torrent.site_id), None)
                if site is not None and download(settings, target.id, site, best.torrent):
                    downloaded += 1
            if not sites:
                break
    finally:
        for site in sites:
            site.close()
    summary = f"搜索 {len(targets)} 部，开始下载 {downloaded} 部"
    return summary + (f"，{len(skipped)} 部等待上线确认（提前搜索已关闭）" if skipped else "")


def revalidate_cached_candidates(settings: AppSettings | None = None, movie_ids: list[int] | None = None) -> int:
    """Re-evaluate stored results locally, without searching sites or downloading."""
    updated = 0
    with session_scope() as session:
        session.execute(text('BEGIN IMMEDIATE'))
        if settings is None:
            row = session.get(Setting, SETTINGS_KEY)
            settings = AppSettings.model_validate(row.value) if row else AppSettings()
        query = select(Movie).where(Movie.last_pt_candidates != [], Movie.status != MovieStatus.COMPLETED)
        if movie_ids is not None:
            query = query.where(Movie.id.in_(movie_ids))
        for movie in session.scalars(query.execution_options(yield_per=50)):
            fields = TorrentInfo.__dataclass_fields__
            # Legacy results do not distinguish real IMDb metadata from search inference.
            hits = [TorrentInfo(**{**{key: value for key, value in item.items() if key in fields}, 'imdb_source': item.get('imdb_source', 'search')}) for item in movie.last_pt_candidates if (item.get('title') or '').strip() and all(key in item for key in ('site_id', 'site_name', 'torrent_id'))]
            _, ranked = rank_for_movie(hits, settings.rules, _movie_query(movie), release_wait_reason(movie))
            movie.last_pt_candidates = [item.to_dict() for item in ranked]
            movie.last_pt_summary = f"找到 {len(ranked)} 个种子，{sum(item.ok for item in ranked)} 个可自动下载" if ranked else "暂无有效资源（已移除标题为空的解析记录）"
            updated += 1
    return updated


def _save_scan(movie_id: int, ranked: list[Evaluated], errors: list[str]) -> None:
    accepted = sum(1 for e in ranked if e.ok)
    if ranked:
        summary = f"找到 {len(ranked)} 个种子，{accepted} 个符合要求"
    else:
        summary = "暂无资源"
    if errors:
        summary += "（" + "；".join(errors) + "）"
    with session_scope() as session:
        movie = session.get(Movie, movie_id)
        if movie is None:
            return
        movie.last_pt_summary = summary
        movie.last_pt_candidates = [e.to_dict() for e in ranked[:KEEP_CANDIDATES]]
        movie.last_pt_scan_at = now()


def download(settings: AppSettings, movie_id: int, site: BaseSite, torrent: TorrentInfo) -> bool:
    """Fetch the torrent from the site and add it to qBittorrent. Returns True on success."""
    with _download_lock:
        with session_scope() as session:
            movie = session.get(Movie, movie_id)
            if movie is None or movie.status == MovieStatus.COMPLETED:
                return False
        return _download(settings, movie_id, site, torrent)


def _download(settings: AppSettings, movie_id: int, site: BaseSite, torrent: TorrentInfo) -> bool:
    try:
        content = site.download(torrent)
        with QBittorrent(settings.qbittorrent) as qb:
            qb.add_torrent(content, f"{torrent.site_name}-{torrent.torrent_id}.torrent")
    except LoginExpired as exc:
        mark_site_invalid(site.config.id, str(exc))
        return False
    except (SiteError, DownloaderError) as exc:
        with session_scope() as session:
            movie = session.get(Movie, movie_id)
            add_event(
                session,
                "download_failed",
                f"《{movie.title if movie else movie_id}》下载失败",
                f"{torrent.title}\n{exc}",
                movie_id=movie_id,
                urgent=True,
                dedupe_key=f"download_failed:{movie_id}",
            )
        return False

    with session_scope() as session:
        movie = session.get(Movie, movie_id)
        if movie is None:
            return True
        mark_downloaded(
            movie,
            {
                "site": torrent.site_name,
                "torrent_id": torrent.torrent_id,
                "title": torrent.title,
                "size_bytes": torrent.size_bytes,
                "detail_url": torrent.detail_url,
                "at": now().isoformat(sep=" "),
            },
        )
        add_event(
            session,
            "downloaded",
            f"《{movie.title}》已开始下载",
            f"{torrent.title}\n{torrent.site_name} · {torrent.size_bytes / GB:.1f} GB · 做种 {torrent.seeders}",
            movie_id=movie.id,
            urgent=True,
        )
    return True


def download_candidate(settings: AppSettings, movie_id: int, index: int, *, site_id: int | None = None, torrent_id: str | None = None) -> None:
    """Manually download one of the torrents found by the last scan."""
    with session_scope() as session:
        movie = session.get(Movie, movie_id)
        if movie is None or index < 0:
            raise SiteError("找不到这个种子，请重新搜索")
        if movie.status == MovieStatus.COMPLETED:
            raise SiteError('该影片已提交过下载，请在 qBittorrent 中查看，无需重复下载')
        candidates = movie.last_pt_candidates or []
        if site_id is not None and torrent_id is not None:
            data = next((dict(item) for item in candidates if item.get('site_id') == site_id and str(item.get('torrent_id')) == torrent_id), None)
            if data is None:
                raise SiteError('该种子已不在当前结果中，请刷新页面')
        else:
            if index >= len(candidates):
                raise SiteError('找不到这个种子，请重新搜索')
            data = dict(candidates[index])
    fields = TorrentInfo.__dataclass_fields__
    torrent = TorrentInfo(**{k: v for k, v in data.items() if k in fields})
    sites = open_sites(settings, [torrent.site_id])
    if not sites:
        raise SiteError("该站点不可用")
    try:
        if not download(settings, movie_id, sites[0], torrent):
            raise SiteError("下载失败，详见动态")
    finally:
        for site in sites:
            site.close()
