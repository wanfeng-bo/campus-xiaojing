from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import TypeVar, cast

from app.domains.students.education_contracts import (
    EDUCATION_REFERENCES,
    education_extra_references,
)
from app.domains.students.family_rules import CurrentFamilyFactActorValidator
from app.platform.contracts import CommandExecutionContext, SchoolId, StaffActorContext
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    CanonicalFactChangeV1,
    ConfirmationReceiptV1,
    PreflightIssueV1,
    PreflightReportV1,
    canonical_plan_sha256,
)
from app.platform.school_facts.ports import (
    PreparedConfirmation,
    SchoolFactApplyResult,
    SchoolFactBulkPreflightReader,
    SchoolFactChangeProvenance,
    SchoolFactOwnOriginalReader,
    SchoolFactOwnOriginalResult,
    SchoolFactOwnResult,
    SchoolFactOwnResultMetadataReader,
    SchoolFactOwnResultPage,
    SchoolFactOwnResultReader,
    SchoolFactOwnResultScope,
    SchoolFactOwnResultTarget,
    SchoolFactOwnTargetPage,
    SchoolFactOwnTargetPageReader,
    SchoolFactOwnTerminal,
    SchoolFactOwnTerminalReader,
    SchoolFactPersistentResultReader,
    SchoolFactPreflightReader,
    SchoolFactRequestContextBinder,
    SchoolFactResidencePreflightReader,
    SchoolFactResultLookup,
    SchoolFactSchoolReactivationPreflightReader,
    SchoolFactTransactionalWriter,
)

_RequestPortT = TypeVar("_RequestPortT")

SCHOOL_FACT_CONFIRMATION_WINDOW = timedelta(minutes=30)
_CREDENTIAL = re.compile(r"^[A-Za-z0-9_-]{40,128}$")
_REFERENCE_FACT_KINDS = {
    "assignment_source_key": "assignment",
    "official_contact_source_key": "official_contact",
    "person_source_key": "person",
    "organization_source_key": "organization",
    "campus_source_key": "location",
    "primary_campus_source_key": "location",
    "administrative_campus_source_key": "location",
    "location_source_key": "location",
    "room_source_key": "location",
    "student_source_key": "student",
    "bed_source_key": "dorm_bed",
    "contact_person_source_key": "person",
    "guardian_person_source_key": "person",
    "review_of_relationship_source_key": "guardian_relationship",
    "publishing_organization_source_key": "organization",
    "policy_source_key": "policy",
    "supersedes_policy_version_source_key": "policy_version",
}


_REFERENCE_FACT_KINDS.update(EDUCATION_REFERENCES)


class SchoolFactChangeRejected(RuntimeError):
    """固定拒绝不可信确认或不满足预检条件的学校事实变更。"""


