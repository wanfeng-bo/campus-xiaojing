from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.platform.contracts import CommandExecutionContext, SchoolId
from app.platform.school_facts.application import SchoolFactChangeService
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    CanonicalFactChangeV1,
)
from app.platform.school_facts.ports import (
    FactVersionProjection,
    PreparedConfirmation,
    PreparedConfirmationResult,
    ResidenceFactProjection,
    SchoolFactApplyResult,
)

NOW = datetime(2026, 8, 20, 8, tzinfo=UTC)


def _change(
    source_key: str,
    *,
    expected_version: int,
    row_number: int,
) -> CanonicalFactChangeV1:
    return CanonicalFactChangeV1.model_validate(
        {
            "schema_version": 1,
            "operation": "upsert",
            "fact_kind": "school",
            "source_key": source_key,
            "expected_version": expected_version,
            "effective_at": NOW,
            "origin": {"sheet_code": "schools", "row_number": row_number},
            "content": {
                "business_code": source_key.upper(),
                "display_name": source_key,
                "timezone_name": "Asia/Shanghai",
                "lifecycle_status": "active",
            },
        }
    )


@dataclass
class _Reader:
    school_version: int = 4
    versions: dict[tuple[str, str], int] = field(default_factory=dict)
    reads: list[tuple[str, str]] = field(default_factory=list)
    missing_references: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def current_school_fact_version(self, school_id: SchoolId) -> int:
        del school_id
        return self.school_version

    def find_fact_version(
        self, school_id: SchoolId, fact_kind: str, source_key: str
    ) -> FactVersionProjection | None:
        del school_id
        self.reads.append((fact_kind, source_key))
        version = self.versions.get((fact_kind, source_key))
        return None if version is None else FactVersionProjection(version=version)

    def unresolved_reference_codes(
        self,
        school_id: SchoolId,
        change: CanonicalFactChangeV1,
    ) -> tuple[str, ...]:
        del school_id
        return self.missing_references.get(change.source_key, ())

    def find_applied_plan(self, school_id: SchoolId, source_sha256: str, plan_sha256: str) -> None:
        del school_id, source_sha256, plan_sha256
        return None


class _WriteTrap:
    def prepare(self, prepared: PreparedConfirmation) -> PreparedConfirmationResult:
        del prepared
        raise AssertionError("预检不得触发 prepare 写端口")

    def confirm(
        self,
        *,
        credential_sha256: str,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_sha256: str,
        now: datetime,
    ) -> SchoolFactApplyResult:
        del credential_sha256, change_set, context, session_sha256, now
        raise AssertionError("预检不得触发 confirm 写端口")


def _change_set(*changes: CanonicalFactChangeV1) -> CanonicalFactChangeSetV1:
    return CanonicalFactChangeSetV1(
        schema_version=1,
        source_sha256="b" * 64,
        base_version=4,
        changes=changes,
    )


def _residence_change(
    source_key: str,
    *,
    operation: str,
    expected_version: int,
    row_number: int,
    effective_at: datetime = NOW,
    student_source_key: str = "student:001",
    bed_source_key: str | None = None,
) -> CanonicalFactChangeV1:
    content: dict[str, object] | None = None
    if operation == "upsert":
        content = {
            "student_source_key": student_source_key,
            "bed_source_key": bed_source_key or f"bed:{source_key}",
        }
    return CanonicalFactChangeV1.model_validate(
        {
            "operation": operation,
            "fact_kind": "student_bed_residence",
            "source_key": source_key,
            "expected_version": expected_version,
            "effective_at": effective_at,
            "origin": {"sheet_code": "student_bed_residences", "row_number": row_number},
            "content": content,
        }
    )


