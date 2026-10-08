"""M-Team (馒头) through its official API, authenticated with an API key."""

import re
from typing import Any

import httpx

from ..http import make_client
from ..torrent_rules import TorrentInfo
from .base import BaseSite, LoginExpired, MovieQuery, SiteConfig, SiteError, looks_like_torrent

DEFAULT_API = "https://api.m-team.cc"
# Movie category ids, including full discs (420/421) and remuxes (439).
MOVIE_CATEGORIES = ["401", "419", "420", "421", "439", "405", "404"]

_IMDB = re.compile(r"(tt\d{6,10})")


class MTeamSite(BaseSite):
    def __init__(self, config: SiteConfig, proxy: str | None = None) -> None:
        super().__init__(config, proxy)
        if not config.api_key:
            raise LoginExpired("未填写馒头 API Key")
        self.api = (config.base_url or DEFAULT_API).rstrip("/")
        self.web = self.api.replace("://api.", "://kp.", 1)
        self._client = make_client(
            proxy=proxy,
            headers={
                "x-api-key": config.api_key,
                "User-Agent": config.user_agent,
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def _post(self, path: str, *, json: dict | None = None, data: dict | None = None) -> Any:
        try:
            resp = self._client.post(f"{self.api}{path}", json=json, data=data)
        except httpx.HTTPError as exc:
            raise SiteError(f"请求失败: {exc}") from exc
        if resp.status_code in (401, 403):
            raise LoginExpired("API Key 无效或已过期")
        if resp.status_code != 200:
            raise SiteError(f"HTTP {resp.status_code}")
        body = resp.json()
        if str(body.get("code")) != "0":
            message = body.get("message") or "未知错误"
            if "key" in message.lower() or "登录" in message or "認證" in message or "认证" in message:
                raise LoginExpired(message)
            raise SiteError(message)
        return body.get("data")

    def test(self) -> str:
        profile = self._post("/api/member/profile") or {}
        return f"已连接，用户 {profile.get('username') or '未知'}"

    def search(self, query: MovieQuery) -> list[TorrentInfo]:
        keywords = [f"https://www.imdb.com/title/{query.imdb_id}"] if query.imdb_id else []
        keywords += query.titles[:1]
        for keyword in keywords:
            data = self._post(
                "/api/torrent/search",
                json={
                    "keyword": keyword,
                    "categories": MOVIE_CATEGORIES,
                    "pageNumber": 1,
                    "pageSize": 100,
                    "visible": 1,
                },
            )
            items = (data or {}).get("data") or []
            if items:
                # M-Team returns each torrent's IMDb link, so hits are verified by matches_movie.
                return [self._to_torrent(item) for item in items]
        return []

    def _to_torrent(self, item: dict[str, Any]) -> TorrentInfo:
        status = item.get("status") or {}
        labels = item.get("labelsNew") or []
        imdb = _IMDB.search(item.get("imdb") or "")
        return TorrentInfo(
            site_id=self.config.id,
            site_name=self.name,
            torrent_id=str(item["id"]),
            title=item.get("name") or "",
            subtitle=" ".join([item.get("smallDescr") or "", *map(str, labels)]).strip(),
            size_bytes=int(item.get("size") or 0),
            seeders=int(status.get("seeders") or 0),
            detail_url=f"{self.web}/detail/{item['id']}",
            imdb_id=imdb.group(1) if imdb else None,
            category=str(item.get("category")) if item.get("category") is not None else None,
        )

    def download(self, torrent: TorrentInfo) -> bytes:
        url = self._post("/api/torrent/genDlToken", data={"id": torrent.torrent_id})
        if not isinstance(url, str) or not url.startswith("http"):
            raise SiteError("获取下载链接失败")
        try:
            resp = self._client.get(url)
        except httpx.HTTPError as exc:
            raise SiteError(f"下载种子失败: {exc}") from exc
        if resp.status_code != 200 or not looks_like_torrent(resp.content):
            raise SiteError(f"下载种子失败: HTTP {resp.status_code}")
        return resp.content
