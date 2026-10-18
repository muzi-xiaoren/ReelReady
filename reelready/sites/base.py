from dataclasses import dataclass, field
import re

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
    aliases: list[str] = field(default_factory=list)

    @property
    def titles(self) -> list[str]:
        return [t for t in dict.fromkeys([self.original_title, self.title, *self.aliases]) if t]


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

    def verify_identity(self, torrent: TorrentInfo) -> None:
        """Adapters may supplement search hits from independent detail metadata."""
        return None

    def close(self) -> None:
        pass


def looks_like_torrent(content: bytes) -> bool:
    # Bencoded dictionaries start with "d" and torrents always carry an "info" dict.
    return content[:1] == b"d" and b"4:info" in content[:4096]


def detail_imdb(html: str) -> str | None:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, 'html.parser')
    for node in soup.select('script, style, nav, footer'):
        node.decompose()
    scopes = soup.select('#descr, #kdescr, #description, .torrent-description')
    def ids(nodes):
        text = ' '.join(str(node) for node in nodes)
        return set(re.findall(r'\btt\d{6,10}\b', text))
    found = ids(scopes) if scopes else set()
    if not found:
        found = ids([soup])
    # Multiple different IDs are ambiguous; never select the desired one from a sidebar.
    return next(iter(found)) if len(found) == 1 else None
