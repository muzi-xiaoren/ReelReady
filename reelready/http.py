import httpx

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def make_client(
    *,
    proxy: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
) -> httpx.Client:
    return httpx.Client(
        proxy=proxy or None,
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
    )
