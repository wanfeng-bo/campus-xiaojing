"""Explicit finite F2 engineering configuration; no authority is inferred from these hints."""

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.platform.school_facts.f2_policy import F2_FACT_KINDS


class DirectoryScopeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    assignment_id: UUID
    operation_code: str
    scope_id: UUID

    @model_validator(mode="after")
    def exact_operation(self) -> Self:
        bits = self.operation_code.split(".")
        if (
            len(bits) != 4
            or bits[:2] != ["school_facts", "f2"]
            or bits[2] not in F2_FACT_KINDS
            or bits[3] not in {"read", "maintain"}
        ):
            raise ValueError("Exact F2 family operation required")
        return self


class DirectoryConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    approval_reference: str = Field(min_length=1, max_length=200)
    scope_bindings: tuple[DirectoryScopeBinding, ...] = Field(max_length=300)
    source_catalog: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    creation_catalog: dict[str, dict[str, str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def finite_unique(self) -> Self:
        pairs = [(b.assignment_id, b.operation_code) for b in self.scope_bindings]
        if len(set(pairs)) != len(pairs):
            raise ValueError("Ambiguous F2 scope binding")
        for catalog in (self.source_catalog, self.creation_catalog):
            if set(catalog) - F2_FACT_KINDS or any(
                len(values) > 100 for values in catalog.values()
            ):
                raise ValueError("Only finite approved F2 facts may be configured")
        return self
