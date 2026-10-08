"""Site management: CookieCloud snapshots, auto-detection and connection tests."""

import logging
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..cookiecloud import CookieCloudError, cookie_header_for, decrypt, detect_cookie_hosts
from ..db import session_scope
from ..models import CookieCloudBlob, Setting, Site, now
from ..settings import AppSettings
from ..site_identity import site_identity
from ..sites.catalog import lookup_site, supported_kind
from ..sites import LoginExpired, SiteError, build_site, snapshot

log = logging.getLogger(__name__)

HIDDEN_SITES_KEY = "hidden_discovery_sites"


def discovery_identity(host: str) -> str:
    host = host.lower().strip().lstrip(".").removeprefix("www.")
    entry = lookup_site(host)
    return "catalog:" + entry["id"] if entry else "host:" + host


def hidden_discovery_sites() -> dict[str, str]:
    with session_scope() as session:
        row = session.get(Setting, HIDDEN_SITES_KEY)
        return dict(row.value) if row else {}


def set_discovery_hidden(host: str, hidden: bool) -> None:
    with session_scope() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        row = session.get(Setting, HIDDEN_SITES_KEY)
        values = dict(row.value) if row else {}
        key = discovery_identity(host)
        if hidden:
            values[key] = host
        else:
            values.pop(key, None)
        if row:
            row.value = values
        else:
            session.add(Setting(key=HIDDEN_SITES_KEY, value=values))


def load_cookiecloud(settings: AppSettings) -> tuple[dict[str, Any] | None, CookieCloudBlob | None]:
    """Decrypt the stored snapshot, if any."""
    cc = settings.cookiecloud
    with session_scope() as session:
        blob = session.get(CookieCloudBlob, cc.uuid)
        if blob is None:
            return None, None
        session.expunge(blob)
    try:
        return decrypt(blob.encrypted, cc.uuid, cc.password, blob.crypto_type), blob
    except CookieCloudError:
        log.warning("Stored CookieCloud snapshot cannot be decrypted")
        return None, blob


def store_cookiecloud(settings: AppSettings, uuid: str, encrypted: str, crypto_type: str) -> int:
    """Save a snapshot pushed by the extension and refresh site cookies. Returns sites updated."""
    cc = settings.cookiecloud
    # Decrypt first so a wrong password is rejected instead of silently stored.
    data = decrypt(encrypted, cc.uuid, cc.password, crypto_type)
    with session_scope() as session:
        blob = session.get(CookieCloudBlob, uuid)
        if blob is None:
            session.add(CookieCloudBlob(uuid=uuid, encrypted=encrypted, crypto_type=crypto_type))
        else:
            blob.encrypted = encrypted
            blob.crypto_type = crypto_type
            blob.updated_at = now()
    return apply_cookies(data)


def apply_cookies(data: dict[str, Any]) -> int:
    updated = 0
    with session_scope() as session:
        for site in session.scalars(select(Site).where(Site.kind.in_(("nexusphp", "unit3d")), Site.use_cookiecloud.is_(True))):
            cookie = cookie_header_for(data, site.base_url)
            if cookie and cookie != site.cookie:
                site.cookie = cookie
                # A new cookie deserves a fresh attempt even if the old one was rejected.
                site.status = "unknown"
                site.status_message = "cookie 已从 CookieCloud 更新"
                updated += 1
    return updated


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def detected_sites(settings: AppSettings) -> list[str]:
    """NexusPHP hosts found in the CookieCloud snapshot that are not configured yet."""
    data, _ = load_cookiecloud(settings)
    if not data:
        return []
    with session_scope() as session:
        known = {site_identity(site.kind, site.base_url) for site in session.scalars(select(Site))}
    return [h for h in detect_cookie_hosts(data) if site_identity(supported_kind(h) or "nexusphp", f"https://{h}") not in known]


def add_site(
    settings: AppSettings,
    *,
    kind: str,
    name: str,
    base_url: str,
    api_key: str | None = None,
    cookie: str | None = None,
    category: str = "standard",
) -> tuple[Site, bool]:
    base_url = base_url.strip().rstrip("/")
    if base_url and "://" not in base_url:
        base_url = f"https://{base_url}"
    key = site_identity(kind, base_url)
    with session_scope() as session:
        existing = session.scalar(select(Site).where(Site.identity_key == key))
        if existing is not None:
            return existing, False
    manual_cookie = bool((cookie or "").strip())
    if kind in ("nexusphp", "unit3d") and not manual_cookie:
        data, _ = load_cookiecloud(settings)
        if data:
            cookie = cookie_header_for(data, base_url)
    try:
        with session_scope() as session:
            site = Site(
                identity_key=key,
                category="custom" if category == "custom" else "standard",
                kind=kind,
                name=name.strip() or _host(base_url) or kind,
                base_url=base_url,
                api_key=(api_key or "").strip() or None,
                cookie=(cookie or "").strip() or None,
                # A pasted cookie should not be overwritten by later CookieCloud pushes.
                use_cookiecloud=kind in ("nexusphp", "unit3d") and not manual_cookie,
            )
            session.add(site)
        return site, True
    except IntegrityError:
        # A second request may have inserted this identity after our initial lookup.
        with session_scope() as session:
            existing = session.scalar(select(Site).where(Site.identity_key == key))
            if existing is None:
                raise
            return existing, False


def test_site(settings: AppSettings, site_id: int) -> tuple[bool, str]:
    with session_scope() as session:
        row = session.get(Site, site_id)
        if row is None:
            return False, "站点不存在"
        config = snapshot(row, settings.network.user_agent)
    ok, message, status = False, "", "error"
    try:
        site = build_site(config)
        try:
            message = site.test()
            ok, status = True, "ok"
        finally:
            site.close()
    except LoginExpired as exc:
        message, status = str(exc), "invalid"
    except SiteError as exc:
        message = str(exc)
    with session_scope() as session:
        row = session.get(Site, site_id)
        if row is not None:
            current = snapshot(row, settings.network.user_agent)
            if (current.kind, current.base_url, current.cookie, current.api_key) != (config.kind, config.base_url, config.cookie, config.api_key):
                return False, "配置已更新，请重新测试连接"
            row.status = status
            row.status_message = message
            row.last_checked_at = now()
    return ok, message
