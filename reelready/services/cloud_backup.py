"""Upload the record export to a GitHub repository and / or a WebDAV folder."""

import base64
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import quote, unquote, urlsplit

import httpx

from ..db import session_scope
from ..http import make_client
from ..settings import AppSettings, BackupSettings
from .backup import export_data
from .events import add_event

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
# Only files written by this feature are ever pruned.
_BACKUP_NAME = re.compile(r"^reelready-backup-\d{8}-\d{6}\.json$")


class CloudBackupError(Exception):
    pass


def backup_filename() -> str:
    return f"reelready-backup-{datetime.now():%Y%m%d-%H%M%S}.json"


def run_cloud_backup(settings: AppSettings) -> str:
    """Upload one export to every enabled target; fails if any target fails."""
    config = settings.backup
    targets = []
    if config.github_enabled:
        targets.append(("GitHub", lambda name, body: _upload_github(config, settings.network.proxy_url, name, body)))
    if config.webdav_enabled:
        targets.append(("WebDAV", lambda name, body: _upload_webdav(config, name, body)))
    if not targets:
        return "未启用任何备份目标，已跳过"
    name = backup_filename()
    body = json.dumps(export_data(), ensure_ascii=False, indent=1).encode()
    done, failed = [], []
    for label, upload in targets:
        try:
            pruned = upload(name, body)
            done.append(f"{label} 已上传" + (f"，清理 {pruned} 份旧备份" if pruned else ""))
        except (CloudBackupError, httpx.HTTPError, ET.ParseError) as exc:
            log.warning("%s backup failed: %s", label, exc)
            failed.append(f"{label} 失败：{exc}")
    if failed:
        with session_scope() as session:
            add_event(session, "backup_failed", "云备份失败", "\n".join(failed), dedupe_key="backup_failed", dedupe_hours=12)
        raise CloudBackupError("；".join(done + failed))
    return f"{name}：" + "；".join(done)


# ---------------------------------------------------------------- GitHub


def _github_client(config: BackupSettings, proxy_url: str) -> httpx.Client:
    if not config.github_token or not re.fullmatch(r"[\w.-]+/[\w.-]+", config.github_repo.strip()):
        raise CloudBackupError("请填写 GitHub 仓库（owner/name）和 Token")
    return make_client(
        proxy=proxy_url if config.github_use_proxy else None,
        headers={
            "Authorization": f"Bearer {config.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=60,
    )


def _github_check(resp: httpx.Response, action: str) -> None:
    if resp.status_code >= 400:
        try:
            message = resp.json().get("message") or resp.text
        except ValueError:
            message = resp.text
        raise CloudBackupError(f"{action}失败（HTTP {resp.status_code}）：{message[:200]}")


def _upload_github(config: BackupSettings, proxy_url: str, name: str, body: bytes) -> int:
    repo = config.github_repo.strip()
    folder = config.github_path.strip().strip("/")
    branch = {"branch": config.github_branch.strip()} if config.github_branch.strip() else {}
    base = f"{GITHUB_API}/repos/{repo}/contents"
    folder_url = f"{base}/{quote(folder)}" if folder else base
    with _github_client(config, proxy_url) as client:
        resp = client.put(f"{folder_url}/{name}", json={
            "message": f"ReelReady backup {name}", "content": base64.b64encode(body).decode(), **branch,
        })
        _github_check(resp, "上传")
        resp = client.get(folder_url, params={"ref": branch["branch"]} if branch else None)
        _github_check(resp, "读取备份目录")
        files = sorted((item for item in resp.json() if _BACKUP_NAME.match(item.get("name", ""))), key=lambda item: item["name"])
        stale = files[:-config.keep_count]
        for item in stale:
            resp = client.request("DELETE", f"{folder_url}/{item['name']}", json={
                "message": f"Remove old ReelReady backup {item['name']}", "sha": item["sha"], **branch,
            })
            _github_check(resp, f"删除旧备份 {item['name']} ")
    return len(stale)


# ---------------------------------------------------------------- WebDAV


def _webdav_client(config: BackupSettings) -> httpx.Client:
    if not config.webdav_url.strip().startswith(("http://", "https://")):
        raise CloudBackupError("请填写 WebDAV 目录地址")
    client = make_client(timeout=60)
    client.auth = httpx.BasicAuth(config.webdav_username, config.webdav_password)
    return client


def _webdav_check(resp: httpx.Response, action: str) -> None:
    if resp.status_code == 401:
        raise CloudBackupError(f"{action}失败：用户名或密码不正确")
    if resp.status_code >= 400:
        raise CloudBackupError(f"{action}失败（HTTP {resp.status_code}）")


def _ensure_folder(client: httpx.Client, folder: str) -> None:
    """Create each missing level of ``folder``; servers answer 405 for ones that exist."""
    parts = urlsplit(folder)
    root = f"{parts.scheme}://{parts.netloc}"
    path = ""
    for segment in [s for s in parts.path.split("/") if s]:
        path += f"/{segment}"
        resp = client.request("PROPFIND", f"{root}{path}/", headers={"Depth": "0"})
        if resp.status_code == 404:
            _webdav_check(client.request("MKCOL", f"{root}{path}/"), "创建目录")
        elif resp.status_code == 401:
            _webdav_check(resp, "访问目录")


def _upload_webdav(config: BackupSettings, name: str, body: bytes) -> int:
    folder = config.webdav_url.strip().rstrip("/") + "/"
    with _webdav_client(config) as client:
        _ensure_folder(client, folder)
        _webdav_check(client.put(folder + name, content=body, headers={"Content-Type": "application/json"}), "上传")
        resp = client.request("PROPFIND", folder, headers={"Depth": "1"})
        _webdav_check(resp, "读取备份目录")
        names = set()
        for href in ET.fromstring(resp.content).iter("{DAV:}href"):
            filename = unquote((href.text or "").rstrip("/").rsplit("/", 1)[-1])
            if _BACKUP_NAME.match(filename):
                names.add(filename)
        stale = sorted(names)[:-config.keep_count]
        for filename in stale:
            _webdav_check(client.delete(folder + quote(filename)), f"删除旧备份 {filename} ")
    return len(stale)
