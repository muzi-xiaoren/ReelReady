"""Export movie records and event history to JSON, and merge such a file back in."""

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, select

from .. import __version__, config
from ..db import session_scope
from ..models import Event, Movie, MovieStatus, now
from .movies import _STATUS_PRIORITY, assign_ids, find_existing

log = logging.getLogger(__name__)

FORMAT = "reelready-backup"
FORMAT_VERSION = 1
# Ids are local to one database; movies are matched by their external ids instead.
_MOVIE_ID_FIELDS = ("douban_id", "tmdb_id", "imdb_id")


class BackupError(Exception):
    pass


def _dump(row: Any) -> dict[str, Any]:
    data = {}
    for column in row.__table__.columns:
        value = getattr(row, column.key)
        data[column.key] = value.isoformat() if isinstance(value, datetime) else value
    return data


def _load(model: type, data: dict[str, Any]) -> dict[str, Any]:
    """Keep the known columns of ``model`` and parse their datetimes; unknown keys are ignored."""
    values = {}
    for column in model.__table__.columns:
        if column.key == "id" or column.key not in data:
            continue
        value = data[column.key]
        if isinstance(column.type, DateTime) and value:
            try:
                value = datetime.fromisoformat(value)
            except (TypeError, ValueError) as exc:
                raise BackupError(f"时间格式不正确：{value}") from exc
        values[column.key] = value
    return values


def export_data() -> dict[str, Any]:
    with session_scope() as session:
        movies = [_dump(movie) for movie in session.scalars(select(Movie).order_by(Movie.id))]
        events = [_dump(event) for event in session.scalars(select(Event).order_by(Event.id))]
    return {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "app_version": __version__,
        "exported_at": now().isoformat(),
        "movies": movies,
        "events": events,
    }


def import_data(raw: bytes) -> dict[str, int]:
    """Merge an export into the local database; existing records are kept and only filled in."""
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise BackupError("文件不是有效的 JSON") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise BackupError("这不是 ReelReady 导出的备份文件")
    if not isinstance(data.get("version"), int) or data["version"] > FORMAT_VERSION:
        raise BackupError("备份文件来自更新的版本，请先升级 ReelReady")
    movies, events = data.get("movies") or [], data.get("events") or []
    if not isinstance(movies, list) or not isinstance(events, list):
        raise BackupError("备份文件内容不完整")

    _backup_database()
    stats = {"created": 0, "merged": 0, "events": 0}
    with session_scope() as session:
        id_map: dict[int, int] = {}
        for item in movies:
            if not isinstance(item, dict):
                continue
            values = _load(Movie, item)
            if not values.get("title"):
                continue
            if values.get("status") not in {s.value for s in MovieStatus}:
                values["status"] = MovieStatus.CANDIDATE
            movie, created = _import_movie(session, values)
            stats["created" if created else "merged"] += 1
            if isinstance(item.get("id"), int):
                id_map[item["id"]] = movie.id
        for item in events:
            if not isinstance(item, dict):
                continue
            values = _load(Event, item)
            values["movie_id"] = id_map.get(values.get("movie_id"))
            # History is imported as already sent so it is never mailed again.
            values["notified"] = True
            if not values.get("kind") or not values.get("title"):
                continue
            duplicate = session.scalar(select(Event.id).where(
                Event.kind == values["kind"], Event.title == values["title"], Event.created_at == values.get("created_at"),
            ).limit(1))
            if duplicate is None:
                session.add(Event(**values))
                stats["events"] += 1
    return stats


def _import_movie(session, values: dict[str, Any]) -> tuple[Movie, bool]:
    ids = {key: values.get(key) for key in _MOVIE_ID_FIELDS}
    existing = find_existing(session, **ids) if any(ids.values()) else None
    if existing is None:
        movie = Movie(**values)
        session.add(movie)
        session.flush()
        return movie, True
    movie = assign_ids(session, existing, **ids)
    # The stronger list wins, as when two records of one movie are merged.
    imported = MovieStatus(values["status"])
    if _STATUS_PRIORITY[imported] > _STATUS_PRIORITY[MovieStatus(movie.status)]:
        movie.status = imported
        movie.watched_from_status = values.get("watched_from_status")
    movie.stage = max(movie.stage or 0, values.get("stage") or 0)
    for key, value in values.items():
        if key in ("status", "stage", "watched_from_status", *_MOVIE_ID_FIELDS):
            continue
        if getattr(movie, key) in (None, "", [], {}) and value not in (None, "", [], {}):
            setattr(movie, key, value)
    session.flush()
    return movie, False


def _backup_database() -> None:
    path = config.DATA_DIR / f"before-import-{datetime.now():%Y%m%d-%H%M%S-%f}.db"
    with sqlite3.connect(config.DB_PATH) as source, sqlite3.connect(path) as backup:
        source.backup(backup)
    log.info("Saved database backup to %s before import", path)
