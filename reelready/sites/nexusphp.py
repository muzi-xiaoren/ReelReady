"""Generic NexusPHP site, authenticated with the browser cookie.

NexusPHP templates differ between sites, so parsing relies on stable anchors (links to
``details.php?id=`` / ``download.php?id=``, the ``sort=`` links in the header row) rather
than on CSS class names.
"""

import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from ..http import make_client
from ..torrent_rules import TorrentInfo
from .base import BaseSite, LoginExpired, MovieQuery, SiteConfig, SiteError, looks_like_torrent

_DETAIL_ID = re.compile(r"details\.php\?id=(\d+)")
_IMDB = re.compile(r"imdb\.com/title/(tt\d{6,10})")
_SIZE = re.compile(r"([\d.,]+)\s*([KMGT])i?B", re.I)
_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
# NexusPHP header sort keys: 5 = size, 7 = seeders.
_SORT_SIZE = re.compile(r"sort=5\b")
_SORT_SEEDERS = re.compile(r"sort=7\b")

# Cookies set by NexusPHP on login; used to recognise NexusPHP sites among synced cookies.
NEXUSPHP_COOKIES = {"c_secure_uid", "c_secure_pass"}


def parse_size(text: str) -> int:
    m = _SIZE.search(text)
    if not m:
        return 0
    return int(float(m.group(1).replace(",", "")) * _UNITS[m.group(2).upper()])


def _int(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else 0


def _is_logged_in(html: str) -> bool:
    return "logout.php" in html or "usercp.php" in html


class NexusPHPSite(BaseSite):
    def __init__(self, config: SiteConfig, proxy: str | None = None) -> None:
        super().__init__(config, proxy)
        if not config.cookie:
            raise LoginExpired("还没有 cookie")
        self.base = config.base_url.rstrip("/") + "/"
        self._client = make_client(
            proxy=proxy,
            headers={
                "Cookie": config.cookie,
                "User-Agent": config.user_agent,
                "Referer": self.base,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, **params: object) -> httpx.Response:
        try:
            resp = self._client.get(urljoin(self.base, path), params=params or None)
        except httpx.HTTPError as exc:
            raise SiteError(f"请求失败: {exc}") from exc
        if resp.status_code in (403, 503) and "cf-" in resp.text[:5000].lower():
            raise SiteError("被 Cloudflare 拦截，请在浏览器里打开一次该站点后重新同步 cookie")
        if resp.status_code != 200:
            raise SiteError(f"HTTP {resp.status_code}")
        return resp

    def _get_page(self, path: str, **params: object) -> str:
        resp = self._get(path, **params)
        html = resp.text
        if "login" in resp.url.path or not _is_logged_in(html):
            raise LoginExpired("cookie 已失效，请在浏览器里重新登录")
        return html

    def test(self) -> str:
        self._get_page("index.php")
        return "已登录"

    def search(self, query: MovieQuery) -> list[TorrentInfo]:
        # search_area=4 searches the IMDb link field, which is far more precise than titles.
        attempts: list[dict[str, object]] = []
        if query.imdb_id:
            attempts.append({"search": query.imdb_id, "search_area": 4})
        for title in query.titles[:1]:
            attempts.append({"search": title, "search_area": 0})
        for params in attempts:
            html = self._get_page("torrents.php", incldead=0, spstate=0, notnewword=1, **params)
            torrents = self._parse_list(html)
            if torrents:
                if params["search_area"] == 4:
                    # Hits of an IMDb search are this movie even when the list shows no IMDb link.
                    for torrent in torrents:
                        torrent.imdb_id = torrent.imdb_id or query.imdb_id
                return torrents
        return []

    def _parse_list(self, html: str) -> list[TorrentInfo]:
        soup = BeautifulSoup(html, "html.parser")
        table = self._find_torrent_table(soup)
        if table is None:
            return []
        rows = _direct_rows(table)
        if not rows:
            return []
        header = rows[0].find_all(["td", "th"], recursive=False)
        size_idx = _column_index(header, _SORT_SIZE)
        seeders_idx = _column_index(header, _SORT_SEEDERS)

        torrents: list[TorrentInfo] = []
        for row in rows[1:]:
            # Cover/icon links may precede the actual torrent title link.
            link = next((anchor for anchor in row.find_all("a", href=_DETAIL_ID) if (anchor.get("title") or anchor.get_text(" ", strip=True)).strip()), None)
            if link is None:
                continue
            torrent_id = _DETAIL_ID.search(link["href"]).group(1)
            title = (link.get("title") or link.get_text(" ", strip=True)).strip()
            if not title:
                continue
            cells = row.find_all("td", recursive=False)
            name_cell = link.find_parent("td")
            subtitle = ""
            if name_cell is not None:
                full = name_cell.get_text(" ", strip=True)
                subtitle = full.replace(title, "", 1).strip()
            imdb = row.find("a", href=_IMDB)

            size = 0
            if size_idx is not None and size_idx < len(cells):
                size = parse_size(cells[size_idx].get_text(" ", strip=True))
            if not size:
                size = next(
                    (parse_size(c.get_text(" ", strip=True)) for c in cells if _SIZE.fullmatch(c.get_text(" ", strip=True))),
                    0,
                )
            seeders = 0
            if seeders_idx is not None and seeders_idx < len(cells):
                seeders = _int(cells[seeders_idx].get_text(strip=True))

            torrents.append(
                TorrentInfo(
                    site_id=self.config.id,
                    site_name=self.name,
                    torrent_id=torrent_id,
                    title=title,
                    subtitle=subtitle,
                    size_bytes=size,
                    seeders=seeders,
                    detail_url=urljoin(self.base, f"details.php?id={torrent_id}"),
                    imdb_id=_IMDB.search(imdb["href"]).group(1) if imdb else None,
                )
            )
        return torrents

    @staticmethod
    def _find_torrent_table(soup: BeautifulSoup) -> Tag | None:
        table = soup.find("table", class_="torrents")
        if table is not None:
            return table
        # Fall back to the table with the most direct rows linking to torrent details.
        best, best_count = None, 0
        for candidate in soup.find_all("table"):
            count = sum(1 for row in _direct_rows(candidate) if row.find("a", href=_DETAIL_ID))
            if count > best_count:
                best, best_count = candidate, count
        return best

    def download(self, torrent: TorrentInfo) -> bytes:
        resp = self._get("download.php", id=torrent.torrent_id)
        if not looks_like_torrent(resp.content):
            if not _is_logged_in(resp.text):
                raise LoginExpired("cookie 已失效，请在浏览器里重新登录")
            raise SiteError("下载种子失败: 站点没有返回种子文件")
        return resp.content


def _direct_rows(table: Tag) -> list[Tag]:
    rows = table.find_all("tr", recursive=False)
    for body in table.find_all("tbody", recursive=False):
        rows.extend(body.find_all("tr", recursive=False))
    return rows


def _column_index(header: list[Tag], pattern: re.Pattern[str]) -> int | None:
    for idx, cell in enumerate(header):
        if cell.find("a", href=pattern):
            return idx
    return None
