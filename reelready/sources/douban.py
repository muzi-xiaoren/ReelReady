"""Douban via the mobile web (rexxar) JSON API, which works without logging in."""

import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..http import make_client
from .movie_metadata import douban_details, matching_titles

API = "https://m.douban.com/rexxar/api/v2"
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

_ID_PATTERNS = [
    re.compile(r"douban\.com/(?:movie/)?subject/(\d+)"),
    re.compile(r"douban\.com/doubanapp/dispatch/movie/(\d+)"),
    re.compile(r"douban://douban\.com/movie/(\d+)"),
]


class DoubanError(Exception):
    pass


@dataclass
class DoubanMovie:
    id: str
    title: str
    original_title: str | None = None
    year: int | None = None
    rating: float | None = None
    votes: int | None = None
    cover: str | None = None
    intro: str | None = None
    # Names of the platforms Douban lists under "在线观看" (腾讯视频, 爱奇艺, ...).
    vendors: list[str] = field(default_factory=list)
    pre_playable_date: str | None = None
    release_date: str | None = None
    imdb_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def parse_douban_id(text: str) -> str | None:
    for pattern in _ID_PATTERNS:
        if m := pattern.search(text):
            return m.group(1)
    return None


def _year(value: Any) -> int | None:
    m = re.search(r"\d{4}", str(value or ""))
    return int(m.group(0)) if m else None


def _rating(item: dict[str, Any]) -> tuple[float | None, int | None]:
    rating = item.get("rating") or {}
    value = rating.get("value")
    return (float(value) if value else None, rating.get("count"))


def _cover(item: dict[str, Any]) -> str | None:
    # Detail responses put the photo's web page in cover.url; pic holds the poster image.
    pic = item.get("pic")
    if isinstance(pic, dict) and (pic.get("large") or pic.get("normal")):
        return pic.get("large") or pic.get("normal")
    cover = item.get("cover")
    if isinstance(cover, dict) and "doubanio.com" in str(cover.get("url") or ""):
        return cover["url"]
    return item.get("cover_url")


