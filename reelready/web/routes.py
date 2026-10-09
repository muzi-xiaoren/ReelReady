import logging
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select

from ..db import session_scope
from ..cookiecloud import synced_cookie_hosts
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
from ..services.pt import download_candidate, release_wait_reason, revalidate_cached_candidates
from ..services.sites import add_site, detected_sites, load_cookiecloud, discovery_identity, hidden_discovery_sites, set_discovery_hidden
from ..services.site_tests import queue_site_test
from ..services.movie_metadata import queue_metadata, metadata_pending
from .forms import REGIONS
from ..site_identity import site_identity
from ..settings import EMAIL_PRESETS, SECTIONS, load_settings, save_settings, update_section
from ..sites import SITE_KINDS, SiteError
from ..sites.catalog import supported_kind, lookup_site, site_icon_url
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
    metadata_loading = metadata_pending(movie_id)
    early_search = load_settings().pt.early_search
    show_pt_results = early_search or not release_wait_reason(movie)
    if movie.metadata_checked_at is None and (movie.douban_id or movie.tmdb_id or movie.imdb_id):
        metadata_loading = queue_metadata(movie_id) or metadata_loading
    return _render(
        request,
        "movie_detail.html",
        nav="movies",
        movie=movie,
        metadata_pending=metadata_loading,
        show_pt_results=show_pt_results,
        release_confirmed=not release_wait_reason(movie),
        region_names=REGIONS,
        events=events,
        stages=[(int(s), label) for s, label in STAGE_LABELS.items()],
        status_label=STATUS_LABELS[MovieStatus(movie.status)],
    )


@router.post("/movies/{movie_id}/metadata")
def movie_metadata_refresh(movie_id: int) -> Response:
    with session_scope() as session:
        if session.get(Movie, movie_id) is None:
            raise HTTPException(404)
    queued = queue_metadata(movie_id)
    return toast("正在后台更新电影资料" if queued else "资料正在更新或队列已满，请稍后查看", HX_Refresh="true")


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
    settings = load_settings()
    with session_scope() as session:
        movie = session.get(Movie, movie_id)
        if movie is None:
            raise HTTPException(404)
        if not settings.pt.early_search and release_wait_reason(movie):
            return toast("提前搜索已关闭，等待数字版日期到达或确认平台上线后再搜索", "warn")
    scheduler.trigger("pt_scan", [movie_id])
    return toast("已加入 PT 搜索队列，稍后刷新查看")


@router.post("/movies/{movie_id}/download/{index}")
def movie_download(movie_id: int, index: int, site_id: Annotated[int | None, Form()] = None, torrent_id: Annotated[str | None, Form()] = None) -> Response:
    if site_id is None or torrent_id is None:
        return toast('页面资源列表已过期，请刷新页面后再下载', 'warn')
    settings = load_settings()
    try:
        download_candidate(settings, movie_id, index, site_id=site_id, torrent_id=torrent_id)
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

    return _render(request, "events.html", nav="events", events=events, kind=kind, kind_labels=KIND_LABELS, retention_days=load_settings().events.retention_days)


@router.post('/events/{event_id}/delete')
def event_delete(event_id: int) -> Response:
    from ..services.events import delete_event
    delete_event(event_id)
    return toast('动态已删除')


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
    synced_hosts = synced_cookie_hosts(data or {})
    hidden = hidden_discovery_sites()
    detected = [host for host in detected_sites(settings) if discovery_identity(host) not in hidden]
    other_sites_by_id = {}
    for host in synced_hosts:
        if discovery_identity(host) in hidden:
            continue
        entry = lookup_site(host)
        if entry and supported_kind(host) in (None, "rousipro"):
            group = other_sites_by_id.setdefault(entry["id"], {
                "name": entry["name"], "icon": site_icon_url(host), "hosts": [],
                "kind": supported_kind(host),
            })
            group["hosts"].append(host)
    other_sites = list(other_sites_by_id.values())
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
        detected=detected,
        detected_kinds={host: supported_kind(host) or "nexusphp" for host in detected},
        detected_names={host: (lookup_site(host) or {}).get("name", host) for host in detected},
        detected_icons={host: site_icon_url(host) for host in detected},
        site_icons={site.id: site_icon_url(_site_host(site.base_url)) for site in sites},
        other_sites=other_sites,
        hidden_sites=[{"host": host, "name": (lookup_site(host) or {}).get("name", host), "icon": site_icon_url(host)} for host in hidden.values()],
        synced_hosts=synced_hosts,
    )


