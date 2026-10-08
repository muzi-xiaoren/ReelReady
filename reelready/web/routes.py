import logging
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select

from ..db import session_scope
from ..downloader import DownloaderError, QBittorrent
from ..models import (
    STAGE_LABELS,
    STATUS_LABELS,
    CookieCloudBlob,
    Event,
    JobState,
    Movie,
    MovieStatus,
    Site,
    Stage,
)
from ..notifier import NotifyError, render_single, send_mail
from ..scheduler import JOBS, next_run, scheduler
from ..services.clients import make_tmdb
from ..services.movies import AddMovieError, add_manual, approve, cache_poster, poster_path
from ..services.pt import download_candidate
from ..services.sites import add_site, detected_sites, load_cookiecloud, test_site
from ..settings import EMAIL_PRESETS, SECTIONS, load_settings, save_settings
from ..sites import SITE_KINDS, SiteError
from ..sources.tmdb import TMDBError
from . import forms
from .app import templates, toast

log = logging.getLogger(__name__)

router = APIRouter()

TABS = [MovieStatus.MONITORING, MovieStatus.CANDIDATE, MovieStatus.COMPLETED, MovieStatus.IGNORED]


def _render(request: Request, name: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"nav": context.pop("nav", ""), **context})


def _jobs_view() -> list[dict]:
    settings = load_settings()
    with session_scope() as session:
        states = {s.name: s for s in session.scalars(select(JobState))}
        for state in states.values():
            session.expunge(state)
    views = []
    for job in JOBS.values():
        state = states.get(job.name)
        last_run = state.last_run_at if state else None
        views.append(
            {
                "name": job.name,
                "label": job.label,
                "last_run": last_run,
                "next_run": next_run(job, settings, last_run),
                "status": state.last_status if state else None,
                "message": state.last_message if state else None,
                "running": scheduler.running == job.label,
            }
        )
    return views


# ---------------------------------------------------------------- movies


@router.get("/")
def home() -> RedirectResponse:
    return RedirectResponse("/movies", status_code=302)


@router.get("/movies", response_class=HTMLResponse)
def movies_page(request: Request, tab: str = MovieStatus.MONITORING, q: str = "") -> HTMLResponse:
    if tab not in {t.value for t in TABS}:
        tab = MovieStatus.MONITORING
    with session_scope() as session:
        counts = dict(session.execute(select(Movie.status, func.count()).group_by(Movie.status)).all())
        query = select(Movie).where(Movie.status == tab)
        if q:
            like = f"%{q.strip()}%"
            query = query.where(Movie.title.like(like) | Movie.original_title.like(like))
        order = {
            MovieStatus.COMPLETED: Movie.completed_at.desc(),
            MovieStatus.CANDIDATE: Movie.created_at.desc(),
        }.get(tab, Movie.stage.desc())
        movies = list(session.scalars(query.order_by(order, Movie.id.desc())))
    return _render(
        request,
        "movies.html",
        nav="movies",
        tab=tab,
        tabs=[(t.value, STATUS_LABELS[t], counts.get(t.value, 0)) for t in TABS],
        movies=movies,
        q=q,
        jobs=_jobs_view(),
        stage_labels=STAGE_LABELS,
    )


@router.get("/movies/{movie_id}", response_class=HTMLResponse)
def movie_detail(request: Request, movie_id: int) -> HTMLResponse:
    with session_scope() as session:
        movie = session.get(Movie, movie_id)
        if movie is None:
            raise HTTPException(404)
        events = list(
            session.scalars(select(Event).where(Event.movie_id == movie_id).order_by(Event.created_at.desc()).limit(50))
        )
    return _render(
        request,
        "movie_detail.html",
        nav="movies",
        movie=movie,
        events=events,
        stages=[(int(s), label) for s, label in STAGE_LABELS.items()],
        status_label=STATUS_LABELS[MovieStatus(movie.status)],
    )


@router.post("/movies/add")
def movie_add(ref: Annotated[str, Form()]) -> Response:
    settings = load_settings()
    try:
        with session_scope() as session:
            movie = add_manual(session, ref, settings)
            movie_id, title = movie.id, movie.title
    except AddMovieError as exc:
        return toast(str(exc), "error")
    scheduler.trigger("check", [movie_id])
    scheduler.trigger("pt_scan", [movie_id])
    return toast(f"已添加《{title}》", HX_Redirect=f"/movies/{movie_id}")


def _set_status(movie_id: int, status: MovieStatus) -> str:
    with session_scope() as session:
        movie = session.get(Movie, movie_id)
        if movie is None:
            raise HTTPException(404)
        if status == MovieStatus.MONITORING:
            approve(movie)
        else:
            movie.status = status
        return movie.title


@router.post("/movies/{movie_id}/approve")
def movie_approve(movie_id: int) -> Response:
    title = _set_status(movie_id, MovieStatus.MONITORING)
    scheduler.trigger("check", [movie_id])
    scheduler.trigger("pt_scan", [movie_id])
    return toast(f"《{title}》已加入监测")


