"""Pure review of original F7 scope coordinates; never dispatch governance."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from scripts.f7_engineering import runtime_configuration as f7

SCHOOL = UUID("c1137be2-0734-4b69-8937-37dbd741f141")
ASSIGNMENT = UUID("d69b992d-d0e5-411e-91bd-a9acc586c659")
TEACHER_ASSIGNMENT = UUID("c6887625-5d0e-45c4-8625-e7c8c553a451")
T0 = datetime(2026, 9, 24, 8, 0, tzinfo=UTC)


def existing_versions():
    return {key: 1 for key in f7.REQUIRED_EXISTING_SOURCES}


def teacher_versions():
    return {key: 1 for key in f7.TEACHER_REQUIRED_EXISTING_SOURCES}


def test_teacher_dependencies_are_two_exact_f3_read_rows_and_leave_old_matrix_unchanged():
    versions = {key: 1 for key in f7.TEACHER_DEPENDENCY_READ_SOURCES}
    rows = f7.teacher_dependency_rows(school_id=SCHOOL, current_versions=versions)
    assert len(rows) == 2
    assert {(r.family, r.fact_kind, r.action, r.object_keys, r.field_names) for r in rows} == {
        ("f3", "person", "read", ("f3:person:teacher-parent",), ("display_name",)),
        ("f3", "assignment", "read", ("f3:assignment:teacher-parent",),
         ("person_source_key",)),
    }
    assert all(not r.creation_keys for r in rows)
    assert len(f7.teacher_read_rows(school_id=SCHOOL, current_versions=teacher_versions())) == 13
    for invalid in ({}, versions | {"f3:person:other": 1}, versions | {next(iter(versions)): 0}):
        with pytest.raises(ValueError):
            f7.teacher_dependency_rows(school_id=SCHOOL, current_versions=invalid)


def test_teacher_dependency_overlay_requires_exact_teacher_proof_and_changes_only_two_targets():
    rows = f7.teacher_dependency_rows(
        school_id=SCHOOL,
        current_versions={key: 1 for key in f7.TEACHER_DEPENDENCY_READ_SOURCES},
    )
    before = _configuration()
    expected_operations = {"school_facts.f3.person.read", "school_facts.f3.assignment.read"}
    old_teacher = [
        {"assignment_id": str(TEACHER_ASSIGNMENT), "operation_code": operation,
         "scope_id": str(UUID(int=400 + i))}
        for i, operation in enumerate(sorted(expected_operations))
    ]
    before["family"]["scope_bindings"].extend(deepcopy(old_teacher))
    before["education"]["dependency_family"]["scope_bindings"].extend(deepcopy(old_teacher))
    original = deepcopy(before)
    proofs = tuple({**p, "assignment_id": str(TEACHER_ASSIGNMENT)} for p in _grants(rows))
    common = dict(original=before, school_id=SCHOOL, assignment_id=TEACHER_ASSIGNMENT,
                  rows=rows, grant_proofs=proofs, grant_epoch=T0,
                  grant_valid_to=T0 + timedelta(days=7))
    after = f7.overlay_receipt_bound_scopes(**common)
    assert before == original
    for target in (after["family"], after["education"]["dependency_family"]):
        replaced = [b for b in target["scope_bindings"]
                    if b["assignment_id"] == str(TEACHER_ASSIGNMENT)]
        assert {b["operation_code"] for b in replaced} == expected_operations
        assert {b["scope_id"] for b in replaced} == {p["scope_id"] for p in proofs}
    expected = deepcopy(before)
    expected["family"]["scope_bindings"] = after["family"]["scope_bindings"]
    expected["education"]["dependency_family"]["scope_bindings"] = (
        after["education"]["dependency_family"]["scope_bindings"]
    )
    assert after == expected
    for changed in (
        {"assignment_id": ASSIGNMENT},
        {"grant_valid_to": T0 + timedelta(days=8)},
        {"grant_proofs": proofs[:-1]},
        {"rows": (rows[0].model_copy(update={"field_names": ("display_name", "business_code")}),
                  rows[1])},
        {"rows": (rows[0].model_copy(update={"object_keys": ("f3:person:other",)}), rows[1])},
    ):
        with pytest.raises(ValueError):
            f7.overlay_receipt_bound_scopes(**(common | changed))


def test_teacher_read_matrix_is_exact_self_teaching_dependencies_without_maintenance():
    rows = f7.teacher_read_rows(school_id=SCHOOL, current_versions=teacher_versions())
    coordinates = {(row.family, row.fact_kind, row.action): row for row in rows}
    assert len(rows) == len(coordinates) == 13
    assert all(row.action == "read" and not row.creation_keys for row in rows)
    assert all(row.finite() is row for row in rows)
    assert coordinates[("f5", "teaching_assignment", "read")].object_keys == (
        "f5:teaching_assignment:culture",
        "f5:teaching_assignment:english",
    )
    assert coordinates[("f2", "person", "read")].object_keys == (
        "f3:person:teacher-parent",
    )
    assert coordinates[("f4", "education_class", "read")].object_keys == (
        "f4:class:a-2023",
    )
    assert coordinates[("f5", "teaching_subject", "read")].object_keys == (
        "f5:teaching_subject:english",
        "f5:teaching_subject:humanities",
        "f5:teaching_subject:math",
    )
    assert not any(
        row.fact_kind in {"student", "guardian_relationship", "fact_quality"}
        for row in rows
    )


def test_teacher_matrix_refuses_source_drift_and_maintainer_cannot_reuse_teacher_receipts():
    versions = teacher_versions()
    versions.pop("f5:teaching_assignment:english")
    with pytest.raises(ValueError):
        f7.teacher_read_rows(school_id=SCHOOL, current_versions=versions)
    rows = f7.first_phase_rows(school_id=SCHOOL, current_versions=existing_versions())
    with pytest.raises(ValueError, match="assignment"):
        f7.overlay_receipt_bound_scopes(
            original=_configuration(), school_id=SCHOOL, assignment_id=TEACHER_ASSIGNMENT,
            rows=rows, grant_proofs=tuple(
                {**proof, "assignment_id": str(TEACHER_ASSIGNMENT)} for proof in _grants(rows)
            ), grant_epoch=T0, grant_valid_to=T0 + timedelta(days=7),
        )


def test_teacher_overlay_requires_own_formal_receipts_and_keeps_maintainer_and_f6_unchanged():
    rows = f7.teacher_read_rows(school_id=SCHOOL, current_versions=teacher_versions())
    before = _configuration()
    grants = tuple(
        {**proof, "assignment_id": str(TEACHER_ASSIGNMENT)} for proof in _grants(rows)
    )
    with pytest.raises(ValueError, match="Missing formal"):
        f7.overlay_receipt_bound_scopes(
            original=before, school_id=SCHOOL, assignment_id=TEACHER_ASSIGNMENT,
            rows=rows, grant_proofs=tuple({**proof, "scope_sha256": "0" * 64} for proof in grants),
            grant_epoch=T0, grant_valid_to=T0 + timedelta(days=7),
        )
    with pytest.raises(ValueError, match="assignment"):
        f7.overlay_receipt_bound_scopes(
            original=before, school_id=SCHOOL, assignment_id=ASSIGNMENT,
            rows=rows, grant_proofs=_grants(rows),
            grant_epoch=T0, grant_valid_to=T0 + timedelta(days=7),
        )
    after = f7.overlay_receipt_bound_scopes(
        original=before, school_id=SCHOOL, assignment_id=TEACHER_ASSIGNMENT,
        rows=rows, grant_proofs=grants,
        grant_epoch=T0, grant_valid_to=T0 + timedelta(days=7),
    )
    assert before == _configuration()
    assert after["residence"] == before["residence"]
    assert after["directory"]["scope_bindings"][:2] == before["directory"]["scope_bindings"]
    assert all(
        binding["assignment_id"] == str(TEACHER_ASSIGNMENT)
        for binding in after["education"]["scope_bindings"]
    )


def test_first_phase_has_exact_q6_q2_dependencies_and_three_new_quality_slots():
    rows = f7.first_phase_rows(
        school_id=SCHOOL, current_versions=existing_versions()
    )
    coordinates = {
        (row.family, row.fact_kind, row.action): row for row in rows
    }
    assert len(coordinates) == len(rows) == 9
    assert coordinates[("f2", "fact_quality", "maintain")].creation_keys == (
        "f7:fact_quality:class_a_teacher",
        "f7:fact_quality:grade_group_a_2026",
        "f7:fact_quality:student_a_status",
    )
    assert coordinates[("f4", "education_grade_group", "read")].object_keys == (
        "f4:grade-group:2026:junior:2",
    )
    assert coordinates[("f4", "education_grade_group", "read")].field_names == (
        "academic_year", "campus_source_key", "grade_source_key",
    )
    assert coordinates[("f4", "education_grade_group", "maintain")].field_names == (
        "grade_source_key",
    )
    assert not any(
        row.fact_kind in {"education_calendar_rule", "location"}
        for row in rows
    )
    assert coordinates[("f3", "student", "maintain")].field_names == ("status",)
    assert ("f3", "student", "read") not in coordinates
    assert coordinates[("f4", "class_teacher_assignment", "maintain")].field_names == (
        "assignment_source_key",
    )
    assert coordinates[("f4", "class_teacher_assignment", "read")].object_keys == (
        "f4:teacher:a-2023:current",
    )
    assert coordinates[("f2", "official_contact", "read")].object_keys == (
        "f4:official_contact:a",
    )
    assert not any(
        key.startswith("f7:fact_quality:residence")
        for row in rows
        for key in row.creation_keys
    )


@pytest.mark.parametrize(
    "mutation,school",
    [
        ("missing", SCHOOL),
        ("extra", SCHOOL),
        ("zero", SCHOOL),
        ("unchanged", UUID(int=9)),
    ],
)
def test_first_phase_refuses_unpinned_or_unpublished_sources(mutation, school):
    versions = existing_versions()
    if mutation == "missing":
        versions.pop("f3:student:a")
    elif mutation == "extra":
        versions["another"] = 1
    elif mutation == "zero":
        versions["f3:student:a"] = 0
    with pytest.raises(ValueError):
        f7.first_phase_rows(school_id=school, current_versions=versions)


def test_q2_pinned_sources_include_existing_grade_group_calendar_and_campus():
    assert {
        "f4:grade-group:2026:junior:2",
        "f4:calendar:junior",
        "f4:location:a",
    } <= set(f7.REQUIRED_EXISTING_SOURCES)
    assert f7.Q2_INHERITED_READS == (
        ("f2", "location", "f4:location:a"),
        ("f4", "education_calendar_rule", "f4:calendar:junior"),
    )


def test_second_phase_requires_formal_publication_and_lists_old_f4_reads_explicitly():
    with pytest.raises(ValueError):
        f7.second_phase_rows(
            school_id=SCHOOL,
            published_quality_versions={"f7:fact_quality:student_a_status": 1},
            existing_f4_quality_versions={
                "f4:fact_quality:a-reserve": 1,
                "f4:fact_quality:b-future": 1,
            },
        )
    rows = f7.second_phase_rows(
        school_id=SCHOOL,
        published_quality_versions={key: 1 for key in f7.NEW_QUALITY_SOURCES},
        existing_f4_quality_versions={key: 1 for key in f7.RETAINED_F4_QUALITY_SOURCES},
    )
    read = next(row for row in rows if row.action == "read")
    assert read.object_keys == tuple(
        sorted((*f7.NEW_QUALITY_SOURCES, *f7.RETAINED_F4_QUALITY_SOURCES))
    )
    assert read.approved_dependency_sources == read.object_keys
    maintain = next(row for row in rows if row.action == "maintain")
    assert maintain.object_keys == tuple(sorted(f7.NEW_QUALITY_SOURCES))
    assert not maintain.creation_keys


def _configuration():
    old_quality_read = str(UUID(int=20))
    old_quality_maintain = str(UUID(int=21))
    retained_f6 = str(UUID(int=22))
    existing_student_read = str(UUID(int=23))
    directory = {
        "scope_bindings": [
            {
                "assignment_id": str(ASSIGNMENT),
                "operation_code": "school_facts.f2.fact_quality.read",
                "scope_id": old_quality_read,
            },
            {
                "assignment_id": str(ASSIGNMENT),
                "operation_code": "school_facts.f2.fact_quality.maintain",
                "scope_id": old_quality_maintain,
            },
        ],
        "source_catalog": {"fact_quality": list(f7.RETAINED_F4_QUALITY_SOURCES)},
        "creation_catalog": {},
    }
    return {
        "directory": deepcopy(directory),
        "family": {"scope_bindings": [{
            "assignment_id": str(ASSIGNMENT),
            "operation_code": "school_facts.f3.student.read",
            "scope_id": existing_student_read,
        }]},
        "education": {
            "dependency_directory": deepcopy(directory),
            "dependency_family": {"scope_bindings": []},
            "scope_bindings": [],
        },
        "residence": {
            "dependency_education": {
                "dependency_directory": {
                    "scope_bindings": [
                        {
                            "assignment_id": str(ASSIGNMENT),
                            "operation_code": "school_facts.f2.fact_quality.read",
                            "scope_id": retained_f6,
                        }
                    ],
                    "source_catalog": {},
                    "creation_catalog": {},
                }
            }
        },
    }


def _grants(rows):
    return tuple(
        {
            "assignment_id": str(ASSIGNMENT),
            "scope_owner": f"school_facts.{row.family}",
            "scope_sha256": row.scope(SCHOOL).document_sha256,
            "duty_code": row.duty_code,
            "scope_id": str(UUID(int=index + 100)),
            "grant_id": str(UUID(int=index + 200)),
        }
        for index, row in enumerate(rows)
    )


def test_business_sample_matrix_never_renews_old_course_or_other_organization_objects():
    create = f7.business_sample_rows(school_id=SCHOOL, published_versions={})
    assert {(r.family, r.fact_kind, r.action) for r in create} == {
        ("f2", "organization", "maintain"), ("f4", "education_stage", "maintain"),
    }
    assert all(not r.object_keys and len(r.creation_keys) == 1 for r in create)
    published = {key: 1 for key in (
        *f7.BUSINESS_SAMPLE_KEYS, "f4:organization:a", "f4:stage:junior",
    )}
    rows = f7.business_sample_rows(school_id=SCHOOL, published_versions=published)
    assert len(rows) == 4
    assert all(not row.creation_keys for row in rows)
    org = next(r for r in rows if (r.fact_kind, r.action) == ("organization", "read"))
    assert set(org.object_keys) == {"f4:organization:a", f7.BUSINESS_SAMPLE_KEYS[0]}
    stage = next(r for r in rows if (r.fact_kind, r.action) == ("education_stage", "read"))
    assert set(stage.object_keys) == {"f4:stage:junior", f7.BUSINESS_SAMPLE_KEYS[1]}
    assert set(stage.field_names) == {
        "display_name", "business_code", "valid_to", "basis_reference",
    }
    maintain = next(r for r in rows if (r.fact_kind, r.action) == ("education_stage", "maintain"))
    assert maintain.object_keys == (f7.BUSINESS_SAMPLE_KEYS[1],)
    assert set(maintain.field_names) == set(stage.field_names)
    assert all("f5:" not in key for r in rows for key in r.object_keys)
    with pytest.raises(ValueError):
        f7.business_sample_rows(
            school_id=SCHOOL, published_versions={f7.BUSINESS_SAMPLE_KEYS[0]: 1}
        )


def test_reviewed_stage_creation_has_complete_projection_without_repeating_organization():
    rows = f7.reviewed_stage_creation_rows(school_id=SCHOOL)
    assert len(rows) == 1
    row = rows[0]
    assert row.key == "f7_sample_stage_create_reviewed"
    assert row.creation_keys == (f7.BUSINESS_SAMPLE_KEYS[1],)
    assert not row.object_keys
    assert set(row.field_names) == {"display_name", "business_code", "valid_to", "basis_reference"}
    original = f7.business_sample_rows(school_id=SCHOOL, published_versions={})
    assert original[1].key == "f7_sample_stage_create"
    assert original[1].field_names == ("display_name",)
    before = _configuration()
    after = f7.overlay_receipt_bound_scopes(
        original=before, school_id=SCHOOL, assignment_id=ASSIGNMENT, rows=rows,
        grant_proofs=_grants(rows), grant_epoch=T0, grant_valid_to=T0 + timedelta(days=7),
    )
    assert after["directory"] == before["directory"]
    assert after["family"] == before["family"]
    assert after["residence"] == before["residence"]
    assert after["education"]["creation_catalog"]["education_stage"] == {
        f7.BUSINESS_SAMPLE_KEYS[1]: f7.BUSINESS_SAMPLE_LABELS[f7.BUSINESS_SAMPLE_KEYS[1]],
    }


def test_business_sample_overlay_keeps_existing_course_scope_and_cleans_only_new_creation_slots():
    before = _configuration()
    course_binding = {"assignment_id": str(ASSIGNMENT),
                      "operation_code": "school_facts.f5.teaching_course.read",
                      "scope_id": str(UUID(int=891))}
    before["education"]["scope_bindings"].append(course_binding)
    dependencies = f7.dependency_read_rows(
        school_id=SCHOOL, current_versions={"f4:stage:junior": 1}
    )
    before = f7.overlay_receipt_bound_scopes(
        original=before, school_id=SCHOOL, assignment_id=ASSIGNMENT, rows=dependencies,
        grant_proofs=_grants(dependencies), grant_epoch=T0, grant_valid_to=T0 + timedelta(days=7),
    )
    create = f7.business_sample_rows(school_id=SCHOOL, published_versions={})
    created = f7.overlay_receipt_bound_scopes(
        original=before, school_id=SCHOOL, assignment_id=ASSIGNMENT, rows=create,
        grant_proofs=_grants(create), grant_epoch=T0, grant_valid_to=T0 + timedelta(days=7),
    )
    assert created["education"]["scope_bindings"][0] == course_binding
    assert created["directory"]["creation_catalog"]["organization"] == {
        f7.BUSINESS_SAMPLE_KEYS[0]: "F7合成纠错演示组织",
    }
    assert created["education"]["creation_catalog"]["education_stage"] == {
        f7.BUSINESS_SAMPLE_KEYS[1]: "F7合成纠错演示学段",
    }
    rows = f7.business_sample_rows(school_id=SCHOOL, published_versions={
        key: 1 for key in (*f7.BUSINESS_SAMPLE_KEYS, "f4:organization:a", "f4:stage:junior")
    })
    after = f7.overlay_receipt_bound_scopes(
        original=created, school_id=SCHOOL, assignment_id=ASSIGNMENT, rows=rows,
        grant_proofs=_grants(rows), grant_epoch=T0, grant_valid_to=T0 + timedelta(days=7),
    )
    assert after["education"]["scope_bindings"][0] == course_binding
    stage_bindings = [binding for binding in after["education"]["scope_bindings"]
                      if binding["operation_code"] == "school_facts.f4.education_stage.read"]
    assert len(stage_bindings) == 1
    stage_row = next(row for row in rows
                     if (row.fact_kind, row.action) == ("education_stage", "read"))
    stage_proof = next(proof for proof in _grants(rows)
                       if proof["duty_code"] == stage_row.duty_code)
    assert stage_bindings[0]["scope_id"] == stage_proof["scope_id"]
    assert set(stage_row.object_keys) == {"f4:stage:junior", f7.BUSINESS_SAMPLE_KEYS[1]}
    assert "education_stage" not in after["education"]["creation_catalog"]
    assert "organization" not in after["directory"]["creation_catalog"]
    assert after["residence"] == before["residence"]


def test_dependency_is_only_current_junior_stage_name_and_overlay_requires_formal_proof():
    rows = f7.dependency_read_rows(
        school_id=SCHOOL, current_versions={"f4:stage:junior": 1}
    )
    assert len(rows) == 1
    row = rows[0]
    assert (row.family, row.fact_kind, row.action) == ("f4", "education_stage", "read")
    assert row.object_keys == row.approved_dependency_sources == ("f4:stage:junior",)
    assert row.field_names == ("display_name",) and not row.creation_keys
    before = _configuration()
    after = f7.overlay_receipt_bound_scopes(
        original=before, school_id=SCHOOL, assignment_id=ASSIGNMENT, rows=rows,
        grant_proofs=_grants(rows), grant_epoch=T0, grant_valid_to=T0 + timedelta(days=7),
    )
    assert after["residence"] == before["residence"]
    assert after["family"] == before["family"]
    added = after["education"]["scope_bindings"][-1]
    assert added["operation_code"] == "school_facts.f4.education_stage.read"
    for invalid in ({}, {"f4:stage:junior": 0}, {"f4:stage:junior": True},
                    {"f4:stage:junior": 1, "f4:stage:senior": 1}):
        with pytest.raises(ValueError, match="formal source versions"):
            f7.dependency_read_rows(school_id=SCHOOL, current_versions=invalid)
    with pytest.raises(ValueError, match="formally reviewed"):
        f7.overlay_receipt_bound_scopes(
            original=before, school_id=SCHOOL, assignment_id=ASSIGNMENT, rows=rows,
            grant_proofs=(), grant_epoch=T0, grant_valid_to=T0 + timedelta(days=7),
        )


def test_configuration_replacement_preserves_f6_and_requires_explicit_old_read_renewal():
    before = _configuration()
    rows = f7.second_phase_rows(
        school_id=SCHOOL,
        published_quality_versions={key: 1 for key in f7.NEW_QUALITY_SOURCES},
        existing_f4_quality_versions={key: 1 for key in f7.RETAINED_F4_QUALITY_SOURCES},
    )
    grants = _grants(rows)
    with pytest.raises(ValueError, match="explicit renewal"):
        f7.overlay_receipt_bound_scopes(
            original=before,
            school_id=SCHOOL,
            assignment_id=ASSIGNMENT,
            rows=rows,
            grant_proofs=grants,
            grant_epoch=T0,
            grant_valid_to=T0 + timedelta(days=7),
            currently_effective_old_read_keys=f7.RETAINED_F4_QUALITY_SOURCES,
            explicitly_reviewed_renewal_keys=(),
        )
    after = f7.overlay_receipt_bound_scopes(
        original=before,
        school_id=SCHOOL,
        assignment_id=ASSIGNMENT,
        rows=rows,
        grant_proofs=grants,
        grant_epoch=T0,
        grant_valid_to=T0 + timedelta(days=7),
        currently_effective_old_read_keys=f7.RETAINED_F4_QUALITY_SOURCES,
        explicitly_reviewed_renewal_keys=f7.RETAINED_F4_QUALITY_SOURCES,
    )
    assert before == _configuration()
    assert (
        after["residence"]["dependency_education"]["dependency_directory"]
        == before["residence"]["dependency_education"]["dependency_directory"]
    )
    assert (
        after["directory"]["scope_bindings"][0]["scope_id"]
        == after["education"]["dependency_directory"]["scope_bindings"][0]["scope_id"]
    )
    assert set(after["directory"]["source_catalog"]["fact_quality"]) == set(
        (*f7.RETAINED_F4_QUALITY_SOURCES, *f7.NEW_QUALITY_SOURCES)
    )


def test_first_phase_overlay_keeps_eight_object_student_read_and_old_quality_read():
    before = _configuration()
    rows = f7.first_phase_rows(school_id=SCHOOL, current_versions=existing_versions())
    after = f7.overlay_receipt_bound_scopes(
        original=before,
        school_id=SCHOOL,
        assignment_id=ASSIGNMENT,
        rows=rows,
        grant_proofs=_grants(rows),
        grant_epoch=T0,
        grant_valid_to=T0 + timedelta(days=7),
    )
    assert after["directory"]["scope_bindings"][0] == before["directory"]["scope_bindings"][0]
    assert after["family"]["scope_bindings"] == [
        before["family"]["scope_bindings"][0],
        {
            "assignment_id": str(ASSIGNMENT),
            "operation_code": "school_facts.f3.student.maintain",
            "scope_id": str(UUID(int=101)),
        },
    ]
    assert after["residence"] == before["residence"]
    assert set(after["directory"]["creation_catalog"]["fact_quality"]) == set(
        f7.NEW_QUALITY_SOURCES
    )


def test_overlay_rejects_unreviewed_row_false_proof_and_rolling_deadline():
    rows = f7.first_phase_rows(school_id=SCHOOL, current_versions=existing_versions())
    kwargs = {
        "original": _configuration(),
        "school_id": SCHOOL,
        "assignment_id": ASSIGNMENT,
        "rows": rows,
        "grant_proofs": _grants(rows),
        "grant_epoch": T0,
        "grant_valid_to": T0 + timedelta(days=7),
    }
    with pytest.raises(ValueError, match="reviewed F7 phase/sample"):
        f7.overlay_receipt_bound_scopes(**{**kwargs, "rows": rows[:-1]})
    wrong_proof = tuple({**proof, "scope_sha256": "0" * 64} for proof in _grants(rows))
    with pytest.raises(ValueError, match="Missing formal"):
        f7.overlay_receipt_bound_scopes(**{**kwargs, "grant_proofs": wrong_proof})
    with pytest.raises(ValueError, match="fixed original-school"):
        f7.overlay_receipt_bound_scopes(
            **{**kwargs, "grant_valid_to": T0 + timedelta(days=8)}
        )
