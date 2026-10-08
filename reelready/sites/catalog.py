"""Offline public site directory. Never sends synchronized cookies to its data source."""

from functools import lru_cache
import json
from pathlib import Path
from urllib.parse import urlsplit


@lru_cache(maxsize=1)
def catalog_by_host() -> dict[str, dict]:
    entries = json.loads(Path(__file__).with_name("catalog.json").read_text(encoding="utf-8"))["sites"]
    hosts = {}
    for entry in entries:
        for url in entry["urls"]:
            host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
            if host:
                hosts[host] = entry
    return hosts


def lookup_site(host: str) -> dict | None:
    # Exact domain/explicit aliases only, so lookalikes and unrelated subdomains do not match.
    return catalog_by_host().get(host.lower().lstrip(".").removeprefix("www."))


def site_icon_url(host: str) -> str | None:
    entry = lookup_site(host)
    icon = (entry or {}).get("icon")
    return f"/static/site-icons/{icon}" if icon else None


def supported_kind(host: str) -> str | None:
    entry = lookup_site(host)
    if entry and entry["id"] == "mteam":
        return "mteam"
    if entry and entry["schema"] == "NexusPHP":
        return "nexusphp"
    if entry and entry["id"] == "rousipro":
        return "rousipro"
    if entry and entry["id"] == "monikadesign":
        return "unit3d"
    return None
