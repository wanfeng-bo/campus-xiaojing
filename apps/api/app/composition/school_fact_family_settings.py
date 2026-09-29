"""Explicit F3 assembly hints; current authority always comes from formal grants."""

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.platform.school_facts.f3_policy import F3_FACT_KINDS, F3_OPERATIONS


class FamilyScopeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    assignment_id: UUID
    operation_code: str
    scope_id: UUID

    @model_validator(mode="after")
    def exact_operation(self) -> Self:
        if self.operation_code not in F3_OPERATIONS:
            raise ValueError("Exact approved F3 operation required.")
        return self


class FamilyReadDependencyBinding(FamilyScopeBinding):
    """Select an existing read scope for exact dependencies; never grant authority."""

    object_keys: tuple[str, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def finite_read_dependencies(self) -> Self:
        if (
            not self.operation_code.endswith(".read")
            or len(set(self.object_keys)) != len(self.object_keys)
            or any(
                not key or key != key.strip() or len(key) > 255 or "*" in key or "?" in key
                for key in self.object_keys
            )
        ):
            raise ValueError("Exact unique read dependency objects required.")
        return self


class FamilyConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    # Local rollout control; disabling never rolls back authority or database state.
    enabled: bool = Field(default=True, strict=True)
    approval_reference: str = Field(min_length=1, max_length=200)
    scope_bindings: tuple[FamilyScopeBinding, ...] = Field(max_length=300)
    read_dependency_bindings: tuple[FamilyReadDependencyBinding, ...] = Field(
        default=(), max_length=100
    )
    source_catalog: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    source_labels: dict[str, dict[str, str]] = Field(default_factory=dict)
    creation_catalog: dict[str, dict[str, str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def finite_unique(self) -> Self:
        pairs = [(b.assignment_id, b.operation_code) for b in self.scope_bindings]
        if len(set(pairs)) != len(pairs):
            raise ValueError("Ambiguous F3 scope binding.")
        dependencies = [
            (binding.assignment_id, binding.operation_code, key)
            for binding in self.read_dependency_bindings
            for key in binding.object_keys
        ]
        if len(set(dependencies)) != len(dependencies):
            raise ValueError("Ambiguous F3 read dependency binding.")
        for catalog in (self.source_catalog, self.source_labels, self.creation_catalog):
            if set(catalog) - F3_FACT_KINDS or any(
                len(values) > 100 for values in catalog.values()
            ):
                raise ValueError("Only finite approved F3 facts may be configured.")
        for kind, labels in self.source_labels.items():
            if not set(labels) <= set(self.source_catalog.get(kind, ())) or any(
                type(label) is not str or not label.strip() or len(label) > 120
                for label in labels.values()
            ):
                raise ValueError("Source labels must describe only finite approved F3 sources.")
        return self