@dataclass
class _BulkResidenceReader(_Reader):
    residence_students: dict[str, str] = field(default_factory=dict)
    residence_beds: dict[str, str] = field(default_factory=dict)

    def find_fact_versions_bulk(
        self,
        school_id: SchoolId,
        targets: tuple[tuple[str, str], ...],
    ) -> dict[tuple[str, str], FactVersionProjection]:
        del school_id
        return {
            target: FactVersionProjection(version=self.versions[target])
            for target in targets
            if target in self.versions
        }

    def unresolved_reference_codes_bulk(
        self,
        school_id: SchoolId,
        changes: tuple[CanonicalFactChangeV1, ...],
    ) -> dict[tuple[str, str], tuple[str, ...]]:
        del school_id
        return {
            (change.fact_kind, change.source_key): self.missing_references.get(
                change.source_key, ()
            )
            for change in changes
        }

    def find_residence_student_source_keys_bulk(
        self,
        school_id: SchoolId,
        residence_source_keys: tuple[str, ...],
    ) -> dict[str, str]:
        del school_id
        return {
            source_key: self.residence_students[source_key]
            for source_key in residence_source_keys
            if source_key in self.residence_students
        }

    def find_residence_facts_bulk(
        self,
        school_id: SchoolId,
        residence_source_keys: tuple[str, ...],
    ) -> dict[str, ResidenceFactProjection]:
        del school_id
        return {
            source_key: ResidenceFactProjection(
                student_source_key=self.residence_students[source_key],
                bed_source_key=self.residence_beds[source_key],
            )
            for source_key in residence_source_keys
            if source_key in self.residence_students and source_key in self.residence_beds
        }


def test_preflight_collects_all_stable_issues_without_creating_write_state() -> None:
    reader = _Reader(versions={("school", "school:existing"): 3})
    service = SchoolFactChangeService(reader=reader, writer=_WriteTrap())

    report = service.preflight(
        school_id=SchoolId(UUID("00000000-0000-0000-0000-000000000001")),
        change_set=_change_set(
            _change("school:existing", expected_version=2, row_number=9),
            _change("school:missing", expected_version=1, row_number=3),
        ),
    )

    assert report.is_clean is False
    assert [(i.sheet_code, i.row_number, i.column_code, i.error_code) for i in report.issues] == [
        ("schools", 3, "source_key", "fact.not_found"),
        ("schools", 9, "expected_version", "fact.version_conflict"),
    ]
    assert report.confirmation_credential is None


def test_preflight_same_content_has_same_plan_hash_and_missing_rows_are_not_deletes() -> None:
    reader = _Reader(versions={("school", "school:existing"): 4})
    service = SchoolFactChangeService(reader=reader, writer=_WriteTrap())
    change_set = _change_set(_change("school:existing", expected_version=4, row_number=2))

    first = service.preflight(
        school_id=SchoolId(UUID("00000000-0000-0000-0000-000000000001")),
        change_set=change_set,
    )
    second = service.preflight(
        school_id=SchoolId(UUID("00000000-0000-0000-0000-000000000001")),
        change_set=change_set,
    )

    assert first.is_clean is True
    assert first.plan_sha256 == second.plan_sha256
    assert reader.reads == [("school", "school:existing")] * 2


def test_preflight_resolves_references_created_earlier_in_the_same_atomic_plan() -> None:
    reader = _Reader(
        missing_references={"student:new": ("person_source_key",)},
    )
    service = SchoolFactChangeService(reader=reader, writer=_WriteTrap())
    person = CanonicalFactChangeV1.model_validate(
        {
            "operation": "upsert",
            "fact_kind": "person",
            "source_key": "person:new",
            "expected_version": 0,
            "effective_at": NOW,
            "origin": {"sheet_code": "people", "row_number": 2},
            "content": {
                "display_name": "新学生",
                "person_kind": "other",
                "lifecycle_status": "active",
            },
        }
    )
    student = CanonicalFactChangeV1.model_validate(
        {
            "operation": "upsert",
            "fact_kind": "student",
            "source_key": "student:new",
            "expected_version": 0,
            "effective_at": NOW,
            "origin": {"sheet_code": "students", "row_number": 2},
            "content": {"person_source_key": "person:new", "status": "active"},
        }
    )

    report = service.preflight(
        school_id=SchoolId(UUID("00000000-0000-0000-0000-000000000001")),
        change_set=_change_set(person, student),
    )

    assert report.is_clean is True
    assert report.issues == ()


