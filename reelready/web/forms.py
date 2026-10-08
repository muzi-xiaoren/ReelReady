"""Render and parse settings sections generically from their pydantic models."""

import re
import typing
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


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
        kind = _kind(info.annotation, extra)
        value = getattr(section, name)
        if kind == "list":
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
                choices=extra.get("choices"),
                custom_only=bool(extra.get("custom_only")),
            )
        )
    return views


def parse(section: BaseModel, form: dict[str, str]) -> BaseModel:
    """Build an updated copy of ``section`` from submitted form values."""
    data = section.model_dump()
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
            if raw:
                data[view.name] = raw
        elif view.kind == "list":
            data[view.name] = [item for item in re.split(r"[,，\s]+", raw) if item]
        elif view.kind == "int":
            data[view.name] = int(float(raw or 0))
        elif view.kind == "float":
            data[view.name] = float(raw or 0)
        else:
            data[view.name] = raw
    return type(section).model_validate(data)
