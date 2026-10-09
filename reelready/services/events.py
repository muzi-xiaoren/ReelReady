import logging
import threading
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select, delete, update
from sqlalchemy.orm import Session

from ..db import session_scope
from ..models import Event, now
from ..notifier import NotifyError, render_digest, render_single, send_mail
from ..settings import AppSettings

log = logging.getLogger(__name__)
_notification_lock = threading.Lock()


def add_event(
    session: Session,
    kind: str,
    title: str,
    message: str = "",
    *,
    movie_id: int | None = None,
    urgent: bool = False,
    notify: bool = True,
    dedupe_key: str | None = None,
    dedupe_hours: float = 24,
) -> Event | None:
    """Record an event; skipped if one with the same ``dedupe_key`` happened recently."""
    if dedupe_key:
        since = now() - timedelta(hours=dedupe_hours)
        recent = session.scalar(
            select(Event.id).where(Event.dedupe_key == dedupe_key, Event.created_at >= since).limit(1)
        )
        if recent:
            return None
    event = Event(
        kind=kind,
        title=title,
        message=message,
        movie_id=movie_id,
        urgent=urgent,
        notified=not notify,
        dedupe_key=dedupe_key,
    )
    session.add(event)
    return event


def _pending(session: Session, urgent: bool) -> list[Event]:
    return list(
        session.scalars(
            select(Event).where(Event.notified.is_(False), Event.urgent.is_(urgent)).order_by(Event.created_at)
        )
    )


def notify_urgent(settings: AppSettings) -> None:
    """Mail every urgent event that has not been sent yet."""
    with _notification_lock:
        _notify_urgent(settings)


def _notify_urgent(settings: AppSettings) -> None:
    with session_scope() as session:
        if not settings.email.enabled:
            session.execute(update(Event).where(Event.notified.is_(False), Event.urgent.is_(True)).values(notified=True))
            return
        ids = list(session.scalars(select(Event.id).where(Event.notified.is_(False), Event.urgent.is_(True)).order_by(Event.created_at)))
    # Commit each successful message separately: a later failure must not undo it.
    for event_id in ids:
        with session_scope() as session:
            event = session.get(Event, event_id)
            if event is None or event.notified:
                continue
            if not settings.email.enabled:
                event.notified = True
                continue
            title = event.title.replace('已开始下载', '下载任务已提交') if event.kind == 'downloaded' else event.title
            try:
                send_mail(settings.email, f"[ReelReady] {title}", render_single(title, event.message, event.created_at))
            except NotifyError as exc:
                log.warning("Failed to send notification: %s", exc)
                return
            event.notified = True


def send_digest(settings: AppSettings) -> str:
    with _notification_lock:
        return _send_digest(settings)


def _send_digest(settings: AppSettings) -> str:
    with session_scope() as session:
        if not settings.email.enabled:
            session.execute(update(Event).where(Event.notified.is_(False), Event.urgent.is_(False)).values(notified=True))
            return '邮件未启用，已跳过'
        events = _pending(session, urgent=False)
        if not events:
            return "没有需要汇总的消息"
        if settings.email.enabled:
            groups: dict[str, list] = defaultdict(list)
            for event in events:
                groups[event.kind].append((event.title, event.message, event.created_at))
            send_mail(settings.email, f"[ReelReady] 每日汇总（{len(events)} 条）", render_digest(dict(groups)))
        for event in events:
            event.notified = True
        return f"已汇总 {len(events)} 条消息" if settings.email.enabled else "邮件未启用，已跳过"


def cleanup_events(settings: AppSettings) -> str:
    days = settings.events.retention_days
    if days is None:
        return '动态永久保留，未清理'
    with _notification_lock, session_scope() as session:
        result = session.execute(delete(Event).where(Event.created_at < now() - timedelta(days=days)))
        return f'已清理 {result.rowcount} 条超过 {days} 天的动态'


def delete_event(event_id: int) -> None:
    with _notification_lock, session_scope() as session:
        session.execute(delete(Event).where(Event.id == event_id))
