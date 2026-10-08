"""CookieCloud-compatible endpoints, so the stock browser extension can push cookies here.

Set the extension's server address to ``http://<host>:<port>/cookiecloud``.
"""

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..cookiecloud import CookieCloudError, decrypt
from ..db import session_scope
from ..models import CookieCloudBlob
from ..services.sites import store_cookiecloud
from ..settings import load_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/cookiecloud")

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def _json(data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status_code, headers=_CORS)


async def _payload(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}
    if "form" in content_type:
        return dict(await request.form())
    return {}


@router.api_route("/{path:path}", methods=["OPTIONS"], include_in_schema=False)
async def preflight(path: str) -> Response:
    return Response(status_code=204, headers=_CORS)


@router.get("/")
@router.get("")
async def index() -> Response:
    return Response("Hello World! API ROOT = /cookiecloud (ReelReady)", headers=_CORS)


@router.post("/update")
async def update(request: Request) -> JSONResponse:
    body = await _payload(request)
    uuid, encrypted = body.get("uuid"), body.get("encrypted")
    crypto_type = body.get("crypto_type") or "legacy"
    if not uuid or not encrypted:
        return _json({"action": "error", "message": "Bad Request"}, 400)
    settings = load_settings()
    if uuid != settings.cookiecloud.uuid:
        return _json({"action": "error", "message": "unknown uuid"}, 403)
    try:
        updated = store_cookiecloud(settings, uuid, encrypted, crypto_type)
    except CookieCloudError as exc:
        log.warning("CookieCloud push rejected: %s", exc)
        return _json({"action": "error", "message": str(exc)}, 400)
    log.info("CookieCloud snapshot received, %d site cookie(s) updated", updated)
    return _json({"action": "done"})


@router.api_route("/get/{uuid}", methods=["GET", "POST"])
async def get(uuid: str, request: Request) -> JSONResponse:
    settings = load_settings()
    if uuid != settings.cookiecloud.uuid:
        return _json({"action": "error", "message": "Not Found"}, 404)
    with session_scope() as session:
        blob = session.get(CookieCloudBlob, uuid)
        if blob is None:
            return _json({"action": "error", "message": "Not Found"}, 404)
        encrypted, crypto_type = blob.encrypted, blob.crypto_type
    crypto_type = request.query_params.get("crypto_type") or crypto_type
    password = (await _payload(request)).get("password")
    if password:
        try:
            return _json(decrypt(encrypted, uuid, password, crypto_type))
        except CookieCloudError as exc:
            return _json({"action": "error", "message": str(exc)}, 400)
    return _json({"encrypted": encrypted, "crypto_type": crypto_type})