def test_preflight_rejects_plan_local_parent_that_starts_after_the_child() -> None:
    """A stable ID in the plan is not usable before its structural slice begins."""

    reader = _Reader(missing_references={"location:child": ("parent_source_key",)})
    service = SchoolFactChangeService(reader=reader, writer=_WriteTrap())
    parent = CanonicalFactChangeV1.model_validate(
        {
            "operation": "upsert",
            "fact_kind": "location",
            "source_key": "location:future-parent",
            "expected_version": 0,
            "effective_at": NOW + timedelta(days=1),
            "origin": {"sheet_code": "locations", "row_number": 2},
            "content": {
                "kind": "campus",
                "business_code": "FUTURE",
                "display_name": "未来校区",
                "display_path": "/未来校区",
                "status": "active",
            },
        }
    )
    child = CanonicalFactChangeV1.model_validate(
        {
            "operation": "upsert",
            "fact_kind": "location",
            "source_key": "location:child",
            "expected_version": 0,
            "effective_at": NOW,
            "origin": {"sheet_code": "locations", "row_number": 3},
            "content": {
                "parent_source_key": "location:future-parent",
                "kind": "building",
                "business_code": "CHILD",
                "display_name": "过早子地点",
                "display_path": "/未来校区/过早子地点",
                "status": "active",
            },
        }
    )

    report = service.preflight(
        school_id=SchoolId(UUID("00000000-0000-0000-0000-000000000001")),
        change_set=_change_set(parent, child),
    )

    assert [(issue.column_code, issue.error_code) for issue in report.issues] == [
        ("parent_source_key", "fact.reference_not_found")
    ]


def test_preflight_rejects_assignment_at_the_same_boundary_as_planned_organization_end() -> None:
    """A database-current organization cannot be consumed at its same-plan end boundary."""

    reader = _Reader(versions={("organization", "organization:ending"): 1})
    service = SchoolFactChangeService(reader=reader, writer=_WriteTrap())
    organization_end = CanonicalFactChangeV1.model_validate(
        {
            "operation": "end",
            "fact_kind": "organization",
            "source_key": "organization:ending",
            "expected_version": 1,
            "effective_at": NOW,
            "origin": {"sheet_code": "organizations", "row_number": 2},
            "content": None,
        }
    )
    assignment = CanonicalFactChangeV1.model_validate(
        {
            "operation": "upsert",
            "fact_kind": "assignment",
            "source_key": "assignment:invalid-boundary",
            "expected_version": 0,
            "effective_at": NOW,
            "origin": {"sheet_code": "assignments", "row_number": 3},
            "content": {
                "person_source_key": "person:staff",
                "organization_source_key": "organization:ending",
                "position_title": "边界后无效任职",
                "semantic_role_code": "family_contact",
                "assignment_kind": "part_time",
                "appointment_source": "manual",
                "approval_reference": "approval:same-plan-boundary",
                "status": "confirmed",
            },
        }
    )

    report = service.preflight(
        school_id=SchoolId(UUID("00000000-0000-0000-0000-000000000001")),
        change_set=_change_set(organization_end, assignment),
    )

    assert ("organization_source_key", "fact.reference_not_found") in {
        (issue.column_code, issue.error_code) for issue in report.issues
    }


@pytest.mark.parametrize(
    ("parent_kind", "parent_key", "consumer_kind", "reference_field", "consumer_content"),
    (
        (
            "organization",
            "organization:ending",
            "assignment",
            "organization_source_key",
            {
                "person_source_key": "person:staff",
                "organization_source_key": "organization:ending",
                "position_title": "跨越终止边界的任职",
                "semantic_role_code": "family_contact",
                "assignment_kind": "part_time",
                "appointment_source": "manual",
                "approval_reference": "approval:cross-boundary",
                "status": "confirmed",
            },
        ),
        (
            "location",
            "location:ending-campus",
            "location",
            "parent_source_key",
            {
                "parent_source_key": "location:ending-campus",
                "kind": "building",
                "business_code": "OPEN-CHILD",
                "display_name": "跨越终止边界的子地点",
                "display_path": "/终止校区/开放子地点",
                "status": "active",
            },
        ),
    ),
)
def test_preflight_rejects_open_consumer_that_crosses_a_same_plan_parent_end(
    parent_kind: str,
    parent_key: str,
    consumer_kind: str,
    reference_field: str,
    consumer_content: dict[str, object],
) -> None:
    """Starting before T is insufficient when the dependent slice remains open after T."""

    boundary = NOW + timedelta(days=2)
    reader = _Reader(versions={(parent_kind, parent_key): 1})
    service = SchoolFactChangeService(reader=reader, writer=_WriteTrap())
    parent_end = CanonicalFactChangeV1.model_validate(
        {
            "operation": "end",
            "fact_kind": parent_kind,
            "source_key": parent_key,
            "expected_version": 1,
            "effective_at": boundary,
            "origin": {"sheet_code": f"{parent_kind}s", "row_number": 2},
            "content": None,
        }
    )
    consumer = CanonicalFactChangeV1.model_validate(
        {
            "operation": "upsert",
            "fact_kind": consumer_kind,
            "source_key": f"{consumer_kind}:cross-boundary",
            "expected_version": 0,
            "effective_at": NOW,
            "origin": {"sheet_code": f"{consumer_kind}s", "row_number": 3},
            "content": consumer_content,
        }
    )

    report = service.preflight(
        school_id=SchoolId(UUID("00000000-0000-0000-0000-000000000001")),
        change_set=_change_set(parent_end, consumer),
    )

    assert (reference_field, "fact.reference_not_found") in {
        (issue.column_code, issue.error_code) for issue in report.issues
    }


