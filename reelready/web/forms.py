"""Render and parse settings sections generically from their pydantic models."""

import re
import typing
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

SECRET_MASK = "••••••••••••"
REGIONS = {"CN": "中国大陆", "HK": "中国香港", "TW": "中国台湾", "US": "美国", "GB": "英国", "JP": "日本", "KR": "韩国", "FR": "法国", "DE": "德国", "IT": "意大利", "ES": "西班牙", "CA": "加拿大", "AU": "澳大利亚", "NZ": "新西兰", "SG": "新加坡", "MY": "马来西亚", "TH": "泰国", "IN": "印度", "BR": "巴西", "MX": "墨西哥", "RU": "俄罗斯", "NL": "荷兰", "SE": "瑞典", "NO": "挪威", "DK": "丹麦", "FI": "芬兰", "CH": "瑞士", "AT": "奥地利", "BE": "比利时", "PL": "波兰", "PT": "葡萄牙", "TR": "土耳其", "ZA": "南非", "ID": "印度尼西亚", "PH": "菲律宾", "VN": "越南", "AE": "阿联酋"}
RESOLUTIONS = {"4320p": "8K · 4320p", "2160p": "4K / UHD · 2160p", "1440p": "2K / QHD · 1440p", "1080p": "全高清 · 1080p / 1080i", "720p": "高清 · 720p", "576p": "标清 · 576p / 576i", "480p": "标清 · 480p / 480i"}


@dataclass
class FieldView:
    name: str
    title: str
    help: str | None
    kind: str  # bool / int / float / str / list / choice / secret
    value: Any
    choices: dict[str, str] | None = None
    custom_only: bool = False


def _kind(annotation: Any, extra: dict[str, Any]) -> str:
    if extra.get("widget"):
        return extra["widget"]
    if extra.get("secret"):
        return "secret"
    if extra.get("choices"):
        return "choice"
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    if typing.get_origin(annotation) is list:
        return "list"
    return "str"


def describe(section: BaseModel) -> list[FieldView]:
    views = []
    for name, info in type(section).model_fields.items():
        extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
        if extra.get('hidden'):
            continue
        kind = _kind(info.annotation, extra)
        value = getattr(section, name)
        choices = extra.get("choices")
        if kind in ("regions", "resolutions"):
            value = [item.upper() if kind == "regions" else {"4K": "2160p", "UHD": "2160p", "8K": "4320p"}.get(item.upper(), item) for item in value]
            catalog = REGIONS if kind == "regions" else RESOLUTIONS
            saved_order = getattr(section, name + '_order', []) if kind == 'regions' else []
            order = list(dict.fromkeys(saved_order + value + list(catalog)))
            choices = {item: catalog.get(item, item) for item in order}
        elif kind == "secret":
            value = SECRET_MASK if value else ""
        elif kind == "list":
            value = ", ".join(value)
        elif kind == "float" and float(value).is_integer():
            value = int(value)
        views.append(
            FieldView(
                name=name,
                title=info.title or name,
                help=info.description,
                kind=kind,
                value=value,
                choices=choices,
                custom_only=bool(extra.get("custom_only")),
            )
        )
    return views


def parse(section: BaseModel, form: dict[str, str]) -> BaseModel:
    """Build an updated copy of ``section`` from submitted form values."""
    data = section.model_dump()
    for name, info in type(section).model_fields.items():
        extra = info.json_schema_extra or {}
        if extra.get('hidden') and name in form:
            order = [item.upper() for item in re.split(r'[,，\s]+', form[name]) if item]
            if any(not re.fullmatch(r'[A-Z]{2}', item) for item in order):
                raise ValueError('地区代码应为两个英文字母')
            data[name] = list(dict.fromkeys(order))
    for view in describe(section):
        raw = form.get(view.name)
        if view.kind == "bool":
            data[view.name] = raw is not None
            continue
        if raw is None:
            continue
        raw = raw.strip()
        if view.kind == "secret":
            # An empty password box means "keep the current value".
            if raw and raw != SECRET_MASK:
                data[view.name] = raw
        elif view.kind in ("list", "regions", "resolutions"):
            values = [item for item in re.split(r"[,，\s]+", raw) if item]
            if view.kind == "regions":
                values = [item.upper() for item in values]
                if any(not re.fullmatch(r"[A-Z]{2}", item) for item in values):
                    raise ValueError("地区代码应为两个英文字母")
            data[view.name] = list(dict.fromkeys(values))
        elif view.kind == "int":
            data[view.name] = int(float(raw or 0))
        elif view.kind == "float":
            data[view.name] = float(raw or 0)
        else:
            data[view.name] = raw
    return type(section).model_validate(data)
