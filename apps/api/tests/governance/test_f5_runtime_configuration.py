import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from scripts.f5_engineering import runtime_configuration as helper
from scripts.f5_engineering.prepare_samples import build_plan


@pytest.fixture
def evidence():
    # Synthetic exact-scope evidence; no live service or private runtime files.
    from hashlib import sha256

    objects = {
        "assignment": [
            "f3:assignment:teacher-parent",
            "f3:assignment:verifier",
            "f4:assignment:a:current",
            "f4:assignment:a:previous",
            "f4:assignment:b:current",
            "f4:assignment:b:previous",
        ],
        "class_placement": [
            "f4:placement:a-2023",
            "f4:placement:a-2028",
            "f4:placement:a-reserve",
            "f4:placement:b-2023",
            "f4:placement:b-2028",
            "f4:placement:b-future",
        ],
        "class_position_definition": [
            "f4:position:a-2023:deputy",
            "f4:position:a-2028:deputy",
            "f4:position:a-reserve:deputy",
            "f4:position:b-2023:deputy",
            "f4:position:b-2028:deputy",
            "f4:position:b-future:deputy",
        ],
        "education_calendar_rule": [
            "f4:calendar:junior",
            "f4:calendar:primary",
            "f4:calendar:senior",
        ],
        "education_class": [
            "f4:class:a-2023",
            "f4:class:a-2028",
            "f4:class:a-reserve",
            "f4:class:b-2023",
            "f4:class:b-2028",
            "f4:class:b-future",
        ],
        "education_grade": [
            "f4:grade:junior:1",
            "f4:grade:junior:2",
            "f4:grade:junior:3",
            "f4:grade:primary:1",
            "f4:grade:primary:2",
            "f4:grade:primary:3",
            "f4:grade:primary:4",
            "f4:grade:primary:5",
            "f4:grade:primary:6",
            "f4:grade:senior:1",
            "f4:grade:senior:2",
            "f4:grade:senior:3",
        ],
        "education_grade_group": ["f4:grade-group:2026:junior:2"],
        "education_stage": ["f4:stage:junior", "f4:stage:primary", "f4:stage:senior"],
        "education_term": ["f4:term:2026:first"],
        "location": [
            "f4:location:a",
            "f4:location:a:building",
            "f4:location:a:floor",
            "f4:location:a:room",
            "f4:location:b",
            "f4:location:b:building",
            "f4:location:b:floor",
            "f4:location:b:room",
        ],
        "official_contact": ["f4:official_contact:a", "f4:official_contact:b"],
        "organization": ["f4:organization:a", "f4:organization:b"],
        "person": [
            "f3:person:caregiver",
            "f3:person:guardian",
            "f3:person:second-guardian",
            "f3:person:student-a",
            "f3:person:student-b",
            "f3:person:student-c",
            "f3:person:teacher-parent",
            "f3:person:unverified-contact",
            "f3:person:verifier",
            "f4:person:a:current",
            "f4:person:a:previous",
            "f4:person:b:current",
            "f4:person:b:previous",
            "f4:person:student:d",
            "f4:person:student:e",
        ],
        "student": ["f3:student:a", "f3:student:b", "f3:student:c", "f4:student:d", "f4:student:e"],
        "student_class_membership": [
            "f4:membership:a",
            "f4:membership:b",
            "f4:membership:c-future",
            "f4:membership:d",
            "f4:membership:e",
        ],
        "student_class_position": [
            "f4:position-holder:a",
            "f4:position-holder:d",
            "f4:position-holder:e",
        ],
        "student_school_relationship": [
            "f3:student-school:a",
            "f3:student-school:b",
            "f3:student-school:c",
            "f4:student_school_relationship:d",
            "f4:student_school_relationship:e",
        ],
    }
    documents = []
    for kind, source_keys in objects.items():
        family = (
            "f4"
            if kind in helper.F4_FACT_FIELDS
            else "f3"
            if kind in helper.F3_FACT_FIELDS
            else "f2"
        )
        fields = helper.F4_FACT_FIELDS.get(kind, helper.F3_FACT_FIELDS.get(kind, ()))
        actions = (
            ("reader", "maintainer")
            if kind in {"student_class_membership", "student_class_position"}
            else ("reader",)
        )
        for action in actions:
            document = {
                "school_id": str(helper.SCHOOL),
                "fact_kind": kind,
                "object_keys": source_keys,
                "creation_keys": [],
            }
            if family != "f2":
                document["field_names"] = sorted(fields)
            documents.append(
                {
                    "assignment_key": helper.OPERATORS[0],
                    "assignment_id": str(UUID(int=1)),
                    "scope_id": str(UUID(int=1000 + len(documents))),
                    "valid_from": "2026-09-14T00:00:00+00:00",
                    "planned_valid_to": "2026-09-20T00:00:00+00:00",
                    "schema_owner": "school_facts." + family,
                    "duty_code": f"{family}_{kind}_{action}",
                    "reference_document": document,
                    "reference_sha256": sha256(
                        json.dumps(
                            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                        ).encode()
                    ).hexdigest(),
                }
            )
    return {
        "snapshot": {"school_id": str(helper.SCHOOL)},
        "observed_at": "2026-09-15T00:00:00+00:00",
        "active_dependencies": documents,
        "student_person_dependencies": [
            {
                "school_id": str(helper.SCHOOL),
                "student_source_key": student,
                "person_source_key": person,
            }
            for student, person in (
                ("f3:student:a", "f3:person:student-a"),
                ("f3:student:b", "f3:person:student-b"),
                ("f3:student:c", "f3:person:student-c"),
                ("f4:student:d", "f4:person:student:d"),
                ("f4:student:e", "f4:person:student:e"),
            )
        ],
    }


