"""Receive cookies from the CookieCloud browser extension.

ReelReady speaks the CookieCloud server protocol (``POST /update``, ``/get/:uuid``), so the
stock extension can push straight to it. Payloads are encrypted with a key derived from
``md5(uuid + "-" + password)[:16]``; two schemes exist:

- ``legacy``: CryptoJS passphrase mode, i.e. OpenSSL ``Salted__`` + EVP_BytesToKey (MD5) + AES-256-CBC
- ``aes-128-cbc-fixed``: the 16 chars are the raw AES-128 key, IV is all zeros
"""

import base64
import hashlib
import json
import time
from typing import Any
from urllib.parse import urlsplit

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from .sites.catalog import lookup_site


class CookieCloudError(Exception):
    pass


def _passphrase(uuid: str, password: str) -> bytes:
    return hashlib.md5(f"{uuid}-{password}".encode()).hexdigest()[:16].encode()


def _evp_bytes_to_key(passphrase: bytes, salt: bytes, length: int) -> bytes:
    derived, block = b"", b""
    while len(derived) < length:
        block = hashlib.md5(block + passphrase + salt).digest()
        derived += block
    return derived[:length]


def decrypt(encrypted: str, uuid: str, password: str, crypto_type: str = "legacy") -> dict[str, Any]:
    try:
        raw = base64.b64decode(encrypted)
        passphrase = _passphrase(uuid, password)
        if crypto_type == "aes-128-cbc-fixed":
            cipher = AES.new(passphrase, AES.MODE_CBC, iv=bytes(16))
            plain = unpad(cipher.decrypt(raw), AES.block_size)
        else:
            if raw[:8] != b"Salted__":
                raise CookieCloudError("无法识别的加密格式")
            key_iv = _evp_bytes_to_key(passphrase, raw[8:16], 48)
            cipher = AES.new(key_iv[:32], AES.MODE_CBC, iv=key_iv[32:])
            plain = unpad(cipher.decrypt(raw[16:]), AES.block_size)
        return json.loads(plain.decode("utf-8"))
    except CookieCloudError:
        raise
    except (ValueError, KeyError, UnicodeDecodeError) as exc:
        raise CookieCloudError("解密失败，请检查用户 KEY 和端对端加密密码") from exc


def _iter_cookies(data: dict[str, Any]):
    for cookies in (data.get("cookie_data") or {}).values():
        for cookie in cookies or []:
            if cookie.get("name") and cookie.get("domain"):
                yield cookie


def _domain_matches(host: str, cookie_domain: str) -> bool:
    domain = cookie_domain.lstrip(".").lower()
    return host == domain or host.endswith(f".{domain}")


def cookie_header_for(data: dict[str, Any], url: str) -> str | None:
    """Build a ``Cookie`` header with every synced cookie that applies to ``url``."""
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return None
    jar: dict[str, str] = {}
    for cookie in _iter_cookies(data):
        if (
            _cookie_active(cookie)
            and _domain_matches(host, cookie["domain"])
            and (not cookie.get("hostOnly") or host == cookie["domain"].lstrip(".").lower())
        ):
            jar[cookie["name"]] = cookie.get("value", "")
    return "; ".join(f"{k}={v}" for k, v in jar.items()) or None


def detect_nexusphp_hosts(data: dict[str, Any]) -> list[str]:
    """Suggest NexusPHP domains from distinctive cookies, without assuming login is valid."""
    names_by_domain: dict[str, set[str]] = {}
    for cookie in _iter_cookies(data):
        if not _cookie_active(cookie):
            continue
        domain = cookie["domain"].lstrip(".").lower()
        names_by_domain.setdefault(domain, set()).add(cookie["name"])
    detected = set()
    for domain, names in names_by_domain.items():
        site = lookup_site(domain)
        if site is not None:
            if site["schema"] == "NexusPHP" and _has_login_cookies(names, site["id"]):
                detected.add(domain)
        elif "c_secure_pass" in names or {"nexusphp_uid", "nexusphp_token"} <= names:
            detected.add(domain)
    # Do not discard a www host with login cookies in favor of a bare host that only
    # carries analytics/Cloudflare cookies; host-only cookies cannot log into both.
    for host in sorted(detected):
        if host.startswith("www.") and host[4:] in detected:
            bare = host[4:]
            if _login_cookie_strength(names_by_domain[host]) > _login_cookie_strength(names_by_domain[bare]):
                detected.remove(bare)
            else:
                detected.remove(host)
    return sorted(detected)


def _cookie_active(cookie: dict[str, Any]) -> bool:
    if not cookie.get("value"):
        return False
    expires = cookie.get("expirationDate")
    return expires is None or expires > time.time()


def _has_login_cookies(names: set[str], site_id: str) -> bool:
    if _login_cookie_strength(names):
        return True
    # Custom authentication names are used only for their registered site.
    custom = {
        "springsunday": {"SPRINGID"},
        "qingwa": {"qw_session"},
        "tjupt": {"access_token"},
        "byr": {"auth_token", "refresh_token"},
        "byrbt": {"auth_token", "refresh_token"},
        "filept": {"sid"},
        "bitbr": {"sid"},
    }
    required = custom.get(site_id)
    return bool(required and required <= names)


def _login_cookie_strength(names: set[str]) -> int:
    if {"c_secure_uid", "c_secure_pass"} <= names or {"nexusphp_uid", "nexusphp_token"} <= names:
        return 2
    return int("c_secure_pass" in names)


def synced_cookie_hosts(data: dict[str, Any]) -> list[str]:
    """All synced domains, available for manual selection without classifying them as PT."""
    return sorted({c["domain"].lstrip(".").lower() for c in _iter_cookies(data)})


def detect_cookie_hosts(data: dict[str, Any]) -> list[str]:
    hosts = set(detect_nexusphp_hosts(data))
    for cookie in _iter_cookies(data):
        entry = lookup_site(cookie["domain"])
        if entry and entry["id"] == "monikadesign" and _cookie_active(cookie):
            name = cookie["name"]
            if name.startswith("remember_web_") or name == "monikadesign_session":
                hosts.add(cookie["domain"].lstrip(".").lower())
    # Known mirrors represent one site. Keep credentials on their actual host.
    choices = {}
    for host in sorted(hosts):
        entry = lookup_site(host)
        choices.setdefault(entry["id"] if entry else host, host)
    return sorted(choices.values())