@router.post("/movies/{movie_id}/ignore")
def movie_ignore(movie_id: int) -> Response:
    title = _set_status(movie_id, MovieStatus.IGNORED)
    return toast(f"《{title}》已移入黑名单，之后不会再被收集")


@router.post("/movies/{movie_id}/restore")
def movie_restore(movie_id: int) -> Response:
    with session_scope() as session:
        movie = session.get(Movie, movie_id)
        if movie is None:
            raise HTTPException(404)
        movie.status = MovieStatus.CANDIDATE if movie.approved_at is None else MovieStatus.MONITORING
        if movie.stage >= Stage.DOWNLOADED:
            movie.status = MovieStatus.COMPLETED
        title, label = movie.title, STATUS_LABELS[MovieStatus(movie.status)]
    return toast(f"《{title}》已恢复到「{label}」")


@router.post("/movies/{movie_id}/redownload")
def movie_redownload(movie_id: int) -> Response:
    """Put a completed movie back into monitoring so it is searched and downloaded again."""
    with session_scope() as session:
        movie = session.get(Movie, movie_id)
        if movie is None:
            raise HTTPException(404)
        approve(movie)
        movie.stage = Stage.STREAMING if movie.streaming else (Stage.DATED if movie.digital_date else Stage.IN_CINEMA)
        movie.downloaded = None
        movie.completed_at = None
        title = movie.title
    scheduler.trigger("pt_scan", [movie_id])
    return toast(f"《{title}》已重新加入监测", HX_Refresh="true")


@router.post("/movies/{movie_id}/delete")
def movie_delete(movie_id: int) -> Response:
    with session_scope() as session:
        movie = session.get(Movie, movie_id)
        if movie is None:
            raise HTTPException(404)
        title = movie.title
        session.delete(movie)
    poster_path(movie_id).unlink(missing_ok=True)
    return toast(f"已删除《{title}》", HX_Redirect="/movies")


@router.post("/movies/{movie_id}/check")
def movie_check(movie_id: int) -> Response:
    scheduler.trigger("check", [movie_id])
    return toast("已加入检测队列，稍后刷新查看")


@router.post("/movies/{movie_id}/search")
def movie_search(movie_id: int) -> Response:
    scheduler.trigger("pt_scan", [movie_id])
    return toast("已加入 PT 搜索队列，稍后刷新查看")


@router.post("/movies/{movie_id}/download/{index}")
def movie_download(movie_id: int, index: int) -> Response:
    settings = load_settings()
    try:
        download_candidate(settings, movie_id, index)
    except SiteError as exc:
        return toast(str(exc), "error")
    from ..services.events import notify_urgent

    notify_urgent(settings)
    return toast("已推送到 qBittorrent", HX_Refresh="true")


@router.get("/posters/{movie_id}")
def poster(movie_id: int) -> Response:
    path = poster_path(movie_id)
    if not path.exists():
        # Douban images refuse hotlinking, so fetch (with a Referer) and cache on first view.
        with session_scope() as session:
            movie = session.get(Movie, movie_id)
        if movie is not None:
            cache_poster(movie, load_settings())
    if path.exists():
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})
    return RedirectResponse("/static/poster-placeholder.svg")


# ---------------------------------------------------------------- jobs & events


@router.post("/jobs/{name}/run")
def job_run(name: str) -> Response:
    if name not in JOBS:
        raise HTTPException(404)
    scheduler.trigger(name)
    return toast(f"「{JOBS[name].label}」已开始运行，完成后刷新查看")


@router.get("/jobs", response_class=HTMLResponse)
def jobs_fragment(request: Request) -> HTMLResponse:
    return _render(request, "partials/jobs.html", jobs=_jobs_view())


@router.get("/events", response_class=HTMLResponse)
def events_page(request: Request, kind: str = "") -> HTMLResponse:
    with session_scope() as session:
        query = select(Event).order_by(Event.created_at.desc()).limit(300)
        if kind:
            query = query.where(Event.kind == kind)
        events = list(session.scalars(query))
    from ..notifier import KIND_LABELS

    return _render(request, "events.html", nav="events", events=events, kind=kind, kind_labels=KIND_LABELS)


# ---------------------------------------------------------------- sites


@router.get("/sites", response_class=HTMLResponse)
def sites_page(request: Request) -> HTMLResponse:
    settings = load_settings()
    with session_scope() as session:
        sites = list(session.scalars(select(Site).order_by(Site.id)))
        blob = session.get(CookieCloudBlob, settings.cookiecloud.uuid)
        synced_at = blob.updated_at if blob else None
    data, _ = load_cookiecloud(settings)
    server = str(request.base_url).rstrip("/") + "/cookiecloud"
    return _render(
        request,
        "sites.html",
        nav="sites",
        sites=sites,
        site_kinds=SITE_KINDS,
        cookiecloud=settings.cookiecloud,
        server=server,
        synced_at=synced_at,
        cookie_domains=len((data or {}).get("cookie_data") or {}),
        detected=detected_sites(settings),
    )