def rows(plan, evidence, published=()):
    return helper.fixed_matrix(plan, evidence, published=published)


def rehash(row):
    from hashlib import sha256

    row["reference_sha256"] = sha256(
        json.dumps(
            row["reference_document"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def test_student_person_dependencies_preserve_existing_and_published_reads(evidence):
    plan = build_plan()
    published = [
        (c["fact_kind"], c["source_key"]) for b in plan["batches"].values() for c in b["changes"]
    ]
    before = deepcopy(evidence)
    result = rows(plan, evidence, published)
    wrapper = next(w for w in result if w["row"]["key"] == "f5_maintainer_f3_person_read")
    person_read = wrapper["row"]
    dependencies = {d["person_source_key"] for d in evidence["student_person_dependencies"]}
    assert (
        set(person_read["object_keys"])
        == {
            "f3:person:teacher-parent",
            "f4:person:a:current",
            "f4:person:b:current",
        }
        | helper.all_inputs(plan)["person"]
    )
    assert {key for b in wrapper["read_dependencies"] for key in b["object_keys"]} == dependencies
    assert set(person_read["field_names"]) == helper.F3_FACT_FIELDS["person"]
    assert all(
        not dependencies.intersection(w["row"]["object_keys"] + w["row"]["creation_keys"])
        for w in result
        if w["row"]["action"] == "maintain" or w["row"]["family"] == "f2"
    )
    assert evidence == before


def test_student_person_dependency_uses_verified_relation_not_key_convention(evidence):
    mapped = "opaque:person:independent-student"
    previous = evidence["student_person_dependencies"][0]["person_source_key"]
    evidence["student_person_dependencies"][0]["person_source_key"] = mapped
    authority = next(
        r for r in evidence["active_dependencies"] if r["duty_code"] == "f3_person_reader"
    )
    authority["reference_document"]["object_keys"].remove(previous)
    authority["reference_document"]["object_keys"].append(mapped)
    rehash(authority)

    result = rows(build_plan(), evidence)

    wrapper = next(w for w in result if w["row"]["key"] == "f5_maintainer_f3_person_read")
    selected = {key for b in wrapper["read_dependencies"] for key in b["object_keys"]}
    assert mapped in selected
    assert previous not in selected


@pytest.mark.parametrize("invalid", ("missing", "duplicate", "other_school", "unapproved"))
def test_unverified_student_person_dependency_cannot_generate_replacement(evidence, invalid):
    dependencies = evidence["student_person_dependencies"]
    if invalid == "missing":
        dependencies.pop()
    elif invalid == "duplicate":
        dependencies.append(deepcopy(dependencies[0]))
    elif invalid == "other_school":
        dependencies[0]["school_id"] = str(UUID(int=99))
    else:
        dependencies[0]["person_source_key"] = "opaque:person:unapproved"

    with pytest.raises(ValueError, match="[Dd]ependency"):
        rows(build_plan(), evidence)


def test_dependency_person_fields_preserve_narrow_original_read(evidence):
    authority = next(
        r for r in evidence["active_dependencies"] if r["duty_code"] == "f3_person_reader"
    )
    narrow = deepcopy(authority)
    narrow["reference_document"]["object_keys"] = ["f3:person:student-a"]
    narrow["reference_document"]["field_names"] = ["display_name", "lifecycle_status"]
    narrow["scope_id"] = str(UUID(int=2000))
    rehash(narrow)
    authority["reference_document"]["object_keys"].remove("f3:person:student-a")
    rehash(authority)
    evidence["active_dependencies"].append(narrow)

    result = rows(build_plan(), evidence)
    wrapper = next(w for w in result if w["row"]["key"] == "f5_maintainer_f3_person_read")
    selected = next(
        b for b in wrapper["read_dependencies"] if "f3:person:student-a" in b["object_keys"]
    )
    assert selected["scope_id"] == narrow["scope_id"]
    assert set(wrapper["row"]["field_names"]) == helper.F3_FACT_FIELDS["person"]


@pytest.mark.parametrize("failure", ("expired", "not_yet_valid", "missing", "no_name"))
def test_inherited_person_scope_requires_current_necessary_read_evidence(evidence, failure):
    authority = next(
        r for r in evidence["active_dependencies"] if r["duty_code"] == "f3_person_reader"
    )
    if failure == "expired":
        authority["planned_valid_to"] = evidence["observed_at"]
    elif failure == "not_yet_valid":
        authority["valid_from"] = "2026-09-16T00:00:00+00:00"
    elif failure == "missing":
        evidence["active_dependencies"].remove(authority)
    else:
        authority["reference_document"]["field_names"].remove("display_name")
        rehash(authority)
    with pytest.raises(ValueError, match="[Dd]ependency"):
        rows(build_plan(), evidence)


def test_dependency_configuration_keeps_primary_binding_and_unrelated_objects(evidence):
    primary = {
        "assignment_id": str(UUID(int=1)),
        "operation_code": "school_facts.f3.person.read",
        "scope_id": str(UUID(int=91)),
    }
    unrelated = {**primary, "scope_id": str(UUID(int=92)), "object_keys": ["person:unrelated"]}
    original = {
        "family": {
            "scope_bindings": [primary],
            "read_dependency_bindings": [unrelated],
            "source_catalog": {"person": ["person:new"]},
        },
        "education": {"dependency_family": {"scope_bindings": [primary], "source_catalog": {}}},
    }
    before = deepcopy(original)
    result = helper.configuration_with_student_person_dependencies(
        original=original, evidence=evidence, identities={helper.OPERATORS[0]: UUID(int=1)}
    )
    assert original == before
    assert result["family"]["scope_bindings"] == [primary]
    assert unrelated in result["family"]["read_dependency_bindings"]
    expected = {d["person_source_key"] for d in evidence["student_person_dependencies"]}
    assert set(result["family"]["source_catalog"]["person"]) == expected | {"person:new"}
    assert {
        key
        for b in result["education"]["dependency_family"]["read_dependency_bindings"]
        for key in b["object_keys"]
    } == expected


@pytest.mark.parametrize("failure", (None, "missing_evidence", "missing_binding"))
def test_receipt_replacement_requires_verified_inherited_dependencies(
    evidence, monkeypatch, failure
):
    wrapper = next(
        w for w in rows(build_plan(), evidence) if w["row"]["key"] == "f5_maintainer_f3_person_read"
    )
    row = helper.ScopeMatrixRow.model_validate(wrapper["row"])
    monkeypatch.setattr(
        helper,
        "governance_bindings",
        lambda **kwargs: [
            {
                "assignment_id": str(UUID(int=1)),
                "scope_owner": "school_facts.f3",
                "scope_sha256": row.scope(helper.SCHOOL).document_sha256,
                "duty_code": row.duty_code,
                "scope_id": str(UUID(int=91)),
            }
        ],
    )
    original = {
        "family": {"scope_bindings": [], "source_catalog": {}},
        "education": {
            "dependency_family": {"scope_bindings": [], "source_catalog": {}},
            "dependency_directory": {},
        },
    }
    if failure == "missing_binding":
        wrapper["read_dependencies"][0]["object_keys"].pop()
    now = datetime(2026, 9, 14, tzinfo=UTC)

    def project():
        return helper.configuration_from_receipts(
            original=original,
            matrix=[wrapper],
            identities={helper.OPERATORS[0]: UUID(int=1)},
            school_id=helper.SCHOOL,
            epoch=now,
            deadline=now + timedelta(days=7),
            receipts=[],
            labels={},
            dependency_evidence=None if failure == "missing_evidence" else evidence,
        )

    if failure:
        with pytest.raises(ValueError, match="dependency"):
            project()
    else:
        result = project()
        assert result["family"]["scope_bindings"][0]["scope_id"] == str(UUID(int=91))
        assert result["family"]["read_dependency_bindings"] == wrapper["read_dependencies"]


def test_initial_reads_do_not_contain_unpublished_roots(evidence):
    result = rows(build_plan(), evidence)
    assert not [
        k
        for w in result
        if w["row"]["action"] == "read"
        for k in w["row"]["object_keys"]
        if k.startswith("f5:")
    ]


def test_f2_staff_maintenance_excludes_students_and_teacher_is_read_only(evidence):
    plan = build_plan()
    published = [
        (c["fact_kind"], c["source_key"]) for b in plan["batches"].values() for c in b["changes"]
    ]
    result = rows(plan, evidence, published)
    for wrapper in result:
        row = wrapper["row"]
        if wrapper["actor_key"] == helper.OPERATORS[1]:
            assert row["action"] == "read"
            assert row["fact_kind"] not in {
                "student",
                "student_school_relationship",
                "student_class_membership",
                "teaching_group_member",
                "fact_quality",
            }
        if row["family"] == "f2" and row["fact_kind"] == "person":
            assert not any("student" in k for k in row["object_keys"] + row["creation_keys"])
    ssr = next(
        w["row"]
        for w in result
        if w["row"]["family"] == "f3"
        and w["row"]["fact_kind"] == "student_school_relationship"
        and w["row"]["action"] == "maintain"
    )
    assert set(ssr["object_keys"]) == {
        "f3:student-school:c",
        *(f"f5:student_school_relationship:{x}" for x in "fgh"),
    }


def test_tampered_evidence_and_missing_old_field_are_rejected(evidence):
    bad = deepcopy(evidence)
    target = next(r for r in bad["active_dependencies"] if r["schema_owner"] == "school_facts.f4")
    target["reference_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="evidence"):
        rows(build_plan(), bad)
    bad = deepcopy(evidence)
    from hashlib import sha256

    for r in bad["active_dependencies"]:
        if r["duty_code"] == "f4_student_class_membership_maintainer":
            r["reference_document"]["field_names"].remove("class_source_key")
            r["reference_sha256"] = sha256(
                json.dumps(
                    r["reference_document"],
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
    with pytest.raises(ValueError, match="fields"):
        rows(build_plan(), bad)


def test_no_receipt_cannot_generate_runtime_authority():
    now = datetime(2026, 9, 14, tzinfo=UTC)
    matrix = [
        {
            "actor_key": helper.OPERATORS[0],
            "row": {
                "key": "x",
                "family": "f5",
                "fact_kind": "teaching_subject",
                "action": "read",
                "object_keys": ["f5:teaching_subject:math"],
                "field_names": ["display_name"],
            },
        }
    ]
    with pytest.raises(ValueError, match="receipt"):
        helper.configuration_from_receipts(
            original={"education": {"dependency_directory": {}, "dependency_family": {}}},
            matrix=matrix,
            identities={helper.OPERATORS[0]: str(UUID(int=1))},
            school_id=helper.SCHOOL,
            epoch=now,
            deadline=now + timedelta(days=7),
            receipts=[],
            labels={},
        )


def test_published_staff_and_unpublished_quality_slot_are_exact(evidence):
    plan = build_plan()
    published = [
        (c["fact_kind"], c["source_key"]) for b in plan["batches"].values() for c in b["changes"]
    ]
    result = rows(plan, evidence, published)
    staff = next(w["row"] for w in result if w["row"]["key"] == "f5_maintainer_f2_person_read")
    assert "f5:person:campus-leader" in staff["object_keys"]
    assert not any("student" in k for k in staff["object_keys"])
    quality = next(
        w["row"] for w in result if w["row"]["key"] == "f5_maintainer_f2_fact_quality_maintain"
    )
    assert quality["creation_keys"] == ["f5:fact_quality:teaching-coverage"]
    assert not quality["object_keys"]


def test_receipt_projection_preserves_original_and_unrelated_catalog(monkeypatch):
    now = datetime(2026, 9, 14, tzinfo=UTC)
    row = helper.ScopeMatrixRow.model_validate_json(
        json.dumps(
            {
                "key": "x",
                "family": "f5",
                "fact_kind": "teaching_subject",
                "action": "maintain",
                "object_keys": ["f5:teaching_subject:math"],
                "creation_keys": ["f5:teaching_subject:new"],
                "field_names": ["display_name"],
            }
        )
    )
    actor = UUID(int=1)
    monkeypatch.setattr(
        helper,
        "governance_bindings",
        lambda **kwargs: [
            {
                "assignment_id": str(actor),
                "scope_owner": "school_facts.f5",
                "scope_sha256": row.scope(helper.SCHOOL).document_sha256,
                "duty_code": row.duty_code,
                "scope_id": str(UUID(int=2)),
            }
        ],
    )
    original = {
        "education": {
            "dependency_directory": {},
            "dependency_family": {},
            "scope_bindings": [],
            "creation_catalog": {
                "teaching_subject": {"old:slot": "Old", "f5:teaching_subject:math": "Math"}
            },
        }
    }
    before = deepcopy(original)
    result = helper.configuration_from_receipts(
        original=original,
        matrix=[{"actor_key": helper.OPERATORS[0], "row": row.model_dump(mode="json")}],
        identities={helper.OPERATORS[0]: actor},
        school_id=helper.SCHOOL,
        epoch=now,
        deadline=now + timedelta(days=7),
        receipts=[],
        labels={},
    )
    assert original == before
    assert set(result["education"]["creation_catalog"]["teaching_subject"]) == {
        "old:slot",
        "f5:teaching_subject:new",
    }
    assert result["education"]["scope_bindings"][0]["scope_id"] == str(UUID(int=2))
