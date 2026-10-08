"""Audit all catalog definitions and optionally check synchronized accounts read-only.

Run in the app's dependency environment. Reports never contain credentials or responses.
No sites are added or modified; no torrents are downloaded.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from sqlalchemy import select

from reelready import config
from reelready.cookiecloud import cookie_header_for, detect_cookie_hosts, synced_cookie_hosts
from reelready.db import session_scope
from reelready.models import Site
from reelready.services.sites import load_cookiecloud
from reelready.settings import load_settings
from reelready.sites import SiteConfig, build_site, snapshot
from reelready.sites.catalog import lookup_site, supported_kind


def audit(source, output, network=False):
    catalog = json.loads((config.PACKAGE_DIR / "sites/catalog.json").read_text(encoding="utf-8"))
    definitions = []
    for entry in catalog["sites"]:
        text = (source / f"{entry['id']}.ts").read_text(encoding="utf-8")
        parent = re.search(r"export default class \w+ extends (\w+)", text)
        inputs = re.search(r"userInputSettingMeta:\s*\[(.*?)\n  \]", text, re.S)
        credentials = re.findall(r'name:\s*"([^"]+)"', inputs[1]) if inputs else []
        host = urlsplit(entry["urls"][0]).hostname
        kind = supported_kind(host)
        mismatch = bool(parent and parent[1] in ("NexusPHP", "Unit3D") and parent[1] != entry["schema"])
        definitions.append({
            "id": entry["id"], "name": entry["name"], "schema": entry["schema"],
            "adapter": kind, "urls": entry["urls"], "is_dead": entry.get("is_dead", False),
            "required_inputs": credentials, "parent": parent[1] if parent else None,
            "schema_mismatch": mismatch,
        })
    settings = load_settings()
    data, _ = load_cookiecloud(settings)
    login_candidates = detect_cookie_hosts(data or {})
    login_ids = {(lookup_site(host) or {}).get("id") for host in login_candidates}
    synced = {}
    for host in synced_cookie_hosts(data or {}):
        entry = lookup_site(host)
        if entry:
            group = synced.setdefault(entry["id"], {"name": entry["name"], "hosts": [], "schema": entry["schema"], "adapter": supported_kind(host), "is_dead": entry.get("is_dead", False), "has_login_cookie": entry["id"] in login_ids})
            group["hosts"].append(host)
    configs = {}
    with session_scope() as session:
        for row in session.scalars(select(Site)):
            configs[(row.kind, row.base_url)] = snapshot(row, settings.network.user_agent)
    for host in login_candidates:
        kind = supported_kind(host) or "nexusphp"
        url = f"https://{host}"
        if not any(c.kind == kind and (lookup_site(urlsplit(c.base_url).hostname) or {}).get("id") == (lookup_site(host) or {}).get("id") for c in configs.values()):
            configs[(kind, url)] = SiteConfig(0, kind, (lookup_site(host) or {}).get("name", host), url, cookie=cookie_header_for(data, url), user_agent=settings.network.user_agent)

    checks = []
    def check(site):
        started = time.monotonic()
        client = None
        try:
            client = build_site(site)
            client.test()
            result = "连接通过"
        except Exception as exc:
            # Store classifications only. Exceptions/URLs could contain credentials.
            from reelready.sites.base import LoginExpired
            result = "登录失效或缺少凭据" if isinstance(exc, LoginExpired) else "连接失败或页面不兼容，需进一步检查"
        finally:
            if client:
                client.close()
        record = {"name": site.name, "host": urlsplit(site.base_url).hostname, "kind": site.kind, "result": result, "seconds": round(time.monotonic() - started, 2)}
        print(json.dumps(record, ensure_ascii=False), flush=True)
        return record
    if network:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(check, site) for site in configs.values()]
            for future in as_completed(futures):
                checks.append(future.result())
    schema_counts = dict(Counter(row["schema"] for row in definitions))
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"), "source_revision": catalog["revision"],
        "definition_count": len(definitions), "schema_counts": schema_counts,
        "mismatches": [row["id"] for row in definitions if row["schema_mismatch"]],
        "dead_count": sum(row["is_dead"] for row in definitions),
        "synced": list(synced.values()), "connection_checks": checks,
        "planned_checks": [{"name": site.name, "host": urlsplit(site.base_url).hostname, "kind": site.kind} for site in configs.values()],
        "definitions": definitions,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "site-audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# ReelReady 站点核对", "", f"生成时间：{report['generated_at']}", "",
        f"全量核对 {len(definitions)} 个站点定义；上游已标记停用 {report['dead_count']} 个。架构与继承关系不一致：{len(report['mismatches'])} 个。",
        "", "实际连接检查仅覆盖已配置站点及含登录 Cookie 的可适配候选。不下载、不新增或修改站点，不保存凭据。未通过不一定代表账号失效，也可能是网络、Cloudflare 或页面变化。",
        "", "## 当前浏览器同步的站点", "", "| 站点 | 架构 | 接入情况 |", "|---|---|---|"]
    definition_by_id = {entry["id"]: entry for entry in definitions}
    for site_id, site in synced.items():
        label = "需个人 API Key" if site["adapter"] in ("rousipro", "mteam") else "已有 Cookie 适配器" if site["adapter"] else "需专用适配器"
        required = definition_by_id[site_id]["required_inputs"]
        if not site["adapter"] and required:
            label += "；还需 " + ", ".join(required)
        if site["adapter"] not in ("rousipro", "mteam") and site["adapter"]:
            label += "；含登录 Cookie" if site["has_login_cookie"] else "；未发现登录 Cookie"
        if site["is_dead"]:
            label += "；上游曾标记停用，需实测确认"
        lines.append(f"| {site['name']} | {site['schema']} | {label} |")
    lines += ["", "## 实际连接检查", "", "| 站点 | 域名 | 结果 |", "|---|---|---|"]
    if not network:
        lines += ["| — | — | 未执行联网检查，待确认具体目的地与凭据使用 |"]
    for check in checks:
        lines.append(f"| {check['name']} | {check['host']} | {check['result']} |")
    lines += ["", "## 待授权的只读连接检查", "", "确认后，只向下列原站点发送该站点对应的已同步 Cookie / API Key。仅检测登录，不下载、不新增或修改站点。", "", "| 站点 | 目的域名 | 凭据 |", "|---|---|---|"]
    for site in report["planned_checks"]:
        credential = "API Key" if site["kind"] in ("mteam", "rousipro") else "该站点的 Cookie"
        lines.append(f"| {site['name']} | {site['host']} | {credential} |")
    lines += ["", "全量定义和各站点所需额外输入字段见同目录 site-audit.json。", ""]
    (output / "site-audit.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"definitions": len(definitions), "schemas": schema_counts, "dead": report["dead_count"], "synced": len(synced), "checks": dict(Counter(c["result"] for c in checks))}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--network", action="store_true")
    args = parser.parse_args()
    audit(args.source, args.output, args.network)
