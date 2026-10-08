import logging
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import session_scope
from ..models import Event, now
from ..notifier import NotifyError, render_digest, render_single, send_mail
from ..settings import AppSettings

log = logging.getLogger(__name__)


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
    with session_scope() as session:
        events = _pending(session, urgent=True)
        if not events:
            return
        if not settings.email.enabled:
            for event in events:
                event.notified = True
            return
        for event in events:
            try:
                send_mail(settings.email, f"[ReelReady] {event.title}", render_single(event.title, event.message))
            except NotifyError as exc:
                log.warning("Failed to send notification: %s", exc)
                return
            event.notified = True


def send_digest(settings: AppSettings) -> str:
    with session_scope() as session:
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
