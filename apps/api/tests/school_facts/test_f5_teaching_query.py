"""Teaching answers preserve offering ownership, time and the authorized set."""

from datetime import UTC, datetime
from importlib import import_module
from importlib.util import find_spec

import pytest


def module():
    name = "app.composition.school_fact_teaching_query"
    assert find_spec(name) is not None, "F5 teaching query is not implemented"
    return import_module(name)


NOW = datetime(2027, 9, 9, tzinfo=UTC)


def row(kind, key, content, start="2026-09-01T00:00:00+00:00", end=None):
    return {
        "fact_kind": kind,
        "source_key": key,
        "version": 1,
        "segments": [
            {
                "effective_at": start,
                "valid_to": end,
                "revision": 1,
                "recorded_at": "2026-08-25T00:00:00+00:00",
                "content": content,
                "source": {
                    "summary": "学校正式教学安排",
                    "publication_reference": "publication:abc",
                },
            }
        ],
    }


def records():
    return [
        row("teaching_subject", "subject:math", {"display_name": "数学"}),
        row("teaching_subject", "subject:science", {"display_name": "科学"}),
        row("teaching_course", "course:math", {"display_name": "基础数学"}),
        row("teaching_course", "course:project", {"display_name": "综合探究"}),
        row(
            "course_subject",
            "link:math",
            {"course_source_key": "course:math", "subject_source_key": "subject:math"},
        ),
        row(
            "course_subject",
            "link:project",
            {"course_source_key": "course:project", "subject_source_key": "subject:math"},
        ),
        row(
            "course_subject",
            "link:science",
            {"course_source_key": "course:project", "subject_source_key": "subject:science"},
        ),
        row(
            "teaching_offering",
            "offering:math",
            {"course_source_key": "course:math", "offering_status": "active"},
        ),
        row(
            "teaching_offering",
            "offering:project",
            {"course_source_key": "course:project", "offering_status": "active"},
        ),
        row(
            "teaching_assignment",
            "teacher:math",
            {
                "offering_source_key": "offering:math",
                "assignment_source_key": "assignment:chen",
                "teaching_role": "lead",
            },
        ),
        row(
            "teaching_assignment",
            "teacher:project",
            {
                "offering_source_key": "offering:project",
                "assignment_source_key": "assignment:li",
                "teaching_role": "lead",
            },
        ),
        row(
            "teaching_assignment",
            "teacher:joint",
            {
                "offering_source_key": "offering:project",
                "assignment_source_key": "assignment:zhou",
                "teaching_role": "co_teacher",
            },
        ),
    ]


def render(rows=None, **kw):
    return module().teaching_view(
        records=records() if rows is None else rows,
        teachers={
            "assignment:chen": {"name": "陈老师"},
            "assignment:li": {"name": "李老师"},
            "assignment:zhou": {"name": "周老师"},
        },
        at=NOW,
        subject_name="数学",
        include_contacts=False,
        **kw,
    )


def test_two_math_courses_and_joint_teachers_keep_their_actual_ownership():
    result = render()
    assert result["status"] == "available"
    assert [(r["course"], [t["name"] for t in r["teachers"]]) for r in result["items"]] == [
        ("基础数学", ["陈老师"]),
        ("综合探究", ["李老师", "周老师"]),
    ]
    assert "coverage" not in result
    assert result["observed_count"] == 2
    assert "唯一" not in str(result)
    assert "publication:abc" not in str(result["items"])


def test_explicit_subject_division_does_not_claim_science_only_teacher_teaches_math():
    rows = records()
    rows[-1]["segments"][0]["content"]["subject_source_keys"] = ["subject:science"]
    assert [t["name"] for t in render(rows)["items"][1]["teachers"]] == ["李老师"]


def test_empty_is_no_current_records_not_a_negative_or_completed_coverage():
    result = render([])
    assert result["status"] == "no_current_records"
    assert result["items"] == [] and "coverage" not in result


def test_each_answer_has_its_published_basis_without_claiming_independent_verification():
    rows = records()
    rows[-3]["segments"][0]["source"]["summary"] = "教务处任课登记"
    result = render(rows)
    assert result["items"][0]["teachers"][0]["basis"]["label"] == "教务处任课登记"
    assert "publication:abc" not in str(result["items"])
    assert "已确认" not in str(result["items"])
    assert result["items"][1]["teachers"][0]["basis"]["label"] == "学校正式教学安排"


def test_missing_teacher_keeps_formal_course_and_does_not_invent_vacancy():
    rows = [r for r in records() if r["fact_kind"] != "teaching_assignment"]
    item = render(rows)["items"][0]
    assert item["course"] == "基础数学" and item["teachers"] == []
    assert item["teacher_status"] == "no_current_records"


def test_open_assignment_continues_across_years_and_exact_handover_does_not_overlap():
    rows = records()
    rows[-3]["segments"][0]["valid_to"] = "2027-09-10T00:00:00+00:00"
    rows.append(
        row(
            "teaching_assignment",
            "teacher:new",
            {
                "offering_source_key": "offering:math",
                "assignment_source_key": "assignment:li",
                "teaching_role": "lead",
            },
            start="2027-09-10T00:00:00+00:00",
        )
    )
    assert render(rows)["items"][0]["teachers"][0]["name"] == "陈老师"
    result = module().teaching_view(
        records=rows,
        teachers={"assignment:chen": {"name": "陈老师"}, "assignment:li": {"name": "李老师"}},
        at=datetime(2027, 9, 10, tzinfo=UTC),
        subject_name="数学",
        include_contacts=False,
    )
    assert result["items"][0]["teachers"][0]["name"] == "李老师"


