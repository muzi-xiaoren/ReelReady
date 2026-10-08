"""The Movie Database (TMDB) v3 API."""

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..http import make_client
from .movie_metadata import tmdb_details

API = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p/w342"

# https://developer.themoviedb.org/reference/movie-release-dates
RELEASE_DIGITAL = 4
RELEASE_PHYSICAL = 5

PROVIDER_KINDS = {"flatrate": "订阅", "free": "免费", "ads": "免费", "rent": "租", "buy": "买"}

_TMDB_URL = re.compile(r"themoviedb\.org/movie/(\d+)")
_IMDB_ID = re.compile(r"\b(tt\d{6,10})\b")


class TMDBError(Exception):
    pass


@dataclass
class TMDBMovie:
    id: int
    title: str
    original_title: str | None = None
    year: int | None = None
    rating: float | None = None
    votes: int | None = None
    poster: str | None = None
    overview: str | None = None
    imdb_id: str | None = None
    digital_date: str | None = None
    providers: list[str] = field(default_factory=list)
    release_date: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def parse_tmdb_id(text: str) -> int | None:
    m = _TMDB_URL.search(text)
    return int(m.group(1)) if m else None


def parse_imdb_id(text: str) -> str | None:
    m = _IMDB_ID.search(text)
    return m.group(1) if m else None


def _from_payload(data: dict[str, Any]) -> TMDBMovie:
    release = data.get("release_date") or ""
    poster = data.get("poster_path")
    return TMDBMovie(
        id=int(data["id"]),
        title=data.get("title") or data.get("original_title") or "",
        original_title=data.get("original_title") or None,
        year=int(release[:4]) if release[:4].isdigit() else None,
        release_date=release or None,
        rating=data.get("vote_average"),
        votes=data.get("vote_count"),
        poster=f"{IMAGE_BASE}{poster}" if poster else None,
        overview=data.get("overview") or None,
    )


def earliest_digital_date(data: dict[str, Any]) -> str | None:
    dates = [
        entry["release_date"][:10]
        for country in (data.get("release_dates") or {}).get("results") or []
        for entry in country.get("release_dates") or []
        if entry.get("type") in (RELEASE_DIGITAL, RELEASE_PHYSICAL) and entry.get("release_date")
    ]
    return min(dates) if dates else None


def providers_in(data: dict[str, Any], regions: list[str]) -> list[str]:
    results = (data.get("watch/providers") or {}).get("results") or {}
    found: list[str] = []
    for region in regions:
        entry = results.get(region.upper()) or {}
        for kind, label in PROVIDER_KINDS.items():
            for provider in entry.get(kind) or []:
                name = f"{region.upper()} · {provider.get('provider_name')}({label})"
                if name not in found:
                    found.append(name)
    return found


class TMDBClient:
    def __init__(self, api_key: str, language: str = "zh-CN", proxy: str | None = None) -> None:
        if not api_key:
            raise TMDBError("未配置 TMDB API Key")
        headers = {"Accept": "application/json"}
        self._params: dict[str, str] = {"language": language}
        # v4 read access tokens are JWTs; v3 keys are 32 hex chars.
        if api_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            self._params["api_key"] = api_key
        self._client = make_client(proxy=proxy, headers=headers)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TMDBClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        try:
            resp = self._client.get(f"{API}{path}", params={**self._params, **params})
        except httpx.HTTPError as exc:
            raise TMDBError(f"TMDB 请求失败: {exc}") from exc
        if resp.status_code == 401:
            raise TMDBError("TMDB API Key 无效")
        if resp.status_code == 404:
            raise TMDBError("TMDB 找不到该条目")
        if resp.status_code != 200:
            raise TMDBError(f"TMDB 返回 HTTP {resp.status_code}")
        return resp.json()

    def now_playing(self, region: str, max_pages: int = 3) -> list[TMDBMovie]:
        movies: list[TMDBMovie] = []
        for page in range(1, max_pages + 1):
            data = self._get("/movie/now_playing", region=region, page=page)
            movies.extend(_from_payload(item) for item in data.get("results") or [])
            if page >= int(data.get("total_pages") or 1):
                break
        return movies

    def movie(self, tmdb_id: int, provider_regions: list[str] | None = None) -> TMDBMovie:
        data = self._get(
            f"/movie/{tmdb_id}", append_to_response="release_dates,watch/providers,external_ids,credits"
        )
        movie = _from_payload(data)
        movie.imdb_id = data.get("imdb_id") or (data.get("external_ids") or {}).get("imdb_id") or None
        movie.digital_date = earliest_digital_date(data)
        movie.providers = providers_in(data, provider_regions or [])
        movie.details = tmdb_details(data)
        return movie

    def find_by_imdb(self, imdb_id: str) -> TMDBMovie | None:
        data = self._get(f"/find/{imdb_id}", external_source="imdb_id")
        results = data.get("movie_results") or []
        return _from_payload(results[0]) if results else None

    def search(self, query: str, year: int | None = None) -> list[TMDBMovie]:
        params: dict[str, Any] = {"query": query}
        if year:
            params["year"] = year
        data = self._get("/search/movie", **params)
        return [_from_payload(item) for item in data.get("results") or []]

    def find(self, titles: list[str], year: int | None) -> TMDBMovie | None:
        """Best-effort lookup of a movie by title + year."""
        for title in dict.fromkeys(t for t in titles if t):
            for result in self.search(title, year):
                if year is None or result.year is None or abs(result.year - year) <= 1:
                    return result
        return None
