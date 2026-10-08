from ..models import Site
from .base import BaseSite, LoginExpired, MovieQuery, SiteConfig, SiteError
from .mteam import MTeamSite
from .nexusphp import NexusPHPSite

SITE_KINDS = {
    "mteam": "馒头 (M-Team)",
    "nexusphp": "NexusPHP",
}

__all__ = [
    "SITE_KINDS",
    "BaseSite",
    "LoginExpired",
    "MovieQuery",
    "SiteConfig",
    "SiteError",
    "build_site",
    "snapshot",
]


def snapshot(site: Site, user_agent: str) -> SiteConfig:
    return SiteConfig(
        id=site.id,
        kind=site.kind,
        name=site.name,
        base_url=site.base_url,
        cookie=site.cookie,
        api_key=site.api_key,
        user_agent=user_agent,
    )


def build_site(config: SiteConfig, proxy: str | None = None) -> BaseSite:
    if config.kind == "mteam":
        return MTeamSite(config, proxy)
    if config.kind == "nexusphp":
        return NexusPHPSite(config, proxy)
    raise SiteError(f"不支持的站点类型: {config.kind}")
