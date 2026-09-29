"""Fixed F7 owner-terminal and bounded-target transport; no generic history query."""

from __future__ import annotations

from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from app.platform.contracts import FrozenContractModel
from app.platform.school_facts.contracts import Sha256Hex
from app.platform.school_facts.executor_contracts import (
    ContextToken,
    SchoolFactExecutorApplyResponse,
    SchoolFactExecutorOwnResultTarget,
    SessionBinding,
)
from app.platform.school_facts.ports import (
    SchoolFactApplyResult,
    SchoolFactOwnTargetPage,
    SchoolFactOwnTerminal,
)


class SchoolFactExecutorOwnTargetPageRequest(FrozenContractModel):
    context_token: ContextToken
    session_binding: SessionBinding
    locator_sha256: Sha256Hex
    offset: int = Field(default=0, strict=True, ge=0, le=50_000)
    limit: int = Field(default=20, strict=True, ge=1, le=100)


class SchoolFactExecutorOwnTerminal(FrozenContractModel):
    locator_sha256: Sha256Hex
    confirmed_at: AwareDatetime
    result: SchoolFactExecutorApplyResponse
    target_count: int = Field(strict=True, ge=1, le=50_000)

    @model_validator(mode="after")
    def require_applied_batch(self) -> Self:
        if not self.result.replayed or self.result.changed_count != self.target_count:
            raise ValueError("Own receipt terminal does not match its complete applied batch.")
        return self

    @classmethod
    def from_domain(cls, value: SchoolFactOwnTerminal) -> Self:
        return cls.model_validate(
            {
                "locator_sha256": value.locator_sha256,
                "confirmed_at": value.confirmed_at,
                "result": SchoolFactExecutorApplyResponse(
                    batch_id=value.result.batch_id,
                    target_version=value.result.target_version,
                    changed_count=value.result.changed_count,
                    replayed=value.result.replayed,
                ),
                "target_count": value.target_count,
            }
        )

    def to_domain(self) -> SchoolFactOwnTerminal:
        return SchoolFactOwnTerminal(
            self.locator_sha256,
            self.confirmed_at,
            SchoolFactApplyResult(
                self.result.batch_id,
                self.result.target_version,
                self.result.changed_count,
                self.result.replayed,
            ),
            self.target_count,
        )


class SchoolFactExecutorOwnTargetPage(FrozenContractModel):
    terminal: SchoolFactExecutorOwnTerminal
    items: tuple[SchoolFactExecutorOwnResultTarget, ...] = Field(max_length=100)
    offset: int = Field(strict=True, ge=0, le=50_000)
    next_offset: int = Field(strict=True, ge=0, le=50_000)
    has_more: bool

    @model_validator(mode="after")
    def require_bounded_continuation(self) -> Self:
        if (
            not self.offset <= self.next_offset <= self.offset + 100
            or self.next_offset > self.terminal.target_count
            or self.has_more != (self.next_offset < self.terminal.target_count)
            or len(self.items) > self.next_offset - self.offset
            or len({(item.fact_kind, item.source_key) for item in self.items}) != len(self.items)
            or any(item.original_result is not None or item.creation_receipt for item in self.items)
        ):
            raise ValueError("Own receipt target page is inconsistent.")
        return self

    @classmethod
    def from_domain(cls, value: SchoolFactOwnTargetPage) -> Self:
        return cls.model_validate(
            {
                "terminal": SchoolFactExecutorOwnTerminal.from_domain(value.terminal),
                "items": tuple(
                    SchoolFactExecutorOwnResultTarget.model_validate(
                        {
                            "fact_kind": item.fact_kind,
                            "source_key": item.source_key,
                            "operation": item.operation,
                            "target_version": item.target_version,
                            "creation_receipt": item.creation_receipt,
                        }
                    )
                    for item in value.items
                ),
                "offset": value.offset,
                "next_offset": value.next_offset,
                "has_more": value.has_more,
            }
        )

    def to_domain(self) -> SchoolFactOwnTargetPage:
        return SchoolFactOwnTargetPage(
            self.terminal.to_domain(),
            tuple(item.to_domain() for item in self.items),
            self.offset,
            self.next_offset,
            self.has_more,
        )


class SchoolFactExecutorOwnTargetPageResponse(FrozenContractModel):
    page: SchoolFactExecutorOwnTargetPage | None
