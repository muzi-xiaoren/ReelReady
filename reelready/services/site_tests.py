"""Bounded connection checks, separate from HTTP requests and scheduled movie jobs."""

from concurrent.futures import ThreadPoolExecutor
import logging
import threading

from ..db import session_scope
from ..models import Site
from ..settings import load_settings
from .sites import test_site

log = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="site-check")
_pending: set[int] = set()
_lock = threading.Lock()
MAX_PENDING = 16


def queue_site_test(site_id: int) -> str:
    with _lock:
        if site_id in _pending:
            return "pending"
        if len(_pending) >= MAX_PENDING:
            return "busy"
        with session_scope() as session:
            site = session.get(Site, site_id)
            if site is None:
                return "missing"
            site.status = "testing"
            site.status_message = "等待 / 正在测试连接"
        _pending.add(site_id)
        _executor.submit(_run, site_id)
        return "queued"


def _run(site_id: int) -> None:
    try:
        test_site(load_settings(), site_id)
    except Exception:
        log.exception("Background connection test failed for site %s", site_id)
        with session_scope() as session:
            site = session.get(Site, site_id)
            if site:
                site.status = "error"
                site.status_message = "连接测试异常，请稍后重试"
    finally:
        with _lock:
            _pending.discard(site_id)
