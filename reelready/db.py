from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


engine = create_engine(
    f"sqlite:///{config.DB_PATH}",
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cursor = dbapi_conn.cursor()
    # WAL lets the web UI read while a background job is writing.
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(movies)")}
        if "details" not in columns:
            connection.exec_driver_sql("ALTER TABLE movies ADD COLUMN details JSON NOT NULL DEFAULT '{}'")
        if "metadata_checked_at" not in columns:
            connection.exec_driver_sql("ALTER TABLE movies ADD COLUMN metadata_checked_at DATETIME")
    _migrate_site_identity()


def _migrate_site_identity() -> None:
    """Upgrade existing SQLite installations and merge accidental duplicate sites."""
    import sqlite3
    from datetime import datetime

    from .models import Movie, Site
    from .site_identity import site_identity

    with engine.begin() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(sites)")}
        if "identity_key" not in columns:
            connection.exec_driver_sql("ALTER TABLE sites ADD COLUMN identity_key VARCHAR(512)")
        if "category" not in columns:
            connection.exec_driver_sql("ALTER TABLE sites ADD COLUMN category VARCHAR(16) NOT NULL DEFAULT 'standard'")
    with session_scope() as session:
        sites = list(session.query(Site).order_by(Site.id))
        seen = {}
        duplicates = []
        for site in sites:
            key = site_identity(site.kind, site.base_url)
            if key in seen:
                duplicates.append((seen[key], site))
            else:
                seen[key] = site
        if duplicates:
            backup_path = config.DATA_DIR / f"before-site-dedup-{datetime.now():%Y%m%d-%H%M%S-%f}.db"
            with sqlite3.connect(config.DB_PATH) as source, sqlite3.connect(backup_path) as backup:
                source.backup(backup)
            for keep, drop in duplicates:
                keep.cookie = keep.cookie or drop.cookie
                keep.api_key = keep.api_key or drop.api_key
                keep.enabled = keep.enabled or drop.enabled
                session.delete(drop)
            remap = {drop.id: keep.id for keep, drop in duplicates}
            for movie in session.query(Movie):
                candidates = [dict(candidate, site_id=remap[candidate["site_id"]]) if candidate.get("site_id") in remap else candidate for candidate in movie.last_pt_candidates]
                if candidates != movie.last_pt_candidates:
                    movie.last_pt_candidates = candidates
                if movie.downloaded and movie.downloaded.get("site_id") in remap:
                    movie.downloaded = dict(movie.downloaded, site_id=remap[movie.downloaded["site_id"]])
            session.flush()
        for key, site in seen.items():
            site.identity_key = key
            if site.status == "testing":
                site.status = "unknown"
                site.status_message = "服务重启，请重新测试连接"
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ix_sites_identity_key ON sites(identity_key)")
