"""Selected-child correction intake projection; all durable operations use the Writer."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Literal, Protocol, cast
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field

from app.composition.school_fact_family_correction_executor import CorrectionReviewPreparation
from app.composition.school_fact_product_identity import ProductIdentity
from app.domains.students.family_corrections import FamilyCorrectionDenied
from app.platform.contracts import (
    ActorContext,
    CommandExecutionContext,
    FrozenContractModel,
    GuardianActorContext,
    StaffActorContext,
)
from app.platform.identity.execution_context import compose_command_context

CorrectionField = Literal[
    "name",
    "enrollment_status",
    "campus",
    "student_number",
    "residence_mode",
    "region",
    "relationship",
    "class_source_key",
    "grade_source_key",
    "assignment_source_key",
    "location_source_key",
    "position_source_key",
    "cohort_source_key",
    "display_name",
    "position_title",
    "person_source_key",
    "organization_source_key",
    "contact_value",
    "contact_kind",
    "service_hours",
    "course_source_key",
    "subject_source_key",
    "offering_source_key",
    "group_source_key",
    "teaching_role",
    "offering_status",
    "source_kind",
    "stage_source_keys",
    "grade_source_keys",
    "campus_source_key",
    "term_source_key",
    "group_mode",
    "student_source_key",
    "school_relationship_source_key",
    "subject_source_keys",
    "duty_scope",
    "scope_basis",
    "stage_source_key",
    "grade_group_source_key",
    "valid_to",
    "bed_source_key",
    "room_source_key",
    "effective_at",
]


class PublicCorrectionReceipt(FrozenContractModel):
    kind: Literal["family_fact_correction_receipt"] = "family_fact_correction_receipt"
    report_id: UUID
    fact_kind: str | None = Field(default=None, min_length=1, max_length=64)
    status: Literal["submitted", "resolved", "reviewed"]
    field_name: CorrectionField
    submitted_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    review_explanation: str | None = Field(default=None, max_length=1000)


class PublicCorrectionPage(FrozenContractModel):
    items: tuple[PublicCorrectionReceipt, ...] = Field(max_length=20)


class PublicStaffCorrection(PublicCorrectionReceipt):
    reference: str
    student_label: str
    field_label: str
    statement: str = Field(max_length=1000)
    required_action: Literal["maintain", "end", "revoke"]
    target_reference: str


class PublicStaffCorrectionPage(FrozenContractModel):
    items: tuple[PublicStaffCorrection, ...] = Field(max_length=20)


class PublicCorrectionOption(FrozenContractModel):
    fact_kind: str | None = Field(default=None, min_length=1, max_length=64)
    field_name: CorrectionField
    label: str = Field(min_length=1, max_length=800)
    reference: str = Field(min_length=8, max_length=1600)


class PublicCorrectionOptions(FrozenContractModel):
    items: tuple[PublicCorrectionOption, ...] = Field(max_length=100)


class CorrectionClientPort(Protocol):
    def prepare_review(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        report_id: UUID,
        explanation: str,
        required_action: Literal["maintain", "end", "revoke"],
    ) -> CorrectionReviewPreparation: ...
    def confirm_review(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        report_id: UUID,
        confirmation_token: str,
        confirmed: bool,
    ) -> BaseModel: ...
    def review_result(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        report_id: UUID,
        confirmation_token: str,
    ) -> BaseModel: ...
    def options(self, *, context: CommandExecutionContext, session_binding: str) -> BaseModel: ...
    def submit(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        target_reference: str,
        field_name: str,
        statement: str,
    ) -> BaseModel: ...
    def own_list(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        limit: int,
    ) -> BaseModel: ...
    def by_key(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        idempotency_key: str,
    ) -> BaseModel | None: ...
    def staff_list(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        fact_kind: str,
        source_key: str,
        field_name: str,
        required_action: Literal["maintain", "end", "revoke"],
        limit: int,
    ) -> BaseModel: ...
    def resolve(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        report_id: UUID,
        writer_receipt_ref: str,
    ) -> BaseModel: ...


_LABELS = {
    "name": "姓名",
    "enrollment_status": "在校状态",
    "campus": "当前校区",
    "student_number": "学号",
    "residence_mode": "走读或住宿",
    "region": "居住地区",
    "relationship": "孩子关联",
    "class_source_key": "孩子班级",
    "grade_source_key": "孩子年级",
    "assignment_source_key": "班级老师",
    "location_source_key": "班级教室",
    "position_source_key": "孩子岗位",
    "cohort_source_key": "孩子届别",
    "display_name": "正式名称",
    "position_title": "领导职务",
    "person_source_key": "任职人员",
    "organization_source_key": "所属组织",
    "contact_value": "官方联系渠道",
    "contact_kind": "渠道类型",
    "service_hours": "服务时间",
    "course_source_key": "课程",
    "subject_source_key": "学科",
    "offering_source_key": "开课",
    "group_source_key": "教学群体",
    "teaching_role": "任教职责",
    "offering_status": "开课状态",
    "source_kind": "名单来源",
    "stage_source_keys": "适用学段",
    "grade_source_keys": "适用年级",
    "campus_source_key": "开课校区",
    "term_source_key": "适用学期",
    "group_mode": "群体使用范围",
    "student_source_key": "成员学生",
    "school_relationship_source_key": "在校关系",
    "subject_source_keys": "任教学科",
    "duty_scope": "任教分工",
    "scope_basis": "适用口径",
    "stage_source_key": "适用学段",
    "grade_group_source_key": "学年年级",
    "valid_to": "有效截止时间",
    "bed_source_key": "住宿床位",
    "room_source_key": "住宿房间",
    "effective_at": "安排生效时间",
}


def _receipt(raw: dict[str, object]) -> PublicCorrectionReceipt:
    # The parent receives a minimal receipt, never the stored statement or other adults' data.
    fields = {k: v for k, v in raw.items() if k in PublicCorrectionReceipt.model_fields}
    return PublicCorrectionReceipt.model_validate_json(json.dumps(fields))


def _items(value: BaseModel, limit: int) -> list[dict[str, object]]:
    raw: object = value.model_dump(mode="json").get("items")
    if not isinstance(raw, list):
        raise RuntimeError("Correction response unavailable")
    rows = cast(list[object], raw)
    if len(rows) > limit or any(type(v) is not dict for v in rows):
        raise RuntimeError("Correction response unavailable")
    return cast(list[dict[str, object]], rows)


class FamilyCorrectionProduct:
    def prepare_review(
        self,
        identity: ProductIdentity,
        request_id: UUID,
        *,
        report_id: UUID,
        explanation: str,
        required_action: Literal["maintain", "end", "revoke"],
    ) -> CorrectionReviewPreparation:
        self._check(identity, staff=True)
        result = self._executor.prepare_review(
            context=self._context(identity, request_id, f"family-review-prepare:{request_id}"),
            session_binding=identity.session_binding,
            report_id=report_id,
            explanation=explanation,
            required_action=required_action,
        )
        self._check(identity, staff=True)
        if result.report_id != report_id:
            raise RuntimeError("Review preparation target changed.")
        return result

    def confirm_review(
        self,
        identity: ProductIdentity,
        request_id: UUID,
        *,
        report_id: UUID,
        confirmation_token: str,
        confirmed: bool,
    ) -> PublicCorrectionReceipt:
        self._check(identity, staff=True)
        result = self._executor.confirm_review(
            context=self._context(identity, request_id, f"family-review-confirm:{report_id}"),
            session_binding=identity.session_binding,
            report_id=report_id,
            confirmation_token=confirmation_token,
            confirmed=confirmed,
        )
        self._after_submission(identity, staff=True)
        receipt = _receipt(result.model_dump(mode="json"))
        if receipt.report_id != report_id or receipt.status != "reviewed":
            raise RuntimeError("Review outcome requires original result lookup.")
        return receipt

    def review_result(
        self,
        identity: ProductIdentity,
        request_id: UUID,
        *,
        report_id: UUID,
        confirmation_token: str,
    ) -> PublicCorrectionReceipt:
        self._check(identity, staff=True)
        result = self._executor.review_result(
            context=self._context(identity, request_id, f"family-review-result:{report_id}"),
            session_binding=identity.session_binding,
            report_id=report_id,
            confirmation_token=confirmation_token,
        )
        self._check(identity, staff=True)
        receipt = _receipt(result.model_dump(mode="json"))
        if receipt.report_id != report_id or receipt.status != "reviewed":
            raise RuntimeError("Review result unavailable.")
        return receipt

    def __init__(
        self,
        *,
        executor: CorrectionClientPort,
        current_actor: Callable[[ActorContext], bool],
        staff_source: Callable[[StaffActorContext, str], tuple[str, str]],
        education_source: Callable[[StaffActorContext, str, str], str] | None = None,
    ) -> None:
        self._executor, self._current, self._staff_source = executor, current_actor, staff_source
        self._education_source = education_source

    def _check(self, identity: ProductIdentity, *, staff: bool = False) -> None:
        expected = StaffActorContext if staff else GuardianActorContext
        if type(identity.actor) is not expected or not self._current(identity.actor):
            raise FamilyCorrectionDenied("Current selected identity required")

    def _after_submission(self, identity: ProductIdentity, *, staff: bool = False) -> None:
        try:
            self._check(identity, staff=staff)
        except FamilyCorrectionDenied:
            # A command may already be durable. Never invite a fresh-key resubmission
            # or disclose its receipt after revocation; recover the original result.
            raise RuntimeError("Submission outcome requires an authorized receipt lookup") from None

    @staticmethod
    def _context(identity: ProductIdentity, request_id: UUID, key: str) -> CommandExecutionContext:
        return compose_command_context(identity.actor, request_id, key)

    def options(
        self, identity: ProductIdentity, request_id: UUID
    ) -> tuple[PublicCorrectionOption, ...]:
        self._check(identity)
        value = self._executor.options(
            context=self._context(identity, request_id, f"family-correction-options:{request_id}"),
            session_binding=identity.session_binding,
        )
        rows = _items(value, 100)
        options = tuple(PublicCorrectionOption.model_validate_json(json.dumps(row)) for row in rows)
        if len({row.reference for row in options}) != len(options):
            raise RuntimeError("Correction options unavailable")
        self._check(identity)
        return options

    def submit(
        self,
        identity: ProductIdentity,
        request_id: UUID,
        *,
        reference: str,
        field_name: str,
        statement: str,
        idempotency_key: UUID,
    ) -> PublicCorrectionReceipt:
        self._check(identity)
        value = self._executor.submit(
            context=self._context(identity, request_id, f"family-correction:{idempotency_key}"),
            session_binding=identity.session_binding,
            target_reference=reference,
            field_name=field_name,
            statement=statement,
        )
        self._after_submission(identity)
        return _receipt(value.model_dump(mode="json"))

    def own_list(
        self, identity: ProductIdentity, request_id: UUID, *, limit: int
    ) -> PublicCorrectionPage:
        self._check(identity)
        value = self._executor.own_list(
            context=self._context(identity, request_id, f"family-correction-list:{request_id}"),
            session_binding=identity.session_binding,
            limit=limit,
        )
        self._check(identity)
        return PublicCorrectionPage(items=tuple(_receipt(row) for row in _items(value, limit)))

    def by_key(
        self,
        identity: ProductIdentity,
        request_id: UUID,
        *,
        key: UUID,
    ) -> PublicCorrectionReceipt | None:
        self._check(identity)
        value = self._executor.by_key(
            context=self._context(identity, request_id, f"family-correction-lookup:{request_id}"),
            session_binding=identity.session_binding,
            idempotency_key=f"family-correction:{key}",
        )
        self._check(identity)
        return None if value is None else _receipt(value.model_dump(mode="json"))

    def staff_list(
        self,
        identity: ProductIdentity,
        request_id: UUID,
        *,
        target_reference: str,
        field_name: CorrectionField,
        required_action: Literal["maintain", "end", "revoke"],
        limit: int,
    ) -> PublicStaffCorrectionPage:
        self._check(identity, staff=True)
        try:
            actor = cast(StaffActorContext, identity.actor)
            from app.platform.school_facts.school_scope_reference import (
                school_scope_reference_coordinates,
            )

            parts = target_reference.split(":")
            coordinate = school_scope_reference_coordinates(target_reference)
            education = (len(parts) == 3 and parts[0] in {"f4", "f5"}) or (
                coordinate is not None and coordinate[0] in {"f4", "f5"} and not coordinate[2]
            )
            if education:
                kind = coordinate[1] if coordinate is not None else parts[1]
                if self._education_source is None or kind not in {
                    "student_class_membership",
                    "student_grade_placement",
                    "class_placement",
                    "class_teacher_assignment",
                    "class_location",
                    "student_class_position",
                    "student_cohort_membership",
                    "teaching_subject",
                    "teaching_course",
                    "course_subject",
                    "teaching_offering",
                    "offering_member_source",
                    "teaching_group",
                    "teaching_group_member",
                    "teaching_assignment",
                    "teaching_organization_scope",
                }:
                    raise ValueError("Education correction target unavailable")
                source = self._education_source(actor, kind, target_reference)
            else:
                kind, source = self._staff_source(actor, target_reference)
        except (ValueError, PermissionError):
            raise FamilyCorrectionDenied("Select the current formal target again") from None
        value = self._executor.staff_list(
            context=self._context(
                identity, request_id, f"family-correction-staff-list:{request_id}"
            ),
            session_binding=identity.session_binding,
            fact_kind=kind,
            source_key=source,
            field_name=field_name,
            required_action=required_action,
            limit=limit,
        )
        self._check(identity, staff=True)
        items: list[PublicStaffCorrection] = []
        for row in _items(value, limit):
            receipt = _receipt(row)
            if receipt.field_name != field_name or type(row.get("statement")) is not str:
                raise RuntimeError("Correction list projection unavailable")
            items.append(
                PublicStaffCorrection(
                    **receipt.model_dump(),
                    reference=str(receipt.report_id),
                    student_label="当前选中对象",
                    field_label=_LABELS[field_name],
                    statement=cast(str, row["statement"]),
                    required_action=required_action,
                    target_reference=target_reference,
                )
            )
        return PublicStaffCorrectionPage(items=tuple(items))

    def resolve(
        self,
        identity: ProductIdentity,
        request_id: UUID,
        *,
        report_id: UUID,
        writer_receipt_ref: str,
    ) -> PublicCorrectionReceipt:
        self._check(identity, staff=True)
        if re.fullmatch(r"sf2receipt_[0-9a-f]{64}", writer_receipt_ref) is None:
            raise FamilyCorrectionDenied("Existing own formal receipt required")
        value = self._executor.resolve(
            context=self._context(identity, request_id, f"family-correction-resolve:{report_id}"),
            session_binding=identity.session_binding,
            report_id=report_id,
            writer_receipt_ref=writer_receipt_ref.removeprefix("sf2receipt_"),
        )
        self._after_submission(identity, staff=True)
        receipt = _receipt(value.model_dump(mode="json"))
        if receipt.report_id != report_id:
            raise RuntimeError("Correction receipt unavailable")
        return receipt
