# ruff: noqa: E501
# pyright: strict
"""Writer-side correction repository and transaction kernel."""

from __future__ import annotations

import base64
import hmac
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

from pydantic import Field
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from app.composition.school_fact_education_authority import EducationAuthority
from app.composition.school_fact_family_authority import (
    FamilyAuthorityComponents,
)
from app.composition.school_fact_family_correction_executor import (
    CorrectionOption,
    CorrectionReviewPreparation,
    FamilyCorrectionExecutorKernel,
)
from app.composition.school_fact_writer_executor import SqlAlchemySchoolFactWriterExecutorKernel
from app.domains.students.family_corrections import (
    FamilyCorrectionDenied,
    FamilyCorrectionReport,
    FamilyCorrectionResolutionTarget,
    FamilyCorrectionService,
    GuardianCorrectionPermit,
)
from app.platform.contracts import (
    CommandExecutionContext,
    FrozenContractModel,
    GuardianActorContext,
    StaffActorContext,
)
from app.platform.school_facts.family_leadership_policy import FamilyLeadershipPolicy

F2_CORRECTION_FIELDS = {
    ("person", "display_name"),
    ("assignment", "position_title"),
    ("assignment", "person_source_key"),
    ("assignment", "organization_source_key"),
    ("official_contact", "contact_value"),
    ("official_contact", "contact_kind"),
    ("official_contact", "service_hours"),
}

F6_CORRECTION_FIELDS = {
    ("student_bed_residence", "bed_source_key"),
    ("student_bed_residence", "effective_at"),
    ("student_bed_residence", "valid_to"),
    ("dorm_bed", "display_name"),
    ("dorm_bed", "room_source_key"),
    ("dorm_room", "room_source_key"),
    ("dormitory_building_profile", "display_name"),
    ("dormitory_building_profile", "location_source_key"),
}

F5_CORRECTION_FIELDS = {
    (kind, field)
    for kind, fields in {
        "teaching_subject": ("display_name", "stage_source_keys"),
        "teaching_course": ("display_name", "stage_source_keys", "grade_source_keys"),
        "course_subject": ("course_source_key", "subject_source_key"),
        "teaching_offering": (
            "course_source_key",
            "offering_status",
            "display_name",
            "campus_source_key",
            "term_source_key",
        ),
        "offering_member_source": (
            "offering_source_key",
            "source_kind",
            "class_source_key",
            "group_source_key",
        ),
        "teaching_group": ("organization_source_key", "group_mode"),
        "teaching_group_member": (
            "group_source_key",
            "student_source_key",
            "school_relationship_source_key",
            "valid_to",
        ),
        "teaching_assignment": (
            "offering_source_key",
            "assignment_source_key",
            "teaching_role",
            "subject_source_keys",
            "duty_scope",
        ),
        "teaching_organization_scope": (
            "organization_source_key",
            "subject_source_key",
            "scope_basis",
            "stage_source_key",
            "grade_source_key",
            "grade_group_source_key",
            "term_source_key",
        ),
    }.items()
    for field in fields
}

_EDUCATION_CORRECTION_FIELDS = {
    ("student_class_membership", "class_source_key"),
    ("student_grade_placement", "grade_source_key"),
    ("class_placement", "grade_source_key"),
    ("class_teacher_assignment", "assignment_source_key"),
    ("class_location", "location_source_key"),
    ("student_class_position", "position_source_key"),
    ("student_cohort_membership", "cohort_source_key"),
} | F5_CORRECTION_FIELDS

_EDUCATION_CORRECTION_LABELS = {
    "class_teacher_assignment": "班主任任职",
    "class_placement": "班级年级安排",
    "student_grade_placement": "孩子个人年级安排",
    "student_class_membership": "孩子所在班级",
    "class_location": "班级教室",
    "student_cohort_membership": "孩子届别归属",
    "student_class_position": "孩子班内职务",
}

_COLUMNS = """id, school_id, reporter_person_id, guardian_relationship_id, student_id,
fact_kind, fact_source_key, field_name, fact_target_version, statement, status, submitted_at,
resolution_locator_sha256, resolved_at, review_explanation"""
_RECEIPT_COLUMNS = _COLUMNS.replace("statement, ", "")


