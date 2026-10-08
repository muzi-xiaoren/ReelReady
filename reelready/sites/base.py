from dataclasses import dataclass, field

from ..torrent_rules import TorrentInfo


class SiteError(Exception):
    """A request to the site failed; worth retrying later."""


class LoginExpired(SiteError):
    """The cookie / API key is no longer accepted by the site."""


@dataclass
class MovieQuery:
    imdb_id: str | None
    title: str
    original_title: str | None
    year: int | None

    @property
    def titles(self) -> list[str]:
        return [t for t in dict.fromkeys([self.original_title, self.title]) if t]


@dataclass
class SiteConfig:
    """Detached snapshot of a ``Site`` row, safe to use outside a DB session."""

    id: int
    kind: str
    name: str
    base_url: str
    cookie: str | None = None
    api_key: str | None = None
    user_agent: str = ""
    extra: dict = field(default_factory=dict)


class BaseSite:
    def __init__(self, config: SiteConfig, proxy: str | None = None) -> None:
        self.config = config
        self.proxy = proxy

    @property
    def name(self) -> str:
        return self.config.name

    def test(self) -> str:
        """Check the credentials; return a short human readable status."""
        raise NotImplementedError

    def search(self, query: MovieQuery) -> list[TorrentInfo]:
        raise NotImplementedError

    def download(self, torrent: TorrentInfo) -> bytes:
        """Fetch the .torrent file contents."""
        raise NotImplementedError

    def close(self) -> None:
        pass


def looks_like_torrent(content: bytes) -> bool:
    # Bencoded dictionaries start with "d" and torrents always carry an "info" dict.
    return content[:1] == b"d" and b"4:info" in content[:4096]