class SchoolFactChangeService:
    """编排只读预检、凭据签发与唯一事务写端口。"""

    def __init__(
        self,
        *,
        reader: SchoolFactPreflightReader,
        writer: SchoolFactTransactionalWriter,
        clock: Callable[[], datetime] | None = None,
        family_actor_validator: CurrentFamilyFactActorValidator | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._clock = clock or (lambda: datetime.now(UTC))
        self._family_actor_validator = family_actor_validator

    @staticmethod
    def _for_request(
        port: _RequestPortT,
        context: CommandExecutionContext | None,
    ) -> _RequestPortT:
        if context is not None and isinstance(port, SchoolFactRequestContextBinder):
            return cast(_RequestPortT, port.for_request(context))
        return port

    def preflight(
        self,
        *,
        school_id: SchoolId,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext | None = None,
        session_binding: str | None = None,
    ) -> PreflightReportV1:
        self._require_read_context(
            school_id=school_id,
            context=context,
            session_binding=session_binding,
        )
        family_changes = tuple(
            change for change in change_set.changes if change.fact_kind == "guardian_relationship"
        )
        if family_changes:
            if context is None or self._family_actor_validator is None:
                raise SchoolFactChangeRejected("家校关系预检缺少当前正式职责核验。")
            actor = self._require_staff_context(context)
            checked_at = self._aware_now()
            for change in family_changes:
                if change.operation in {"end", "revoke"}:
                    action = change.operation
                else:
                    assert change.content is not None
                    action = (
                        "request" if change.content.status == "pending_verification" else "verify"
                    )
                validation = self._family_actor_validator.validate(
                    actor=actor,
                    action=action,
                    target=str(change.source_key),
                    checked_at=checked_at,
                )
                if (
                    validation.actor_person_id != actor.person_id
                    or validation.actor_assignment_id != actor.assignment_id
                    or validation.action != action
                    or validation.checked_at != checked_at
                ):
                    raise SchoolFactChangeRejected("家校关系职责核验结果与当前操作者不匹配。")
        reader = self._for_request(self._reader, context)
        plan_sha256 = canonical_plan_sha256(change_set)
        if self._is_single_school_reactivation(change_set):
            if not isinstance(reader, SchoolFactSchoolReactivationPreflightReader):
                raise SchoolFactChangeRejected("学校重新启用预检缺少停用学校窄读取能力。")
            return self._preflight_school_reactivation(
                school_id=school_id,
                change_set=change_set,
                plan_sha256=plan_sha256,
                reader=reader,
            )
        if (
            reader.find_applied_plan(
                school_id,
                change_set.source_sha256,
                plan_sha256,
            )
            is not None
        ):
            return PreflightReportV1(
                plan_sha256=plan_sha256,
                is_clean=True,
                issues=(),
            )
        issues: list[PreflightIssueV1] = []
        planned_changes = {
            (change.fact_kind, change.source_key): change for change in change_set.changes
        }
        planned_targets = {
            (change.fact_kind, change.source_key): change
            for change in change_set.changes
            if change.operation in {"upsert", "reactivate", "correct"}
        }
        targets = tuple((change.fact_kind, change.source_key) for change in change_set.changes)
        bulk_reader = reader if isinstance(reader, SchoolFactBulkPreflightReader) else None
        bulk_versions = (
            bulk_reader.find_fact_versions_bulk(school_id, targets)
            if bulk_reader is not None
            else None
        )
        bulk_missing_references = (
            bulk_reader.unresolved_reference_codes_bulk(school_id, change_set.changes)
            if bulk_reader is not None
            else None
        )
        if reader.current_school_fact_version(school_id) != change_set.base_version:
            first = change_set.changes[0].origin
            issues.append(
                PreflightIssueV1(
                    sheet_code=first.sheet_code,
                    row_number=first.row_number,
                    column_code="base_version",
                    error_code="school.version_conflict",
                )
            )
        for change in change_set.changes:
            target = (change.fact_kind, change.source_key)
            projection = (
                bulk_versions.get(target)
                if bulk_versions is not None
                else reader.find_fact_version(
                    school_id,
                    change.fact_kind,
                    change.source_key,
                )
            )
            if projection is None and change.expected_version != 0:
                issues.append(self._issue(change, "source_key", "fact.not_found"))
            elif projection is not None and projection.version != change.expected_version:
                issues.append(self._issue(change, "expected_version", "fact.version_conflict"))
            elif projection is None and change.operation != "upsert":
                issues.append(self._issue(change, "source_key", "fact.not_found"))
            missing_reference_codes = (
                bulk_missing_references.get(target, ())
                if bulk_missing_references is not None
                else reader.unresolved_reference_codes(school_id, change)
            )
            invalid_planned_reference_codes: set[str] = set()
            for code, referenced_kind, referenced_key in self._reference_bindings(change):
                segment = (
                    change.replacement_segments[int(code.split(".")[1])]
                    if code.startswith("replacement_segments.")
                    and change.replacement_segments is not None
                    else None
                )
                if self._planned_reference_is_invalid(
                    planned_changes.get((referenced_kind, referenced_key)),
                    referenced_at=segment.effective_at if segment else change.effective_at,
                    referenced_until=(
                        segment.valid_to
                        if segment
                        else change.content.valid_to or change.content.effective_to
                        if change.content is not None
                        else None
                    ),
                ):
                    invalid_planned_reference_codes.add(code)
                    issues.append(self._issue(change, code, "fact.reference_not_found"))
            for code in missing_reference_codes:
                segment = (
                    change.replacement_segments[int(code.split(".")[1])]
                    if code.startswith("replacement_segments.")
                    and change.replacement_segments is not None
                    else None
                )
                if code in invalid_planned_reference_codes:
                    continue
                referenced_kind = (
                    "organization"
                    if code == "parent_source_key" and change.fact_kind == "organization"
                    else "location"
                    if code == "parent_source_key" and change.fact_kind == "location"
                    else _REFERENCE_FACT_KINDS.get(code)
                )
                referenced_key = getattr(change.content, code, None)
                exact_binding = next(
                    (
                        (kind, key)
                        for field, kind, key in self._reference_bindings(change)
                        if field == code
                    ),
                    None,
                )
                if exact_binding is not None:
                    referenced_kind, referenced_key = exact_binding
                if (
                    referenced_kind is not None
                    and isinstance(referenced_key, str)
                    and self._planned_reference_is_usable(
                        planned_targets.get((referenced_kind, referenced_key)),
                        code.split(".")[-1],
                        referenced_at=segment.effective_at if segment else change.effective_at,
                    )
                ):
                    continue
                issues.append(self._issue(change, code, "fact.reference_not_found"))
        residence_reader = (
            reader if isinstance(reader, SchoolFactResidencePreflightReader) else None
        )
        if residence_reader is not None:
            issues.extend(
                self._residence_transition_issues(
                    school_id=school_id,
                    change_set=change_set,
                    reader=residence_reader,
                )
            )
        elif self._contains_residence_transition(change_set):
            first_upsert = next(
                change
                for change in change_set.changes
                if change.fact_kind == "student_bed_residence" and change.operation == "upsert"
            )
            issues.append(
                self._issue(
                    first_upsert,
                    "source_key",
                    "residence.transition_projection_unavailable",
                )
            )
        education_check = getattr(reader, "education_preflight_codes", None)
        if change_set.schema_version in {3, 4} and not issues and callable(education_check):
            issues.extend(
                self._issue(change_set.changes[0], "content", code)
                for code in education_check(school_id, change_set.changes)
            )
        residence_check = getattr(reader, "residence_preflight_issues", None)
        if change_set.schema_version == 4 and not issues and callable(residence_check):
            found = residence_check(school_id, change_set.changes)
            for change in change_set.changes:
                issues.extend(
                    self._issue(change, "content", code)
                    for code in found.get((change.fact_kind, change.source_key), ())
                )
        ordered = tuple(
            sorted(
                issues,
                key=lambda item: (
                    item.sheet_code,
                    item.row_number,
                    item.column_code,
                    item.error_code,
                ),
            )
        )
        return PreflightReportV1(
            plan_sha256=plan_sha256,
            is_clean=not ordered,
            issues=ordered,
        )

    def _preflight_school_reactivation(
        self,
        *,
        school_id: SchoolId,
        change_set: CanonicalFactChangeSetV1,
        plan_sha256: str,
        reader: SchoolFactSchoolReactivationPreflightReader,
    ) -> PreflightReportV1:
        projection = reader.preflight_school_reactivation(
            school_id,
            change_set,
            plan_sha256,
        )
        if projection.replay_result is not None:
            return PreflightReportV1(
                plan_sha256=plan_sha256,
                is_clean=True,
                issues=(),
            )

        change = change_set.changes[0]
        issues: list[PreflightIssueV1] = []
        if projection.current_fact_version != change_set.base_version:
            issues.append(
                PreflightIssueV1(
                    sheet_code=change.origin.sheet_code,
                    row_number=change.origin.row_number,
                    column_code="base_version",
                    error_code="school.version_conflict",
                )
            )
        if projection.current_target_version is None:
            issues.append(self._issue(change, "source_key", "fact.not_found"))
        elif projection.current_target_version != change.expected_version:
            issues.append(self._issue(change, "expected_version", "fact.version_conflict"))
        ordered = tuple(
            sorted(
                issues,
                key=lambda item: (
                    item.sheet_code,
                    item.row_number,
                    item.column_code,
                    item.error_code,
                ),
            )
        )
        return PreflightReportV1(
            plan_sha256=plan_sha256,
            is_clean=not ordered,
            issues=ordered,
        )

    @staticmethod
    def _is_single_school_reactivation(
        change_set: CanonicalFactChangeSetV1,
    ) -> bool:
        return (
            len(change_set.changes) == 1
            and change_set.changes[0].fact_kind == "school"
            and change_set.changes[0].operation == "reactivate"
        )

    @staticmethod
    def _reference_bindings(
        change: CanonicalFactChangeV1,
    ) -> tuple[tuple[str, str, str], ...]:
        if change.replacement_segments is not None:
            return tuple(
                (f"replacement_segments.{index}." + field, kind, key)
                for index, segment in enumerate(change.replacement_segments)
                for field, kind, key in SchoolFactChangeService._reference_bindings(
                    change.model_copy(
                        update={
                            "replacement_segments": None,
                            "content": segment.content,
                            "effective_at": segment.effective_at,
                        }
                    )
                )
            )
        if change.content is None:
            return ()
        bindings = [
            (field_code, fact_kind, value)
            for field_code, fact_kind in _REFERENCE_FACT_KINDS.items()
            if isinstance((value := getattr(change.content, field_code, None)), str)
        ]
        bindings.extend(education_extra_references(change.fact_kind, change.content))
        parent_source_key = change.content.parent_source_key
        if isinstance(parent_source_key, str):
            parent_kind = "organization" if change.fact_kind == "organization" else "location"
            bindings.append(("parent_source_key", parent_kind, parent_source_key))
        return tuple(bindings)

    @staticmethod
    def _planned_reference_is_invalid(
        planned: CanonicalFactChangeV1 | None,
        *,
        referenced_at: datetime,
        referenced_until: datetime | None,
    ) -> bool:
        if planned is None:
            return False
        if planned.operation == "revoke":
            return True
        if planned.operation != "end":
            return False
        if referenced_at >= planned.effective_at:
            return True
        return referenced_until is None or referenced_until > planned.effective_at

    @staticmethod
    def _planned_reference_is_usable(
        planned: CanonicalFactChangeV1 | None,
        field_code: str,
        *,
        referenced_at: datetime,
    ) -> bool:
        if planned is None or planned.effective_at > referenced_at:
            return False
        if field_code not in {
            "campus_source_key",
            "primary_campus_source_key",
            "administrative_campus_source_key",
        }:
            return True
        return (
            planned.fact_kind == "location"
            and planned.content is not None
            and planned.content.kind == "campus"
        )

    @staticmethod
    def _contains_residence_transition(change_set: CanonicalFactChangeSetV1) -> bool:
        residence_operations = {
            change.operation
            for change in change_set.changes
            if change.fact_kind == "student_bed_residence"
        }
        return "end" in residence_operations and "upsert" in residence_operations

    def prepare_confirmation(
        self,
        *,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_binding: str,
        provenance: SchoolFactChangeProvenance | None = None,
    ) -> ConfirmationReceiptV1:
        actor = self._require_staff_context(context)
        session_sha256 = self._session_sha256(session_binding)
        report = self.preflight(
            school_id=actor.school_id,
            change_set=change_set,
            context=context,
            session_binding=session_binding,
        )
        if not report.is_clean:
            raise SchoolFactChangeRejected("学校事实变更未通过预检。")
        now = self._aware_now()
        expires_at = now + SCHOOL_FACT_CONFIRMATION_WINDOW
        writer = self._for_request(self._writer, context)
        prepared_result = writer.prepare(
            PreparedConfirmation(
                school_id=actor.school_id,
                actor_user_id=actor.account_user_id,
                actor_person_id=actor.person_id,
                actor_assignment_id=actor.assignment_id,
                actor_assignment_version=actor.assignment_version,
                authorization_version=actor.authorization_version,
                request_id=context.request_id,
                idempotency_key_sha256=sha256(context.idempotency_key.encode("utf-8")).hexdigest(),
                plan_sha256=report.plan_sha256,
                source_sha256=change_set.source_sha256,
                base_version=change_set.base_version,
                session_sha256=session_sha256,
                expires_at=expires_at,
                change_set=change_set,
                provenance=(
                    provenance
                    if provenance is not None
                    else SchoolFactChangeProvenance(
                        channel="admin",
                        raw_source_sha256=change_set.source_sha256,
                        template_version=f"canonical_v{change_set.schema_version}",
                        mapping_version=f"manual_v{change_set.schema_version}",
                        mapping_config_sha256=sha256(
                            f"school_facts:manual_v{change_set.schema_version}:canonical-contract".encode(
                                "ascii"
                            )
                        ).hexdigest(),
                        source_size_bytes=None,
                        worksheet_names=(),
                    )
                ),
            )
        )
        if (
            _CREDENTIAL.fullmatch(prepared_result.credential) is None
            or prepared_result.expires_at.tzinfo is None
            or prepared_result.expires_at.utcoffset() is None
            or prepared_result.expires_at <= now
        ):
            raise RuntimeError("学校事实写端口返回了无效确认凭据。")
        return ConfirmationReceiptV1(
            plan_sha256=report.plan_sha256,
            credential=prepared_result.credential,
            expires_at=prepared_result.expires_at,
        )

    def check_result(
        self,
        *,
        school_id: SchoolId,
        change_set: CanonicalFactChangeSetV1,
    ) -> SchoolFactApplyResult | None:
        """只按规范来源与计划摘要查权威终态，绝不重入 confirm。"""

        return self._reader.find_applied_plan(
            school_id,
            change_set.source_sha256,
            canonical_plan_sha256(change_set),
        )

    def check_result_by_digest(
        self,
        *,
        school_id: SchoolId,
        source_sha256: str,
        plan_sha256: str,
        context: CommandExecutionContext | None = None,
        session_binding: str | None = None,
    ) -> SchoolFactApplyResult | None:
        """供最小结果 locator 查询权威终态；不需要、也不恢复完整敏感计划。"""

        self._require_read_context(
            school_id=school_id,
            context=context,
            session_binding=session_binding,
        )
        reader = self._for_request(self._reader, context)
        return reader.find_applied_plan(school_id, source_sha256, plan_sha256)

    def check_result_by_locator(
        self,
        *,
        school_id: SchoolId,
        actor_user_id: int,
        session_binding: str,
        locator_sha256: str,
        context: CommandExecutionContext | None = None,
    ) -> SchoolFactResultLookup:
        """从唯一写核持久元数据恢复结果；不依赖进程内计划。"""

        self._require_read_context(
            school_id=school_id,
            context=context,
            session_binding=session_binding if context is not None else None,
        )
        if context is not None and context.actor.account_user_id != actor_user_id:
            raise SchoolFactChangeRejected("学校事实结果查询身份不匹配。")
        reader = self._for_request(self._reader, context)
        if not isinstance(reader, SchoolFactPersistentResultReader):
            return SchoolFactResultLookup(found=False, result=None)
        return reader.find_result_by_locator(
            school_id,
            actor_user_id,
            self._session_sha256(session_binding),
            locator_sha256,
        )

    def _own_result_reader(
        self, context: CommandExecutionContext, allowed_targets: SchoolFactOwnResultScope
    ) -> tuple[StaffActorContext, SchoolFactOwnResultReader]:
        actor = self._require_staff_context(context)
        if (
            type(allowed_targets) is not frozenset
            or len(allowed_targets) > 4096
            or any(
                type(item) is not tuple
                or len(item) != 2
                or any(type(value) is not str or not value or len(value) > 255 for value in item)
                for item in allowed_targets
            )
        ):
            raise SchoolFactChangeRejected("本人回执缺少有界的当前精确对象范围。")
        reader = self._for_request(self._reader, context)
        if not isinstance(reader, SchoolFactOwnResultReader):
            raise SchoolFactChangeRejected("本人历史回执读取能力不可用。")
        return actor, reader

    def locate_own_result_targets(
        self,
        *,
        context: CommandExecutionContext,
        locator_sha256: str,
    ) -> SchoolFactOwnResultScope | None:
        actor = self._require_staff_context(context)
        if type(locator_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", locator_sha256) is None:
            raise SchoolFactChangeRejected("本人回执定位摘要无效。")
        reader = self._for_request(self._reader, context)
        if not isinstance(reader, SchoolFactOwnResultMetadataReader):
            raise SchoolFactChangeRejected("本人回执定位能力不可用。")
        return reader.find_own_result_targets(
            actor.school_id, actor.account_user_id, actor.person_id, locator_sha256
        )

    def check_own_terminal_by_locator(
        self,
        *,
        context: CommandExecutionContext,
        locator_sha256: str,
    ) -> SchoolFactOwnTerminal | None:
        """Writer-internal owner lookup; its caller must check current receipt duty."""

        actor = self._require_staff_context(context)
        if type(locator_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", locator_sha256) is None:
            raise SchoolFactChangeRejected("本人回执定位摘要无效。")
        reader = self._for_request(self._reader, context)
        if not isinstance(reader, SchoolFactOwnTerminalReader):
            raise SchoolFactChangeRejected("本人回执终态读取能力不可用。")
        return reader.find_own_terminal_by_locator(
            actor.school_id, actor.account_user_id, actor.person_id, locator_sha256
        )

    def read_own_target_page(
        self,
        *,
        context: CommandExecutionContext,
        locator_sha256: str,
        offset: int,
        limit: int,
    ) -> SchoolFactOwnTargetPage | None:
        """Writer-internal coordinates, not an authorized product response."""

        actor = self._require_staff_context(context)
        if type(locator_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", locator_sha256) is None:
            raise SchoolFactChangeRejected("本人回执定位摘要无效。")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise SchoolFactChangeRejected("本人回执明细分页边界无效。")
        reader = self._for_request(self._reader, context)
        if not isinstance(reader, SchoolFactOwnTargetPageReader):
            raise SchoolFactChangeRejected("本人回执明细读取能力不可用。")
        return reader.read_own_target_page(
            actor.school_id, actor.account_user_id, actor.person_id, locator_sha256, offset, limit
        )

    def list_own_results(
        self,
        *,
        context: CommandExecutionContext,
        allowed_targets: SchoolFactOwnResultScope,
        limit: int = 20,
        creation_only_targets: SchoolFactOwnResultScope = frozenset(),
    ) -> SchoolFactOwnResultPage:
        actor, reader = self._own_result_reader(context, allowed_targets)
        self._own_result_reader(context, creation_only_targets)
        if not creation_only_targets <= allowed_targets:
            raise SchoolFactChangeRejected("Creation receipt targets exceed authorized scope.")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise SchoolFactChangeRejected("本人回执目录数量超出固定边界。")
        return reader.list_own_results(
            actor.school_id,
            actor.account_user_id,
            actor.person_id,
            allowed_targets,
            limit,
            **({"creation_only_targets": creation_only_targets} if creation_only_targets else {}),
        )

    def check_own_result_by_locator(
        self,
        *,
        context: CommandExecutionContext,
        allowed_targets: SchoolFactOwnResultScope,
        locator_sha256: str,
        creation_only_targets: SchoolFactOwnResultScope = frozenset(),
    ) -> SchoolFactOwnResult | None:
        actor, reader = self._own_result_reader(context, allowed_targets)
        self._own_result_reader(context, creation_only_targets)
        if not creation_only_targets <= allowed_targets:
            raise SchoolFactChangeRejected("Creation receipt targets exceed authorized scope.")
        if type(locator_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", locator_sha256) is None:
            raise SchoolFactChangeRejected("本人回执定位摘要无效。")
        return reader.find_own_result_by_locator(
            actor.school_id,
            actor.account_user_id,
            actor.person_id,
            locator_sha256,
            allowed_targets,
            **({"creation_only_targets": creation_only_targets} if creation_only_targets else {}),
        )

    def read_own_original_target(
        self,
        *,
        context: CommandExecutionContext,
        result: SchoolFactOwnResult,
        target: SchoolFactOwnResultTarget,
        fields: tuple[str, ...],
    ) -> SchoolFactOwnOriginalResult:
        actor = self._require_staff_context(context)
        if target not in result.targets or target.creation_receipt:
            raise SchoolFactChangeRejected("Original field target is not authorized.")
        reader = self._for_request(self._reader, context)
        if not isinstance(reader, SchoolFactOwnOriginalReader):
            raise SchoolFactChangeRejected("Original receipt projection is unavailable.")
        return reader.read_own_original_target(actor, result, target, fields)

    def confirm(
        self,
        *,
        credential: str,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> SchoolFactApplyResult:
        self._require_staff_context(context)
        if _CREDENTIAL.fullmatch(credential) is None:
            raise SchoolFactChangeRejected("确认凭据无效。")
        writer = self._for_request(self._writer, context)
        return writer.confirm(
            credential_sha256=sha256(credential.encode("ascii")).hexdigest(),
            change_set=change_set,
            context=context,
            session_sha256=self._session_sha256(session_binding),
            now=self._aware_now(),
        )

    @staticmethod
    def _issue(change: object, column_code: str, error_code: str) -> PreflightIssueV1:
        if not isinstance(change, CanonicalFactChangeV1):
            raise TypeError("内部事实变更类型无效。")
        return PreflightIssueV1(
            sheet_code=change.origin.sheet_code,
            row_number=change.origin.row_number,
            column_code=column_code,
            error_code=error_code,
        )

    @classmethod
    def _residence_transition_issues(
        cls,
        *,
        school_id: SchoolId,
        change_set: CanonicalFactChangeSetV1,
        reader: SchoolFactResidencePreflightReader,
    ) -> tuple[PreflightIssueV1, ...]:
        """按学生、边界时刻与床位配对显式切片；零配对仍是入住或退宿。"""

        ends = tuple(
            change
            for change in change_set.changes
            if change.fact_kind == "student_bed_residence" and change.operation == "end"
        )
        upserts = tuple(
            change
            for change in change_set.changes
            if change.fact_kind == "student_bed_residence" and change.operation == "upsert"
        )
        if not ends or not upserts:
            return ()
        projections = reader.find_residence_facts_bulk(
            school_id,
            tuple(change.source_key for change in ends),
        )
        issues = [
            cls._issue(
                change,
                "source_key",
                "residence.transition_projection_missing",
            )
            for change in ends
            if change.source_key not in projections
        ]
        ends_by_boundary: dict[tuple[str, datetime], list[CanonicalFactChangeV1]] = {}
        for change in ends:
            projection = projections.get(change.source_key)
            if projection is not None:
                ends_by_boundary.setdefault(
                    (projection.student_source_key, change.effective_at), []
                ).append(change)
        upserts_by_boundary: dict[tuple[str, datetime], list[CanonicalFactChangeV1]] = {}
        for change in upserts:
            assert change.content is not None
            upserts_by_boundary.setdefault(
                (str(change.content.student_source_key), change.effective_at), []
            ).append(change)

        for boundary in sorted(
            ends_by_boundary.keys() & upserts_by_boundary.keys(),
            key=lambda item: (item[0], item[1]),
        ):
            old_changes = ends_by_boundary[boundary]
            new_changes = upserts_by_boundary[boundary]
            if len(old_changes) == len(new_changes) == 1:
                # 床位相同是连续切片，床位不同是调宿；两者都由同一对显式 unit 表达。
                old_bed = projections[old_changes[0].source_key].bed_source_key
                new_bed = str(new_changes[0].content.bed_source_key)  # type: ignore[union-attr]
                if old_bed == new_bed:
                    continue
                # 异床是调宿；同样保留这两个显式 unit，由单事务确认与两条 lineage 落账。
                continue
            old_beds = {projections[change.source_key].bed_source_key for change in old_changes}
            new_beds = {
                str(change.content.bed_source_key)
                for change in new_changes
                if change.content is not None
            }
            transition_kind = "slice" if old_beds & new_beds else "transfer"
            for change in (*old_changes, *new_changes):
                issues.append(
                    cls._issue(
                        change,
                        "source_key",
                        f"residence.{transition_kind}_ambiguous",
                    )
                )
        return tuple(issues)

    def _require_read_context(
        self,
        *,
        school_id: SchoolId,
        context: CommandExecutionContext | None,
        session_binding: str | None,
    ) -> None:
        if context is None and session_binding is None:
            return
        if context is None or session_binding is None:
            raise SchoolFactChangeRejected("学校事实只读操作缺少可信执行上下文。")
        actor = self._require_staff_context(context)
        if actor.school_id != school_id:
            raise SchoolFactChangeRejected("学校事实只读操作学校上下文不匹配。")
        self._session_sha256(session_binding)

    @staticmethod
    def _require_staff_context(context: CommandExecutionContext) -> StaffActorContext:
        if (
            type(context) is not CommandExecutionContext
            or type(context.actor) is not StaffActorContext
        ):
            raise SchoolFactChangeRejected("学校事实正式变更只接受服务端签发的教职工上下文。")
        return context.actor

    @staticmethod
    def _session_sha256(session_binding: str) -> str:
        if not isinstance(session_binding, str):
            raise SchoolFactChangeRejected("会话绑定无效。")
        normalized = session_binding.strip()
        if normalized != session_binding or not 2 <= len(normalized) <= 256:
            raise SchoolFactChangeRejected("会话绑定无效。")
        return sha256(normalized.encode("utf-8")).hexdigest()

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("学校事实写核时钟必须携带时区。")
        return now
