"""Where downloaded movies live, as reported by qBittorrent, for local playback."""

import logging
import re
from typing import Any

from sqlalchemy import select

from ..db import session_scope
from ..downloader import DownloaderError, QBittorrent
from ..models import Movie
from ..settings import AppSettings

log = logging.getLogger(__name__)

MAX_STATUS_IDS = 500


def _normalized(text: str) -> str:
    return re.sub(r"[^0-9a-z一-鿿]", "", (text or "").lower())


def match_legacy(downloaded: dict[str, Any], rows: list[dict]) -> dict | None:
    """Find the torrent of a download recorded before info-hashes were stored."""
    title = _normalized(downloaded.get("title", ""))
    if not title:
        return None
    size = downloaded.get("size_bytes") or 0

    def same_size(row: dict) -> bool:
        total = row.get("total_size") or row.get("size") or 0
        return bool(size and total) and abs(total - size) <= size * 0.02

    exact = [row for row in rows if _normalized(row.get("name", "")) == title]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        exact = [row for row in exact if same_size(row)]
        return exact[0] if len(exact) == 1 else None
    loose = []
    for row in rows:
        name = _normalized(row.get("name", ""))
        if len(name) >= 8 and (name in title or title in name) and same_size(row):
            loose.append(row)
    return loose[0] if len(loose) == 1 else None


def _describe(settings: AppSettings, row: dict | None) -> dict[str, Any]:
    if row is None:
        return {"state": "missing", "message": "qBittorrent 中找不到这个种子"}
    progress = float(row.get("progress") or 0)
    raw_path = row.get("content_path") or ""
    if not raw_path and row.get("save_path") and row.get("name"):
        raw_path = row["save_path"].rstrip("/\\") + "/" + row["name"]
    path = settings.playback.host_path(raw_path) if raw_path else ""
    if row.get("state") == "missingFiles":
        state, message = "missing_files", "qBittorrent 报告文件已丢失"
    elif progress >= 1:
        state, message = "ready", "已下载完成"
    else:
        state, message = "downloading", f"下载中 {int(progress * 100)}%"
    return {"state": state, "progress": round(progress, 4), "path": path, "message": message}


def local_status(settings: AppSettings, movie_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Download state and local path per movie; raises DownloaderError if qBittorrent is unreachable."""
    movie_ids = list(dict.fromkeys(movie_ids))[:MAX_STATUS_IDS]
    with session_scope() as session:
        records = {
            movie.id: dict(movie.downloaded)
            for movie in session.scalars(select(Movie).where(Movie.id.in_(movie_ids)))
            if movie.downloaded
        }
    if not records:
        return {}
    hashes = [record["hash"].lower() for record in records.values() if record.get("hash")]
    legacy = [movie_id for movie_id, record in records.items() if not record.get("hash")]
    with QBittorrent(settings.qbittorrent) as qb:
        rows = qb.torrents() if legacy else qb.torrents(hashes)
    by_hash = {str(row.get("hash", "")).lower(): row for row in rows}

    found_hashes: dict[int, str] = {}
    result: dict[int, dict[str, Any]] = {}
    for movie_id, record in records.items():
        if record.get("hash"):
            row = by_hash.get(record["hash"].lower())
        else:
            row = match_legacy(record, rows)
            if row is not None and row.get("hash"):
                found_hashes[movie_id] = str(row["hash"]).lower()
        result[movie_id] = _describe(settings, row)

    if found_hashes:
        # Remember the match so later lookups ask qBittorrent for exactly this torrent.
        with session_scope() as session:
            for movie_id, info_hash in found_hashes.items():
                movie = session.get(Movie, movie_id)
                if movie is not None and movie.downloaded and not movie.downloaded.get("hash"):
                    movie.downloaded = dict(movie.downloaded, hash=info_hash)
    return result


def movie_status(settings: AppSettings, movie_id: int) -> dict[str, Any]:
    try:
        status = local_status(settings, [movie_id]).get(movie_id)
    except DownloaderError as exc:
        return {"state": "error", "message": str(exc)}
    return status or {"state": "not_downloaded", "message": "这部影片还没有下载记录"}
