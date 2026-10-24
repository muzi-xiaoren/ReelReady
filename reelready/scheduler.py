"""A tiny scheduler running jobs sequentially on one background thread.

Jobs are due when their interval has elapsed since the last run recorded in the database,
so runs missed while the computer was off are caught up right after startup.
"""

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .db import session_scope
from .models import JobState, now
from .services.checker import run_check
from .services.cloud_backup import run_cloud_backup
from .services.collector import run_collect
from .services.events import notify_urgent, send_digest, cleanup_events
from .services.pt import run_pt_scan
from .settings import AppSettings, load_settings

log = logging.getLogger(__name__)

TICK_SECONDS = 30
STARTUP_DELAY_SECONDS = 10
MAX_PENDING_REQUESTS = 64
DIGEST_RETRY_MINUTES = (5, 15, 30)


@dataclass(frozen=True)
class Job:
    name: str
    label: str
    run: Callable[..., str]
    interval: Callable[[AppSettings], float] | None  # hours; None means "once a day"
    takes_movies: bool = False
    # Disabled jobs only run when triggered by hand.
    enabled: Callable[[AppSettings], bool] | None = None


JOBS: dict[str, Job] = {
    job.name: job
    for job in [
        Job("collect", "收集院线高分片", run_collect, lambda s: s.collect.interval_hours),
        Job("check", "检测上线状态", run_check, lambda s: s.check.interval_hours, takes_movies=True),
        Job("pt_scan", "搜索 PT 并下载", run_pt_scan, lambda s: s.pt.interval_hours, takes_movies=True),
        Job("digest", "发送每日汇总", lambda s: send_digest(s), None),
        Job("events_cleanup", "清理到期动态", cleanup_events, lambda s: 24),
        Job("backup", "云备份", run_cloud_backup, lambda s: s.backup.interval_hours, enabled=lambda s: s.backup.auto_enabled),
    ]
}


def _is_due(job: Job, settings: AppSettings, last_run: datetime | None, current: datetime, state: JobState | None = None) -> bool:
    if job.enabled is not None and not job.enabled(settings):
        return False
    if job.interval is None:
        slot = current.replace(hour=settings.email.digest_hour % 24, minute=0, second=0)
        return (current >= slot and (last_run is None or last_run < slot)) or (
            job.name == 'digest' and state is not None and state.retry_at is not None and current >= state.retry_at
        )
    if last_run is None:
        return True
    return current - last_run >= timedelta(hours=max(job.interval(settings), 0.1))


def next_run(job: Job, settings: AppSettings, last_run: datetime | None, state: JobState | None = None) -> datetime | None:
    if job.enabled is not None and not job.enabled(settings):
        return None
    current = now()
    if job.interval is None:
        slot = current.replace(hour=settings.email.digest_hour % 24, minute=0, second=0)
        if last_run is not None and last_run >= slot:
            slot += timedelta(days=1)
        if job.name == 'digest' and state is not None and state.retry_at is not None:
            return min(slot, state.retry_at)
        return slot
    if last_run is None:
        return current
    return last_run + timedelta(hours=max(job.interval(settings), 0.1))


@dataclass
class _Request:
    job: str
    movie_ids: list[int] | None = None


class Scheduler:
    def __init__(self) -> None:
        self._queue: queue.Queue[_Request] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self.running: str | None = None
        self._request_lock = threading.Lock()
        self._pending_requests: set[tuple] = set()
        self._active_request: tuple | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(_Request(""))

    @staticmethod
    def _request_key(job: str, movie_ids: list[int] | None) -> tuple:
        return job, tuple(sorted(set(movie_ids))) if movie_ids is not None else None

    def trigger(self, job: str, movie_ids: list[int] | None = None) -> bool:
        """Queue a manual run; it executes on the scheduler thread."""
        key = self._request_key(job, movie_ids)
        with self._request_lock:
            if job not in JOBS or self._stop.is_set() or key in self._pending_requests or key == self._active_request or len(self._pending_requests) >= MAX_PENDING_REQUESTS:
                return False
            self._pending_requests.add(key)
            self._queue.put(_Request(job, list(movie_ids) if movie_ids is not None else None))
        return True

    def _loop(self) -> None:
        if self._stop.wait(STARTUP_DELAY_SECONDS):
            return
        while not self._stop.is_set():
            self._run_due()
            try:
                request = self._queue.get(timeout=TICK_SECONDS)
            except queue.Empty:
                continue
            if request.job in JOBS:
                # A manual run of a full job counts as its scheduled run.
                try:
                    self._execute(JOBS[request.job], request.movie_ids, record=request.movie_ids is None)
                finally:
                    with self._request_lock:
                        self._pending_requests.discard(self._request_key(request.job, request.movie_ids))

    def _run_due(self) -> None:
        settings = load_settings()
        with session_scope() as session:
            states = {s.name: s for s in session.query(JobState)}
        current = now()
        for job in JOBS.values():
            if self._stop.is_set():
                return
            with self._request_lock:
                if self._request_key(job.name, None) in self._pending_requests:
                    continue
            state = states.get(job.name)
            if _is_due(job, settings, state.last_run_at if state else None, current, state):
                self._execute(job, None, record=True)
        # Initial information lookup only: never approves, searches PT or downloads.
        from .services.movie_metadata import queue_candidate_backfill
        if not self._stop.is_set():
            queue_candidate_backfill()

    def _execute(self, job: Job, movie_ids: list[int] | None, record: bool) -> None:
        with self._request_lock:
            self._active_request = self._request_key(job.name, movie_ids)
        try:
            self._execute_job(job, movie_ids, record)
        finally:
            with self._request_lock:
                self._active_request = None

    def _execute_job(self, job: Job, movie_ids: list[int] | None, record: bool) -> None:
        settings = load_settings()
        self.running = job.label
        status, message = "ok", ""
        try:
            message = job.run(settings, movie_ids) if job.takes_movies else job.run(settings)
            log.info("Job %s finished: %s", job.name, message)
        except Exception as exc:
            status, message = "error", str(exc) or exc.__class__.__name__
            log.exception("Job %s failed", job.name)
        finally:
            self.running = None
        try:
            notify_urgent(settings)
        except Exception:
            log.exception("Sending urgent notifications failed")
        if record:
            with session_scope() as session:
                state = session.get(JobState, job.name) or JobState(name=job.name)
                if job.name == 'digest':
                    _record_digest_retry(state, settings, status, now())
                state.last_run_at = now()
                state.last_status = status
                state.last_message = message
                session.add(state)


def _record_digest_retry(state: JobState, settings: AppSettings, status: str, current: datetime) -> None:
    """Persist the retry budget so a container restart cannot reset it."""
    slot = current.replace(hour=settings.email.digest_hour % 24, minute=0, second=0)
    new_day = current >= slot and (state.last_run_at is None or state.last_run_at < slot)
    if new_day or state.last_run_at is None:
        state.retry_count = 0
    elif state.retry_at is not None:
        state.retry_count = (state.retry_count or 0) + 1
    if status == 'ok' or not settings.email.enabled:
        state.retry_count = 0
        state.retry_at = None
    elif (state.retry_count or 0) < len(DIGEST_RETRY_MINUTES):
        state.retry_at = current + timedelta(minutes=DIGEST_RETRY_MINUTES[state.retry_count or 0])
    else:
        state.retry_at = None


scheduler = Scheduler()
