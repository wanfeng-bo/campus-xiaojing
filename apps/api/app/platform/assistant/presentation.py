from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, cast

from .contracts import FallbackPresentationV1, JsonObject, NonNullJsonValue


@dataclass(frozen=True, slots=True)
class PreparedPresentation:
    """只区分展示值与可入模型安全账本的未填值模板。"""

    display_kind: Literal["text", "card"]
    display_payload: NonNullJsonValue
    model_safe_template: str | None = None


def contains_forbidden_local_token(
    text: str,
    *,
    json_values: tuple[object, ...] = (),
    opaque_values: tuple[str | None, ...] = (),
) -> bool:
    """只做字符串命中；老师原话不属于此处输入。"""

    tokens = [value for value in opaque_values if value]
    for value in json_values:
        local_tokens = _local_value_tokens(value)
        if local_tokens is None:
            return True
        tokens.extend(local_tokens)
    return any(token in text for token in tokens)


def prepare_fallback(fallback: FallbackPresentationV1) -> PreparedPresentation:
    return PreparedPresentation(
        display_kind=fallback.display_kind,
        display_payload=fallback.display_payload,
    )


def prepare_precomposed(
    *,
    display_kind: Literal["text", "card"],
    display_template: str,
    field_map: JsonObject,
    declared_field_names: tuple[str, ...],
    fallback: FallbackPresentationV1,
) -> PreparedPresentation:
    if display_kind != "text" or not set(field_map).issubset(declared_field_names):
        return prepare_fallback(fallback)
    rendered = fill_template_once(
        display_template,
        field_map,
        placeholder_kind="field",
    )
    if rendered is None:
        return prepare_fallback(fallback)
    return PreparedPresentation(
        display_kind="text",
        display_payload=rendered,
        model_safe_template=display_template,
    )


def prepare_composed_template(
    *,
    rendered_template: str,
    slot_map: JsonObject,
    fallback: FallbackPresentationV1,
) -> PreparedPresentation:
    rendered = fill_template_once(
        rendered_template,
        slot_map,
        placeholder_kind="slot",
    )
    if rendered is None:
        return prepare_fallback(fallback)
    return PreparedPresentation(
        display_kind="text",
        display_payload=rendered,
        model_safe_template=rendered_template,
    )


def fill_template_once(
    template: str,
    values: JsonObject,
    *,
    placeholder_kind: Literal["field", "slot"],
) -> str | None:
    """一次扫描、一次填值；插入值中的大括号永不会再被解析。"""

    fragments: list[str] = []
    names: list[str] = []
    literal_start = 0
    index = 0
    while index < len(template):
        character = template[index]
        if character == "}":
            return None
        if character != "{":
            index += 1
            continue
        closing = template.find("}", index + 1)
        if closing < 0:
            return None
        name = template[index + 1 : closing]
        if not _valid_placeholder(name, placeholder_kind):
            return None
        fragments.append(template[literal_start:index])
        names.append(name)
        literal_start = closing + 1
        index = closing + 1
    fragments.append(template[literal_start:])

    if len(names) != len(values) or len(set(names)) != len(names) or set(names) != set(values):
        return None

    serialized_values: dict[str, str] = {}
    for name in names:
        serialized = _local_text(values[name])
        if serialized is None:
            return None
        local_tokens = _local_value_tokens(values[name])
        if local_tokens is None or any(
            token in fragment for token in local_tokens for fragment in fragments
        ):
            return None
        serialized_values[name] = serialized

    rendered: list[str] = [fragments[0]]
    for position, name in enumerate(names):
        rendered.extend((serialized_values[name], fragments[position + 1]))
    return "".join(rendered)


def _valid_placeholder(name: str, kind: Literal["field", "slot"]) -> bool:
    if kind == "slot":
        return (
            2 <= len(name) <= 12
            and name[0] == "S"
            and name[1] in "123456789"
            and name[1:].isdigit()
        )
    allowed = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
    return (
        1 <= len(name) <= 120
        and name[0].isalpha()
        and all(character in allowed for character in name)
    )


def _local_text(value: object) -> str | None:
    if type(value) is str:
        return value
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return None


def _local_value_tokens(value: object) -> tuple[str, ...] | None:
    """递归枚举完整 JSON、对象键、非空字符串与标量的稳定文本。"""

    serialized = _local_text(value)
    if serialized is None:
        return None
    tokens = [serialized] if serialized else []
    if type(value) is list:
        for item in cast(list[object], value):
            nested = _local_value_tokens(item)
            if nested is None:
                return None
            tokens.extend(nested)
    elif type(value) is dict:
        for key, item in cast(dict[str, object], value).items():
            if key:
                tokens.append(key)
            nested = _local_value_tokens(item)
            if nested is None:
                return None
            tokens.extend(nested)
    return tuple(tokens)


__all__ = [
    "PreparedPresentation",
    "contains_forbidden_local_token",
    "fill_template_once",
    "prepare_composed_template",
    "prepare_fallback",
    "prepare_precomposed",
]
