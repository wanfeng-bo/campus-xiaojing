"""Explicit admin_v3 input: full policy text never passes through Excel."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Literal

from pydantic import AwareDatetime, Field

from app.platform.contracts import FrozenContractModel
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV2,
    CanonicalFactChangeV2,
    CanonicalFactContentV2,
    FactOperation,
    FactOriginV1,
    SchoolFactKind,
    StableSourceKey,
)


class SchoolFactJsonChangeV3(FrozenContractModel):
    operation: FactOperation
    fact_kind: SchoolFactKind
    source_key: StableSourceKey
    expected_version: int = Field(strict=True, ge=0)
    effective_at: AwareDatetime
    content: CanonicalFactContentV2 | None


class SchoolFactJsonInputV3(FrozenContractModel):
    input_version: Literal["admin_v3"]
    base_version: int = Field(strict=True, ge=1)
    changes: tuple[SchoolFactJsonChangeV3, ...] = Field(min_length=1, max_length=1000)


class SchoolFactJsonPlanBuilder:
    def build(self, request: SchoolFactJsonInputV3) -> CanonicalFactChangeSetV2:
        changes = tuple(
            CanonicalFactChangeV2(
                operation=item.operation,
                fact_kind=item.fact_kind,
                source_key=item.source_key,
                expected_version=item.expected_version,
                effective_at=item.effective_at,
                origin=FactOriginV1(sheet_code="admin_v3", row_number=index + 2),
                content=item.content,
            )
            for index, item in enumerate(request.changes)
        )
        payload = {
            "input_version": request.input_version,
            "changes": [item.model_dump(mode="json", exclude_none=True) for item in changes],
        }
        digest = sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return CanonicalFactChangeSetV2(
            source_sha256=digest, base_version=request.base_version, changes=changes
        )