def _site_host(url: str) -> str:
    from urllib.parse import urlsplit

    host = urlsplit(url).hostname or ""
    # M-Team's API and website use different hosts.
    return "kp.m-team.cc" if host == "api.m-team.cc" else host


@router.post("/sites/discovery/visibility")
def site_discovery_visibility(host: Annotated[str, Form()], hidden: Annotated[bool, Form()] = True) -> Response:
    host = host.strip().lower().lstrip(".")
    data, _ = load_cookiecloud(load_settings())
    known = {discovery_identity(item) for item in synced_cookie_hosts(data or {})}
    saved = hidden_discovery_sites()
    if discovery_identity(host) not in known and discovery_identity(host) not in saved:
        return toast("该站点不在浏览器同步记录中", "error")
    set_discovery_hidden(host, hidden)
    return toast("已归入隐藏站点" if hidden else "已恢复显示；含登录 Cookie 时会出现在发现列表", HX_Trigger_After_Settle="sitesChanged")


@router.post("/sites/add")
def site_add(
    kind: Annotated[str, Form()],
    base_url: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    api_key: Annotated[str, Form()] = "",
    cookie: Annotated[str, Form()] = "",
    category: Annotated[str, Form()] = "standard",
) -> Response:
    if kind not in SITE_KINDS:
        return toast("不支持的站点类型", "error")
    if kind == "mteam":
        base_url = base_url or "https://api.m-team.cc"
        name = name or "馒头"
        if not api_key.strip():
            return toast("请填写馒头的 API Key", "error")
    elif kind == "rousipro":
        base_url = base_url or "https://rousi.pro"
        name = name or "Rousi Pro"
        if not api_key.strip():
            return toast("请在 Rousi Pro 账户设置创建个人 API Key，并填写到这里", "error")
    elif not base_url.strip():
        return toast("请填写站点地址", "error")
    settings = load_settings()
    site, created = add_site(settings, kind=kind, name=name, base_url=base_url, api_key=api_key, cookie=cookie, category=category)
    if not created:
        return toast(f"「{site.name}」已存在，未重复添加", HX_Trigger_After_Settle="sitesChanged")
    result = queue_site_test(site.id)
    message = "正在后台测试连接" if result == "queued" else "测试队列已满，可稍后点击测试连接"
    return toast(f"已添加「{site.name}」，{message}", HX_Trigger_After_Settle="sitesChanged")


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
        if site.status == "testing":
            return toast("连接测试进行中，请完成后再编辑配置", "warn")
        site.name = name.strip() or site.name
        site.base_url = base_url.strip().rstrip("/") or site.base_url
        key = site_identity(site.kind, site.base_url)
        other = session.scalar(select(Site.id).where(Site.identity_key == key, Site.id != site_id))
        if other:
            session.rollback()
            return toast("该站点已存在，请编辑已有站点", "warn")
        site.identity_key = key
        if api_key.strip():
            site.api_key = api_key.strip()
        if cookie.strip():
            site.cookie = cookie.strip()
        site.use_cookiecloud = use_cookiecloud is not None
        site.status = "unknown"
    result = queue_site_test(site_id)
    return toast("已保存，连接测试在后台进行" if result != "busy" else "已保存，测试队列已满，请稍后测试", HX_Trigger_After_Settle="sitesChanged")


@router.post("/sites/{site_id}/toggle")
def site_toggle(site_id: int) -> Response:
    with session_scope() as session:
        site = session.get(Site, site_id)
        if site is None:
            raise HTTPException(404)
        site.enabled = not site.enabled
        enabled = site.enabled
    return toast("已启用" if enabled else "已停用", HX_Trigger_After_Settle="sitesChanged")


@router.post("/sites/{site_id}/test")
def site_test(site_id: int) -> Response:
    result = queue_site_test(site_id)
    messages = {"queued": "已开始后台测试", "pending": "该站点正在测试，请稍候", "busy": "测试队列已满，请稍后重试", "missing": "站点不存在"}
    return toast(messages[result], "warn" if result in ("busy", "missing") else "ok", HX_Trigger_After_Settle="sitesChanged")


@router.post("/sites/{site_id}/delete")
def site_delete(site_id: int) -> Response:
    with session_scope() as session:
        site = session.get(Site, site_id)
        if site is None:
            raise HTTPException(404)
        session.delete(site)
    return toast("已删除站点", HX_Trigger_After_Settle="sitesChanged")


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
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    try:
        update_section(section, lambda current: forms.parse(current, form))
    except ValueError as exc:
        return toast(f"保存失败：{exc}", "error")
    if section in ('rules', 'pt'):
        revalidate_cached_candidates()
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