def test_preflight_accepts_bounded_assignment_that_ends_with_its_same_plan_parent() -> None:
    """A dependent slice wholly contained before the parent end remains representable."""

    boundary = NOW + timedelta(days=2)
    reader = _Reader(versions={("organization", "organization:ending"): 1})
    service = SchoolFactChangeService(reader=reader, writer=_WriteTrap())
    organization_end = CanonicalFactChangeV1.model_validate(
        {
            "operation": "end",
            "fact_kind": "organization",
            "source_key": "organization:ending",
            "expected_version": 1,
            "effective_at": boundary,
            "origin": {"sheet_code": "organizations", "row_number": 2},
            "content": None,
        }
    )
    assignment = CanonicalFactChangeV1.model_validate(
        {
            "operation": "upsert",
            "fact_kind": "assignment",
            "source_key": "assignment:contained",
            "expected_version": 0,
            "effective_at": NOW,
            "origin": {"sheet_code": "assignments", "row_number": 3},
            "content": {
                "person_source_key": "person:staff",
                "organization_source_key": "organization:ending",
                "position_title": "边界前结束的任职",
                "semantic_role_code": "family_contact",
                "assignment_kind": "part_time",
                "appointment_source": "manual",
                "approval_reference": "approval:contained",
                "valid_to": boundary,
                "status": "confirmed",
            },
        }
    )

    report = service.preflight(
        school_id=SchoolId(UUID("00000000-0000-0000-0000-000000000001")),
        change_set=_change_set(organization_end, assignment),
    )

    assert not any(
        issue.column_code == "organization_source_key"
        and issue.error_code == "fact.reference_not_found"
        for issue in report.issues
    )


def test_preflight_rejects_child_history_under_a_same_plan_revoked_location() -> None:
    """Revoke invalidates every time in its epoch, including an earlier child slice."""

    reader = _Reader(versions={("location", "location:revoked-campus"): 1})
    service = SchoolFactChangeService(reader=reader, writer=_WriteTrap())
    location_revoke = CanonicalFactChangeV1.model_validate(
        {
            "operation": "revoke",
            "fact_kind": "location",
            "source_key": "location:revoked-campus",
            "expected_version": 1,
            "effective_at": NOW + timedelta(days=1),
            "origin": {"sheet_code": "locations", "row_number": 2},
            "content": None,
        }
    )
    historical_child = CanonicalFactChangeV1.model_validate(
        {
            "operation": "upsert",
            "fact_kind": "location",
            "source_key": "location:invalid-child",
            "expected_version": 0,
            "effective_at": NOW,
            "origin": {"sheet_code": "locations", "row_number": 3},
            "content": {
                "parent_source_key": "location:revoked-campus",
                "kind": "building",
                "business_code": "INVALID-CHILD",
                "display_name": "撤销校区下的历史子地点",
                "display_path": "/撤销校区/历史子地点",
                "status": "active",
            },
        }
    )

    report = service.preflight(
        school_id=SchoolId(UUID("00000000-0000-0000-0000-000000000001")),
        change_set=_change_set(location_revoke, historical_child),
    )

    assert ("parent_source_key", "fact.reference_not_found") in {
        (issue.column_code, issue.error_code) for issue in report.issues
    }