@router.post("/sites/add")
def site_add(
    kind: Annotated[str, Form()],
    base_url: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    api_key: Annotated[str, Form()] = "",
    cookie: Annotated[str, Form()] = "",
) -> Response:
    if kind not in SITE_KINDS:
        return toast("不支持的站点类型", "error")
    if kind == "mteam":
        base_url = base_url or "https://api.m-team.cc"
        name = name or "馒头"
        if not api_key.strip():
            return toast("请填写馒头的 API Key", "error")
    elif not base_url.strip():
        return toast("请填写站点地址", "error")
    settings = load_settings()
    site = add_site(settings, kind=kind, name=name, base_url=base_url, api_key=api_key, cookie=cookie)
    ok, message = test_site(settings, site.id)
    return toast(f"已添加「{site.name}」：{message}", "ok" if ok else "warn", HX_Refresh="true")


@router.post("/sites/{site_id}/update")
def site_update(
    site_id: int,
    name: Annotated[str, Form()] = "",
    base_url: Annotated[str, Form()] = "",
    api_key: Annotated[str, Form()] = "",
    cookie: Annotated[str, Form()] = "",
    use_cookiecloud: Annotated[str | None, Form()] = None,
) -> Response:
    with session_scope() as session:
        site = session.get(Site, site_id)
        if site is None:
            raise HTTPException(404)
        site.name = name.strip() or site.name
        site.base_url = base_url.strip().rstrip("/") or site.base_url
        if api_key.strip():
            site.api_key = api_key.strip()
        if cookie.strip():
            site.cookie = cookie.strip()
        site.use_cookiecloud = use_cookiecloud is not None
        site.status = "unknown"
    ok, message = test_site(load_settings(), site_id)
    return toast(f"已保存：{message}", "ok" if ok else "warn", HX_Refresh="true")


@router.post("/sites/{site_id}/toggle")
def site_toggle(site_id: int) -> Response:
    with session_scope() as session:
        site = session.get(Site, site_id)
        if site is None:
            raise HTTPException(404)
        site.enabled = not site.enabled
        enabled = site.enabled
    return toast("已启用" if enabled else "已停用", HX_Refresh="true")


@router.post("/sites/{site_id}/test")
def site_test(site_id: int) -> Response:
    ok, message = test_site(load_settings(), site_id)
    return toast(message, "ok" if ok else "error", HX_Refresh="true")


@router.post("/sites/{site_id}/delete")
def site_delete(site_id: int) -> Response:
    with session_scope() as session:
        site = session.get(Site, site_id)
        if site is None:
            raise HTTPException(404)
        session.delete(site)
    return toast("已删除站点", HX_Refresh="true")


@router.post("/sites/cookiecloud/regenerate")
def cookiecloud_regenerate() -> Response:
    from ..settings import CookieCloudSettings

    settings = load_settings()
    settings.cookiecloud = CookieCloudSettings()
    save_settings(settings)
    return toast("已重新生成，请在 CookieCloud 扩展里同步更新", HX_Refresh="true")


# ---------------------------------------------------------------- settings


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    settings = load_settings()
    sections = [(key, label, forms.describe(getattr(settings, key))) for key, label in SECTIONS]
    return _render(request, "settings.html", nav="settings", sections=sections, presets=EMAIL_PRESETS)


@router.post("/settings/{section}")
async def settings_save(section: str, request: Request) -> Response:
    if section not in dict(SECTIONS):
        raise HTTPException(404)
    settings = load_settings()
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    try:
        updated = forms.parse(getattr(settings, section), form)
    except ValueError as exc:
        return toast(f"保存失败：{exc}", "error")
    setattr(settings, section, updated)
    save_settings(settings)
    return toast("设置已保存")


@router.post("/test/email")
def test_email() -> Response:
    settings = load_settings()
    try:
        send_mail(
            settings.email,
            "[ReelReady] 测试邮件",
            render_single("测试邮件", "收到这封邮件说明邮件通知配置正确。"),
        )
    except NotifyError as exc:
        return toast(str(exc), "error")
    recipients = settings.email.recipients or [settings.email.username]
    return toast(f"已发送到 {', '.join(recipients)}")


@router.post("/test/qbittorrent")
def test_qbittorrent() -> Response:
    try:
        with QBittorrent(load_settings().qbittorrent) as qb:
            version = qb.version()
    except DownloaderError as exc:
        return toast(str(exc), "error")
    return toast(f"连接成功，qBittorrent {version}")


@router.post("/test/tmdb")
def test_tmdb() -> Response:
    tmdb = make_tmdb(load_settings())
    if tmdb is None:
        return toast("还没有填写 TMDB API Key", "error")
    try:
        with tmdb:
            movie = tmdb.movie(603)
    except TMDBError as exc:
        return toast(str(exc), "error")
    return toast(f"连接成功（{movie.title}）")
