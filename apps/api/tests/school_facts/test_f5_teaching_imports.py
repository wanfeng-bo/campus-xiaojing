from datetime import UTC, datetime
from importlib import import_module
from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.domains.students.education_change_planner import EducationFactSnapshot

AT = datetime(2026, 9, 1, tzinfo=UTC)
OLD = datetime(2026, 1, 1, tzinfo=UTC)


def module():
    return import_module("app.domains.students.teaching_excel")


def test_template_has_only_finite_kind_fields_and_omission_instructions():
    book = load_workbook(BytesIO(module().create_teaching_template("teaching_course")))
    headers = [c.value for c in book["teaching"][1]]
    assert "display_name" in headers and "source_key" in headers
    assert "student_source_key" not in headers
    assert "省略不删除" in str(book["说明"]["A1"].value)


def test_incremental_omission_preserves_existing_rows_and_reports_no_change():
    rows = (
        EducationFactSnapshot(
            fact_kind="teaching_course",
            source_key="course:one",
            version=2,
            effective_at=OLD,
            content={"display_name": "数学", "business_code": "M"},
        ),
    )
    plan = module().plan_teaching_import(
        "teaching_course",
        [{"source_key": "course:one", "display_name": "数学"}],
        rows,
        effective_at=AT,
    )
    assert not plan.changes and plan.unchanged_keys == ("course:one",)
    assert not module().plan_teaching_import("teaching_course", [], rows, effective_at=AT).changes


def test_complete_replacement_requires_explicit_selected_scope_and_ends_only_that_scope():
    rows = tuple(
        EducationFactSnapshot(
            fact_kind="teaching_course",
            source_key="course:" + k,
            version=1,
            effective_at=OLD,
            content={"display_name": k},
        )
        for k in ("one", "two")
    )
    with pytest.raises(ValueError):
        module().plan_teaching_import("teaching_course", [], rows, effective_at=AT, mode="replace")
    plan = module().plan_teaching_import(
        "teaching_course",
        [],
        rows,
        effective_at=AT,
        mode="replace",
        replacement_keys=frozenset({"course:one"}),
    )
    assert len(plan.changes) == 1 and plan.changes[0]["source_key"] == "course:one"


def test_empty_optional_cell_omits_but_explicit_clear_removes():
    row = EducationFactSnapshot(
        fact_kind="teaching_course",
        source_key="course:one",
        version=1,
        effective_at=OLD,
        content={"display_name": "数学", "business_code": "M"},
    )
    assert (
        not module()
        .plan_teaching_import(
            "teaching_course",
            [{"source_key": "course:one", "business_code": ""}],
            (row,),
            effective_at=AT,
        )
        .changes
    )
    plan = module().plan_teaching_import(
        "teaching_course",
        [{"source_key": "course:one", "business_code": "__CLEAR__"}],
        (row,),
        effective_at=AT,
    )
    assert (
        "business_code" not in plan.changes[0]["content"]
        or plan.changes[0]["content"]["business_code"] is None
    )


def test_duplicate_stable_keys_and_capacity_fail_without_partial_plan():
    rows = [{"source_key": "course:one", "display_name": "数学"}] * 2
    with pytest.raises(ValueError):
        module().plan_teaching_import("teaching_course", rows, (), effective_at=AT)
    with pytest.raises(ValueError):
        module().plan_teaching_import(
            "teaching_course",
            [{"source_key": "course:one", "display_name": "数学"}],
            (),
            effective_at=AT,
            max_changes=0,
        )


def test_incremental_import_reaches_normal_preview_with_signed_reference_and_guards():
    from uuid import uuid4

    from app.composition.school_fact_education_product import EducationBatchInput
    from tests.school_facts.test_f4_education_maintenance import environment, record

    coordinator, actor = environment(
        [record("teaching_course", "course:one", display_name="原课程")]
    )
    reference = "f4:teaching_course:course:one"
    payload = EducationBatchInput(
        fact_kind="teaching_course",
        target_ref=reference,
        fields={},
        reason="课程名核实",
        import_rows=({"reference": reference, "display_name": "新课程"},),
        import_target_refs=(reference,),
    )
    resolved = coordinator.resolve_change(
        actor=actor, payload=payload, request_id=uuid4(), effective_at=AT
    )
    assert resolved.change_set.changes[0].content.display_name == "新课程"
    assert resolved.summary.after["display_name"] == "新课程"
    assert resolved.summary_revalidate is not None
    resolved.summary_revalidate(actor)


def test_import_update_keeps_existing_explicit_expiry():
    from app.domains.students.education_change_planner import EducationTimelineSegment

    end = datetime(2027, 1, 1, tzinfo=UTC)
    row = EducationFactSnapshot(
        fact_kind="teaching_course",
        source_key="course:one",
        version=1,
        effective_at=OLD,
        content={"display_name": "旧名"},
        segments=(
            EducationTimelineSegment(
                effective_at=OLD, valid_to=end, content={"display_name": "旧名"}
            ),
        ),
    )
    plan = module().plan_teaching_import(
        "teaching_course",
        [{"source_key": "course:one", "display_name": "新名"}],
        (row,),
        effective_at=AT,
    )
    assert plan.changes[0]["content"]["valid_to"].startswith("2027-01-01")


@pytest.mark.parametrize("denied", [False, True])
def test_table_assignment_resolves_external_reference_before_planning(denied):
    import json
    from uuid import uuid4

    from app.composition.school_fact_education_product import EducationBatchInput
    from tests.school_facts.test_f4_education_maintenance import environment, record

    coordinator, actor = environment(
        [
            record(
                "teaching_assignment",
                "teacher:math",
                offering_source_key="offer:math",
                assignment_source_key="staff:li",
                teaching_role="lead",
            ),
            record(
                "teaching_offering",
                "offer:math",
                course_source_key="course:math",
                offering_status="active",
            ),
        ]
    )
    repo = coordinator.repository
    old_source, old_reference = repo.source_for_reference, repo.reference_for
    resolved = []

    def source(**kw):
        if kw["fact_kind"] == "assignment":
            resolved.append(kw["reference"])
            if denied:
                raise PermissionError("current exact assignment scope denied")
        return old_source(**kw)

    def reference(**kw):
        # Real signed references cannot be reused as stable source coordinates.
        assert not kw["source_key"].startswith(("f3:", "f4:"))
        return old_reference(**kw)

    repo.source_for_reference, repo.reference_for = source, reference
    payload = EducationBatchInput.model_validate_json(
        json.dumps(
            {
                "fact_kind": "teaching_assignment",
                "target_ref": "f4:teaching_assignment:teacher:math",
                "fields": {},
                "reason": "Explicit teacher change",
                "import_mode": "incremental",
                "import_target_refs": ["f4:teaching_assignment:teacher:math"],
                "import_rows": [
                    {
                        "reference": "f4:teaching_assignment:teacher:math",
                        "operation": "upsert",
                        "offering_source_key": "f4:teaching_offering:offer:math",
                        "assignment_source_key": "f3:assignment:staff:chen",
                        "teaching_role": "lead",
                    }
                ],
            }
        )
    )
    if denied:
        with pytest.raises(PermissionError, match="current exact"):
            coordinator.resolve_change(
                actor=actor, payload=payload, request_id=uuid4(), effective_at=AT
            )
    else:
        result = coordinator.resolve_change(
            actor=actor, payload=payload, request_id=uuid4(), effective_at=AT
        )
        assert result.summary.after["assignment_ref"] == "f4:assignment:staff:chen"
        assert result.change_set.base_version == 3
        result.summary_revalidate(actor)
    assert "f3:assignment:staff:chen" in resolved