class DoubanClient:
    def __init__(self, proxy: str | None = None, request_delay: float = 1.0) -> None:
        self._client = make_client(
            proxy=proxy,
            headers={"User-Agent": MOBILE_UA, "Referer": "https://m.douban.com/movie/"},
        )
        self._delay = request_delay
        self._last_request = 0.0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DoubanClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        wait = self._last_request + self._delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            resp = self._client.get(f"{API}{path}", params=params)
        except httpx.HTTPError as exc:
            raise DoubanError(f"豆瓣请求失败: {exc}") from exc
        finally:
            self._last_request = time.monotonic()
        if resp.status_code != 200:
            raise DoubanError(f"豆瓣返回 HTTP {resp.status_code}")
        data = resp.json()
        if isinstance(data, dict) and data.get("code") and data.get("msg"):
            raise DoubanError(f"豆瓣接口错误: {data['msg']}")
        return data

    def now_showing(self) -> list[DoubanMovie]:
        movies: list[DoubanMovie] = []
        start = 0
        while True:
            data = self._get("/subject_collection/movie_showing/items", start=start, count=50)
            items = data.get("subject_collection_items") or []
            for item in items:
                if item.get("type") not in (None, "movie"):
                    continue
                rating, votes = _rating(item)
                movies.append(
                    DoubanMovie(
                        id=str(item["id"]),
                        title=item.get("title") or "",
                        original_title=item.get("original_title") or None,
                        year=_year(item.get("year")),
                        release_date=item.get("release_date") or None,
                        rating=rating,
                        votes=votes,
                        cover=_cover(item),
                    )
                )
            start += len(items)
            if not items or start >= int(data.get("total") or 0):
                return movies

    def recent_high_rated(self, first_year: int, last_year: int, min_rating: float, *,
                          progress: dict | None = None, checkpoint: Callable | None = None) -> Iterator[DoubanMovie]:
        """Resume bounded batches instead of repeatedly reading only the first 150.

        A cursor advances only after the consumer has processed the entire page.
        No database transaction is held while fetching or consuming a page.
        """
        import json

        progress = progress if progress is not None else {}
        years = list(range(last_year, first_year - 1, -1))
        if not years:
            return
        next_year = progress.get('next_year', last_year)
        if next_year in years:
            index = years.index(next_year)
            years = years[index:] + years[:index]
        cursors = progress.setdefault('years', {})
        budget = 6  # At most 300 feed entries per run, regardless of the year range.
        for year in years:
            cursor = cursors.setdefault(str(year), {'offset': 0})
            start = max(0, int(cursor.get('offset', 0)))
            seen = set()
            for _ in range(min(3, budget)):
                budget -= 1
                data = self._get('/movie/recommend', start=start, count=50, sort='S',
                                 tags=str(year), selected_categories=json.dumps({'类型': '电影'}),
                                 range=f'{min_rating},10')
                items = data.get('items') or []
                if not isinstance(items, list):
                    raise DoubanError('豆瓣推荐列表格式异常，已保留扫描进度')
                page_ids = {str(item['id']) for item in items if isinstance(item, dict) and item.get('id')}
                repeated = bool(page_ids) and (
                    page_ids.issubset(seen) or
                    (start > 0 and page_ids == set(cursor.get('last_ids') or []))
                )
                for item in ([] if repeated else items):
                    if not isinstance(item, dict) or item.get('type') not in (None, 'movie') or not item.get('id'):
                        continue
                    identity = str(item['id'])
                    if identity in seen:
                        continue
                    seen.add(identity)
                    rating, votes = _rating(item)
                    yield DoubanMovie(id=identity, title=item.get('title') or '',
                                      year=_year(item.get('year')), rating=rating, votes=votes,
                                      cover=_cover(item))
                ended = repeated or len(items) < 50 or not page_ids
                start = 0 if ended else start + len(items)
                cursor.update(offset=start, last_ids=sorted(page_ids),
                              stop_reason='列表重复' if repeated else '已到列表末尾' if ended else '分批继续')
                progress['next_year'] = year
                if checkpoint:
                    checkpoint()
                if ended:
                    break
            progress['next_year'] = year - 1 if year > first_year else last_year
            if checkpoint:
                checkpoint()
            if budget <= 0:
                break

    def detail(self, douban_id: str) -> DoubanMovie:
        data = self._get(f"/movie/{douban_id}")
        details = douban_details(data)
        rating, votes = _rating(data)
        vendors = [v.get("title") for v in data.get("vendors") or [] if v.get("title")]
        return DoubanMovie(
            id=str(data.get("id") or douban_id),
            title=data.get("title") or "",
            original_title=data.get("original_title") or None,
            year=_year(data.get("year")),
            release_date=details.get("release_date"),
            rating=rating,
            votes=votes,
            cover=_cover(data),
            intro=data.get("intro") or None,
            vendors=vendors,
            pre_playable_date=data.get("pre_playable_date") or None,
            imdb_id=data.get("imdb_id") or None,
            details=details,
        )

    def search(self, query: str, count: int = 10) -> list[DoubanMovie]:
        data = self._get("/search/movie", q=query, count=count)
        results: list[DoubanMovie] = []
        for item in data.get("items") or []:
            target = item.get("target") or {}
            if item.get("target_type") not in (None, "movie") or not target.get("id"):
                continue
            rating, votes = _rating(target)
            results.append(
                DoubanMovie(
                    id=str(target["id"]),
                    title=target.get("title") or "",
                    year=_year(target.get("year")),
                    rating=rating,
                    votes=votes,
                    cover=_cover(target),
                )
            )
        return results

    def find(self, titles: list[str], year: int | None) -> DoubanMovie | None:
        """Best-effort lookup of a movie on Douban by title + year."""
        for title in dict.fromkeys(t for t in titles if t):
            for result in self.search(title):
                if matching_titles(titles, [result.title, result.original_title or '']) and (
                    year is None or result.year is None or abs(result.year - year) <= 1
                ):
                    return result
        return None
