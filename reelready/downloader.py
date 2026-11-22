"""qBittorrent Web API v2 client."""

import hashlib

import httpx
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .http import make_client
from .settings import QBittorrentSettings


class DownloaderError(Exception):
    pass


def _bencode_end(data: bytes, pos: int) -> int:
    """Index just past the bencoded value starting at ``pos``."""
    lead = data[pos:pos + 1]
    if lead == b"i":
        return data.index(b"e", pos) + 1
    if lead in (b"l", b"d"):
        pos += 1
        while data[pos:pos + 1] != b"e":
            if not data[pos:pos + 1]:
                raise ValueError("unterminated container")
            pos = _bencode_end(data, pos)
        return pos + 1
    if lead.isdigit():
        colon = data.index(b":", pos)
        end = colon + 1 + int(data[pos:colon])
        if end > len(data):
            raise ValueError("truncated string")
        return end
    raise ValueError("invalid bencode")


def torrent_info_hash(content: bytes) -> str | None:
    """The v1 info-hash qBittorrent reports for a .torrent file, or None if unreadable."""
    try:
        if content[:1] != b"d":
            return None
        pos = 1
        while content[pos:pos + 1] != b"e":
            key_end = _bencode_end(content, pos)
            key = content[content.index(b":", pos) + 1:key_end]
            value_end = _bencode_end(content, key_end)
            if key == b"info":
                return hashlib.sha1(content[key_end:value_end]).hexdigest()
            pos = value_end
    except (ValueError, IndexError):
        return None
    return None


class QBittorrent:
    def __init__(self, config: QBittorrentSettings) -> None:
        if not config.url:
            raise DownloaderError("未配置 qBittorrent 地址")
        self.config = config
        self.base = config.url.rstrip("/")
        address = urlsplit(self.base)
        if Path('/.dockerenv').exists() and address.hostname in ('localhost', '127.0.0.1', '::1'):
            host = 'host.docker.internal' + (f':{address.port}' if address.port else '')
            self.base = urlunsplit(address._replace(netloc=host))
        # qBittorrent rejects requests whose Referer/Origin does not match its own host.
        self._client = make_client(headers={"Referer": self.base}, trust_env=False)
        self._logged_in = False

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "QBittorrent":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            return self._client.request(method, f"{self.base}/api/v2{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise DownloaderError(f"连接 qBittorrent 失败: {exc}") from exc

    def login(self) -> None:
        if self._logged_in:
            return
        if self.config.username:
            resp = self._request(
                "POST",
                "/auth/login",
                data={"username": self.config.username, "password": self.config.password},
            )
            if resp.status_code == 403:
                raise DownloaderError("qBittorrent 拒绝登录（失败次数过多被临时封禁）")
            if resp.text.strip() != "Ok.":
                raise DownloaderError("qBittorrent 用户名或密码错误")
        self._logged_in = True

    def version(self) -> str:
        self.login()
        resp = self._request("GET", "/app/version")
        if resp.status_code == 403:
            raise DownloaderError("qBittorrent 未授权，请检查用户名和密码")
        if resp.status_code != 200:
            raise DownloaderError(f"qBittorrent 返回 HTTP {resp.status_code}")
        return resp.text.strip()

    def _ensure_category(self) -> None:
        if not self.config.category:
            return
        data = {"category": self.config.category}
        if self.config.save_path:
            data["savePath"] = self.config.save_path
        # 409 means the category already exists, which is fine.
        self._request("POST", "/torrents/createCategory", data=data)

    def add_torrent(self, content: bytes, filename: str) -> None:
        self.login()
        self._ensure_category()
        data: dict[str, str] = {}
        if self.config.save_path:
            data["savepath"] = self.config.save_path
        if self.config.category:
            data["category"] = self.config.category
        if self.config.tags:
            data["tags"] = self.config.tags
        resp = self._request(
            "POST",
            "/torrents/add",
            data=data,
            files={"torrents": (filename, content, "application/x-bittorrent")},
        )
        if resp.status_code == 403:
            raise DownloaderError("qBittorrent 未授权，请检查用户名和密码")
        if resp.status_code != 200 or resp.text.strip() == "Fails.":
            raise DownloaderError(f"qBittorrent 添加种子失败: {resp.text.strip() or resp.status_code}")

    def torrents(self, hashes: list[str] | None = None) -> list[dict]:
        """Torrent rows from qBittorrent; all torrents (in the configured category) without hashes."""
        self.login()
        params: dict[str, str] = {}
        if hashes:
            params["hashes"] = "|".join(hashes)
        elif self.config.category:
            params["category"] = self.config.category
        resp = self._request("GET", "/torrents/info", params=params)
        if resp.status_code == 403:
            raise DownloaderError("qBittorrent 未授权，请检查用户名和密码")
        if resp.status_code != 200:
            raise DownloaderError(f"qBittorrent 返回 HTTP {resp.status_code}")
        try:
            rows = resp.json()
        except ValueError as exc:
            raise DownloaderError("qBittorrent 返回了无法识别的种子列表") from exc
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
