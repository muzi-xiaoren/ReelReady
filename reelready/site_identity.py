from urllib.parse import urlsplit

from .sites.catalog import lookup_site


def site_identity(kind: str, url: str) -> str:
    parsed = urlsplit(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    entry = lookup_site(host)
    if entry:
        return f"{kind}:catalog:{entry['id']}"
    port = f":{parsed.port}" if parsed.port and parsed.port not in (80, 443) else ""
    return f"{kind}:{host}{port}{parsed.path.rstrip('/')}"
