from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .ports import AssistantModulePort


class UnknownAssistantModuleError(LookupError):
    """超级大脑给出的模块句柄不在当前静态目录中。"""


@dataclass(frozen=True, slots=True)
class AssistantModuleCatalogEntry:
    """随代码装配的单个模型安全模块说明和机械入口。"""

    module_ref: str
    model_safe_capability: str
    module: AssistantModulePort
    model_visible_field_names: tuple[str, ...] = ()
    presentation_capabilities: tuple[Literal["direct", "precomposed", "compose"], ...] = ("direct",)

    def __post_init__(self) -> None:
        try:
            field_names = tuple(self.model_visible_field_names)
            capabilities = tuple(self.presentation_capabilities)
        except TypeError:
            raise ValueError("catalog metadata must be iterable") from None
        object.__setattr__(self, "model_visible_field_names", field_names)
        object.__setattr__(self, "presentation_capabilities", capabilities)

        allowed = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_.-")
        if (
            not 1 <= len(self.module_ref) <= 120
            or not self.module_ref[0].islower()
            or any(character not in allowed for character in self.module_ref)
        ):
            raise ValueError("module_ref is invalid")
        if not 1 <= len(self.model_safe_capability) <= 8_000 or not any(
            not character.isspace() for character in self.model_safe_capability
        ):
            raise ValueError("model_safe_capability is invalid")
        if len(set(field_names)) != len(field_names):
            raise ValueError("model-visible field names must be unique")
        field_characters = frozenset(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"
        )
        if any(
            not 1 <= len(name) <= 120
            or not name[0].isalpha()
            or any(character not in field_characters for character in name)
            for name in field_names
        ):
            raise ValueError("model-visible field name is invalid")
        allowed_capabilities = frozenset({"direct", "precomposed", "compose"})
        if any(capability not in allowed_capabilities for capability in capabilities):
            raise ValueError("presentation capability is invalid")
        if not capabilities or len(set(capabilities)) != len(capabilities):
            raise ValueError("presentation capabilities must be non-empty and unique")
        if len(_format_model_safe_context(self)) > 8_000:
            raise ValueError("model-safe catalog context is too long")


def _format_model_safe_context(entry: AssistantModuleCatalogEntry) -> str:
    return (
        f"module_ref={entry.module_ref}; capability={entry.model_safe_capability}; "
        f"model_visible_field_names=[{','.join(sorted(entry.model_visible_field_names))}]; "
        f"presentation_capabilities=[{','.join(sorted(entry.presentation_capabilities))}]"
    )


@dataclass(frozen=True, slots=True, init=False)
class StaticAssistantCatalog:
    """构造后不可增删的代码内目录；不提供运行时注册入口。"""

    _entries: tuple[AssistantModuleCatalogEntry, ...]

    def __init__(self, entries: tuple[AssistantModuleCatalogEntry, ...]) -> None:
        frozen_entries = tuple(entries)
        if len({entry.module_ref for entry in frozen_entries}) != len(frozen_entries):
            raise ValueError("duplicate module_ref")
        object.__setattr__(self, "_entries", frozen_entries)

    def model_safe_context(self) -> tuple[str, ...]:
        return tuple(_format_model_safe_context(entry) for entry in self._entries)

    def resolve(self, module_ref: str) -> AssistantModuleCatalogEntry:
        for entry in self._entries:
            if entry.module_ref == module_ref:
                return entry
        raise UnknownAssistantModuleError("module is not installed")


__all__ = [
    "AssistantModuleCatalogEntry",
    "StaticAssistantCatalog",
    "UnknownAssistantModuleError",
]
