"""User-tunable settings, stored as one JSON document in the ``settings`` table.

Each section is a pydantic model. Field ``title`` / ``description`` double as the labels of
the settings page, which is rendered generically from these models.
"""

import secrets
import string
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import text

from .db import session_scope
from .models import Setting

SETTINGS_KEY = "app"

EDGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0"
)


def _field(default: Any, title: str, help: str | None = None, **extra: Any) -> Any:
    if isinstance(default, list):
        return Field(default_factory=lambda: list(default), title=title, description=help, json_schema_extra=extra)
    return Field(default, title=title, description=help, json_schema_extra=extra or None)


def _random_token(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class CollectSettings(BaseModel):
    interval_hours: float = _field(24, "收集间隔(小时)")
    max_age_years: int = Field(1, ge=0, le=100, title="只收近几年的片 · 年")
    max_age_months: int = Field(0, ge=0, le=11, title="额外月份", description="年和月相加：0 年 6 月表示近半年。按上映日期筛选；来源仅提供年份时按年份判断。")
    douban_enabled: bool = _field(True, "收集豆瓣正在热映")
    douban_min_rating: float = _field(7.5, "豆瓣最低评分")
    douban_min_votes: int = _field(5000, "豆瓣最少评价人数")
    tmdb_enabled: bool = _field(True, "收集 TMDB 各地区院线")
    tmdb_regions: list[str] = _field(["CN", "HK", "US", "GB", "JP", "KR", "FR"], "TMDB 院线地区", "开启的地区参与院线收集；全部关闭时跳过 TMDB 院线收集。", widget="regions")
    tmdb_regions_order: list[str] = _field([], "地区显示顺序", hidden=True)
    tmdb_min_rating: float = _field(7.0, "TMDB 最低评分")
    tmdb_min_votes: int = _field(200, "TMDB 最少投票数")


class CheckSettings(BaseModel):
    interval_hours: float = _field(6, "上线检测间隔(小时)")
    provider_regions_order: list[str] = _field([], "地区显示顺序", hidden=True)
    provider_regions: list[str] = _field(
        ["US", "GB", "HK", "JP", "KR", "FR"], "流媒体地区", "开启的地区出现在线播放、租赁或购买即视为已上线；中国大陆主要参考豆瓣播放源。", widget="regions"
    )


class PTSettings(BaseModel):
    interval_hours: float = _field(2, "PT 搜索间隔(小时)")
    request_delay_seconds: float = _field(5, "每次请求间隔(秒)", "避免触发站点风控")
    early_search: bool = _field(False, "提前搜索未上线影片", "关闭时：等数字版日期到达或有可看平台再搜索；开启时：允许提前查看资源，但未确认上线前不会自动下载。自动任务和「立即搜索 PT」都遵守此开关。")


class TorrentRules(BaseModel):
    resolutions: list[str] = _field(["2160p", "1080p"], "允许的清晰度（同做种数时从上到下优先）", "先筛选清晰度、体积、做种数和片源，优先下载做种人数最多的资源；人数相同时按清晰度顺序、中字和 HDR 择优。全部不符合时继续监测。", widget="resolutions")
    max_size_gb: float = _field(30, "单个种子体积上限(GB)")
    min_seeders: int = _field(1, "最少做种数")
    prefer_chinese_sub: bool = _field(True, "中字优先")
    prefer_hdr: bool = _field(True, "HDR / 杜比视界优先")
    exclude_remux: bool = _field(True, "排除 Remux 和原盘")


class QBittorrentSettings(BaseModel):
    url: str = _field("http://host.docker.internal:8080", "Web UI 地址", "Docker 里访问宿主机用 host.docker.internal")
    username: str = _field("admin", "用户名")
    password: str = _field("", "密码", secret=True)
    save_path: str = _field("", "保存路径", r"留空用 qBittorrent 默认路径，例如 D:\Movies")
    category: str = _field("", "分类", "可留空")
    tags: str = _field("", "标签", "可留空，多个用逗号分隔")


EMAIL_PRESETS: dict[str, dict[str, Any]] = {
    "qq": {"label": "QQ 邮箱", "host": "smtp.qq.com", "port": 465, "security": "ssl"},
    "163": {"label": "网易 163", "host": "smtp.163.com", "port": 465, "security": "ssl"},
    "126": {"label": "网易 126", "host": "smtp.126.com", "port": 465, "security": "ssl"},
    "yeah": {"label": "网易 yeah.net", "host": "smtp.yeah.net", "port": 465, "security": "ssl"},
    "aliyun": {"label": "阿里邮箱", "host": "smtp.aliyun.com", "port": 465, "security": "ssl"},
    "gmail": {"label": "Gmail", "host": "smtp.gmail.com", "port": 465, "security": "ssl"},
    "icloud": {"label": "iCloud", "host": "smtp.mail.me.com", "port": 587, "security": "starttls"},
    "outlook": {"label": "Outlook / Hotmail", "host": "smtp-mail.outlook.com", "port": 587, "security": "starttls"},
    "custom": {"label": "自定义", "host": "", "port": 465, "security": "ssl"},
}


class EmailSettings(BaseModel):
    enabled: bool = _field(False, "启用邮件通知")
    provider: str = _field(
        "qq", "邮箱服务商", choices={k: v["label"] for k, v in EMAIL_PRESETS.items()}
    )
    smtp_host: str = _field("", "SMTP 服务器", "仅「自定义」时生效", custom_only=True)
    smtp_port: int = _field(465, "SMTP 端口", "仅「自定义」时生效", custom_only=True)
    security: str = _field(
        "ssl", "加密方式", "仅「自定义」时生效", custom_only=True,
        choices={"ssl": "SSL/TLS", "starttls": "STARTTLS", "none": "不加密"},
    )
    username: str = _field("", "邮箱账号", "完整邮箱地址，也作为发件人")
    password: str = _field("", "授权码 / 应用专用密码", "QQ/163 用授权码，Gmail/iCloud 用应用专用密码", secret=True)
    recipients: list[str] = _field([], "收件人", "多个用逗号分隔，留空则发给自己")
    digest_hour: int = _field(9, "每日汇总发送时间(点)", "0-23")

    def resolved_server(self) -> tuple[str, int, str]:
        if self.provider == "custom" or self.provider not in EMAIL_PRESETS:
            return self.smtp_host, self.smtp_port, self.security
        preset = EMAIL_PRESETS[self.provider]
        return preset["host"], preset["port"], preset["security"]


class TMDBSettings(BaseModel):
    api_key: str = _field("", "API Key", "themoviedb.org 申请的 v3 API Key 或 v4 Read Access Token", secret=True)
    language: str = _field("zh-CN", "语言")
    use_proxy: bool = _field(True, "通过代理访问", "国内直连 TMDB 不稳定，填了代理地址时建议开启")


class NetworkSettings(BaseModel):
    proxy_url: str = _field("", "代理地址", "例如 http://host.docker.internal:7890，留空不使用")
    user_agent: str = _field(EDGE_UA, "User-Agent", "访问 PT 站时使用，最好和你的浏览器一致")


class CookieCloudSettings(BaseModel):
    uuid: str = Field(default_factory=lambda: _random_token(22))
    password: str = Field(default_factory=lambda: _random_token(24))


class EventsSettings(BaseModel):
    retention_days: int | None = Field(default=None, ge=1, le=36500, title="动态保留天数", description="留空永久保留。填写正整数后，每天自动删除超过该天数的动态（包括未发送的通知），不影响影片和下载任务。", json_schema_extra={"widget": "optional_int"})


class AppSettings(BaseModel):
    collect: CollectSettings = Field(default_factory=CollectSettings)
    check: CheckSettings = Field(default_factory=CheckSettings)
    pt: PTSettings = Field(default_factory=PTSettings)
    rules: TorrentRules = Field(default_factory=TorrentRules)
    qbittorrent: QBittorrentSettings = Field(default_factory=QBittorrentSettings)
    email: EmailSettings = Field(default_factory=EmailSettings)
    tmdb: TMDBSettings = Field(default_factory=TMDBSettings)
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    events: EventsSettings = Field(default_factory=EventsSettings)
    cookiecloud: CookieCloudSettings = Field(default_factory=CookieCloudSettings)


# Sections shown on the settings page, in order.
SECTIONS: list[tuple[str, str]] = [
    ("collect", "收集"),
    ("check", "上线检测"),
    ("pt", "PT 搜索"),
    ("rules", "选种规则"),
    ("qbittorrent", "qBittorrent"),
    ("email", "邮件通知"),
    ("events", "动态记录"),
    ("tmdb", "TMDB"),
    ("network", "网络"),
]


def load_settings() -> AppSettings:
    with session_scope() as session:
        row = session.get(Setting, SETTINGS_KEY)
        if row is None:
            settings = AppSettings()
            # Persist right away so generated CookieCloud credentials stay stable.
            session.add(Setting(key=SETTINGS_KEY, value=settings.model_dump()))
            return settings
        return AppSettings.model_validate(row.value)


def save_settings(settings: AppSettings) -> None:
    with session_scope() as session:
        row = session.get(Setting, SETTINGS_KEY)
        if row is None:
            session.add(Setting(key=SETTINGS_KEY, value=settings.model_dump()))
        else:
            row.value = settings.model_dump()


def update_section(section: str, update: Callable[[BaseModel], BaseModel]) -> None:
    """Merge one form with the latest settings atomically, preserving other groups."""
    with session_scope() as session:
        session.execute(text('BEGIN IMMEDIATE'))
        row = session.get(Setting, SETTINGS_KEY)
        settings = AppSettings.model_validate(row.value) if row else AppSettings()
        setattr(settings, section, update(getattr(settings, section)))
        if row:
            row.value = settings.model_dump()
        else:
            session.add(Setting(key=SETTINGS_KEY, value=settings.model_dump()))
