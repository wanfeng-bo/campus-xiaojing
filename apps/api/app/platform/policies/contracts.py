from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, StringConstraints

from app.platform.contracts import FrozenContractModel, SchoolId

PolicyText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
PolicyCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
]


class PolicyVersionSnapshot(FrozenContractModel):
    """公开指定时点实际生效的制度版本，不暴露写入能力。"""

    policy_id: UUID
    policy_version_id: UUID
    school_id: SchoolId
    business_code: PolicyCode
    title: PolicyText
    classification: Literal["public", "internal", "restricted"]
    effective_from: AwareDatetime
    effective_to: AwareDatetime | None
    supersedes_policy_version_id: UUID | None
    captured_at: AwareDatetime
