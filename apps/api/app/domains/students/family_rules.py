from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from app.platform.contracts import StaffActorContext

FamilyAction = Literal["request", "verify", "end", "revoke"]
FamilyRelationshipStatus = Literal[
    "draft", "pending_verification", "confirmed", "rejected", "ended", "revoked"
]


@dataclass(frozen=True, slots=True)
class FamilyActorValidation:
    actor_person_id: UUID
    actor_assignment_id: UUID
    checked_at: datetime
    scope_kind: Literal["school", "relationship"]
    scope_id: UUID
    action: FamilyAction


class CurrentFamilyFactActorValidator(Protocol):
    def validate(
        self, *, actor: StaffActorContext, action: FamilyAction, target: str, checked_at: datetime
    ) -> FamilyActorValidation: ...


@dataclass(frozen=True, slots=True)
class FamilyRelationshipState:
    source_key: str
    authority_version: int | None
    status: FamilyRelationshipStatus
    guardian_person_id: UUID
    student_id: UUID
    requested_by_person_id: UUID
    requested_by_assignment_id: UUID
    review_of_relationship_id: UUID | None


def validate_family_transition(
    *,
    previous: FamilyRelationshipState,
    desired_status: FamilyRelationshipStatus,
    actor: FamilyActorValidation,
) -> None:
    if previous.status in {"ended", "revoked", "rejected"}:
        raise ValueError("已终止的家校关系必须使用新来源键重新申请。")
    if actor.actor_person_id == previous.guardian_person_id:
        raise ValueError("核验人员不能核验自己的家校关系。")
    if desired_status == "confirmed":
        if actor.action != "verify":
            raise ValueError("正式发布必须经过当前核验职责。")
        if (
            actor.actor_person_id == previous.requested_by_person_id
            or actor.actor_assignment_id == previous.requested_by_assignment_id
        ):
            raise ValueError("申请人与核验人必须分离。")
        if previous.status != "pending_verification" or previous.authority_version != 1:
            raise ValueError("只有本版待核验关系可以正式发布。")


def validate_new_family_request(
    *,
    actor: FamilyActorValidation,
    guardian_person_id: UUID,
    student_id: UUID,
    predecessor: FamilyRelationshipState | None,
) -> None:
    """Require one retained endpoint and a terminal predecessor for a binding correction."""

    if actor.action != "request":
        raise ValueError("家校关系申请必须经过当前申请职责。")
    if actor.actor_person_id == guardian_person_id:
        raise ValueError("工作人员不能申请自己的家校关系。")
    if predecessor is not None:
        if predecessor.status not in {"ended", "revoked"}:
            raise ValueError("错关联纠正必须先停用旧关系。")
        same_guardian = predecessor.guardian_person_id == guardian_person_id
        same_student = predecessor.student_id == student_id
        if same_guardian and same_student:
            raise ValueError("完全相同的关系不属于错关联改绑。")
        if not same_guardian and not same_student:
            raise ValueError("错关联纠正必须至少保留一个关联端点。")