def test_preflight_rejects_ambiguous_same_student_same_time_transition() -> None:
    old_keys = ("residence:old-a", "residence:old-b")
    reader = _BulkResidenceReader(
        versions={("student_bed_residence", source_key): 1 for source_key in old_keys},
        residence_students={source_key: "student:001" for source_key in old_keys},
        residence_beds={
            "residence:old-a": "bed:a",
            "residence:old-b": "bed:b",
        },
    )
    service = SchoolFactChangeService(reader=reader, writer=_WriteTrap())
    report = service.preflight(
        school_id=SchoolId(UUID("00000000-0000-0000-0000-000000000001")),
        change_set=_change_set(
            _residence_change(
                old_keys[0],
                operation="end",
                expected_version=1,
                row_number=2,
            ),
            _residence_change(
                old_keys[1],
                operation="end",
                expected_version=1,
                row_number=3,
            ),
            _residence_change(
                "residence:new",
                operation="upsert",
                expected_version=0,
                row_number=4,
                bed_source_key="bed:c",
            ),
        ),
    )

    assert [issue.error_code for issue in report.issues] == [
        "residence.transfer_ambiguous",
        "residence.transfer_ambiguous",
        "residence.transfer_ambiguous",
    ]


def test_residence_transition_fails_closed_without_the_narrow_projection_reader() -> None:
    old_key = "residence:old"
    reader = _Reader(versions={("student_bed_residence", old_key): 1})
    service = SchoolFactChangeService(reader=reader, writer=_WriteTrap())

    report = service.preflight(
        school_id=SchoolId(UUID("00000000-0000-0000-0000-000000000001")),
        change_set=_change_set(
            _residence_change(
                old_key,
                operation="end",
                expected_version=1,
                row_number=2,
            ),
            _residence_change(
                "residence:new",
                operation="upsert",
                expected_version=0,
                row_number=3,
            ),
        ),
    )

    assert [issue.error_code for issue in report.issues] == [
        "residence.transition_projection_unavailable"
    ]


def test_residence_transition_fails_closed_when_projection_reader_omits_the_end() -> None:
    old_key = "residence:old"
    reader = _BulkResidenceReader(versions={("student_bed_residence", old_key): 1})
    service = SchoolFactChangeService(reader=reader, writer=_WriteTrap())

    report = service.preflight(
        school_id=SchoolId(UUID("00000000-0000-0000-0000-000000000001")),
        change_set=_change_set(
            _residence_change(
                old_key,
                operation="end",
                expected_version=1,
                row_number=2,
            ),
            _residence_change(
                "residence:new",
                operation="upsert",
                expected_version=0,
                row_number=3,
            ),
        ),
    )

    assert [issue.error_code for issue in report.issues] == [
        "residence.transition_projection_missing"
    ]


def test_preflight_accepts_same_student_transition_without_residence_edge() -> None:
    old_key = "residence:old"
    reader = _BulkResidenceReader(
        versions={("student_bed_residence", old_key): 1},
        residence_students={old_key: "student:001"},
        residence_beds={old_key: "bed:old"},
    )
    service = SchoolFactChangeService(reader=reader, writer=_WriteTrap())

    report = service.preflight(
        school_id=SchoolId(UUID("00000000-0000-0000-0000-000000000001")),
        change_set=_change_set(
            _residence_change(
                old_key,
                operation="end",
                expected_version=1,
                row_number=2,
            ),
            _residence_change(
                "residence:new",
                operation="upsert",
                expected_version=0,
                row_number=3,
            ),
        ),
    )

    assert report.is_clean is True
    assert report.issues == ()


def test_preflight_accepts_same_bed_continuous_slice_pair() -> None:
    old_key = "residence:old"
    reader = _BulkResidenceReader(
        versions={("student_bed_residence", old_key): 1},
        residence_students={old_key: "student:001"},
        residence_beds={old_key: "bed:stable"},
    )
    service = SchoolFactChangeService(reader=reader, writer=_WriteTrap())

    report = service.preflight(
        school_id=SchoolId(UUID("00000000-0000-0000-0000-000000000001")),
        change_set=_change_set(
            _residence_change(
                old_key,
                operation="end",
                expected_version=1,
                row_number=2,
            ),
            _residence_change(
                "residence:new",
                operation="upsert",
                expected_version=0,
                row_number=3,
                bed_source_key="bed:stable",
            ),
        ),
    )

    assert report.is_clean is True
    assert report.issues == ()
