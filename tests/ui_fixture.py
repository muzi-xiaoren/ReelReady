"""Isolated UI smoke server: synthetic cookies and delayed fake connection checks."""
import time
from unittest.mock import patch
import uvicorn
from reelready import config, db
from reelready.models import Site, Movie, now
from reelready.services import sites, site_tests
from reelready.web import routes
from reelready.web.app import create_app

config.ensure_dirs()
db.init_db()
with db.session_scope() as session:
    session.add(Movie(title='电影资料预览（模拟数据）', year=2026, overview='这是一段用于界面验证的剧情介绍。\n新增资料会展示上映日期、影片类型、片长以及演员和角色。', metadata_checked_at=now(), details={'release_date': '2026-08-11', 'genres': ['剧情', '喜剧'], 'runtime': 120, 'countries': ['中国大陆'], 'languages': ['普通话'], 'directors': [{'name': '示例导演'}], 'writers': [{'name': '示例编剧'}], 'cast': [{'name': '演员甲', 'role': '主角'}, {'name': '演员乙', 'role': '餐厅老板'}, {'name': '演员丙', 'role': '朋友'}], 'releases': [{'date': '2026-08-11', 'region': 'CN'}], 'message': '资料已更新（模拟）'}))
data = {"cookie_data": {host: [{"domain": host, "name": "c_secure_pass", "value": "synthetic"}] for host in ("tjupt.org", "hdfans.org", "13city.org")}}
def fake_check(settings, site_id):
    time.sleep(5)
    with db.session_scope() as session:
        site = session.get(Site, site_id)
        if site:
            site.status = "ok"
            site.status_message = "UI test completed"
    return True, "UI test completed"
with patch.object(sites, "load_cookiecloud", return_value=(data, None)), patch.object(routes, "load_cookiecloud", return_value=(data, None)), patch.object(site_tests, "test_site", side_effect=fake_check):
    uvicorn.run(create_app(), host="0.0.0.0", port=8765)
