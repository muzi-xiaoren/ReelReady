from ..settings import AppSettings
from ..sources.douban import DoubanClient
from ..sources.tmdb import TMDBClient


def tmdb_proxy(settings: AppSettings) -> str | None:
    if settings.tmdb.use_proxy and settings.network.proxy_url:
        return settings.network.proxy_url
    return None


def make_tmdb(settings: AppSettings) -> TMDBClient | None:
    if not settings.tmdb.api_key:
        return None
    return TMDBClient(settings.tmdb.api_key, settings.tmdb.language, proxy=tmdb_proxy(settings))


def make_douban() -> DoubanClient:
    return DoubanClient()
