"""F6 configuration binds explicit duties; it cannot grant them."""

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.composition.school_fact_education_settings import EducationConfiguration
from app.platform.school_facts.f6_policy import F6_FACT_KINDS, F6_OPERATIONS


class ResidenceScopeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    assignment_id: UUID
    operation_code: str
    scope_id: UUID

    @model_validator(mode="after")
    def exact_operation(self) -> Self:
        if self.operation_code not in F6_OPERATIONS:
            raise ValueError("Exact F6 operation required")
        return self


class ResidenceConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    enabled: bool = Field(default=True, strict=True)
    approval_reference: str = Field(min_length=1, max_length=200)
    scope_bindings: tuple[ResidenceScopeBinding, ...] = Field(max_length=500)
    creation_catalog: dict[str, dict[str, str]] = Field(default_factory=dict)
    dependency_education: EducationConfiguration | None = None

    @model_validator(mode="after")
    def finite_unique(self) -> Self:
        pairs = [(b.assignment_id, b.operation_code) for b in self.scope_bindings]
        if len(set(pairs)) != len(pairs) or set(self.creation_catalog) - F6_FACT_KINDS:
            raise ValueError("Ambiguous or unknown F6 binding")
        if any(
            len(values) > 100
            or any(
                not key.strip()
                or "*" in key
                or "?" in key
                or len(key) > 255
                or not label.strip()
                or len(label) > 120
                for key, label in values.items()
            )
            for values in self.creation_catalog.values()
        ):
            raise ValueError("Finite exact F6 creation catalog required")
        return self