def test_suspended_offering_and_student_status_are_not_claimed_as_attendance():
    rows = records()
    rows[7]["segments"][0]["content"]["offering_status"] = "suspended"
    result = render(rows, enrollment_status="suspended")
    assert result["items"][0]["offering_status"] == "暂停"
    assert result["participation"] == "休学期间保留的教学关联不能证明实际参加课程。"


def test_term_lookup_does_not_invent_missing_next_dates_or_copy_assignment():
    m = module()
    current = row(
        "education_term",
        "term:current",
        {"display_name": "第一学期"},
        start="2027-09-01T00:00:00+00:00",
        end="2028-02-01T00:00:00+00:00",
    )
    resolution = m.resolve_teaching_period((current,), now=NOW, period="next_term")
    assert resolution.status == "period_not_recorded"
    assert resolution.start is None and resolution.end is None
    with pytest.raises(ValueError):
        m.resolve_teaching_period((current,), now=NOW.replace(tzinfo=None), period="current_term")


def test_quality_negative_is_limited_to_its_offering_role_not_all_teachers():
    rows = [r for r in records() if r["source_key"] != "teacher:project"]
    quality = [
        {
            "offering_source_key": "offering:project",
            "teaching_role": "lead",
            "quality_kind": "negative",
            "negative_code": "no_teaching_assignment",
            "effective_at": "2026-09-01T00:00:00+00:00",
            "valid_to": None,
        }
    ]
    result = render(rows, quality=quality)
    assert result["items"][1]["role_status"] == {"主讲": "学校明确未安排"}
    assert [t["name"] for t in result["items"][1]["teachers"]] == ["周老师"]
    assert not result["items"][0].get("role_status")
    assert result["items"][1]["teacher_status"] == "available"
    assert "coverage" not in result


def test_quality_contradiction_is_visible_and_source_not_falsely_verified():
    quality = [
        {
            "offering_source_key": "offering:math",
            "teaching_role": "lead",
            "quality_kind": "negative",
            "negative_code": "no_teaching_assignment",
            "effective_at": "2026-09-01T00:00:00+00:00",
        }
    ]
    result = render(quality=quality)
    assert result["items"][0]["teacher_status"] == "conflict"
    assert result["items"][0]["teachers"][0]["name"] == "陈老师"
    assert result["status"] == "conflict"
    assert not result["items"][1].get("role_status")


def test_exact_teacher_dispute_stays_on_one_teacher_and_does_not_relabel_offering():
    statement = {
        "offering_source_key": "offering:project",
        "target_source_key": "teacher:joint",
        "quality_kind": "dispute",
        "dispute_status": "open",
        "effective_at": "2026-09-01T00:00:00Z",
    }
    result = render(quality=[statement])
    first, second = result["items"]
    assert "quality" not in second and "notices" not in second
    assert "quality_declarations" not in second["teachers"][0]
    assert second["teachers"][1]["status"] == "conflict"
    assert (
        second["teachers"][1]["quality_declarations"][0]["status"] == "这条资料存在尚未解决的争议"
    )
    assert first["teacher_status"] == "available"
    assert result["status"] == "conflict"
    assert "teacher:joint" not in str(result)
    expired = render(quality=[statement | {"valid_to": "2027-09-08T00:00:00Z"}])
    assert "quality_declarations" not in expired["items"][1]["teachers"][1]
    assert expired["status"] == "available"


@pytest.mark.parametrize(
    "source_time",
    [{}, {"published_at": None}, {"published_at": "2026-08-24T10:20:00+00:00"}],
    ids=["unrecorded-publication", "null-publication", "distinct-publication"],
)
def test_teaching_basis_keeps_publication_and_recording_times_separate_for_every_owner(source_time):
    from copy import deepcopy

    from app.composition.school_fact_education_gateway import education_model_material

    rows = records()
    recorded = "2026-08-25T12:34:00+00:00"
    for record in rows:
        segment = record["segments"][0]
        segment["recorded_at"] = recorded
        segment["source"].update(source_time)
    quality = [
        {
            "offering_source_key": "offering:math",
            "quality_kind": "coverage",
            "coverage_status": "unknown",
            "effective_at": "2026-09-01T00:00:00+00:00",
            "recorded_at": recorded,
            "source": {"label": "范围声明", **source_time},
        }
    ]
    before = deepcopy((rows, quality))
    view = render(rows, quality=quality)
    view["fact_timezone_name"] = "Asia/Shanghai"
    material = education_model_material(view, teaching_answer=True)
    for result, key in ((view, "basis"), (material, "来源时间")):
        item = result["items"][0]
        for basis in (item[key], item["teachers"][0][key], item["quality"][0][key]):
            assert basis["published_at"] == source_time.get("published_at")
            assert basis["recorded_at"] == recorded
            assert "publication_reference" not in basis
    for basis in (
        material["items"][0]["来源时间"],
        material["items"][0]["teachers"][0]["来源时间"],
        material["items"][0]["quality"][0]["来源时间"],
    ):
        assert "2026-08-25 20:34:00" in basis["time_labels"]["recorded_at"]
        if source_time.get("published_at") is None:
            assert "published_at" not in basis["time_labels"]
        else:
            assert "2026-08-24 18:20:00" in basis["time_labels"]["published_at"]
    assert (
        "recorded_at是系统登记时间，published_at是来源发布时间"
        in material["interpretation"]["time"]
    )
    assert (rows, quality) == before
