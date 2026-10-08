"""Rousi Pro's PeerGo API v1; public contract from PT-Depiler rousipro.ts."""

from urllib.parse import urljoin, urlsplit

import httpx

from ..http import make_client
from ..torrent_rules import TorrentInfo
from .base import BaseSite, LoginExpired, MovieQuery, SiteError, looks_like_torrent


class RousiProSite(BaseSite):
    def __init__(self, config, proxy=None):
        super().__init__(config, proxy)
        if not config.api_key:
            raise LoginExpired("请在 Rousi Pro 账户设置创建个人 API Key，Cookie 不能替代 API Key")
        self.base = config.base_url.rstrip("/") + "/"
        self._client = make_client(proxy=proxy, headers={
            "Authorization": f"Bearer {config.api_key}",
            "User-Agent": config.user_agent,
            "Accept": "application/json",
        })
        self._client.follow_redirects = False

    def close(self):
        self._client.close()

    def _get(self, path, **params):
        url = urljoin(self.base, path)
        if urlsplit(url)[:2] != urlsplit(self.base)[:2]:
            raise SiteError("拒绝向其他站点发送 Rousi Pro API Key")
        try:
            response = self._client.get(url, params=params or None)
        except httpx.HTTPError as exc:
            # Signed download URLs and keys must not appear in logs/toasts.
            raise SiteError("Rousi Pro 网络请求失败，请检查网络或稍后重试") from None
        if response.status_code == 401:
            raise LoginExpired("Rousi Pro API Key 无效或已过期")
        if response.status_code == 403:
            raise SiteError("Rousi Pro API 权限不足，请检查 API Key 授权范围")
        if response.status_code != 200:
            raise SiteError(f"Rousi Pro HTTP {response.status_code}")
        return response

    def _api(self, path, **params):
        try:
            body = self._get(path, **params).json()
        except ValueError:
            raise SiteError("Rousi Pro 未返回有效 API 数据") from None
        if not isinstance(body, dict) or str(body.get("code")) not in ("0", "200"):
            raise SiteError("Rousi Pro API 返回错误，请检查密钥权限和站点状态")
        data = body.get("data")
        if not isinstance(data, dict):
            raise SiteError("Rousi Pro API 数据格式已变化")
        return data

    def test(self):
        profile = self._api("api/v1/profile")
        if not profile.get("id") or not profile.get("username"):
            raise SiteError("Rousi Pro 未返回已认证的用户信息")
        return "已连接 Rousi Pro"

    def search(self, query: MovieQuery):
        # PeerGo has no IMDb filter: search titles and let matches_movie verify hits.
        for title in query.titles:
            data = self._api("api/v1/torrents", keyword=title, page=1, page_size=100)
            items = data.get("torrents")
            if not isinstance(items, list):
                raise SiteError("Rousi Pro 搜索数据格式已变化")
            if items:
                return [TorrentInfo(
                    site_id=self.config.id, site_name=self.name,
                    torrent_id=str(item["id"]), title=item.get("title") or "",
                    subtitle=item.get("subtitle") or "", size_bytes=int(item.get("size") or 0),
                    seeders=int(item.get("seeders") or 0), category=item.get("category"),
                    detail_url=urljoin(self.base, f"torrents/{item['id']}"),
                ) for item in items]
        return []

    def download(self, torrent: TorrentInfo):
        if not torrent.torrent_id.isdigit():
            raise SiteError("无效的 Rousi Pro 种子编号")
        detail = self._api(f"api/v1/torrents/{torrent.torrent_id}")
        url = detail.get("download_url")
        if not isinstance(url, str) or not url:
            raise SiteError("该种子不可下载，请检查下载权限；付费种子需自行处理")
        response = self._get(url)
        if not looks_like_torrent(response.content):
            raise SiteError("Rousi Pro 没有返回有效种子文件")
        return response.content
