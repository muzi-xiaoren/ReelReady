"""Monikadesign's Unit3D HTML interface, authenticated using browser cookies.

Selectors follow the public PT-Depiler Unit3D and Monikadesign definitions.
"""

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..torrent_rules import TorrentInfo
from .base import LoginExpired, MovieQuery, SiteError, looks_like_torrent
from .nexusphp import NexusPHPSite, _int, parse_size

_DETAIL = re.compile(r"/torrents/(\d+)(?:[/?#]|$)")
_IMDB = re.compile(r"tt\d{6,10}")


class Unit3DSite(NexusPHPSite):
    # Reuse the Cookie HTTP transport; all site-specific operations are overridden.
    def _get_page(self, path: str, **params: object) -> str:
        response = self._get(path, **params)
        soup = BeautifulSoup(response.text, "html.parser")
        if "login" in response.url.path or soup.select_one('input[type="password"]'):
            raise LoginExpired("cookie 已失效，请在浏览器里重新登录 Monikadesign")
        if not soup.select_one('a[href*="/logout"], form[action*="/logout"]'):
            raise SiteError("未识别到登录状态，请确认 Cookie 有效或站点页面是否发生变化")
        return response.text

    def test(self) -> str:
        self._get_page("torrents")
        return "已登录"

    def search(self, query: MovieQuery) -> list[TorrentInfo]:
        attempts = []
        if query.imdb_id:
            attempts.append({"imdbId": query.imdb_id.removeprefix("tt")})
        attempts.extend({"name": title} for title in query.titles[:1])
        for params in attempts:
            html = self._get_page("torrents", perPage=100, **params)
            torrents = self._parse_list(html)
            if torrents:
                if "imdbId" in params:
                    for torrent in torrents:
                        torrent.imdb_id = torrent.imdb_id or query.imdb_id
                return torrents
        return []

    def _parse_list(self, html: str) -> list[TorrentInfo]:
        soup = BeautifulSoup(html, "html.parser")
        results, seen = [], set()
        for row in soup.select("table tbody tr"):
            link = row.select_one("a.view-torrent, a.torrent-search--list__name")
            match = _DETAIL.search(link.get("href", "")) if link else None
            if not match or match[1] in seen:
                continue
            seen.add(match[1])
            size = row.select_one("td.torrent-listings-size, td.torrent-search--list__size")
            seeders = row.select_one('a[href*="/peers"] > span.text-green, td.torrent-search--list__seeders')
            subtitle = row.select_one("span.torrent-listings-subhead")
            imdb = row.find("a", href=_IMDB)
            results.append(TorrentInfo(
                site_id=self.config.id, site_name=self.name, torrent_id=match[1],
                title=link.get_text(" ", strip=True),
                subtitle=subtitle.get_text(" ", strip=True) if subtitle else "",
                size_bytes=parse_size(size.get_text(" ", strip=True)) if size else 0,
                seeders=_int(seeders.get_text(strip=True)) if seeders else 0,
                detail_url=urljoin(self.base, link["href"]),
                imdb_id=_IMDB.search(imdb["href"])[0] if imdb else None,
            ))
        return results

    def download(self, torrent: TorrentInfo) -> bytes:
        if not torrent.torrent_id.isdigit():
            raise SiteError("无效的种子编号")
        html = self._get_page(f"torrents/{torrent.torrent_id}")
        soup = BeautifulSoup(html, "html.parser")
        link = soup.select_one('a[href*="/download/"], a[href*="/download_check/"]')
        if link is None:
            raise SiteError("详情页未找到下载链接，站点页面可能已变化")
        url = urljoin(self.base, link["href"].replace("/download_check/", "/download/"))
        if urlsplit(url).netloc != urlsplit(self.base).netloc:
            raise SiteError("详情页返回了其他站点的下载链接")
        response = self._get(url)
        if not looks_like_torrent(response.content):
            if "login" in response.url.path:
                raise LoginExpired("cookie 已失效，请重新登录")
            raise SiteError("下载失败，站点没有返回种子文件")
        return response.content
