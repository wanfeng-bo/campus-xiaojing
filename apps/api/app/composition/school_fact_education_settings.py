"""Finite education assembly coordinates; configuration does not grant authority."""

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.composition.school_fact_directory_settings import DirectoryConfiguration
from app.composition.school_fact_family_settings import FamilyConfiguration
from app.platform.school_facts.f4_policy import F4_FACT_KINDS, F4_OPERATIONS
from app.platform.school_facts.f5_policy import F5_FACT_KINDS, F5_OPERATIONS


class EducationScopeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    assignment_id: UUID
    operation_code: str
    scope_id: UUID

    @model_validator(mode="after")
    def exact_operation(self) -> Self:
        if self.operation_code not in F4_OPERATIONS | F5_OPERATIONS:
            raise ValueError("Exact education operation required.")
        return self


class EducationConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    enabled: bool = Field(default=True, strict=True)
    approval_reference: str = Field(min_length=1, max_length=200)
    scope_bindings: tuple[EducationScopeBinding, ...] = Field(max_length=500)
    dependency_directory: DirectoryConfiguration | None = None
    dependency_family: FamilyConfiguration | None = None
    creation_catalog: dict[str, dict[str, str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def finite_unique(self) -> Self:
        pairs = [(b.assignment_id, b.operation_code) for b in self.scope_bindings]
        if len(set(pairs)) != len(pairs):
            raise ValueError("Ambiguous education operation binding.")
        if set(self.creation_catalog) - (F4_FACT_KINDS | F5_FACT_KINDS):
            raise ValueError("Unknown education creation kind.")
        for values in self.creation_catalog.values():
            if len(values) > 100 or any(
                not key
                or "*" in key
                or "?" in key
                or len(key) > 255
                or not label.strip()
                or len(label) > 120
                for key, label in values.items()
            ):
                raise ValueError("Finite exact education creation coordinates required.")
        return self
