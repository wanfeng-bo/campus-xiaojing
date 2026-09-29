"""Selected-child teaching evidence remains current without holding connections."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path

import pytest

from tests.test_f3_family_read import NOW, setup
from tests.test_f3_product_identity import guardian


def test_current_relationship_migration_keeps_owner_rls_and_uses_stable_source():
    source = (
        Path(__file__).parents[2] / "alembic/versions/20260914_40_f5_current_school_relationship.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision = "20260914_39"' in source
    assert "enrolled.source_key=a.payload->>'school_relationship_source_key'" in source
    assert "enrolled.student_id=p_student" in source
    assert "enrolled.school_id=p_school" in source
    assert "referenced.id=sr.relationship_id" not in source
    assert "l.target_version=enrolled.version" in source
    assert "l.batch_id=enrolled.import_batch_id" in source
    assert "CREATE POLICY" not in source and "GRANT SELECT" not in source
    assert source.count("CREATE OR REPLACE FUNCTION") == 1


def fixture(caregiver=False):
    name = "app.composition.school_fact_family_teaching_query"
    assert find_spec(name), "F5 parent teaching service is not implemented"
    m = import_module(name)
    old, actor, state = setup(caregiver=caregiver)
    state.payload = {
        "status": "no_current_records",
        "student_id": str(state.basis.student_id),
        "student_version": 1,
        "relationship_version": 1,
        "school_fact_version": 3,
        "records": [],
        "related_teachers": [],
        "enrollment_status": "enrolled",
        "next_boundary": (NOW + timedelta(hours=1)).isoformat(),
        "as_of": NOW.isoformat(),
    }
    state.clock = NOW
    state.reads, state.stamps = 0, 0

    def read(actor, **kwargs):
        state.reads += 1
        return deepcopy(state.payload)

    def stamp(actor):
        state.stamps += 1
        return state.payload["school_fact_version"]

    old.repository.teaching_read = read
    old.repository.teaching_stamp = stamp
    service = m.FamilyTeachingQueryService(
        repository=old.repository,
        current_actor=lambda a: state.active and a == actor,
        clock=lambda: state.clock,
    )
    return service, actor, state


def test_empty_result_is_answerable_and_new_match_invalidates_without_full_reread():
    service, actor, state = fixture()
    result = service.query(actor=actor, subject_name="数学")
    assert result.public()["status"] == "no_current_records"
    assert result.evidence.valid(actor)
    assert state.reads == 1 and state.stamps >= 1
    state.payload["school_fact_version"] += 1
    assert not result.evidence.valid(actor)


@pytest.mark.parametrize("change", ["logout", "revoke", "expiry", "time", "other_child"])
def test_use_rechecks_identity_permissions_and_temporal_dependencies(change):
    service, actor, state = fixture()
    result = service.query(actor=actor)
    current = actor
    if change == "logout":
        state.active = False
    if change == "revoke":
        state.basis = None
    if change == "expiry":
        state.basis = replace(state.basis, valid_to=NOW)
    if change == "time":
        state.clock += timedelta(hours=1)
    if change == "other_child":
        current = guardian(2)
    assert not result.evidence.valid(current)


def test_caregiver_cannot_get_teacher_and_foreign_projection_fails_closed():
    service, actor, state = fixture(True)
    assert service.query(actor=actor).public()["status"] == "denied"
    assert state.reads == 0
    service, actor, state = fixture()
    state.payload["student_id"] = "other-child"
    assert service.query(actor=actor).public()["status"] == "unavailable"


def test_future_query_is_allowed_with_current_guardian_authority_not_future_login():
    service, actor, state = fixture()
    at = NOW + timedelta(days=1)
    state.payload["as_of"] = at.isoformat()
    result = service.query(actor=actor, as_of=at)
    assert result.public()["status"] == "no_current_records"
    assert result.public()["future_basis"] == "按目前已登记的安排"


def test_term_window_keeps_formal_local_midnight_and_does_not_end_open_facts():
    from app.composition.school_fact_education_gateway import education_model_material
    from tests.school_facts.test_f5_teaching_query import row

    service, actor, state = fixture()
    state.payload.update(
        period_start="2027-02-14T16:00:00+00:00",
        period_end="2027-07-14T16:00:00+00:00",
        records=[
            row(
                "education_calendar_rule",
                "calendar",
                {"display_name": "校历", "timezone_name": "Asia/Shanghai"},
            )
        ],
    )
    public = service.query(actor=actor, period="next_term").public()
    assert public["fact_timezone_name"] == "Asia/Shanghai"
    material = education_model_material(public)
    window = material["query_window"]
    assert "2027-02-15 00:00:00" in window["time_labels"]["start"]
    assert "2027-07-15 00:00:00" in window["time_labels"]["end_exclusive"]
    assert "不改变" in window["meaning"]


def test_unrequested_sensitive_teacher_fields_are_rejected_before_rendering():
    service, actor, state = fixture()
    state.payload["related_teachers"] = [
        {"assignment_source_key": "x", "name": "陈老师", "private_mobile": "unsafe"}
    ]
    assert service.query(actor=actor).public()["status"] == "unavailable"


def test_term_keeps_handover_and_empty_interval_instead_of_answering_only_first_day():
    from tests.school_facts.test_f5_teaching_query import row

    service, actor, state = fixture()
    base = [
        row("teaching_course", "c", {"display_name": "基础数学"}, start=NOW.isoformat()),
        row(
            "teaching_offering",
            "o",
            {"course_source_key": "c", "offering_status": "active"},
            start=NOW.isoformat(),
        ),
    ]
    end = NOW + timedelta(days=10)
    middle = NOW + timedelta(days=2)
    second = NOW + timedelta(days=5)

    def point(at, name):
        records = deepcopy(base)
        teachers = []
        if name:
            records.append(
                row(
                    "teaching_assignment",
                    "a",
                    {
                        "offering_source_key": "o",
                        "assignment_source_key": "staff",
                        "teaching_role": "lead",
                    },
                    start=at.isoformat(),
                )
            )
            teachers.append({"assignment_source_key": "staff", "name": name})
        return {"as_of": at.isoformat(), "records": records, "related_teachers": teachers}

    state.payload.update(point(NOW, "陈老师"))
    state.payload.update(
        status="available",
        period_end=end.isoformat(),
        period_slices=[point(NOW, "陈老师"), point(middle, None), point(second, "李老师")],
    )
    result = service.query(actor=actor, period="current_term").public()
    assert result["status"] == "available"
    assert [(part["from"], part["until"]) for part in result["timeline"]] == [
        (NOW.isoformat(), middle.isoformat()),
        (middle.isoformat(), second.isoformat()),
        (second.isoformat(), end.isoformat()),
    ]
    assert [
        part["items"][0]["teachers"][0]["name"] if part["items"][0]["teachers"] else None
        for part in result["timeline"]
    ] == ["陈老师", None, "李老师"]
    assert result["timeline"][1]["items"][0]["teacher_status"] == "no_current_records"


def test_term_later_slice_overbroad_fields_fail_closed_and_duplicate_points_reject():
    service, actor, state = fixture()
    future = NOW + timedelta(days=2)
    state.payload.update(
        period_end=future.isoformat(),
        period_slices=[
            {
                "as_of": NOW.isoformat(),
                "records": [],
                "related_teachers": [
                    {"assignment_source_key": "a", "name": "李老师", "private_mobile": "hidden"}
                ],
            }
        ],
    )
    assert service.query(actor=actor, period="current_term").public()["status"] == "unavailable"
    state.payload["period_slices"] = [
        {"as_of": NOW.isoformat(), "records": [], "related_teachers": []}
    ] * 2
    assert service.query(actor=actor, period="current_term").public()["status"] == "unavailable"


@pytest.mark.parametrize(
    "status,label",
    [
        ("suspended", "休学"),
        ("enrolled", "在读"),
        ("graduated", "毕业"),
        ("transferred_out", "转出"),
        ("withdrawn", "退学"),
        (None, "未取得学生状态"),
    ],
)
def test_model_material_states_student_status_without_claiming_attendance(status, label):
    from app.composition.school_fact_education_gateway import education_model_material

    service, actor, state = fixture()
    from tests.school_facts.test_f5_teaching_query import row

    state.payload["enrollment_status"] = status
    state.payload["status"] = "available"
    state.payload["records"] = [
        row("teaching_course", "c", {"display_name": "综合探究"}, start=NOW.isoformat()),
        row(
            "teaching_offering",
            "o",
            {"course_source_key": "c", "offering_status": "active"},
            start=NOW.isoformat(),
        ),
    ]
    material = education_model_material(service.query(actor=actor).public())
    assert material["items"][0]["offering_status"] == "持续开设"
    participation = material["student_participation"]
    assert participation["enrollment_status"] == (status or "unknown")
    assert participation["enrollment_label"] == label
    assert participation["actual_participation"] == "unknown"
    assert "实际到课" in participation["evidence_meaning"]
    if status == "suspended":
        assert "该孩子在所查时点处于休学状态" in participation["evidence_meaning"]
    assert "不能证明" in participation["evidence_meaning"]


def test_period_student_state_belongs_to_each_interval():
    from app.composition.school_fact_education_gateway import education_model_material

    service, actor, state = fixture()
    middle, end = NOW + timedelta(days=1), NOW + timedelta(days=2)
    state.payload.update(
        enrollment_status="suspended",
        period_end=end.isoformat(),
        period_slices=[
            {"as_of": NOW.isoformat(), "enrollment_status": "suspended", "records": []},
            {"as_of": middle.isoformat(), "enrollment_status": "enrolled", "records": []},
        ],
    )
    result = education_model_material(service.query(actor=actor, period="current_term").public())
    assert [part["student_participation"]["enrollment_status"] for part in result["timeline"]] == [
        "suspended",
        "enrolled",
    ]
    assert all(
        part["student_participation"]["actual_participation"] == "unknown"
        for part in result["timeline"]
    )
