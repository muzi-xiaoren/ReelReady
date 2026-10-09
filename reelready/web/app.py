import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__, config
from ..db import init_db
from ..settings import load_settings

log = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(config.TEMPLATE_DIR))


def _when(value: datetime | None) -> str:
    if value is None:
        return "—"
    delta = datetime.now() - value
    seconds = delta.total_seconds()
    if seconds < 0:
        seconds = -seconds
        if seconds < 3600:
            return f"{max(int(seconds // 60), 1)} 分钟后"
        if seconds < 86400:
            return f"{int(seconds // 3600)} 小时后"
        return value.strftime("%m-%d %H:%M")
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    if seconds < 86400 * 7:
        return f"{int(seconds // 86400)} 天前"
    return value.strftime("%Y-%m-%d")


def _filesize(value: int | None) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit not in ("B", "KB") else f"{size:.0f} {unit}"
        size /= 1024
    return ""


templates.env.filters["when"] = _when
templates.env.filters["filesize"] = _filesize
templates.env.globals["version"] = __version__


def toast(message: str, level: str = "ok", response: Response | None = None, **extra_headers: str) -> Response:
    """An empty HTMX response that pops a toast in the browser."""
    response = response or Response(status_code=200)
    # Header values must be ASCII; json.dumps escapes the Chinese text.
    response.headers["HX-Trigger"] = json.dumps({"toast": {"message": message, "level": level}})
    for key, value in extra_headers.items():
        response.headers[key.replace("_", "-")] = value
    return response


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from ..scheduler import scheduler

    config.ensure_dirs()
    init_db()
    load_settings()  # creates defaults (and CookieCloud credentials) on first start
    from ..services.pt import revalidate_cached_candidates
    revalidate_cached_candidates()
    scheduler.start()
    log.info("ReelReady %s started, data dir %s", __version__, config.DATA_DIR)
    yield
    scheduler.stop()


def create_app() -> FastAPI:
    from . import cookiecloud_api, routes

    app = FastAPI(title="ReelReady", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def require_htmx_for_writes(request: Request, call_next):
        # Every UI write goes through HTMX, which sends this header. A cross-site form post
        # cannot set it, so other web pages cannot change settings behind the user's back.
        if (
            request.method == "POST"
            and not request.url.path.startswith("/cookiecloud/")
            and request.headers.get("hx-request") != "true"
        ):
            return Response("Forbidden", status_code=403)
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")
    app.include_router(cookiecloud_api.router)
    app.include_router(routes.router)
    return app
