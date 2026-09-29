"""Education completeness uses the current authorized exact education target."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.composition.school_fact_directory_product import FactChangeInput
from app.composition.school_fact_education_dependencies import EducationDependencyProductAdapter
from app.composition.school_fact_education_maintenance import EducationMaintenanceCoordinator
from app.composition.school_fact_education_product import EducationBatchInput
from app.platform.school_facts.f7_coverage_basis import mother_set_digest
from tests.school_facts.test_f4_education_quality import actor

AT = datetime(2026, 9, 24, tzinfo=UTC)


def coordinate_authority():
    from app.composition.school_fact_education_authority import EducationAuthority

    return SimpleNamespace(
        require_scope=lambda *_args, **kwargs: SimpleNamespace(
            scope=SimpleNamespace(
                field_names=("teacher_role",)
                if kwargs["fact_kind"] == "class_teacher_assignment"
                else ("teaching_role",),
            )
        ),
        require_object=lambda *_args, **_kwargs: None,
        require_fields=EducationAuthority.require_fields,
    )


def through_coordinator(adapter, payload, person):
    coordinator = EducationMaintenanceCoordinator(
        SimpleNamespace(repository=adapter.repository),
        family=None,
        dependencies=adapter,
    )
    result = coordinator.resolve_change(
        actor=person,
        payload=EducationBatchInput.model_validate_json(payload.model_dump_json()),
        request_id=UUID(int=9),
        effective_at=AT,
    )
    assert coordinator.canonical_version == 3
    return result


@pytest.mark.parametrize("coordinated", [False, True])
@pytest.mark.parametrize("target_kind", ["class_teacher_assignment", "teaching_assignment"])
def test_complete_exact_education_claim_requires_current_authorized_target(
    target_kind,
    coordinated,
):
    person = actor()
    reads = []
    rechecks = []
    target = "education:arrangement"
    fields = {
        "quality_kind": "coverage",
        "coverage_status": "complete",
        "target_fact_kind": target_kind,
        "target_source_key": target,
        "owner_kind": "education",
        "education_target_kind": "education_class",
        "education_target_source_key": "class:a",
        "responsibility": "正式核查人",
        "basis_reference": "原资料复核",
        "verified_at": AT.isoformat(),
    }
    bridge = SimpleNamespace(
        creation_coordinates=lambda *_args: {"school_fact_version": 4},
        require_change_set=lambda *_args: SimpleNamespace(revalidate=lambda: None),
        education_authority=coordinate_authority(),
        clock=lambda: AT,
    )
    refs = SimpleNamespace(
        source_for=lambda *_args, **_kw: "quality:new",
        resolve_fields=lambda *_args, **_kw: dict(fields),
        public_fields=lambda _actor, raw: dict(raw),
    )

    def read_records(_actor, kind, **kwargs):
        reads.append((kind, kwargs))
        return SimpleNamespace(
            records=(
                {
                    "source_key": target,
                    "fact_kind": target_kind,
                    "version": 3,
                    "segments": [{"effective_at": AT.isoformat(), "valid_to": None, "content": {}}],
                },
            ),
            school_fact_version=4,
            proof=SimpleNamespace(revalidate=lambda: rechecks.append(True)),
        )

    adapter = EducationDependencyProductAdapter(
        bridge=bridge,
        references=refs,
        directory_adapter=None,
        repository=SimpleNamespace(read_records=read_records),
    )
    payload = FactChangeInput.model_validate(
        {
            "fact_kind": "fact_quality",
            "creation_ref": "f4create:fact_quality:opaque",
            "operation": "upsert",
            "fields": {},
            "reason": "核对正式任教资料",
        }
    )
    resolved = (
        through_coordinator(adapter, payload, person)
        if coordinated
        else adapter.resolve_change(
            actor=person, payload=payload, request_id=UUID(int=9), effective_at=AT
        )
    )
    assert resolved.change_set.schema_version == 4
    canonical = resolved.change_set.changes[0]
    assert canonical.schema_version == 4
    assert canonical.content.coverage_basis.member_count == 1
    assert canonical.content.coverage_basis.member_sha256 == mother_set_digest(((target, 3),))[1]
    expected_field = (
        "teacher_role" if target_kind == "class_teacher_assignment" else "teaching_role"
    )
    assert reads == [
        (target_kind, {"fields": (expected_field,), "source_keys": (target,), "as_of": AT})
    ]
    assert "coverage_basis" not in resolved.summary.after
    assert resolved.summary_revalidate is not None
    resolved.summary_revalidate(person)
    assert rechecks


def test_client_cannot_submit_coverage_proof_in_education_fields():
    adapter = EducationDependencyProductAdapter(
        bridge=SimpleNamespace(), references=None, directory_adapter=None, repository=None
    )
    payload = FactChangeInput.model_validate(
        {
            "fact_kind": "fact_quality",
            "creation_ref": "f4create:fact_quality:opaque",
            "operation": "upsert",
            "fields": {"coverage_basis": {"member_count": 0}},
            "reason": "核对正式资料",
        }
    )
    with pytest.raises(ValueError, match="受控"):
        adapter.resolve_change(
            actor=actor(), payload=payload, request_id=UUID(int=9), effective_at=AT
        )


@pytest.mark.parametrize("coordinated", [False, True])
def test_complete_grade_group_set_previews_only_current_authorized_campus_year_members(coordinated):
    person = actor()
    reads = []
    rechecks = []
    fields = {
        "quality_kind": "coverage",
        "coverage_status": "complete",
        "target_fact_kind": "education_grade_group",
        "target_source_key": None,
        "owner_kind": "education",
        "education_target_kind": "education_calendar_rule",
        "education_target_source_key": "calendar:campus-a",
        "field_name": "grade_source_key",
        "campus_source_key": "campus:a",
        "academic_year": 2026,
        "responsibility": "正式核查人",
        "basis_reference": "甲校区本学年年级组清单核对",
        "verified_at": AT.isoformat(),
    }
    bridge = SimpleNamespace(
        creation_coordinates=lambda *_args: {"school_fact_version": 4},
        require_change_set=lambda *_args: SimpleNamespace(revalidate=lambda: None),
    )
    refs = SimpleNamespace(
        source_for=lambda *_args, **_kw: "quality:new",
        resolve_fields=lambda *_args, **_kw: dict(fields),
        public_fields=lambda _actor, raw: dict(raw),
    )

    def read_records(_actor, kind, **kwargs):
        reads.append((kind, kwargs))
        assert kind == "education_grade_group"
        return SimpleNamespace(
            records=(
                {
                    "source_key": "group:a",
                    "fact_kind": kind,
                    "version": 3,
                    "segments": [
                        {
                            "effective_at": AT.isoformat(),
                            "valid_to": None,
                            "content": {
                                "grade_source_key": "grade:7",
                                "campus_source_key": "campus:a",
                                "academic_year": 2026,
                            },
                        }
                    ],
                },
                {
                    "source_key": "group:b",
                    "fact_kind": kind,
                    "version": 2,
                    "segments": [
                        {
                            "effective_at": AT.isoformat(),
                            "valid_to": None,
                            "content": {
                                "grade_source_key": "grade:8",
                                "campus_source_key": "campus:a",
                                "academic_year": 2026,
                            },
                        }
                    ],
                },
            ),
            school_fact_version=4,
            proof=SimpleNamespace(revalidate=lambda: rechecks.append(True)),
        )

    adapter = EducationDependencyProductAdapter(
        bridge=bridge,
        references=refs,
        directory_adapter=None,
        repository=SimpleNamespace(read_records=read_records),
    )
    payload = FactChangeInput.model_validate(
        {
            "fact_kind": "fact_quality",
            "creation_ref": "f4create:fact_quality:opaque",
            "operation": "upsert",
            "fields": {},
            "reason": "核对完整母集",
        }
    )
    resolved = (
        through_coordinator(adapter, payload, person)
        if coordinated
        else adapter.resolve_change(
            actor=person,
            payload=payload,
            request_id=UUID(int=9),
            effective_at=AT,
        )
    )
    basis = resolved.change_set.changes[0].content.coverage_basis
    assert basis.schema_version == 2
    assert basis.authorized_source_keys == ("group:a", "group:b")
    assert basis.member_sha256 == mother_set_digest((("group:a", 3), ("group:b", 2)))[1]
    assert reads == [
        (
            "education_grade_group",
            {
                "fields": ("grade_source_key", "campus_source_key", "academic_year"),
                "source_keys": None,
                "filter": {"campus_source_key": "campus:a", "academic_year": 2026},
                "as_of": AT,
            },
        )
    ]
    resolved.summary_revalidate(person)
    assert rechecks


@pytest.mark.parametrize("complete_index", [None, 0, 1])
def test_education_batch_keeps_required_child_contract_without_mutating_coordinator(complete_index):
    import json

    checked = []
    permission = SimpleNamespace(revalidate=lambda: checked.append("current-permission"))

    def fields_for(_actor, fields, **_kwargs):
        common = {
            "owner_kind": "education",
            "education_target_kind": "education_class",
            "education_target_source_key": "class:a",
            "target_fact_kind": "class_teacher_assignment",
            "target_source_key": "teacher:a",
            "responsibility": "合成核查人",
            "basis_reference": "正式合成核查",
            "verified_at": AT.isoformat(),
            **fields,
        }
        if fields["quality_kind"] == "coverage":
            return common | {"coverage_status": "complete"}
        return common | {"source_status": "available"}

    repository = SimpleNamespace(
        read_records=lambda *_a, **_k: SimpleNamespace(
            records=(
                {
                    "source_key": "teacher:a",
                    "fact_kind": "class_teacher_assignment",
                    "version": 2,
                    "segments": [{"effective_at": AT.isoformat(), "valid_to": None, "content": {}}],
                },
            ),
            school_fact_version=4,
            proof=permission,
        )
    )
    dependency = EducationDependencyProductAdapter(
        bridge=SimpleNamespace(
            creation_coordinates=lambda *_: {"school_fact_version": 4},
            require_change_set=lambda *_: permission,
            education_authority=coordinate_authority(),
            clock=lambda: AT,
        ),
        references=SimpleNamespace(
            source_for=lambda _actor, _kind, reference, **_kwargs: reference,
            resolve_fields=fields_for,
            public_fields=lambda _actor, fields: fields,
        ),
        directory_adapter=None,
        repository=repository,
    )
    coordinator = EducationMaintenanceCoordinator(
        SimpleNamespace(repository=repository),
        family=None,
        dependencies=dependency,
    )
    changes = [
        {
            "fact_kind": "fact_quality",
            "creation_ref": f"quality:{i}",
            "operation": "upsert",
            "reason": "合成精确核验",
            "fields": {"quality_kind": "coverage" if i == complete_index else "source"},
        }
        for i in range(2)
    ]
    payload = EducationBatchInput.model_validate_json(
        json.dumps(
            changes[0] | {"additional_changes": changes[1:]},
        )
    )
    resolved = coordinator.resolve_change(
        actor=actor(),
        payload=payload,
        request_id=UUID(int=10),
        effective_at=AT,
    )
    expected_version = 3 if complete_index is None else 4
    assert resolved.change_set.schema_version == expected_version
    assert {c.schema_version for c in resolved.change_set.changes} == {expected_version}
    assert len(resolved.change_set.changes) == 2
    for i, change in enumerate(resolved.change_set.changes):
        assert change.source_key == f"quality:{i}"
        has_basis = getattr(change.content, "coverage_basis", None) is not None
        assert has_basis == (i == complete_index)
    assert coordinator.canonical_version == 3
    before = len(checked)
    resolved.summary_revalidate(actor())
    assert len(checked) > before
