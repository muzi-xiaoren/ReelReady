from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class MovieStatus(StrEnum):
    """Which list a movie sits in."""

    CANDIDATE = "candidate"  # collected, waiting for the user to approve
    MONITORING = "monitoring"  # approved, being checked and searched on PT sites
    COMPLETED = "completed"  # torrent handed to the downloader
    IGNORED = "ignored"  # removed by the user; never collected again


class Stage(IntEnum):
    """How far a movie has progressed towards being downloadable. Only moves forward."""

    IN_CINEMA = 0
    DATED = 1  # a digital / streaming release date is known
    STREAMING = 2  # available on at least one streaming / VOD platform
    DOWNLOADED = 3


STATUS_LABELS = {
    MovieStatus.CANDIDATE: "待确认",
    MovieStatus.MONITORING: "监测中",
    MovieStatus.COMPLETED: "已完成",
    MovieStatus.IGNORED: "黑名单",
}

STAGE_LABELS = {
    Stage.IN_CINEMA: "院线中",
    Stage.DATED: "已定档",
    Stage.STREAMING: "已上线",
    Stage.DOWNLOADED: "已下载",
}


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


class Movie(Base):
    __tablename__ = "movies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default=MovieStatus.CANDIDATE, index=True)
    stage: Mapped[int] = mapped_column(Integer, default=Stage.IN_CINEMA)
    source: Mapped[str] = mapped_column(String(16), default="manual")  # douban / tmdb / manual

    title: Mapped[str] = mapped_column(String(255))
    original_title: Mapped[str | None] = mapped_column(String(255))
    year: Mapped[int | None] = mapped_column(Integer)
    poster_url: Mapped[str | None] = mapped_column(Text)
    overview: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metadata_checked_at: Mapped[datetime | None] = mapped_column(DateTime)

    douban_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    imdb_id: Mapped[str | None] = mapped_column(String(16), unique=True)

    douban_rating: Mapped[float | None] = mapped_column(Float)
    douban_votes: Mapped[int | None] = mapped_column(Integer)
    tmdb_rating: Mapped[float | None] = mapped_column(Float)
    tmdb_votes: Mapped[int | None] = mapped_column(Integer)

    digital_date: Mapped[str | None] = mapped_column(String(10))  # YYYY-MM-DD
    streaming: Mapped[list[str]] = mapped_column(JSON, default=lambda: [])

    last_pt_summary: Mapped[str | None] = mapped_column(Text)
    last_pt_candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=lambda: [])
    downloaded: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_pt_scan_at: Mapped[datetime | None] = mapped_column(DateTime)

    @property
    def stage_label(self) -> str:
        return STAGE_LABELS[Stage(self.stage)]

    @property
    def display_title(self) -> str:
        if self.original_title and self.original_title != self.title:
            return f"{self.title} {self.original_title}"
        return self.title

    @property
    def douban_url(self) -> str | None:
        return f"https://movie.douban.com/subject/{self.douban_id}/" if self.douban_id else None

    @property
    def tmdb_url(self) -> str | None:
        return f"https://www.themoviedb.org/movie/{self.tmdb_id}" if self.tmdb_id else None

    @property
    def imdb_url(self) -> str | None:
        return f"https://www.imdb.com/title/{self.imdb_id}/" if self.imdb_id else None


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    movie_id: Mapped[int | None] = mapped_column(ForeignKey("movies.id", ondelete="SET NULL"), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text, default="")
    # Urgent events are mailed immediately; the rest go into the daily digest.
    urgent: Mapped[bool] = mapped_column(Boolean, default=False)
    notified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identity_key: Mapped[str | None] = mapped_column(String(512), unique=True)
    category: Mapped[str] = mapped_column(String(16), default="standard")
    kind: Mapped[str] = mapped_column(String(16))  # mteam / nexusphp
    name: Mapped[str] = mapped_column(String(64))
    base_url: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cookie: Mapped[str | None] = mapped_column(Text)
    api_key: Mapped[str | None] = mapped_column(Text)
    # Refresh the cookie from CookieCloud whenever the browser pushes a new snapshot.
    use_cookiecloud: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # unknown / testing / ok / invalid / error
    status_message: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)


class JobState(Base):
    __tablename__ = "job_states"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_status: Mapped[str | None] = mapped_column(String(16))  # ok / error
    last_message: Mapped[str | None] = mapped_column(Text)


class CookieCloudBlob(Base):
    """Latest encrypted snapshot pushed by the CookieCloud browser extension."""

    __tablename__ = "cookiecloud"

    uuid: Mapped[str] = mapped_column(String(64), primary_key=True)
    encrypted: Mapped[str] = mapped_column(Text)
    crypto_type: Mapped[str] = mapped_column(String(32), default="legacy")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
