"""Portable record or full-configuration backups, restored in one transaction."""

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import DateTime, select
from sqlalchemy.exc import SQLAlchemyError
from pydantic import ValidationError

from .. import __version__, config
from ..db import session_scope
from ..models import CookieCloudBlob, Event, JobState, Movie, MovieStatus, Setting, Site, now
from ..settings import AppSettings, SETTINGS_KEY, load_settings
from ..site_identity import site_identity
from ..sites import SITE_KINDS
from .movies import _STATUS_PRIORITY, assign_ids, find_existing

log = logging.getLogger(__name__)

FORMAT = "reelready-backup"
FORMAT_VERSION = 2
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
                raise BackupError('备份中的时间格式不正确') from exc
        values[column.key] = value
    return values


def export_data(scope: str = 'records') -> dict[str, Any]:
    if scope not in ('records', 'full'):
        raise BackupError('请选择有效的备份范围')
    if scope == 'full':
        load_settings()  # Persist generated CookieCloud credentials before exporting.
    with session_scope() as session:
        movies = [_dump(movie) for movie in session.scalars(select(Movie).order_by(Movie.id))]
        events = [_dump(event) for event in session.scalars(select(Event).order_by(Event.id))]
        data = {'format': FORMAT, 'version': FORMAT_VERSION, 'scope': scope,
                'app_version': __version__, 'exported_at': now().isoformat(),
                'movies': movies, 'events': events}
        if scope == 'full':
            for key, model in [('settings', Setting), ('sites', Site), ('cookiecloud', CookieCloudBlob), ('jobs', JobState)]:
                data[key] = [_dump(row) for row in session.scalars(select(model))]
    return data


def import_data(raw: bytes, scope: str = 'records') -> dict[str, int]:
    """Merge records; full mode also replaces matching configuration keys/sites."""
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise BackupError("文件不是有效的 JSON") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise BackupError("这不是 ReelReady 导出的备份文件")
    if type(data.get("version")) is not int or not 1 <= data['version'] <= FORMAT_VERSION:
        raise BackupError("备份文件来自更新的版本，请先升级 ReelReady")
    if scope not in ('records', 'full'):
        raise BackupError('请选择有效的备份范围')
    if data['version'] >= 2 and data.get('scope') not in ('records', 'full'):
        raise BackupError('备份文件缺少有效的范围标记')
    movies, events = data.get("movies") or [], data.get("events") or []
    if not isinstance(movies, list) or not isinstance(events, list):
        raise BackupError("备份文件内容不完整")
    restore_full = scope == 'full' and data.get('scope') == 'full'
    if restore_full:
        _validate_configuration(data)

    _backup_database()
    stats = {"created": 0, "merged": 0, "events": 0}
    try:
        with session_scope() as session:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            site_map = _restore_configuration(session, data, stats) if restore_full else {}
            _restore_records(session, movies, events, site_map, stats)
    except (SQLAlchemyError, TypeError, ValueError, KeyError) as exc:
        # Database diagnostics may include passwords/Cookies from bound parameters.
        raise BackupError('备份字段不正确，未导入任何数据，请检查文件或升级后重试') from exc
    return stats


def _restore_records(session, movies, events, site_map, stats):
    id_map: dict[int, int] = {}
    for item in movies:
        if not isinstance(item, dict):
            continue
        values = _load(Movie, item)
        _remap_site_references(values, site_map)
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
            session.flush()
            stats["events"] += 1


def _validate_configuration(data):
    for key in ('settings', 'sites', 'cookiecloud', 'jobs'):
        if not isinstance(data.get(key), list) or any(not isinstance(row, dict) for row in data[key]):
            raise BackupError('完整备份的配置内容不完整')
    settings = next((row.get('value') for row in data['settings'] if row.get('key') == SETTINGS_KEY), None)
    if not isinstance(settings, dict):
        raise BackupError('完整备份缺少应用设置')
    try:
        AppSettings.model_validate(settings)
    except ValidationError as exc:
        raise BackupError('备份中的应用设置格式不正确') from exc
    for row in data['settings']:
        if not isinstance(row.get('key'), str) or not row['key'] or 'value' not in row:
            raise BackupError('备份中的设置格式不正确')
    for row in data['sites']:
        try:
            url = urlsplit(row.get('base_url') or '')
            valid = row.get('kind') in SITE_KINDS and row.get('name') and url.scheme in ('http', 'https') and url.hostname
            site_identity(row.get('kind', ''), row.get('base_url') or '')
        except (TypeError, ValueError, AttributeError) as exc:
            raise BackupError('备份中的站点配置格式不正确') from exc
        if not valid:
            raise BackupError('备份中的站点配置格式不正确')
    for key, primary in (('cookiecloud', 'uuid'), ('jobs', 'name')):
        if any(not isinstance(row.get(primary), str) or not row[primary] for row in data[key]):
            raise BackupError('备份中的同步数据或任务记录格式不正确')


def _restore_configuration(session, data, stats):
    for key, model, primary in [('settings', Setting, 'key'), ('cookiecloud', CookieCloudBlob, 'uuid'), ('jobs', JobState, 'name')]:
        stats[key] = 0
        for item in data[key]:
            values = _load(model, item)
            if model is Setting and item['key'] == SETTINGS_KEY:
                values['value'] = AppSettings.model_validate(item['value']).model_dump()
            existing = session.get(model, values[primary])
            if existing is None:
                session.add(model(**values))
            else:
                for name, value in values.items():
                    setattr(existing, name, value)
            stats[key] += 1
    site_map = {}
    stats['sites'] = 0
    existing_sites = {site_identity(site.kind, site.base_url): site for site in session.scalars(select(Site))}
    for item in data['sites']:
        values = _load(Site, item)
        values['identity_key'] = site_identity(values['kind'], values['base_url'])
        if values.get('status') == 'testing':
            values.update(status='unknown', status_message='导入后请重新测试连接')
        site = existing_sites.get(values['identity_key'])
        if site is None:
            site = Site(**values)
            session.add(site)
            existing_sites[values['identity_key']] = site
        else:
            for name, value in values.items():
                setattr(site, name, value)
        session.flush()
        if type(item.get('id')) is int:
            site_map[item['id']] = site.id
        stats['sites'] += 1
    return site_map


def _remap_site_references(values, site_map):
    # A foreign database's numeric site id must never point at an unrelated local site.
    values['last_pt_candidates'] = [dict(item, site_id=site_map[item['site_id']])
                                    for item in values.get('last_pt_candidates') or []
                                    if isinstance(item, dict) and item.get('site_id') in site_map]
    if isinstance(values.get('downloaded'), dict):
        values['downloaded'] = dict(values['downloaded'], site_id=site_map.get(values['downloaded'].get('site_id')))


def _import_movie(session, values: dict[str, Any]) -> tuple[Movie, bool]:
    ids = {key: values.get(key) for key in _MOVIE_ID_FIELDS}
    existing = find_existing(session, **ids) if any(ids.values()) else None
    if not any(ids.values()) and values.get('created_at'):
        existing = session.scalar(select(Movie).where(
            Movie.douban_id.is_(None), Movie.tmdb_id.is_(None), Movie.imdb_id.is_(None),
            Movie.title == values['title'], Movie.year == values.get('year'),
            Movie.created_at == values['created_at'],
        ).order_by(Movie.id).limit(1))
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
