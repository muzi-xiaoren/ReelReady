"""Parse release names and pick the best torrent according to the user's rules."""

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .settings import TorrentRules

GB = 1024**3

_RESOLUTION = [
    (2160, re.compile(r"(?<!\d)2160[pi]\b|\b4K\b|\bUHD\b", re.I)),
    (1080, re.compile(r"(?<!\d)1080[pi]\b", re.I)),
    (720, re.compile(r"(?<!\d)720p\b", re.I)),
    (480, re.compile(r"(?<!\d)(?:480|576)[pi]\b", re.I)),
]
# Pirated cinema recordings and pre-release screeners.
_BAD_SOURCE = re.compile(
    r"\b(?:CAM|CAMRip|HDCAM|TS|HDTS|TELESYNC|TC|HDTC|TELECINE|SCR|DVDSCR|SCREENER|HQCAM)\b|枪版|抢先版",
    re.I,
)
_REMUX = re.compile(r"\bREMUX\b", re.I)
# Full discs: "Blu-ray" with a hyphen is the scene convention for untouched discs, encodes say "BluRay".
_DISC = re.compile(r"\bBlu-ray\b|\bBDMV\b|\bISO\b|原盘|\bDIY\b", re.I)
_ENCODE = re.compile(r"\bx26[45]\b|\bWEB-?(?:DL|Rip)\b", re.I)
_HDR = re.compile(r"\bHDR(?:10\+?|10Plus)?\b|\bDV\b|\bDoVi\b|Dolby\s?Vision|杜比视界|\bHLG\b", re.I)
_CHINESE_SUB = re.compile(r"中字|中文字幕|简中|繁中|简繁|简英|繁英|中英|双语|官译|国配|国语|\bCHS\b|\bCHT\b", re.I)
_SOURCES = [
    ("WEB-DL", re.compile(r"\bWEB-?DL\b|\bWEB\b", re.I)),
    ("WEBRip", re.compile(r"\bWEB-?Rip\b", re.I)),
    ("Remux", _REMUX),
    ("BluRay", re.compile(r"\bBlu-?Ray\b|\bBDRip\b|\bBD\b", re.I)),
    ("HDTV", re.compile(r"\bHDTV\b", re.I)),
]

# M-Team category ids for full discs and remuxes.
MTEAM_DISC_CATEGORIES = {"420", "421"}
MTEAM_REMUX_CATEGORIES = {"439"}


@dataclass
class TorrentInfo:
    site_id: int
    site_name: str
    torrent_id: str
    title: str
    subtitle: str = ""
    size_bytes: int = 0
    seeders: int = 0
    detail_url: str | None = None
    imdb_id: str | None = None
    category: str | None = None


@dataclass
class Parsed:
    resolution: int | None = None
    source: str | None = None
    hdr: bool = False
    chinese_sub: bool = False
    remux: bool = False
    disc: bool = False
    bad_source: bool = False


@dataclass
class Evaluated:
    torrent: TorrentInfo
    parsed: Parsed
    ok: bool
    reason: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self.torrent), "ok": self.ok, "reason": self.reason, "tags": self.tags}


def parse(torrent: TorrentInfo) -> Parsed:
    title = torrent.title
    text = f"{torrent.title} {torrent.subtitle}"
    parsed = Parsed()
    for value, pattern in _RESOLUTION:
        if pattern.search(title):
            parsed.resolution = value
            break
    for name, pattern in _SOURCES:
        if pattern.search(title):
            parsed.source = name
            break
    parsed.hdr = bool(_HDR.search(title))
    parsed.chinese_sub = bool(_CHINESE_SUB.search(text))
    parsed.remux = bool(_REMUX.search(title)) or torrent.category in MTEAM_REMUX_CATEGORIES
    parsed.disc = (
        bool(_DISC.search(text)) and not _ENCODE.search(title)
    ) or torrent.category in MTEAM_DISC_CATEGORIES
    parsed.bad_source = bool(_BAD_SOURCE.search(title))
    return parsed


def _allowed_resolutions(rules: TorrentRules) -> list[int]:
    allowed: list[int] = []
    for item in rules.resolutions:
        m = re.search(r"\d+", item)
        if m:
            value = 2160 if item.strip().upper() in ("4K", "UHD") else int(m.group(0))
            allowed.append(value)
        elif item.strip().upper() in ("4K", "UHD"):
            allowed.append(2160)
    return allowed


def _tags(parsed: Parsed) -> list[str]:
    tags = []
    if parsed.resolution:
        tags.append(f"{parsed.resolution}p")
    if parsed.source:
        tags.append(parsed.source)
    if parsed.hdr:
        tags.append("HDR")
    if parsed.chinese_sub:
        tags.append("中字")
    if parsed.disc:
        tags.append("原盘")
    return tags


def evaluate(torrent: TorrentInfo, rules: TorrentRules) -> Evaluated:
    parsed = parse(torrent)
    allowed = _allowed_resolutions(rules)

    def reject(reason: str) -> Evaluated:
        return Evaluated(torrent, parsed, ok=False, reason=reason, tags=_tags(parsed))

    if parsed.bad_source:
        return reject("枪版 / 非正式片源")
    if parsed.resolution not in allowed:
        return reject(f"清晰度 {parsed.resolution or '未知'} 不在允许范围")
    if rules.exclude_remux and (parsed.remux or parsed.disc):
        return reject("Remux / 原盘")
    if rules.max_size_gb and torrent.size_bytes > rules.max_size_gb * GB:
        return reject(f"体积 {torrent.size_bytes / GB:.1f} GB 超过上限")
    if torrent.seeders < rules.min_seeders:
        return reject("做种数不足")
    return Evaluated(torrent, parsed, ok=True, tags=_tags(parsed))


def _score(item: Evaluated, rules: TorrentRules) -> tuple:
    allowed = _allowed_resolutions(rules)
    return (
        -allowed.index(item.parsed.resolution),
        item.parsed.chinese_sub if rules.prefer_chinese_sub else False,
        item.parsed.hdr if rules.prefer_hdr else False,
        item.torrent.seeders,
    )


def pick_best(torrents: list[TorrentInfo], rules: TorrentRules) -> tuple[Evaluated | None, list[Evaluated]]:
    """Return the best acceptable torrent (if any) and all torrents, best first."""
    evaluated = [evaluate(t, rules) for t in torrents]
    accepted = sorted((e for e in evaluated if e.ok), key=lambda e: _score(e, rules), reverse=True)
    rejected = sorted((e for e in evaluated if not e.ok), key=lambda e: e.torrent.seeders, reverse=True)
    return (accepted[0] if accepted else None), accepted + rejected


_NORMALIZE = re.compile(r"[^0-9a-z一-鿿]+")


def _normalize(text: str) -> str:
    return _NORMALIZE.sub(" ", text.lower()).strip()


def matches_movie(
    torrent: TorrentInfo,
    *,
    imdb_id: str | None,
    titles: list[str],
    year: int | None,
) -> bool:
    """Whether a search hit is really the movie we are looking for."""
    if imdb_id and torrent.imdb_id:
        return torrent.imdb_id == imdb_id
    text = f" {_normalize(torrent.title)} {_normalize(torrent.subtitle)} "
    if year and not any(str(y) in text for y in (year - 1, year, year + 1)):
        return False
    for title in titles:
        norm = _normalize(title)
        if norm and f" {norm} " in text:
            return True
        # CJK titles are not space separated in release names / subtitles.
        if norm and re.search(r"[一-鿿]", norm) and norm.replace(" ", "") in text.replace(" ", ""):
            return True
    return False