def _sha(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _request_sha(permit: GuardianCorrectionPermit, statement: str) -> str:
    value = json.dumps(
        [
            str(permit.relationship_id),
            str(permit.student_id),
            permit.fact_kind,
            permit.source_key,
            permit.field_name,
            statement,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _sha(value)


def _row(m: RowMapping) -> FamilyCorrectionReport:
    return FamilyCorrectionReport(
        cast(UUID, m["id"]),
        cast(UUID, m["school_id"]),
        cast(UUID, m["reporter_person_id"]),
        cast(UUID, m["guardian_relationship_id"]),
        cast(UUID, m["student_id"]),
        cast(str, m["fact_kind"]),
        cast(str, m["fact_source_key"]),
        cast(str, m["field_name"]),
        cast(str, m.get("statement", "")),
        cast(Literal["submitted", "resolved", "reviewed"], m["status"]),
        cast(datetime, m["submitted_at"]),
        cast(str | None, m["resolution_locator_sha256"]),
        cast(datetime | None, m["resolved_at"]),
        cast(int, m["fact_target_version"]),
        cast(str | None, m.get("review_explanation")),
    )


class FamilyCorrectionReferenceCodec:
    def __init__(self, key: bytes) -> None:
        if type(key) is not bytes or len(key) < 32:
            raise ValueError("Correction reference key is too short.")
        self._key = hmac.digest(key, b"family-correction-reference-v1", "sha256")

    def encode(
        self,
        *,
        actor: GuardianActorContext,
        fact_kind: str,
        source_key: str,
        field_name: str,
        target_version: int,
    ) -> str:
        payload = json.dumps(
            [
                str(actor.school_id),
                str(actor.person_id),
                str(actor.guardian_relationship_id),
                actor.relationship_version,
                fact_kind,
                source_key,
                field_name,
                target_version,
            ],
            separators=(",", ":"),
        ).encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        signature = hmac.new(self._key, encoded.encode(), sha256).hexdigest()
        return f"fc1.{encoded}.{signature}"

    def decode(self, *, actor: GuardianActorContext, reference: str) -> tuple[str, str, str, int]:
        try:
            prefix, encoded, signature = reference.split(".")
            if prefix != "fc1" or not hmac.compare_digest(
                signature, hmac.new(self._key, encoded.encode(), sha256).hexdigest()
            ):
                raise ValueError
            raw: object = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            payload = cast(list[object], raw) if type(raw) is list else None
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise FamilyCorrectionDenied("Correction target is invalid.") from exc
        if (
            payload is None
            or len(payload) != 8
            or payload[:4]
            != [
                str(actor.school_id),
                str(actor.person_id),
                str(actor.guardian_relationship_id),
                actor.relationship_version,
            ]
            or any(type(value) is not str for value in payload[4:7])
            or type(payload[7]) is not int
            or payload[7] < 1
        ):
            raise FamilyCorrectionDenied("Correction target is no longer current.")
        return (
            cast(str, payload[4]),
            cast(str, payload[5]),
            cast(str, payload[6]),
            payload[7],
        )


class GuardianCorrectionOption(FrozenContractModel):
    fact_kind: str | None = Field(default=None, min_length=1, max_length=64)
    field_name: str
    label: str
    reference: str


class CorrectionReviewCodec:
    def __init__(self, key: bytes):
        if len(key) < 32:
            raise ValueError("Review confirmation key is too short")
        self._key = hmac.digest(key, b"family-correction-review-v1", "sha256")

    @staticmethod
    def _binding(actor: StaffActorContext, session_binding: str):
        return [
            str(actor.school_id),
            str(actor.person_id),
            str(actor.assignment_id),
            actor.assignment_version,
            actor.authorization_version,
            _sha(session_binding),
        ]

    def encode(
        self,
        *,
        actor: StaffActorContext,
        session_binding: str,
        report_id: UUID,
        target_version: int,
        explanation: str,
        action: str,
        expires_at: datetime,
    ) -> str:
        value = {
            "binding": self._binding(actor, session_binding),
            "report_id": str(report_id),
            "target_version": target_version,
            "explanation": explanation,
            "action": action,
            "expires_at": expires_at.isoformat(),
        }
        encoded = (
            base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
            .decode()
            .rstrip("=")
        )
        return "fr1." + encoded + "." + hmac.new(self._key, encoded.encode(), sha256).hexdigest()

    def decode(
        self,
        *,
        actor: StaffActorContext,
        session_binding: str,
        token: str,
        now: datetime,
        for_result: bool = False,
    ) -> dict:
        try:
            if len(token) > 8192:
                raise ValueError
            prefix, encoded, signature = token.split(".")
            if prefix != "fr1" or not hmac.compare_digest(
                signature, hmac.new(self._key, encoded.encode(), sha256).hexdigest()
            ):
                raise ValueError
            value = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            if value["binding"] != self._binding(actor, session_binding) or (
                not for_result and datetime.fromisoformat(value["expires_at"]) <= now
            ):
                raise ValueError
            return value
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            raise FamilyCorrectionDenied("Review confirmation changed or expired.") from None


class SqlGuardianCorrectionAuthority:
    def __init__(
        self,
        *,
        session: Session,
        codec: FamilyCorrectionReferenceCodec,
        leadership_policy: Callable[[], FamilyLeadershipPolicy] | None = None,
        f5_enabled: bool = False,
        f6_enabled: bool = False,
    ) -> None:
        self._session, self._codec = session, codec
        self._leadership_policy, self._f5_enabled = leadership_policy, f5_enabled
        self._f6_enabled = f6_enabled

    def _extra_rows(self, actor: GuardianActorContext) -> list[RowMapping]:
        params: dict[str, object] = {
            "school": actor.school_id,
            "adult": actor.person_id,
            "relationship": actor.guardian_relationship_id,
            "relationship_version": actor.relationship_version,
        }
        rows: list[RowMapping] = []
        if self._f6_enabled:
            rows.extend(
                self._session.execute(
                    text("""SELECT field_name,label,fact_kind,fact_source_key,target_version,student_id
                FROM app.school_fact_family_residence_targets(:school,:adult,:relationship,:relationship_version)"""),
                    params,
                )
                .mappings()
                .all()
            )
        if self._f5_enabled:
            rows.extend(
                self._session.execute(
                    text("""SELECT field_name,label,fact_kind,fact_source_key,target_version,student_id
                FROM app.school_fact_family_teaching_targets(:school,:adult,:relationship,:relationship_version)"""),
                    params,
                )
                .mappings()
                .all()
            )
        if self._leadership_policy is not None:
            policy = self._leadership_policy()
            if policy.school_id != actor.school_id:
                raise FamilyCorrectionDenied("Leadership configuration belongs to another school.")
            school_codes, campus_codes = policy.codes("all")
            rows.extend(
                self._session.execute(
                    text("""SELECT field_name,label,fact_kind,fact_source_key,target_version,student_id
                FROM app.school_fact_family_leadership_targets(:school,:adult,:relationship,:relationship_version,
                CAST(:school_codes AS text[]),CAST(:campus_codes AS text[]),true)"""),
                    params
                    | {"school_codes": list(school_codes), "campus_codes": list(campus_codes)},
                )
                .mappings()
                .all()
            )
        if any(
            (row["fact_kind"], row["field_name"])
            not in F5_CORRECTION_FIELDS | F2_CORRECTION_FIELDS | F6_CORRECTION_FIELDS
            for row in rows
        ):
            raise FamilyCorrectionDenied("Overbroad correction target projection.")
        return rows

    def options(self, actor: GuardianActorContext) -> tuple[GuardianCorrectionOption, ...]:
        rows = (
            self._session.execute(
                text("""
            SELECT field_name,label,fact_kind,fact_source_key,target_version,student_id
            FROM app.school_fact_family_correction_targets(
              :school,:adult,:relationship,:relationship_version)
            ORDER BY field_name,fact_kind,fact_source_key
        """),
                {
                    "school": actor.school_id,
                    "adult": actor.person_id,
                    "relationship": actor.guardian_relationship_id,
                    "relationship_version": actor.relationship_version,
                },
            )
            .mappings()
            .all()
        )
        rows = list(rows) + self._extra_rows(actor)
        if len(rows) > 100:
            raise FamilyCorrectionDenied(
                "Correction targets exceed the finite bound; narrow the target."
            )
        return tuple(
            GuardianCorrectionOption(
                fact_kind=cast(str, row["fact_kind"]),
                field_name=cast(str, row["field_name"]),
                label=_EDUCATION_CORRECTION_LABELS.get(
                    cast(str, row["fact_kind"]), cast(str, row["label"])
                ),
                reference=self._codec.encode(
                    actor=actor,
                    fact_kind=cast(str, row["fact_kind"]),
                    source_key=cast(str, row["fact_source_key"]),
                    field_name=cast(str, row["field_name"]),
                    target_version=cast(int, row["target_version"]),
                ),
            )
            for row in rows
        )

    def require_submit(
        self,
        *,
        actor: GuardianActorContext,
        target_reference: str,
        field_name: str,
        checked_at: datetime,
    ) -> GuardianCorrectionPermit:
        kind, source, signed_field, version = self._codec.decode(
            actor=actor, reference=target_reference
        )
        if signed_field != field_name:
            raise FamilyCorrectionDenied("Correction field does not match its target.")
        current = {item.reference: item for item in self.options(actor)}
        if target_reference not in current:
            raise FamilyCorrectionDenied("Correction target is no longer current.")
        if (kind, field_name) in F2_CORRECTION_FIELDS | F5_CORRECTION_FIELDS | F6_CORRECTION_FIELDS:
            matching = [
                r
                for r in self._extra_rows(actor)
                if (r["fact_kind"], r["fact_source_key"], r["field_name"], r["target_version"])
                == (kind, source, field_name, version)
            ]
            if len(matching) != 1:
                raise FamilyCorrectionDenied("Correction target is no longer current.")
            return GuardianCorrectionPermit(
                actor=actor,
                student_id=cast(UUID, matching[0]["student_id"]),
                relationship_id=actor.guardian_relationship_id,
                fact_kind=kind,
                source_key=source,
                field_name=field_name,
                checked_at=checked_at,
                target_version=version,
            )
        row = (
            self._session.execute(
                text("""
            SELECT student_id FROM app.school_fact_family_correction_targets(
              :school,:adult,:relationship,:relationship_version)
            WHERE fact_kind=:kind AND fact_source_key=:source
              AND field_name=:field AND target_version=:version
        """),
                {
                    "school": actor.school_id,
                    "adult": actor.person_id,
                    "relationship": actor.guardian_relationship_id,
                    "relationship_version": actor.relationship_version,
                    "kind": kind,
                    "source": source,
                    "field": field_name,
                    "version": version,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise FamilyCorrectionDenied("Correction target is no longer current.")
        return GuardianCorrectionPermit(
            actor=actor,
            student_id=cast(UUID, row["student_id"]),
            relationship_id=actor.guardian_relationship_id,
            fact_kind=kind,
            source_key=source,
            field_name=field_name,
            checked_at=checked_at,
            target_version=version,
        )

    def require_current(self, actor: GuardianActorContext, *, checked_at: datetime) -> None:
        del checked_at
        if not self.options(actor):
            raise FamilyCorrectionDenied("Guardian relationship is no longer current.")

    def revalidate(self, permit: GuardianCorrectionPermit, *, checked_at: datetime) -> None:
        del checked_at
        expected = (permit.fact_kind, permit.source_key, permit.field_name, permit.target_version)
        if not any(
            self._codec.decode(actor=permit.actor, reference=item.reference) == expected
            for item in self.options(permit.actor)
            if item.field_name == permit.field_name
        ):
            raise FamilyCorrectionDenied("Correction authority changed.")


class SqlFamilyCorrectionRepository:
    """Repository bound to one already authenticated Writer transaction."""

    def __init__(self, session: Session, *, review_enabled: bool = True) -> None:
        self._session = session
        self._columns = (
            _COLUMNS
            if review_enabled
            else _COLUMNS.replace("review_explanation", "NULL::text AS review_explanation")
        )
        self._receipt_columns = self._columns.replace("statement, ", "")

    def current_target_version(self, *, school_id: UUID, fact_kind: str, source_key: str) -> int:
        # The canonical fact Writer takes the same school lock before applying facts.
        self._session.execute(
            text("SELECT id FROM schools WHERE id=:school FOR UPDATE"), {"school": school_id}
        ).scalar_one()
        return int(
            self._session.scalar(
                text("""SELECT COALESCE(max(l.target_version),0)
            FROM school_fact_change_lineage l JOIN master_data_import_batches b
            ON b.school_id=l.school_id AND b.id=l.batch_id AND b.status='applied'
            WHERE l.school_id=:school AND l.fact_kind=:kind AND l.fact_source_key=:source"""),
                {"school": school_id, "kind": fact_kind, "source": source_key},
            )
            or 0
        )

    def review_result(
        self, *, actor: StaffActorContext, report_id: UUID, command_sha256: str
    ) -> FamilyCorrectionReport | None:
        row = (
            self._session.execute(
                text(
                    f"SELECT {self._columns} FROM family_fact_correction_reports WHERE school_id=:school AND id=:id AND reviewer_person_id=:person AND reviewer_assignment_id=:assignment AND review_command_sha256=:command AND status='reviewed'"
                ),
                {
                    "school": actor.school_id,
                    "id": report_id,
                    "person": actor.person_id,
                    "assignment": actor.assignment_id,
                    "command": command_sha256,
                },
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else _row(row)

    def review_unchanged(
        self,
        *,
        actor: StaffActorContext,
        report_id: UUID,
        explanation: str,
        target_version: int,
        command_sha256: str,
        reviewed_at: datetime,
    ) -> FamilyCorrectionReport:
        existing = self.review_result(
            actor=actor, report_id=report_id, command_sha256=command_sha256
        )
        if existing is not None:
            return existing
        row = (
            self._session.execute(
                text(f"""UPDATE family_fact_correction_reports SET status='reviewed',
            resolved_at=:at,review_explanation=:explanation,reviewer_person_id=:person,
            reviewer_assignment_id=:assignment,reviewer_assignment_version=:assignment_version,
            reviewer_authorization_version=:authorization_version,review_command_sha256=:command,review_target_version=:version
            WHERE school_id=:school AND id=:id AND status='submitted' RETURNING {self._columns}"""),
                {
                    "school": actor.school_id,
                    "id": report_id,
                    "at": reviewed_at,
                    "explanation": explanation,
                    "person": actor.person_id,
                    "assignment": actor.assignment_id,
                    "assignment_version": actor.assignment_version,
                    "authorization_version": actor.authorization_version,
                    "command": command_sha256,
                    "version": target_version,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise FamilyCorrectionDenied("Review report has already been settled.")
        return _row(row)

    def submit(
        self, *, permit: GuardianCorrectionPermit, statement: str, idempotency_key: str
    ) -> FamilyCorrectionReport:
        key, request = _sha(idempotency_key), _request_sha(permit, statement)
        existing = (
            self._session.execute(
                text(
                    f"SELECT {self._columns}, request_sha256 FROM family_fact_correction_reports WHERE school_id=:school AND reporter_person_id=:person AND idempotency_key_sha256=:key FOR UPDATE"
                ),
                {"school": permit.actor.school_id, "person": permit.actor.person_id, "key": key},
            )
            .mappings()
            .first()
        )
        if existing is not None:
            if existing["request_sha256"] != request:
                raise FamilyCorrectionDenied("Idempotency key conflict.")
            return _row(existing)
        result = (
            self._session.execute(
                text(
                    f"INSERT INTO family_fact_correction_reports (id,school_id,reporter_person_id,guardian_relationship_id,student_id,fact_kind,fact_source_key,field_name,fact_target_version,statement,idempotency_key_sha256,request_sha256,status,submitted_at) VALUES (:id,:school,:person,:relationship,:student,:kind,:source,:field,:target_version,:statement,:key,:request,'submitted',:at) RETURNING {self._columns}"
                ),
                {
                    "id": uuid4(),
                    "school": permit.actor.school_id,
                    "person": permit.actor.person_id,
                    "relationship": permit.relationship_id,
                    "student": permit.student_id,
                    "kind": permit.fact_kind,
                    "source": permit.source_key,
                    "field": permit.field_name,
                    "target_version": permit.target_version,
                    "statement": statement,
                    "key": key,
                    "request": request,
                    "at": permit.checked_at,
                },
            )
            .mappings()
            .one()
        )
        return _row(result)

    def own(self, *, actor: GuardianActorContext, report_id: UUID) -> FamilyCorrectionReport | None:
        value = (
            self._session.execute(
                text(
                    f"SELECT {self._receipt_columns} FROM family_fact_correction_reports WHERE school_id=:school AND reporter_person_id=:person AND guardian_relationship_id=:relationship AND id=:id"
                ),
                {
                    "school": actor.school_id,
                    "person": actor.person_id,
                    "relationship": actor.guardian_relationship_id,
                    "id": report_id,
                },
            )
            .mappings()
            .first()
        )
        return None if value is None else _row(value)

    def own_by_key(
        self, *, actor: GuardianActorContext, idempotency_key: str
    ) -> FamilyCorrectionReport | None:
        value = (
            self._session.execute(
                text(
                    f"SELECT {self._receipt_columns} FROM family_fact_correction_reports "
                    "WHERE school_id=:school AND reporter_person_id=:person "
                    "AND guardian_relationship_id=:relationship "
                    "AND idempotency_key_sha256=:key"
                ),
                {
                    "school": actor.school_id,
                    "person": actor.person_id,
                    "relationship": actor.guardian_relationship_id,
                    "key": _sha(idempotency_key),
                },
            )
            .mappings()
            .first()
        )
        return None if value is None else _row(value)

    def list_own(
        self, *, actor: GuardianActorContext, limit: int
    ) -> tuple[FamilyCorrectionReport, ...]:
        values = (
            self._session.execute(
                text(
                    f"SELECT {self._receipt_columns} FROM family_fact_correction_reports "
                    "WHERE school_id=:school AND reporter_person_id=:person "
                    "AND guardian_relationship_id=:relationship "
                    "ORDER BY submitted_at DESC,id DESC LIMIT :limit"
                ),
                {
                    "school": actor.school_id,
                    "person": actor.person_id,
                    "relationship": actor.guardian_relationship_id,
                    "limit": limit,
                },
            )
            .mappings()
            .all()
        )
        return tuple(_row(value) for value in values)

    def get_target(
        self, *, school_id: UUID, report_id: UUID
    ) -> FamilyCorrectionResolutionTarget | None:
        value = (
            self._session.execute(
                text(
                    "SELECT id,school_id,fact_kind,fact_source_key,field_name,status,"
                    "submitted_at,fact_target_version FROM family_fact_correction_reports "
                    "WHERE school_id=:school AND id=:id FOR UPDATE"
                ),
                {"school": school_id, "id": report_id},
            )
            .mappings()
            .first()
        )
        if value is None:
            return None
        return FamilyCorrectionResolutionTarget(
            cast(UUID, value["id"]),
            cast(UUID, value["school_id"]),
            cast(str, value["fact_kind"]),
            cast(str, value["fact_source_key"]),
            cast(str, value["field_name"]),
            cast(Literal["submitted", "resolved", "reviewed"], value["status"]),
            cast(datetime, value["submitted_at"]),
            cast(int, value["fact_target_version"]),
        )

    def list_target(
        self, *, school_id: UUID, fact_kind: str, source_key: str, field_name: str, limit: int
    ) -> tuple[FamilyCorrectionReport, ...]:
        values = (
            self._session.execute(
                text(
                    f"SELECT {self._columns} FROM family_fact_correction_reports WHERE school_id=:school AND fact_kind=:kind AND fact_source_key=:source AND field_name=:field AND status='submitted' ORDER BY submitted_at,id LIMIT :limit"
                ),
                {
                    "school": school_id,
                    "kind": fact_kind,
                    "source": source_key,
                    "field": field_name,
                    "limit": limit,
                },
            )
            .mappings()
            .all()
        )
        return tuple(_row(value) for value in values)

    def resolve(
        self, *, report_id: UUID, locator_sha256: str, resolved_at: datetime
    ) -> FamilyCorrectionReport:
        value = (
            self._session.execute(
                text(
                    f"UPDATE family_fact_correction_reports SET status='resolved',resolution_locator_sha256=:locator,resolved_at=:at WHERE id=:id AND status='submitted' RETURNING {self._columns}"
                ),
                {"locator": locator_sha256, "at": resolved_at, "id": report_id},
            )
            .mappings()
            .first()
        )
        if value is None:
            raise FamilyCorrectionDenied("Correction report changed.")
        return _row(value)


class CurrentOwnReceiptReader(Protocol):
    def check(self, *, locator_sha256: str) -> object | None: ...


class AppliedFieldDifferenceReader(Protocol):
    def changed(
        self, *, fact_kind: str, source_key: str, target_version: int, field_name: str
    ) -> bool: ...


class SqlAppliedFieldDifferenceReader:
    def __init__(self, *, session: Session, school_id: UUID) -> None:
        self._session, self._school_id = session, school_id

    def changed(
        self, *, fact_kind: str, source_key: str, target_version: int, field_name: str
    ) -> bool:
        function = (
            "school_fact_f6_field_changed"
            if (fact_kind, field_name) in F6_CORRECTION_FIELDS
            else "school_fact_f2_family_field_changed"
            if (fact_kind, field_name) in F2_CORRECTION_FIELDS
            else "school_fact_f5_field_changed"
            if (fact_kind, field_name) in F5_CORRECTION_FIELDS
            else "school_fact_f3_field_changed"
        )
        value = self._session.scalar(
            text(f"SELECT app.{function}( :school,:kind,:source,:version,:field)"),
            {
                "school": self._school_id,
                "kind": fact_kind,
                "source": source_key,
                "version": target_version,
                "field": field_name,
            },
        )
        return value is True


class ExactCorrectionReceiptVerifier:
    """Accepts only this actor's current receipt and a proven target-version difference."""

    def __init__(
        self, *, own_results: CurrentOwnReceiptReader, differences: AppliedFieldDifferenceReader
    ) -> None:
        self._own_results = own_results
        self._differences = differences

    def require_applied_target(
        self,
        *,
        actor: StaffActorContext,
        locator_sha256: str,
        fact_kind: str,
        source_key: str,
        field_name: str,
        minimum_target_version: int,
        submitted_at: datetime,
    ) -> Literal["maintain", "end", "revoke"]:
        del actor  # ownership and current actor scope are enforced by own_results.
        receipt = self._own_results.check(locator_sha256=locator_sha256)
        if receipt is None or getattr(receipt, "confirmed_at", submitted_at) < submitted_at:
            raise FamilyCorrectionDenied("Formal receipt predates the correction report.")
        targets = getattr(receipt, "targets", ())
        matching = tuple(
            target
            for target in targets
            if target.fact_kind == fact_kind and target.source_key == source_key
        )
        if len(matching) != 1:
            raise FamilyCorrectionDenied("Formal receipt does not contain the correction target.")
        target = matching[0]
        if target.target_version <= minimum_target_version:
            raise FamilyCorrectionDenied("Formal receipt predates the reported target version.")
        if field_name == "relationship":
            if target.operation not in {"end", "revoke"}:
                raise FamilyCorrectionDenied(
                    "Formal receipt did not end the reported relationship."
                )
            return cast(Literal["end", "revoke"], target.operation)
        operations = (
            {"upsert", "correct", "end", "revoke"}
            if (fact_kind, field_name) in F6_CORRECTION_FIELDS
            else {"upsert", "correct", "cancel", "end"}
            if (fact_kind, field_name) in _EDUCATION_CORRECTION_FIELDS
            else {"upsert"}
        )
        if target.operation not in operations or not self._differences.changed(
            fact_kind=fact_kind,
            source_key=source_key,
            target_version=target.target_version,
            field_name=field_name,
        ):
            raise FamilyCorrectionDenied("Formal receipt did not change the reported field.")
        return "maintain"


class CorrectionWriterTransaction(Protocol):
    def __call__(
        self, context: CommandExecutionContext, session_binding: str
    ) -> AbstractContextManager[Session]: ...


class GuardianCorrectionAuthorityFactory(Protocol):
    def __call__(self, session: Session) -> SqlGuardianCorrectionAuthority: ...


class CorrectionServiceFactory(Protocol):
    def __call__(
        self,
        repository: SqlFamilyCorrectionRepository,
        session: Session,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> FamilyCorrectionService: ...


class SqlFamilyCorrectionExecutorKernel(FamilyCorrectionExecutorKernel):
    def __init__(
        self,
        *,
        transaction: CorrectionWriterTransaction,
        services: CorrectionServiceFactory,
        guardian_authority: GuardianCorrectionAuthorityFactory,
        review_codec: CorrectionReviewCodec | None = None,
        review_enabled: bool = True,
        review_commands_enabled: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._transaction, self._services = transaction, services
        self._guardian_authority = guardian_authority
        self._review_codec, self._clock = review_codec if review_enabled else None, clock
        self._review_enabled = review_enabled
        self._review_commands_enabled = review_commands_enabled

    def prepare_review(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        report_id: UUID,
        explanation: str,
        required_action: Literal["maintain", "end", "revoke"],
    ) -> CorrectionReviewPreparation:
        if not self._review_commands_enabled:
            raise FamilyCorrectionDenied(
                "New review commands are disabled; existing results remain available."
            )
        if type(context.actor) is not StaffActorContext or self._review_codec is None:
            raise FamilyCorrectionDenied("Current Staff review context required.")
        with self._transaction(context, session_binding) as session:
            service = self._services(
                SqlFamilyCorrectionRepository(session, review_enabled=self._review_enabled),
                session,
                context,
                session_binding,
            )
            report, version, clean = service.prepare_review(
                actor=context.actor,
                report_id=report_id,
                explanation=explanation,
                action=required_action,
            )
            if report.status != "submitted":
                raise FamilyCorrectionDenied("Review already settled; read its original result.")
        expires = self._clock() + timedelta(minutes=5)
        token = self._review_codec.encode(
            actor=context.actor,
            session_binding=session_binding,
            report_id=report_id,
            target_version=version,
            explanation=clean,
            action=required_action,
            expires_at=expires,
        )
        return CorrectionReviewPreparation(
            report_id=report_id,
            field_name=report.field_name,
            explanation=clean,
            target_version=version,
            expires_at=expires,
            confirmation_token=token,
        )

    def _review_payload(
        self,
        context: CommandExecutionContext,
        binding: str,
        report_id: UUID,
        token: str,
        *,
        for_result: bool = False,
    ) -> dict:
        if type(context.actor) is not StaffActorContext or self._review_codec is None:
            raise FamilyCorrectionDenied("Current Staff review context required.")
        value = self._review_codec.decode(
            actor=context.actor,
            session_binding=binding,
            token=token,
            now=self._clock(),
            for_result=for_result,
        )
        if value["report_id"] != str(report_id):
            raise FamilyCorrectionDenied("Review report does not match confirmation.")
        return value

    def confirm_review(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        report_id: UUID,
        confirmation_token: str,
        confirmed: bool,
    ) -> FamilyCorrectionReport:
        if not self._review_commands_enabled:
            raise FamilyCorrectionDenied(
                "New review commands are disabled; recover an existing original result."
            )
        value = self._review_payload(context, session_binding, report_id, confirmation_token)
        if type(context.actor) is not StaffActorContext:
            raise FamilyCorrectionDenied("Current Staff review context required.")
        actor = context.actor
        return self._run(
            context,
            session_binding,
            lambda service: service.review_unchanged(
                actor=actor,
                report_id=report_id,
                explanation=value["explanation"],
                expected_target_version=value["target_version"],
                command_sha256=_sha(confirmation_token),
                confirmed=confirmed,
                action=value["action"],
            ),
        )

    def review_result(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        report_id: UUID,
        confirmation_token: str,
    ) -> FamilyCorrectionReport:
        value = self._review_payload(
            context, session_binding, report_id, confirmation_token, for_result=True
        )
        if type(context.actor) is not StaffActorContext:
            raise FamilyCorrectionDenied("Current Staff review context required.")
        actor = context.actor
        return self._run(
            context,
            session_binding,
            lambda service: service.review_result(
                actor=actor,
                report_id=report_id,
                command_sha256=_sha(confirmation_token),
                action=value["action"],
            ),
        )

    def _run(
        self,
        context: CommandExecutionContext,
        session_binding: str,
        call: Callable[[FamilyCorrectionService], FamilyCorrectionReport],
    ) -> FamilyCorrectionReport:
        with self._transaction(context, session_binding) as session:
            return call(
                self._services(
                    SqlFamilyCorrectionRepository(session, review_enabled=self._review_enabled),
                    session,
                    context,
                    session_binding,
                )
            )

    def options(
        self, *, context: CommandExecutionContext, session_binding: str
    ) -> tuple[CorrectionOption, ...]:
        if type(context.actor) is not GuardianActorContext:
            raise FamilyCorrectionDenied("Guardian context required.")
        with self._transaction(context, session_binding) as session:
            authority = self._guardian_authority(session)
            before = authority.options(context.actor)
            authority.require_current(context.actor, checked_at=datetime.now().astimezone())
            after = authority.options(context.actor)
            if before != after:
                raise FamilyCorrectionDenied("Correction options changed.")
            return tuple(
                CorrectionOption(
                    fact_kind=x.fact_kind,
                    field_name=x.field_name,
                    label=x.label,
                    reference=x.reference,
                )
                for x in after
            )

    def submit(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        target_reference: str,
        field_name: str,
        statement: str,
    ) -> FamilyCorrectionReport:
        if type(context.actor) is not GuardianActorContext:
            raise FamilyCorrectionDenied("Guardian context required.")
        actor = context.actor
        return self._run(
            context,
            session_binding,
            lambda service: service.submit(
                actor=actor,
                target_reference=target_reference,
                field_name=field_name,
                statement=statement,
                idempotency_key=context.idempotency_key,
            ),
        )

    def own(
        self, *, context: CommandExecutionContext, session_binding: str, report_id: UUID
    ) -> FamilyCorrectionReport:
        if type(context.actor) is not GuardianActorContext:
            raise FamilyCorrectionDenied("Guardian context required.")
        actor = context.actor
        return self._run(
            context, session_binding, lambda service: service.own(actor=actor, report_id=report_id)
        )

    def own_by_key(
        self, *, context: CommandExecutionContext, session_binding: str, idempotency_key: str
    ) -> FamilyCorrectionReport:
        if type(context.actor) is not GuardianActorContext:
            raise FamilyCorrectionDenied("Guardian context required.")
        actor = context.actor
        return self._run(
            context,
            session_binding,
            lambda service: service.own_by_key(actor=actor, idempotency_key=idempotency_key),
        )

    def list_own(
        self, *, context: CommandExecutionContext, session_binding: str, limit: int
    ) -> tuple[FamilyCorrectionReport, ...]:
        if type(context.actor) is not GuardianActorContext:
            raise FamilyCorrectionDenied("Guardian context required.")
        actor = context.actor
        with self._transaction(context, session_binding) as session:
            return self._services(
                SqlFamilyCorrectionRepository(session, review_enabled=self._review_enabled),
                session,
                context,
                session_binding,
            ).list_own(actor=actor, limit=limit)

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
    ) -> tuple[FamilyCorrectionReport, ...]:
        if type(context.actor) is not StaffActorContext:
            raise FamilyCorrectionDenied("Staff context required.")
        actor = context.actor
        with self._transaction(context, session_binding) as session:
            return self._services(
                SqlFamilyCorrectionRepository(session, review_enabled=self._review_enabled),
                session,
                context,
                session_binding,
            ).staff_list(
                actor=actor,
                fact_kind=fact_kind,
                source_key=source_key,
                field_name=field_name,
                action=required_action,
                limit=limit,
            )

    def resolve(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        report_id: UUID,
        writer_receipt_ref: str,
    ) -> FamilyCorrectionReport:
        if type(context.actor) is not StaffActorContext:
            raise FamilyCorrectionDenied("Staff context required.")
        actor = context.actor
        return self._run(
            context,
            session_binding,
            lambda service: service.resolve(
                actor=actor, report_id=report_id, locator_sha256=writer_receipt_ref
            ),
        )


class F2CorrectionAuthority(Protocol):
    def require_action(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str,
        source_key: str,
        field_name: str,
        action: Literal["maintain", "end", "revoke"],
        checked_at: datetime,
    ) -> object: ...


class StaffCorrectionAuthorityAdapter:
    def __init__(
        self,
        authority: FamilyAuthorityComponents,
        education_authority: EducationAuthority | None = None,
        f2_authority: F2CorrectionAuthority | None = None,
        residence_authority: F2CorrectionAuthority | None = None,
    ) -> None:
        self._authority = authority
        self._education = education_authority
        self._f2 = f2_authority
        self._residence = residence_authority

    def require_action(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str,
        source_key: str,
        field_name: str,
        action: Literal["maintain", "end", "revoke"],
        checked_at: datetime,
    ) -> object:
        if (
            fact_kind in {kind for kind, _ in F6_CORRECTION_FIELDS}
            and (fact_kind, field_name) not in F6_CORRECTION_FIELDS
        ):
            raise FamilyCorrectionDenied("Unsupported residence correction field.")
        if (fact_kind, field_name) in F6_CORRECTION_FIELDS:
            if self._residence is None or action != "maintain":
                raise FamilyCorrectionDenied("Residence correction authority unavailable.")
            return self._residence.require_action(
                actor=actor,
                fact_kind=fact_kind,
                source_key=source_key,
                field_name=field_name,
                action=action,
                checked_at=checked_at,
            )
        if (fact_kind, field_name) in F2_CORRECTION_FIELDS:
            if self._f2 is None or action != "maintain":
                raise FamilyCorrectionDenied("F2 correction maintenance authority unavailable.")
            return self._f2.require_action(
                actor=actor,
                fact_kind=fact_kind,
                source_key=source_key,
                field_name=field_name,
                action=action,
                checked_at=checked_at,
            )
        if (fact_kind, field_name) in _EDUCATION_CORRECTION_FIELDS:
            if self._education is None or action != "maintain":
                raise FamilyCorrectionDenied("Education correction authority unavailable.")
            permit = self._education.require_scope(
                actor, action="maintain", fact_kind=fact_kind, checked_at=checked_at
            )
            self._education.require_object(permit, source_key, is_creation=False)
            self._education.require_fields(permit, (field_name,))
            return permit
        permit = self._authority.require_scope(
            actor,
            action=action,
            fact_kind=fact_kind,
            source_key=source_key,
            is_creation=False,
            checked_at=checked_at,
        )
        required = {
            ("person", "name"): {"display_name"},
            ("student_school_relationship", "enrollment_status"): {"enrollment_status"},
            ("student_school_relationship", "student_number"): {"student_number"},
            ("student_school_relationship", "residence_mode"): {"residence_mode"},
            ("student_school_relationship", "campus"): {"primary_campus_source_key"},
            ("person_region", "region"): {"district_code", "district_name"},
            ("guardian_relationship", "relationship"): set[str](),
        }.get((fact_kind, field_name))
        if required is None or not required <= set(permit.scope.field_names):
            raise FamilyCorrectionDenied("Correction field is outside current Staff scope.")
        return permit


class _KernelOwnReader:
    def __init__(
        self,
        kernel: SqlAlchemySchoolFactWriterExecutorKernel,
        context: CommandExecutionContext,
        binding: str,
    ) -> None:
        self._kernel, self._context, self._binding = kernel, context, binding

    def check(self, *, locator_sha256: str) -> object | None:
        return self._kernel.check_own_result_by_locator(
            context=self._context, session_binding=self._binding, locator_sha256=locator_sha256
        )


class _UnavailableReceiptVerifier:
    def require_applied_target(self, **_kwargs: object) -> Literal["maintain", "end", "revoke"]:
        raise FamilyCorrectionDenied("Staff receipt context required.")


def create_family_correction_kernel(
    *,
    writer_kernel: SqlAlchemySchoolFactWriterExecutorKernel,
    family_authority: FamilyAuthorityComponents,
    reference_key: bytes,
    clock: Callable[[], datetime],
    education_authority: EducationAuthority | None = None,
    f2_authority: F2CorrectionAuthority | None = None,
    leadership_policy: Callable[[], FamilyLeadershipPolicy] | None = None,
    f5_enabled: bool = False,
    f6_enabled: bool = False,
    residence_authority: F2CorrectionAuthority | None = None,
    review_enabled: bool = True,
    review_commands_enabled: bool = True,
) -> SqlFamilyCorrectionExecutorKernel:
    codec = FamilyCorrectionReferenceCodec(reference_key)

    def guardian(session: Session) -> SqlGuardianCorrectionAuthority:
        return SqlGuardianCorrectionAuthority(
            session=session,
            codec=codec,
            leadership_policy=leadership_policy,
            f5_enabled=f5_enabled,
            f6_enabled=f6_enabled,
        )

    def services(
        repository: SqlFamilyCorrectionRepository,
        session: Session,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> FamilyCorrectionService:
        if type(context.actor) is StaffActorContext:
            own = _KernelOwnReader(writer_kernel, context, session_binding)
            receipt = ExactCorrectionReceiptVerifier(
                own_results=own,
                differences=SqlAppliedFieldDifferenceReader(
                    session=session, school_id=context.actor.school_id
                ),
            )
        else:
            receipt = _UnavailableReceiptVerifier()
        return FamilyCorrectionService(
            repository=repository,
            guardian_authority=guardian(session),
            staff_authority=StaffCorrectionAuthorityAdapter(
                family_authority, education_authority, f2_authority, residence_authority
            ),
            receipt_verifier=receipt,
            clock=clock,
        )

    return SqlFamilyCorrectionExecutorKernel(
        transaction=writer_kernel.correction_transaction,
        services=services,
        guardian_authority=guardian,
        review_codec=CorrectionReviewCodec(reference_key),
        review_enabled=review_enabled,
        review_commands_enabled=review_commands_enabled,
        clock=clock,
    )
