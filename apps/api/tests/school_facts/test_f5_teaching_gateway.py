from importlib import import_module
from importlib.util import find_spec
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.platform.assistant.runtime import AssistantRuntimeError
from app.platform.assistant.tool_loop import ModelToolCall, ToolCallContext
from tests.school_facts.test_f4_education_reader import actor
from tests.school_facts.test_f5_staff_teaching_query import service


def fixture():
    name = "app.composition.school_fact_teaching_gateway"
    assert find_spec(name) is not None, "Teaching gateway is missing"
    api = import_module(name)
    query, repo, _, _ = service()
    identity = SimpleNamespace(actor=actor(), session_binding="session")
    gateway = api.StaffTeachingGateway(resolve_actor=lambda ref: identity, query=query)
    context = ToolCallContext("actor", "conversation", "turn", "run", 1, uuid4())
    call = ModelToolCall(
        call_id="teaching-1", name=api.MODULE + ".query", arguments={"view": "my_teaching"}
    )
    return api, gateway, repo, identity, context, call


@pytest.mark.parametrize(
    ("outcome", "status"),
    [("no_current_records", "not_recorded"), ("conflict", "conflict")],
)
def test_teaching_join_missing_or_conflicting_name_is_not_missing_teaching(outcome, status):
    from tests.school_facts.test_f4_education_gateway import related_family_reader

    _, gateway, _, _, context, call = fixture()
    gateway.query.external_reader, _ = related_family_reader(outcome=outcome)
    result = gateway.execute(call, context)
    assert result.model_content["status"] == "incomplete"
    assert result.model_content["related_fact"] == {
        "status": status,
        "fact_kind": "person",
        "fields": ["display_name"],
    }
    assert result.fact_card is None and result.history_check is None


def test_teaching_family_name_scope_denial_is_not_source_outage():
    from app.composition.school_fact_family_authority import FamilyFactAuthorityDenied
    from tests.school_facts.test_f4_education_gateway import related_family_reader

    _, gateway, _, _, context, call = fixture()
    gateway.query.external_reader, _ = related_family_reader(error=FamilyFactAuthorityDenied)
    result = gateway.execute(call, context)
    assert result.model_content["status"] == "denied"
    assert result.fact_card is None and result.history_check is None


@pytest.mark.parametrize("error", [PermissionError, ValueError, RuntimeError])
def test_teaching_denial_retains_only_fixed_execution_note_not_current_permission(error):
    _, gateway, _, _, context, call = fixture()

    def fail(*args, **kwargs):
        raise error("private query details")

    gateway.query.query = fail
    result = gateway.execute(call, context)
    assert result.history_execution_note == (
        "read_returned_no_values" if error is PermissionError else None
    )
    assert result.history_check is None and result.fact_card is None
    assert "private" not in str(result.model_content)


def test_teaching_gateway_proves_history_and_only_projects_business_values():
    _, gateway, repo, _, context, call = fixture()
    turn = gateway.for_turn("actor")
    result = turn.execute(call, context)
    assert result.model_content["status"] == "available"
    assert "陈老师" in str(result.model_content)
    assert "source_key" not in str(result.model_content)
    assert "interpretation" in result.model_content["supporting_evidence"]
    assert "validity_meaning" not in str(result.model_content)
    card_teacher = result.fact_card["items"][0]["teachers"][0]
    model_teacher = result.model_content["items"][0]["teachers"][0]
    assert model_teacher["supporting_evidence"]["登记来源"] == card_teacher["basis"]["label"]
    assert "basis" not in model_teacher
    assert set(card_teacher["basis"]) == {"label", "published_at", "recorded_at"}
    assert result.history_check.valid()
    assert result.history_execution_note is None
    repo.projection.current_version = lambda _: 11
    assert not result.history_check.valid()
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("actor")


def representative_teaching_material():
    from copy import deepcopy

    from app.composition.school_fact_family_teaching_query import _student_participation

    items = [
        {
            "course": course,
            "effective_from": "2026-09-01T00:00:00Z",
            "effective_to": None,
            "coverage": "unknown",
            "audiences": [{"name": "跨班组", "source": "独立教学名单，需单独维护"}],
            "teachers": [
                {
                    "name": "甲老师",
                    "role": "主讲",
                    "effective_from": "2026-09-01T00:00:00Z",
                    "effective_to": "2027-09-01T00:00:00Z",
                    "reference": "signed-authorized-teacher",
                    "basis": {
                        "label": "学校正式登记",
                        "source_key": "private-source",
                        "published_at": None,
                        "recorded_at": None,
                    },
                }
            ],
        }
        for course in ("课程甲", "课程乙")
    ]
    return {
        "kind": "family_teaching",
        "status": "available",
        "items": items,
        "scope": "本次获准对象及期间",
        "coverage": "unknown",
        "future_basis": "按目前已登记的安排",
        "fact_timezone_name": "Asia/Shanghai",
        "query_window": {
            "start": "2027-02-01T00:00:00Z",
            "end_exclusive": "2027-07-01T00:00:00Z",
            "meaning": "查询期间包含起点、不包含终点；不改变各项事实自身的有效期。",
        },
        "participation": "休学期间保留的教学关联不能证明实际参加课程。",
        "student_participation": _student_participation("suspended"),
        "timeline": [
            {
                "from": "2027-02-01T00:00:00Z",
                "until": "2027-07-01T00:00:00Z",
                "items": deepcopy(items),
                "student_participation": _student_participation("enrolled"),
            }
        ],
    }


def test_teaching_interpretation_is_once_and_all_factual_boundaries_remain():
    from copy import deepcopy

    from app.composition.school_fact_education_gateway import education_model_material

    raw = representative_teaching_material()
    original = deepcopy(raw)
    result = education_model_material(raw, teaching_answer=True)
    assert raw == original
    rendered = str(result)
    assert "validity_meaning" not in rendered and "coverage_meaning" not in rendered
    assert "evidence_meaning" not in rendered
    rules = result["interpretation"]
    assert "不含终点" in rules["time"] and "未登记" in rules["time"]
    assert "未返回" in rules["coverage"]
    assert "休学" in rules["participation"] and "实际参加" in rules["participation"]
    assert result["student_participation"]["enrollment_status"] == "suspended"
    assert result["timeline"][0]["student_participation"]["enrollment_status"] == "enrolled"
    assert result["student_participation"]["actual_participation"] == "unknown"
    assert result["future_basis"] == raw["future_basis"]
    assert result["scope"] == raw["scope"] and result["reality_coverage"] == {"status": "unknown"}
    assert result["query_window"]["end_exclusive"] == raw["query_window"]["end_exclusive"]
    assert "meaning" not in result["query_window"]
    assert result["collection_scope"]["unreturned_records"] == "not_determined"
    assert result["items"][0]["effective_to"] is None
    teacher = result["items"][0]["teachers"][0]
    assert teacher["effective_to"] == "2027-09-01T00:00:00Z"
    assert "Asia/Shanghai" in teacher["time_labels"]["effective_to"]
    assert teacher["tool_context"]["reference"] == "signed-authorized-teacher"
    assert teacher["登记来源"] == "学校正式登记" and "private-source" not in rendered
    assert "独立登记" in result["items"][0]["audiences"][0]["membership_basis"]


@pytest.mark.parametrize("with_coordinates", [False, True])
def test_query_dates_identify_offering_and_each_teacher_without_mixing_source_times(
    with_coordinates,
):
    from copy import deepcopy

    from app.composition.school_fact_education_gateway import education_model_material

    def period(owner, start, end):
        return {
            "effective_from": start,
            "effective_to": end,
            "basis": {
                "label": f"{owner}的正式来源",
                "published_at": "2030-08-20T00:00:00Z",
                "recorded_at": "2030-09-25T00:00:00Z",
            },
        }

    teachers = [
        {
            "name": "甲老师",
            "role": "主讲",
            "subjects": ["数学"],
            **period("甲老师任教", "2030-09-21T00:00:00Z", None),
        },
        {
            "name": "乙老师",
            "role": "共同任教",
            "subjects": ["物理"],
            **period("乙老师任教", "2030-10-01T00:00:00Z", "2031-01-01T00:00:00Z"),
        },
    ]
    offering = {
        "course": "综合课程",
        "offering": "正式开课",
        "teachers": teachers,
        "teacher_status": "conflict",
        "offering_status": "暂停",
        "notices": ["学校登记存在争议"],
        **period("本开课", "2030-09-14T00:00:00Z", "2031-06-01T00:00:00Z"),
    }
    if with_coordinates:
        offering.update(fact_kind="teaching_offering", reference="authorized-offering")
        for index, teacher in enumerate(teachers):
            teacher.update(fact_kind="teaching_assignment", reference=f"authorized-teacher-{index}")
    raw = {
        "kind": "family_teaching",
        "status": "available",
        "fact_timezone_name": "Asia/Shanghai",
        "items": [offering],
        "coverage": "partial",
        "future_basis": "按目前已登记的安排",
        "timeline": [{"from": "2030-10-01T00:00:00Z", "items": [deepcopy(offering)]}],
    }
    original = deepcopy(raw)
    result = education_model_material(raw, teaching_answer=True, query_answer=True)
    for target in (result["items"][0], result["timeline"][0]["items"][0]):
        evidence = target["supporting_evidence"]
        assert evidence["有效期归属"] == "本开课"
        assert evidence["effective_from"] == "2030-09-14T00:00:00Z"
        assert evidence["effective_to"] == "2031-06-01T00:00:00Z"
        assert "2030-09-14 08:00:00" in evidence["time_labels"]["effective_from"]
        assert "time_labels" not in target
        assert evidence["登记来源"] == "本开课的正式来源"
        assert target["teacher_status"] == "conflict" and target["offering_status"] == "暂停"
        assert target["notices"] == offering["notices"]
        for actual, expected in zip(target["teachers"], teachers, strict=True):
            own = actual["supporting_evidence"]
            assert own["有效期归属"] == "本教师的任教关系"
            assert own["effective_from"] == expected["effective_from"]
            assert own["effective_to"] == expected["effective_to"]
            assert own["登记来源"] == expected["basis"]["label"]
            assert own["来源时间"]["published_at"] == expected["basis"]["published_at"]
            assert own["来源时间"]["recorded_at"] == expected["basis"]["recorded_at"]
            assert "supporting_evidence" not in own["time_labels"]
            assert "2030-09-14" not in str(own)
            assert actual["role"] == expected["role"] and actual["subjects"] == expected["subjects"]
            if with_coordinates:
                assert actual["tool_context"]["reference"] == expected["reference"]
    assert result["future_basis"] == raw["future_basis"]
    assert result["reality_coverage"] == {"status": "partial"}
    assert raw == original
    # Maintenance and the original formal card are not reinterpreted.
    unchanged = education_model_material(raw, teaching_answer=True)
    assert "有效期归属" not in str(unchanged)
    assert unchanged["items"][0]["effective_from"] == original["items"][0]["effective_from"]


@pytest.mark.parametrize("kind", ["person", "future_teaching_kind"])
def test_query_does_not_assign_teaching_time_owner_to_an_unknown_fact_schema(kind):
    from app.composition.school_fact_education_gateway import education_model_material

    raw = {
        "items": [
            {
                "fact_kind": kind,
                "course": "课程",
                "offering": "对象",
                "teachers": [
                    {
                        "fact_kind": kind,
                        "name": "老师",
                        "role": "主讲",
                        "subjects": ["数学"],
                        "effective_from": "2030-09-21T00:00:00Z",
                        "effective_to": None,
                    }
                ],
                "teacher_status": "available",
                "offering_status": "持续开设",
                "effective_from": "2030-09-14T00:00:00Z",
                "effective_to": None,
            }
        ],
    }
    result = education_model_material(raw, teaching_answer=True, query_answer=True)
    assert "有效期归属" not in str(result)
    assert (
        result["items"][0]["supporting_evidence"]["effective_from"]
        == raw["items"][0]["effective_from"]
    )


@pytest.mark.parametrize("teaching_answer", [False, True])
def test_shared_material_deduplicates_normal_explanations_without_dropping_limits(teaching_answer):
    from copy import deepcopy

    from app.composition.school_fact_education_gateway import education_model_material

    raw = representative_teaching_material()
    original = deepcopy(raw)
    material = education_model_material(raw, teaching_answer=teaching_answer)

    assert "validity_meaning" not in str(material)
    assert "coverage_meaning" not in str(material)
    assert "未登记截止" in material["interpretation"]["time"]
    assert "未返回" in material["interpretation"]["coverage"]
    assert material["items"][0]["coverage"] == "unknown"
    assert material["items"][0]["teachers"][0]["role"] == "主讲"
    assert material["items"][0]["teachers"][0]["effective_to"] == "2027-09-01T00:00:00Z"
    assert material["items"][0]["teachers"][0]["登记来源"] == "学校正式登记"
    assert material["items"][0]["teachers"][0]["tool_context"]["reference"] == (
        "signed-authorized-teacher"
    )
    assert material["future_basis"] == original["future_basis"]
    assert material["scope"] == original["scope"]
    assert material["reality_coverage"] == {"status": "unknown"}
    assert raw == original


def test_other_education_model_material_shares_time_and_coverage_interpretation():
    from app.composition.school_fact_education_gateway import education_model_material

    result = education_model_material({"effective_to": None, "coverage": "unknown"})
    assert "未登记截止" in result["interpretation"]["time"]
    assert "validity_meaning" not in result
    assert "coverage_meaning" not in result
    assert result["coverage"] == "unknown"
    assert "未返回" in result["interpretation"]["coverage"]


@pytest.mark.parametrize("teaching_answer", [False, True])
def test_point_teaching_times_do_not_describe_absent_audit_or_period_fields(teaching_answer):
    from copy import deepcopy

    from app.composition.school_fact_education_gateway import education_model_material

    raw = {
        "status": "available",
        "as_of": "2026-09-14T08:00:00+08:00",
        "items": [
            {
                "teachers": [
                    {
                        "name": "甲老师",
                        "effective_from": "2026-09-01T00:00:00+08:00",
                        "effective_to": None,
                        "basis": {
                            "label": "学校登记",
                            "recorded_at": "2026-09-02T00:00:00+08:00",
                            "published_at": "2026-08-31T00:00:00+08:00",
                        },
                    }
                ],
            }
        ],
        "fact_timezone_name": "Asia/Shanghai",
    }
    before = deepcopy(raw)
    material = education_model_material(raw, teaching_answer=teaching_answer)
    meaning = material["interpretation"]["time"]
    for unused in ("known_at", "query_window", "period_start", "period_end", "timeline"):
        assert unused not in meaning
    for field in ("as_of", "effective_from", "effective_to", "recorded_at", "published_at"):
        assert field in meaning
    assert "未登记截止" in meaning
    assert "补正" in meaning and "未来" in meaning
    teacher = material["items"][0]["teachers"][0]
    assert teacher["登记来源"] == "学校登记"
    assert teacher["来源时间"] == {
        key: value
        for key, value in raw["items"][0]["teachers"][0]["basis"].items()
        if key != "label"
    }
    assert "time_labels" not in str(material)
    assert raw == before


@pytest.mark.parametrize("teaching_answer", [False, True])
def test_publication_evidence_is_business_material_at_each_target_without_quality_inference(
    teaching_answer,
):
    from copy import deepcopy

    from app.composition.school_fact_education_gateway import education_model_material

    def source(label):
        return {
            "label": label,
            "published_at": None,
            "recorded_at": "2026-09-14T01:23:45+00:00",
        }

    raw = {
        "kind": "family_teaching",
        "status": "conflict",
        "fact_timezone_name": "Asia/Shanghai",
        "future_basis": "按目前已登记的安排",
        "items": [
            {
                "reference": "authorized-offering",
                "basis": source("教务处课程登记"),
                "teachers": [{"reference": "authorized-assignment", "basis": source("任教登记")}],
                "quality": [
                    {
                        "kind": "争议说明",
                        "coverage": "unknown",
                        "basis": source("尚待学校核对的争议声明"),
                    }
                ],
            }
        ],
    }
    before = deepcopy(raw)
    material = education_model_material(raw, teaching_answer=teaching_answer)
    item = material["items"][0]
    assert item["tool_context"]["reference"] == "authorized-offering"
    assert item["teachers"][0]["tool_context"]["reference"] == "authorized-assignment"
    for target, label in (
        (item, "教务处课程登记"),
        (item["teachers"][0], "任教登记"),
        (item["quality"][0], "尚待学校核对的争议声明"),
    ):
        assert target["登记来源"] == label
        assert "basis" not in target
        assert target["来源时间"]["published_at"] is None
        assert target["来源时间"]["recorded_at"] == "2026-09-14T01:23:45+00:00"
        assert "2026-09-14 09:23:45" in target["来源时间"]["time_labels"]["recorded_at"]
        assert "verified" not in str(target)
    assert material["status"] == "conflict"
    assert item["quality"][0]["coverage"] == "unknown"
    assert material["future_basis"] == raw["future_basis"]
    assert material["collection_scope"]["basis"] == "current_identity_and_query"
    assert raw == before


def test_unrecognized_basis_does_not_acquire_publication_semantics():
    from app.composition.school_fact_education_gateway import education_model_material

    for basis in ("current_identity_and_query", {"label": "用途未定义"}, {"method": "other"}):
        assert education_model_material({"basis": basis}, teaching_answer=True) == {"basis": basis}


@pytest.mark.parametrize(
    "field,meaning",
    [
        ("as_of", "所问事实时点"),
        ("known_at", "审计知悉时点"),
        ("recorded_at", "系统登记时间"),
        ("published_at", "来源发布时间"),
    ],
)
def test_individual_time_axis_does_not_invent_other_time_axes(field, meaning):
    from app.composition.school_fact_education_gateway import education_model_material

    raw = {field: "2026-09-14T08:00:00+08:00"}
    material = education_model_material(raw, teaching_answer=True)
    explanation = material["interpretation"]["time"]
    assert field in explanation and meaning in explanation
    for other in {"as_of", "known_at", "recorded_at", "published_at"} - {field}:
        assert other not in explanation
    assert "区间" not in explanation and "未登记截止" not in explanation
    assert material[field] == raw[field]


@pytest.mark.parametrize("teaching_answer", [False, True])
@pytest.mark.parametrize(
    "status,items,outcome",
    [
        ("available", [{"name": "所查老师"}], "已取得本次所查正式登记"),
        ("known", [{"name": "所查老师"}], "已取得本次所查正式登记"),
        ("no_current_records", [], "本次条件和时点内没有当前登记"),
        ("not_recorded", [], "本次条件和时点内没有当前登记"),
        ("conflict", [{"name": "所查老师"}], "本次所查事实存在冲突"),
    ],
)
def test_education_material_separates_read_facts_returned_set_and_reality_coverage(
    teaching_answer, status, items, outcome
):
    from copy import deepcopy

    from app.composition.school_fact_education_gateway import education_model_material

    raw = {
        "status": status,
        "items": items,
        "observed_count": len(items),
        "coverage": "unknown",
        "scope": "已返回的所问对象范围",
        "selection_scope": {"kind": "teaching_assignment", "resolution": "resolved"},
        "selection": {"target_reference": "original-reference"},
    }
    original = deepcopy(raw)
    material = education_model_material(raw, teaching_answer=teaching_answer)
    assert material["fact_reading"]["outcome"] == outcome
    assert material["authorized_result_set"] == {"returned_count": len(items)}
    assert material["reality_coverage"] == {"status": "unknown"}
    assert "coverage" not in material and "coverage_meaning" not in material
    assert material["selection"]["tool_context"]["target_reference"] == "original-reference"
    assert material["selection_scope"] == original["selection_scope"]
    assert raw == original


def test_education_material_retains_nested_limits_and_quality_conflicts():
    from app.composition.school_fact_education_gateway import education_model_material

    raw = {
        "status": "known",
        "teachers": {
            "status": "conflict",
            "items": [{"name": "所查老师", "status": "name_not_available"}],
            "coverage": "partial",
            "has_more": True,
            "truncated": True,
            "notice": "学校登记存在争议",
        },
    }
    result = education_model_material(raw)
    group = result["teachers"]
    assert group["fact_reading"]["outcome"] == "本次所查事实存在冲突"
    assert group["authorized_result_set"] == {
        "returned_count": 1,
        "has_more": True,
        "truncated": True,
    }
    assert group["reality_coverage"] == {"status": "partial"}
    assert group["notice"] == raw["teachers"]["notice"]
    assert group["items"][0]["status"] == "name_not_available"


@pytest.mark.parametrize("teaching_answer", [False, True])
@pytest.mark.parametrize(
    "effective,recorded,published",
    [
        ("2024-01-01T00:00:00+08:00", "2026-03-01T00:00:00+08:00", "2026-03-02T00:00:00+08:00"),
        ("2026-04-01T00:00:00+08:00", "2026-04-02T00:00:00+08:00", "2026-06-01T00:00:00+08:00"),
        ("2030-01-01T00:00:00+08:00", "2026-05-01T00:00:00+08:00", "2026-05-02T00:00:00+08:00"),
    ],
)
def test_education_time_axes_stay_distinct_for_correction_late_publication_and_future(
    teaching_answer, effective, recorded, published
):
    import json
    from copy import deepcopy

    from app.composition.school_fact_education_gateway import education_model_material

    raw = {
        "fact_timezone_name": "Asia/Shanghai",
        "as_of": "2029-01-01T00:00:00+08:00",
        "known_at": "2026-07-01T00:00:00+08:00",
        "query_window": {
            "start": "2029-01-01T00:00:00+08:00",
            "end_exclusive": "2031-01-01T00:00:00+08:00",
        },
        "records": [
            {
                "effective_at": effective,
                "effective_to": None,
                "recorded_at": recorded,
                "published_at": published,
                "version": version,
            }
            for version in (2, 3)
        ],
    }
    original = deepcopy(raw)
    result = education_model_material(raw, teaching_answer=teaching_answer)
    meaning = result["interpretation"]["time"]
    assert "effective_at" in meaning and "事实生效" in meaning
    assert "recorded_at" in meaning and "登记时间" in meaning
    assert "published_at" in meaning and "发布时间" in meaning
    assert "known_at" in meaning and "query_window" in meaning
    assert "补正" in meaning and "未来" in meaning
    assert json.dumps(result, ensure_ascii=False).count(meaning) == 1
    assert "validity_meaning" not in str(result)
    assert result["as_of"] == raw["as_of"] and result["known_at"] == raw["known_at"]
    for key in ("start", "end_exclusive"):
        assert result["query_window"][key] == raw["query_window"][key]
    for actual, expected in zip(result["records"], raw["records"], strict=True):
        for key in ("effective_at", "effective_to", "recorded_at", "published_at"):
            assert actual[key] == expected[key]
        assert actual["tool_context"]["version"] == expected["version"]
    assert raw == original


def test_family_gateway_projects_once_without_changing_the_fact_card():
    from copy import deepcopy

    from app.composition.school_fact_family_teaching_gateway import NAME, FamilyTeachingToolGateway
    from tests.test_f3_product_identity import guardian

    raw = representative_teaching_material()
    identity = SimpleNamespace(actor=guardian())
    query = SimpleNamespace(
        query=lambda **_: SimpleNamespace(public=lambda: deepcopy(raw), evidence=None)
    )
    turn = FamilyTeachingToolGateway(resolve_actor=lambda _: identity, query=query).for_turn(
        "actor"
    )
    result = turn.execute(
        ModelToolCall(call_id="family", name=NAME, arguments={}),
        ToolCallContext("actor", "conversation", "turn", "run", 1, uuid4()),
    )
    assert result.fact_card == raw
    assert set(result.model_content["supporting_evidence"]["interpretation"]) == {
        "time",
        "coverage",
        "participation",
        "result_scope",
    }
    assert "validity_meaning" not in str(result.model_content)
    assert result.model_content["student_participation"]["enrollment_status"] == "suspended"


@pytest.mark.parametrize(
    "status", ["available", "conflict", "no_current_records", "source_unavailable"]
)
@pytest.mark.parametrize("coverage", ["unknown", "partial", "complete"])
def test_query_evidence_layers_preserve_values_and_visible_limits(status, coverage):
    from copy import deepcopy

    from app.composition.school_fact_education_gateway import education_model_material

    raw = representative_teaching_material()
    raw.update(status=status, coverage=coverage, has_more=True, truncated=True)
    raw["items"][0].update(offering_status="暂停", notices=["学校登记存在争议"])
    raw["items"][1]["teachers"][0]["basis"]["label"] = "另一任教来源"
    if status in {"no_current_records", "source_unavailable"}:
        raw["items"] = []
    original = deepcopy(raw)
    legacy = education_model_material(raw, teaching_answer=True)
    actual = education_model_material(raw, teaching_answer=True, query_answer=True)
    assert actual["status"] == status
    if "reality_coverage" in legacy:
        assert actual["reality_coverage"] == {"status": coverage}
    else:
        assert actual["coverage"] == coverage
    assert actual["has_more"] is True and actual["truncated"] is True
    assert actual["query_window"] == legacy["query_window"]
    assert actual["future_basis"] == raw["future_basis"]
    assert actual["student_participation"] == legacy["student_participation"]
    assert "interpretation" not in actual
    for row, expected in zip(actual["items"], legacy["items"], strict=True):
        assert row["coverage"] == expected["coverage"]
        assert row.get("offering_status") == expected.get("offering_status")
        assert row.get("notices") == expected.get("notices")
        teacher = row["teachers"][0]
        assert "effective_from" not in teacher and "登记来源" not in teacher
        assert teacher["supporting_evidence"]["登记来源"] == expected["teachers"][0]["登记来源"]
        assert teacher["tool_context"] == expected["teachers"][0]["tool_context"]

    def restore(value):
        if isinstance(value, list):
            return [restore(item) for item in value]
        if not isinstance(value, dict):
            return value
        output = {
            key: restore(item)
            for key, item in value.items()
            if key not in {"supporting_evidence", "collection_scope", "related_context"}
        }
        output.update(
            {key: restore(item) for key, item in value.get("supporting_evidence", {}).items()}
        )
        output.update(
            {key: restore(item) for key, item in value.get("related_context", {}).items()}
        )
        context = output.get("tool_context")
        if isinstance(context, dict):
            for key in (
                "authorized_result_set",
                "fact_reading",
                "observed_count",
                "selection_scope",
            ):
                if key in context:
                    output[key] = context.pop(key)
            if not context:
                output.pop("tool_context")
        return output

    assert restore(actual) == restore(legacy)
    assert raw == original


def test_identity_switch_and_session_replacement_invalidate_result():
    _, gateway, _, identity, context, call = fixture()
    turn = gateway.for_turn("actor")
    result = turn.execute(call, context)
    identity.session_binding = "another-session"
    assert not result.history_check.valid()
    with pytest.raises(AssistantRuntimeError):
        turn.revalidate("another-actor")


def test_schema_rejects_model_identity_raw_filters_and_naive_time():
    api, gateway, _, _, _, _ = fixture()
    assert all(d.owner == api.MODULE for d in gateway.descriptors)
    for kwargs in (
        {"assignment_source_key": "someone:else"},
        {"filter": {"x": "y"}},
        {"as_of": "2026-09-01T00:00:00"},
        {"view": "teaching"},
    ):
        with pytest.raises(ValueError):
            api.TeachingQueryInput(**kwargs)


@pytest.mark.parametrize(
    "extra",
    [
        {"view": "my_teaching"},
        {"view": "my_organizations"},
        {"view": "teaching", "subject_name": "数学"},
        {"view": "teaching", "course_name": "数学课程"},
        {"view": "teaching", "name": "一班"},
    ],
)
def test_target_reference_model_schema_rejects_ambiguous_selectors(extra):
    api, *_ = fixture()
    with pytest.raises(ValueError):
        api.TeachingQueryInput.model_validate(
            {"target_reference": "f4:teaching_assignment:" + "a" * 64, **extra}
        )


@pytest.mark.parametrize(
    "reference",
    [
        "f3:assignment:" + "a" * 64,
        "f4:teaching_subject:" + "a" * 64,
        "f4create:teaching_assignment:" + "a" * 64,
        "f4audit:teaching_assignment:" + "a" * 64,
        "teaching:a",
    ],
)
def test_target_reference_only_declares_two_existing_signed_families(reference):
    api, *_ = fixture()
    with pytest.raises(ValueError):
        api.TeachingQueryInput.model_validate({"view": "teaching", "target_reference": reference})


def family_input_environment():
    from app.composition.school_fact_family_teaching_gateway import FamilyTeachingToolGateway
    from tests.school_facts.test_f5_family_teaching import fixture as family_fixture

    query, actor, state = family_fixture()
    gateway = FamilyTeachingToolGateway(
        resolve_actor=lambda _: SimpleNamespace(actor=actor),
        query=query,
    )
    return gateway.for_turn("family"), state


@pytest.mark.parametrize(
    "arguments",
    [
        {"as_of": "2027-09-09"},
        {"as_of": "2027-09-09T00:00:00"},
        {"as_of": "2027-09-09T00:00:00+08:00", "period": "current_term"},
        {"as_of": "2027-09-09T00:00:00+08:00", "period": "next_term"},
        {"subject_name": "   "},
        {"course_name": "\t"},
    ],
)
def test_family_invalid_teaching_input_returns_recoverable_selection(arguments):
    from app.composition.school_fact_family_teaching_gateway import NAME

    turn, state = family_input_environment()
    result = turn.execute(
        ModelToolCall(call_id="invalid", name=NAME, arguments=arguments),
        ToolCallContext("family", "conversation", "turn", "run", 1, uuid4()),
    )
    assert result.model_content["status"] == "unsupported_selection"
    assert result.history_check is None and result.fact_card is None
    assert state.reads == 0
    assert "2027-09-09" not in str(result.model_content)


@pytest.mark.parametrize("model_name", ["FamilyTeachingInput", "TeachingQueryInput"])
def test_teaching_date_period_contract_is_explicit_and_enforced(model_name):
    import json

    from app.composition.school_fact_family_teaching_gateway import FamilyTeachingInput
    from app.composition.school_fact_teaching_gateway import TeachingQueryInput

    model = {
        "FamilyTeachingInput": FamilyTeachingInput,
        "TeachingQueryInput": TeachingQueryInput,
    }[model_name]
    properties = model.model_json_schema()["properties"]
    assert "period=current" in properties["as_of"]["description"]
    assert "as_of" in properties["period"]["description"]
    with pytest.raises(ValueError):
        model.model_validate_json(
            json.dumps(
                {
                    "as_of": "2027-09-09T00:00:00+08:00",
                    "period": "next_term",
                }
            )
        )
    valid = model.model_validate_json(json.dumps({"as_of": "2027-09-09T00:00:00+08:00"}))
    assert valid.as_of.utcoffset() is not None and valid.period == "current"


@pytest.mark.parametrize("status", ["denied", "unavailable", "no_current_records"])
def test_family_input_feedback_does_not_reclassify_query_status(status):
    from app.composition.school_fact_family_teaching_gateway import NAME
    from app.composition.school_fact_family_teaching_query import FamilyTeachingResult

    turn, _ = family_input_environment()
    seen = []

    def query(**kwargs):
        seen.append(kwargs)
        return FamilyTeachingResult({"status": status})

    turn.owner.query = SimpleNamespace(query=query)
    result = turn.execute(
        ModelToolCall(call_id="valid", name=NAME, arguments={"as_of": "2027-09-09T00:00:00+08:00"}),
        ToolCallContext("family", "conversation", "turn", "run", 1, uuid4()),
    )
    assert result.model_content["status"] == status
    assert len(seen) == 1 and seen[0]["as_of"].utcoffset() is not None


@pytest.mark.parametrize("error", [RuntimeError, ValueError, PermissionError])
def test_family_unknown_query_and_permission_failures_are_not_input_or_empty(error):
    from app.composition.school_fact_family_teaching_gateway import NAME

    turn, _ = family_input_environment()

    def fail(**kwargs):
        raise error("synthetic failure")

    turn.owner.query = SimpleNamespace(query=fail)
    with pytest.raises(error):
        turn.execute(
            ModelToolCall(call_id="failure", name=NAME, arguments={}),
            ToolCallContext("family", "conversation", "turn", "run", 1, uuid4()),
        )


def family_history_identity_environment():
    from app.composition.school_fact_family_teaching_gateway import NAME, FamilyTeachingToolGateway
    from app.composition.school_fact_family_teaching_query import FamilyTeachingResult
    from app.domains.students.family_read import FamilyReadEvidence
    from tests.test_f3_product_identity import guardian

    original = guardian()
    state = SimpleNamespace(
        actor=original, logged_out=False, allowed=True, boundary=False, resolves=0, proofs=0
    )

    def resolve(_):
        state.resolves += 1
        if state.logged_out:
            raise AssistantRuntimeError("synthetic logout")
        return SimpleNamespace(actor=state.actor)

    def valid(actor):
        state.proofs += 1
        return actor == original and state.allowed and not state.boundary

    evidence = FamilyReadEvidence(valid)
    query = SimpleNamespace(
        query=lambda **_: FamilyTeachingResult({"status": "available", "items": []}, evidence)
    )
    turn = FamilyTeachingToolGateway(resolve_actor=resolve, query=query).for_turn("family")
    result = turn.execute(
        ModelToolCall(call_id="history", name=NAME, arguments={}),
        ToolCallContext("family", "conversation", "turn", "run", 1, uuid4()),
    )
    state.resolves = state.proofs = 0
    assert result.history_check is not None
    return turn, result.history_check, state


def test_family_history_same_batch_has_one_fresh_resolve_after_registration():
    from app.composition.school_fact_read_evidence import validation_batch

    turn, check, state = family_history_identity_environment()
    with validation_batch():
        turn.revalidate("family")
        assert check.valid() and check.refresh()
        # Only gateway's mandatory current-context read has happened yet.
        assert state.resolves == 1 and state.proofs == 0
    assert state.resolves == 2 and state.proofs == 1
    with validation_batch():
        assert check.valid()
    assert state.resolves == 3 and state.proofs == 2


@pytest.mark.parametrize("change", ["child", "logout", "revoked", "boundary"])
@pytest.mark.parametrize("inside_batch", [False, True])
def test_family_history_identity_and_authority_changes_still_fail_closed(change, inside_batch):
    from app.composition.school_fact_read_evidence import validation_batch
    from tests.test_f3_product_identity import guardian

    _, check, state = family_history_identity_environment()

    def mutate():
        if change == "child":
            state.actor = guardian(2)
        elif change == "logout":
            state.logged_out = True
        elif change == "revoked":
            state.allowed = False
        else:
            state.boundary = True

    if inside_batch:
        with pytest.raises(AssistantRuntimeError):
            with validation_batch():
                assert check.valid()
                mutate()  # Fresh identity and proof must run after this change.
    else:
        mutate()
        assert check.valid() is False
    assert state.resolves == 1


def test_family_history_outside_batch_checks_immediately_and_never_reuses_authority():
    _, check, state = family_history_identity_environment()
    assert check.valid() is True
    assert state.resolves == 1 and state.proofs == 1
    state.allowed = False
    assert check.refresh() is False
    assert state.resolves == 2 and state.proofs == 2


@pytest.mark.parametrize("teaching_answer", [False, True])
@pytest.mark.parametrize("target_status", ["not_recorded", "known_negative"])
def test_related_record_source_does_not_become_an_absent_targets_statement(
    teaching_answer, target_status
):
    """Material preserves attribution; this is not a natural-language accuracy test."""
    from copy import deepcopy

    from app.composition.school_fact_education_gateway import education_model_material

    positions = {"status": target_status, "items": [], "coverage": "unknown"}
    if target_status == "known_negative":
        positions.update(
            notice="学校已明确登记该岗位空缺。", quality_basis=["对应岗位的明确否定依据"]
        )
    raw = {
        "status": "known",
        "positions": positions,
        "teachers": {
            "status": "known",
            "items": [
                {
                    "name": "关联老师",
                    "effective_from": "2026-09-01T00:00:00+08:00",
                    "effective_to": None,
                    "basis": {
                        "label": "仅对应任课登记的来源",
                        "published_at": "2026-08-30T00:00:00+08:00",
                        "recorded_at": "2026-08-31T00:00:00+08:00",
                    },
                }
            ],
            "coverage": "unknown",
        },
    }
    original = deepcopy(raw)
    material = education_model_material(raw, teaching_answer=teaching_answer)
    target = material["positions"]
    assert target["status"] == target_status
    assert target["authorized_result_set"] == {"returned_count": 0}
    assert target["reality_coverage"] == {"status": "unknown"}
    assert target["fact_reading"]["outcome"] == (
        "学校已明确登记此范围的否定事实"
        if target_status == "known_negative"
        else "本次条件和时点内没有当前登记"
    )
    assert "仅对应任课登记的来源" not in str(target)
    if target_status == "known_negative":
        assert target["quality_basis"] == positions["quality_basis"]
        assert target["notice"] == positions["notice"]
    else:
        assert "notice" not in target and "quality_basis" not in target
    teacher = material["teachers"]["items"][0]
    assert teacher["登记来源"] == original["teachers"]["items"][0]["basis"]["label"]
    assert teacher["effective_from"] == original["teachers"]["items"][0]["effective_from"]
    assert teacher["effective_to"] is None
    assert teacher["来源时间"] == {
        "published_at": "2026-08-30T00:00:00+08:00",
        "recorded_at": "2026-08-31T00:00:00+08:00",
    }
    assert raw == original


def test_query_material_distinguishes_returned_count_from_total_and_quality_from_source():
    from copy import deepcopy

    from app.composition.school_fact_education_gateway import education_model_material

    raw = {
        "status": "known",
        "members": {
            "status": "known",
            "items": [
                {
                    "name": "另一位同学",
                    "publication_evidence": [
                        {"applies_to": "归班关系", "source": {"label": "归班正式发布依据"}},
                    ],
                }
            ],
            "coverage": "unknown",
            "quality_basis": [],
        },
    }
    original = deepcopy(raw)
    material = education_model_material(raw, query_answer=True)["members"]
    assert material["tool_context"]["authorized_result_set"]["returned_count"] == 1
    assert material["collection_scope"]["population_total"] is None
    assert material["collection_scope"]["unreturned_records"] == "not_determined"
    assert material["supporting_evidence"]["独立质量声明"] == []
    assert "quality_basis" not in material
    evidence = material["items"][0]["supporting_evidence"]["publication_evidence"]
    assert evidence[0]["applies_to"] == "归班关系"
    assert evidence[0]["source"]["label"] == "归班正式发布依据"
    assert raw == original


@pytest.mark.parametrize("status", ["available", "no_current_records"])
def test_query_normal_selection_context_does_not_become_an_unfinished_task(status):
    from copy import deepcopy

    from app.composition.school_fact_education_gateway import education_model_material

    raw = {
        "status": status,
        "coverage": "unknown",
        "selection_scope": {"kind": "teaching_assignment", "resolution": "resolved"},
        "items": []
        if status == "no_current_records"
        else [
            {
                "name": "课程",
                "audiences": [{"name": "班级", "source": "行政班实时成员"}],
                "teachers": [{"name": "甲"}, {"name": "乙"}],
                "notices": ["未来安排"],
                "quality_status": "not_authorized",
            }
        ],
    }
    original = deepcopy(raw)
    result = education_model_material(raw, teaching_answer=True, query_answer=True)
    assert result["status"] == status
    assert "selection_scope" not in result
    assert result["tool_context"]["selection_scope"] == raw["selection_scope"]
    assert result["reality_coverage"] == {"status": "unknown"}
    assert result["collection_scope"]["population_total"] is None
    assert "meaning" not in result["collection_scope"]
    if result["items"]:
        item = result["items"][0]
        assert item["teachers"] == raw["items"][0]["teachers"]
        audience = item["audiences"][0]
        assert audience["name"] == "班级" and "membership_basis" not in audience
        assert audience["related_context"]["membership_basis"] == "随行政班成员关系变化"
        assert item["notices"] == ["未来安排"]
        assert item["quality_status"] == "not_authorized"
    assert raw == original


@pytest.mark.parametrize(
    "selection",
    [
        {"kind": "teaching_assignment", "resolution": "ambiguous"},
        {"kind": "future_kind", "resolution": "resolved"},
        {"kind": "teaching_assignment", "resolution": "resolved", "future_risk": "retain"},
    ],
)
def test_query_keeps_ambiguous_or_unknown_selection_in_primary_material(selection):
    from app.composition.school_fact_education_gateway import education_model_material

    result = education_model_material(
        {"status": "conflict", "selection_scope": selection, "items": [], "coverage": "partial"},
        query_answer=True,
    )
    assert result["selection_scope"] == selection
    assert result["status"] == "conflict"
    assert result["reality_coverage"] == {"status": "partial"}


def test_query_normal_offering_state_is_related_context_but_teachers_and_risks_stay_primary():
    from copy import deepcopy

    from app.composition.school_fact_education_gateway import education_model_material

    teachers = [
        {"name": "甲老师", "role": "主讲", "subjects": ["数学"], "reference": "teacher-a"},
        {"name": "乙老师", "role": "共同任教", "subjects": ["物理"], "reference": "teacher-b"},
    ]
    raw = {
        "status": "available",
        "coverage": "partial",
        "future_basis": "按目前安排",
        "items": [
            {
                "course": "综合课程",
                "offering": "正式开课",
                "offering_status": state,
                "teachers": teachers,
                "teacher_status": "conflict",
                "notices": ["教师安排存在争议"],
                "quality": [{"dispute_status": "open"}],
                "effective_from": "2030-09-01T00:00:00+08:00",
                "effective_to": "2031-01-01T00:00:00+08:00",
            }
            for state in ("持续开设", "暂停", "unknown-future-state")
        ],
    }
    original = deepcopy(raw)
    result = education_model_material(raw, teaching_answer=True, query_answer=True)
    current, paused, unknown = result["items"]
    assert "offering_status" not in current
    assert current["related_context"]["offering_status"] == "持续开设"
    assert paused["offering_status"] == "暂停"
    assert unknown["offering_status"] == "unknown-future-state"
    assert "related_context" not in paused and "related_context" not in unknown
    for item in result["items"]:
        assert item["course"] == "综合课程" and item["offering"] == "正式开课"
        assert [teacher["role"] for teacher in item["teachers"]] == ["主讲", "共同任教"]
        assert [teacher["subjects"] for teacher in item["teachers"]] == [["数学"], ["物理"]]
        assert item["teachers"][1]["tool_context"]["reference"] == "teacher-b"
        assert item["teacher_status"] == "conflict"
        assert item["notices"] == ["教师安排存在争议"]
        assert item["quality"] == [{"dispute_status": "open"}]
        assert item["supporting_evidence"]["effective_to"] == "2031-01-01T00:00:00+08:00"
    assert result["reality_coverage"] == {"status": "partial"}
    assert result["future_basis"] == "按目前安排"
    assert raw == original
    # Records and maintenance projections retain their existing field layout.
    assert education_model_material(raw)["items"][0]["offering_status"] == "持续开设"


@pytest.mark.parametrize(
    ("status", "label", "normal"),
    [
        ("enrolled", "正常在班", True),
        ("suspended", "休学，保留原班关联", False),
        ("graduated", "已毕业，班关联待核实", False),
        ("transferred_out", "已转出，班关联待核实", False),
        ("conflict", "在校状态有冲突", False),
        (None, "在校状态未明确", False),
        ("enrolled", "unknown-label", False),
        ("new-status", "正常在班", False),
    ],
)
def test_query_only_known_normal_class_member_status_moves_to_its_related_context(
    status, label, normal
):
    from copy import deepcopy

    from app.composition.school_fact_education_gateway import education_model_material

    row = {"name": "甲同学", "enrollment_status": status, "status_label": label}
    raw = {
        "status": "known",
        "name": "一班",
        "members": {"status": "known", "items": [row], "coverage": "unknown"},
        "other_records": {"items": [deepcopy(row)]},
    }
    original = deepcopy(raw)
    result = education_model_material(raw, query_answer=True)
    member = result["members"]["items"][0]
    assert member["name"] == "甲同学"
    if normal:
        assert "enrollment_status" not in member and "status_label" not in member
        assert member["related_context"] == {
            "enrollment_status": status,
            "status_label": label,
        }
    else:
        assert member["enrollment_status"] == status
        assert member["status_label"] == label
        assert "related_context" not in member
    assert result["other_records"]["items"][0] == row
    assert result["members"]["reality_coverage"] == {"status": "unknown"}
    assert raw == original


@pytest.mark.parametrize(
    ("status", "label", "normal"),
    [
        ("enrolled", "在读", True),
        ("suspended", "休学", False),
        ("unknown", "未取得学生状态", False),
    ],
)
def test_query_participation_keeps_actual_participation_unknown_and_only_layers_normal_enrollment(
    status, label, normal
):
    from app.composition.school_fact_education_gateway import education_model_material

    participation = {
        "enrollment_status": status,
        "enrollment_label": label,
        "actual_participation": "unknown",
    }
    result = education_model_material({"student_participation": participation}, query_answer=True)[
        "student_participation"
    ]
    assert result["actual_participation"] == "unknown"
    if normal:
        assert "enrollment_status" not in result and "enrollment_label" not in result
        assert result["related_context"] == {
            "enrollment_status": status,
            "enrollment_label": label,
        }
    else:
        assert result == participation


def test_query_normal_state_layering_preserves_unrecognized_shapes_and_context_collisions():
    from app.composition.school_fact_education_gateway import education_model_material

    member = {
        "name": "甲同学",
        "enrollment_status": "enrolled",
        "status_label": "正常在班",
        "related_context": {"enrollment_status": "unrecognized-previous-state"},
    }
    # A similarly named value outside the known offering projection is not reclassified.
    other = {"course": "课程", "offering_status": "持续开设", "unrecognized": "retain"}
    participation = {
        "enrollment_status": "enrolled",
        "enrollment_label": "在读",
        "actual_participation": "unrecognized-new-evidence",
    }
    result = education_model_material(
        {
            "members": {"items": [member]},
            "items": [other],
            "student_participation": participation,
        },
        query_answer=True,
    )
    assert result["members"]["items"][0] == member
    assert result["items"][0] == other
    assert result["student_participation"] == participation


def test_prepare_material_summarizes_each_change_without_losing_full_evidence():
    from copy import deepcopy

    from app.composition.school_fact_education_gateway import education_model_material

    first = {
        "fact_kind": "teaching_assignment",
        "label": "任教",
        "reference": "target-a",
        "operation": "upsert",
        "effective_at": "2030-01-01T00:00:00+08:00",
        "before": {"assignment_ref": "old-ref", "teaching_role": "lead"},
        "after": {"assignment_ref": "new-ref", "teaching_role": "lead"},
        "reference_labels": {"old-ref": "甲", "new-ref": "乙"},
        "reference_labels_as_of": "2030-01-01T00:00:00+08:00",
        "before_segments": [],
        "after_segments": [],
    }
    second = {
        "fact_kind": "class",
        "label": "班级",
        "reference": "target-b",
        "operation": "correct",
        "effective_at": "2020-01-01T00:00:00+08:00",
        "before": {"display_name": "一班"},
        "after": {"display_name": "一班"},
        "before_segments": [{"fields": {"display_name": "旧名"}}],
        "after_segments": [{"fields": {"display_name": "新名"}}],
    }
    summary = {**first, "impacts": [first, second], "regroup": {"notice": "范围警告"}}
    raw = {"proposal_ref": "proposal", "summary": summary}
    original = deepcopy(raw)
    material = education_model_material(raw)
    items = material["summary"]["items"]
    assert len(items) == 2
    assert [c["field"] for c in items[0]["changes"]] == ["任课教师"]
    assert items[0]["changes"][0]["values"]["before"]["关联名称"]["任课教师"] == "甲"
    assert items[1]["changes"] == []
    assert items[1]["timeline_changed"] is True
    assert items[1]["operation"] == "纠正历史时间段"
    assert material["summary"]["regroup"] == {"notice": "范围警告"}
    assert set(material["supporting_evidence"]["interpretation"]) == {"time"}
    assert items[1]["timeline_changes"]["before_segments"]
    assert items[1]["timeline_changes"]["after_segments"]
    assert material["tool_context"]["proposal_ref"] == "proposal"
    assert raw == original


@pytest.mark.parametrize(
    "operation", ["upsert", "end", "revoke", "reactivate", "correct", "cancel"]
)
@pytest.mark.parametrize(
    "before,after", [(None, {"display_name": "新"}), ({"display_name": "旧"}, None)]
)
def test_prepare_material_preserves_missing_side_and_all_operations(operation, before, after):
    from app.composition.school_fact_education_gateway import education_model_material

    material = education_model_material(
        {
            "proposal_ref": "proposal",
            "summary": {
                "fact_kind": "class",
                "operation": operation,
                "before": before,
                "after": after,
            },
        }
    )
    change = material["summary"]["items"][0]["changes"][0]
    assert (change["values"]["before"] is None) == (before is None)
    assert (change["values"]["after"] is None) == (after is None)
    assert "supporting_evidence" not in material


def test_prepare_model_omits_unchanged_payload_but_preserves_changed_timeline_and_refs():
    import json
    from copy import deepcopy

    from app.composition.school_fact_education_gateway import education_model_material

    stable = "unchanged-value-" * 300
    before = {"assignment_ref": "old-reference", "teaching_role": stable}
    after = {**before, "assignment_ref": "new-reference"}
    summary = {
        "fact_kind": "teaching_assignment",
        "reference": "target-reference",
        "label": "任教",
        "operation": "upsert",
        "before": before,
        "after": after,
        "reference_labels": {"old-reference": "甲", "new-reference": "乙", "unused": stable},
        "before_segments": [{"effective_at": "2020-01-01", "valid_to": None, "fields": before}],
        "after_segments": [
            {"effective_at": "2020-01-01", "valid_to": "2030-01-01", "fields": before},
            {"effective_at": "2030-01-01", "valid_to": None, "fields": after},
        ],
    }
    raw = {"proposal_ref": "proposal", "summary": {**summary, "impacts": [summary]}}
    original = deepcopy(raw)
    material = education_model_material(raw)
    encoded = json.dumps(material)
    assert stable not in encoded
    assert "unused" not in encoded
    assert len(encoded) < len(json.dumps(raw)) / 2
    entry = material["summary"]["items"][0]
    assert entry["tool_context"]["reference"] == "target-reference"
    assert len(entry["timeline_changes"]["after_segments"]) == 2
    assert (
        entry["timeline_changes"]["before_segments"][0]["fields"]["tool_context"]["assignment_ref"]
        == "old-reference"
    )
    assert "关联名称" not in entry["timeline_changes"]["before_segments"][0]["fields"]
    assert raw == original


def _material_time_explanations(value):
    if isinstance(value, list):
        return [text for item in value for text in _material_time_explanations(item)]
    if not isinstance(value, dict):
        return []
    explanation = value.get("interpretation", {}).get("time")
    return ([explanation] if explanation else []) + [
        text for item in value.values() for text in _material_time_explanations(item)
    ]


def test_prepare_time_meaning_is_single_evidence_without_losing_actual_change_or_references():
    from copy import deepcopy

    from app.composition.school_fact_education_gateway import education_model_material

    before = {"assignment_ref": "old-teacher", "teaching_role": "lead"}
    after = {**before, "assignment_ref": "new-teacher"}
    start, changed_at = "2030-01-01T00:00:00+08:00", "2030-09-10T00:00:00+08:00"
    summary = {
        "fact_kind": "teaching_assignment",
        "label": "持续任课",
        "reference": "teaching-target",
        "operation": "upsert",
        "effective_at": changed_at,
        "reason": "已确定的教师交接",
        "source_label": "正式任教登记",
        "reference_labels": {"old-teacher": "甲老师", "new-teacher": "乙老师"},
        "before": before,
        "after": after,
        "before_segments": [{"effective_at": start, "valid_to": None, "fields": before}],
        "after_segments": [
            {"effective_at": start, "valid_to": changed_at, "fields": before},
            {"effective_at": changed_at, "valid_to": None, "fields": after},
        ],
    }
    raw = {"proposal_ref": "pending-proposal", "summary": summary}
    original = deepcopy(raw)
    material = education_model_material(raw)

    explanations = _material_time_explanations(material)
    assert len(explanations) == 1
    assert material["supporting_evidence"]["interpretation"]["time"] == explanations[0]
    assert "未来安排不因已登记就成为当前有效事实" in explanations[0]
    assert "未登记截止时间不表示事实已结束" in explanations[0]
    assert "interpretation" not in material
    assert material["tool_context"]["proposal_ref"] == "pending-proposal"
    item = material["summary"]["items"][0]
    assert item["operation"] == "登记或更新"
    assert item["effective_at"] == changed_at
    assert item["reason"] == summary["reason"]
    assert item["source_label"] == summary["source_label"]
    assert item["tool_context"]["reference"] == "teaching-target"
    assert [change["field"] for change in item["changes"]] == ["任课教师"]
    values = item["changes"][0]["values"]
    assert values["before"]["关联名称"]["任课教师"] == "甲老师"
    assert values["after"]["tool_context"]["assignment_ref"] == "new-teacher"
    assert item["timeline_changed"] is True
    segments = item["timeline_changes"]
    assert segments["before_segments"][0]["effective_at"] == start
    assert segments["after_segments"][0]["valid_to"] == changed_at
    assert segments["after_segments"][1]["valid_to"] is None
    assert (
        segments["after_segments"][1]["fields"]["tool_context"]["assignment_ref"] == "new-teacher"
    )
    assert raw == original


def test_prepare_timeline_only_correction_remains_explicit_with_single_time_meaning():
    from app.composition.school_fact_education_gateway import education_model_material

    fields = {"display_name": "一班"}
    old_at, new_at = "2029-09-02T00:00:00+08:00", "2029-09-01T00:00:00+08:00"
    raw = {
        "proposal_ref": "history-proposal",
        "summary": {
            "fact_kind": "class",
            "reference": "class-reference",
            "operation": "correct",
            "before": fields,
            "after": fields,
            "effective_at": new_at,
            "before_segments": [{"effective_at": old_at, "valid_to": None, "fields": fields}],
            "after_segments": [{"effective_at": new_at, "valid_to": None, "fields": fields}],
            "regroup": {"notice": "正式历史补正", "affected_count": 1},
        },
    }
    material = education_model_material(raw)
    assert len(_material_time_explanations(material)) == 1
    item = material["summary"]["items"][0]
    assert item["changes"] == [] and item["timeline_changed"] is True
    assert item["operation"] == "纠正历史时间段"
    assert item["timeline_changes"]["before_segments"][0]["effective_at"] == old_at
    assert item["timeline_changes"]["after_segments"][0]["effective_at"] == new_at
    assert material["summary"]["regroup"] == raw["summary"]["regroup"]
    assert material["tool_context"]["proposal_ref"] == "history-proposal"


def test_prepare_material_preserves_unknown_batch_shape_without_claiming_simple_diff():
    from app.composition.school_fact_education_gateway import education_model_material

    raw = {"proposal_ref": "proposal", "summary": {"impacts": [{"unrecognized": "risk"}]}}
    material = education_model_material(raw)
    assert material["summary"]["impacts"] == [{"unrecognized": "risk"}]


def test_prepare_time_grouping_does_not_change_unknown_contract_or_non_time_interpretation():
    from app.composition.school_fact_education_gateway import education_model_material

    summary = {
        "fact_kind": "class",
        "operation": "correct",
        "effective_at": "2029-09-01T00:00:00+08:00",
        "before": {"display_name": "一班"},
        "after": {"display_name": "二班"},
        "regroup": {"coverage": "unknown", "notice": "尚须核对相关范围"},
    }
    material = education_model_material({"proposal_ref": "proposal", "summary": summary})
    assert "coverage" in material["interpretation"]
    assert "time" not in material["interpretation"]
    assert material["summary"]["regroup"] == summary["regroup"]
    assert len(_material_time_explanations(material)) == 1

    unknown = {**summary, "future_risk": "保留原始合同"}
    fallback = education_model_material({"proposal_ref": "proposal", "summary": unknown})
    assert fallback["summary"]["future_risk"] == "保留原始合同"
    assert "items" not in fallback["summary"]
    assert "time" in fallback["interpretation"]
    assert "supporting_evidence" not in fallback


@pytest.mark.parametrize("side", ["before_segments", "after_segments"])
def test_prepare_model_preserves_unpaired_segment_fields_and_extra_metadata(side):
    from app.composition.school_fact_education_gateway import education_model_material

    segment = {
        "effective_at": "2030-01-01",
        "valid_to": None,
        "fields": {"assignment_ref": "future-reference", "teaching_role": "lead"},
        "future_notice": "additional risk",
    }
    summary = {
        "fact_kind": "teaching_assignment",
        "operation": "cancel",
        "before": None,
        "after": None,
        "before_segments": [],
        "after_segments": [],
        side: [segment],
    }
    material = education_model_material({"proposal_ref": "proposal", "summary": summary})
    retained = material["summary"]["items"][0]["timeline_changes"][side][0]
    assert retained["fields"]["tool_context"]["assignment_ref"] == "future-reference"
    assert retained["fields"]["teaching_role"] == "主讲"
    assert retained["future_notice"] == "additional risk"


def test_prepare_model_keeps_source_only_timeline_changes_and_unknown_risks():
    from app.composition.school_fact_education_gateway import education_model_material

    segment = {
        "effective_at": "2020-01-01",
        "valid_to": None,
        "fields": {"display_name": "一班"},
        "source": {"published_at": "2021-01-01"},
    }
    summary = {
        "fact_kind": "class",
        "operation": "correct",
        "before": {"display_name": "一班"},
        "after": {"display_name": "一班"},
        "before_segments": [segment],
        "after_segments": [{**segment, "source": {"published_at": "2022-01-01"}}],
    }
    material = education_model_material({"proposal_ref": "proposal", "summary": summary})
    item = material["summary"]["items"][0]
    assert item["changes"] == []
    assert item["timeline_changes"]["before_segments"][0]["source"]["published_at"] == "2021-01-01"
    assert item["timeline_changes"]["after_segments"][0]["source"]["published_at"] == "2022-01-01"
    for modified in (
        {**summary, "future_risk": "retain"},
        {**summary, "impacts": [{**summary, "future_risk": "retain"}]},
    ):
        fallback = education_model_material({"proposal_ref": "proposal", "summary": modified})
        assert "items" not in fallback["summary"]
        assert "retain" in str(fallback)


def test_education_describe_explains_existing_partial_update_and_contextual_teacher_label():
    from app.composition.school_fact_education_product import (
        EducationProductAdapter,
        education_field_descriptor,
    )

    adapter = EducationProductAdapter(repository=SimpleNamespace(), external=SimpleNamespace())
    description = adapter.describe(actor=actor(), fact_kind="teaching_assignment")
    rules = description["input_semantics"]
    assert "完整内容" in rules["required"]
    assert "仅传修改字段" in rules["existing_upsert"]
    assert "省略" in rules["existing_upsert"]
    assert "完整" in rules["creation_or_no_original"]
    assert "null" in rules["null"] and "校验" in rules["null"]
    field = next(f for f in description["fields"] if f["name"] == "assignment_ref")
    assert field["required"] is True
    assert field["label"] == "任课教师"
    assert field["reference_kind"] == "assignment"
    assert (
        education_field_descriptor("assignment_ref", "class_teacher_assignment")["label"]
        == "教职工任职"
    )


def test_education_maintenance_discovery_explains_person_assignment_dependency():
    from app.composition.school_fact_education_gateway import (
        EDUCATION_ASSISTANT_INSTRUCTION,
        EducationToolGateway,
    )

    prepare = next(d for d in EducationToolGateway.descriptors if d.kind == "prepare")
    for guidance in (EDUCATION_ASSISTANT_INSTRUCTION, prepare.description):
        assert "school_facts.directory.lookup" in guidance
        assert "assignment" in guidance
        assert "任职引用" in guidance
    assert "一并选择" in EDUCATION_ASSISTANT_INSTRUCTION
    assert prepare.load_on_demand
    assert "确认" in prepare.description


def test_prepare_material_uses_verified_relationship_names_without_losing_tool_coordinates():
    from app.composition.school_fact_education_gateway import education_model_material

    material = education_model_material(
        {
            "summary": {
                "fact_kind": "teaching_assignment",
                "reference_labels": {"old-ref": "甲老师", "new-ref": "乙老师"},
                "reference_labels_as_of": "2030-01-01T00:00:00+08:00",
                "before": {"assignment_ref": "old-ref", "teaching_role": "lead"},
                "after": {"assignment_ref": "new-ref", "teaching_role": "lead"},
            }
        }
    )["summary"]
    assert "reference_labels" not in material
    assert material["tool_context"]["reference_labels"] == {
        "old-ref": "甲老师",
        "new-ref": "乙老师",
    }
    assert material["before"]["关联名称"]["任课教师"] == "甲老师"
    assert material["after"]["关联名称"]["任课教师"] == "乙老师"
    assert material["before"]["tool_context"]["assignment_ref"] == "old-ref"
    assert material["after"]["tool_context"]["assignment_ref"] == "new-ref"


@pytest.mark.parametrize("section", ["members", "teachers", "courses"])
@pytest.mark.parametrize("coverage", ["unknown", "partial", "complete"])
@pytest.mark.parametrize("count", [0, 1, 3])
def test_query_collections_keep_observed_facts_separate_from_unestablished_population(
    section, coverage, count
):
    from copy import deepcopy

    from app.composition.school_fact_education_gateway import education_model_material

    raw = {
        "status": "known",
        "name": "独立对象",
        section: {
            "status": "known" if count else "not_recorded",
            "items": [{"name": f"获准对象{index}"} for index in range(count)],
            "coverage": coverage,
        },
    }
    original = deepcopy(raw)
    group = education_model_material(raw, query_answer=True)[section]

    assert group["items"] == original[section]["items"]
    assert group["status"] == original[section]["status"]
    assert "fact_reading" not in group
    assert group["tool_context"]["fact_reading"]["outcome"] == (
        "已取得本次所查正式登记" if count else "本次条件和时点内没有当前登记"
    )
    assert group["collection_scope"] == {
        "basis": "current_identity_and_query",
        "unreturned_records": "not_determined",
        "population_total": None,
        "whole_register_total": None,
    }
    # A publication saying registration is complete still does not establish
    # that this identity can see the entire population. The observed count is
    # retained as operational metadata, not advertised as an answer statistic.
    assert group["reality_coverage"] == {"status": coverage}
    assert "authorized_result_set" not in group
    assert group["tool_context"]["authorized_result_set"] == {"returned_count": count}
    assert raw == original


def test_query_collection_projection_preserves_conflict_and_pagination():
    from app.composition.school_fact_education_gateway import education_model_material

    raw = {
        "status": "conflict",
        "items": [{"name": "第一项", "reference": "safe-reference"}],
        "coverage": "partial",
        "has_more": True,
        "truncated": True,
        "next_cursor": "opaque-authorized-cursor",
        "notice": "所问关系存在未解决冲突",
    }
    material = education_model_material(raw, query_answer=True)
    assert material["status"] == "conflict"
    assert material["notice"] == raw["notice"]
    assert material["has_more"] is True and material["truncated"] is True
    assert material["next_cursor"] == raw["next_cursor"]
    assert material["items"][0]["tool_context"]["reference"] == "safe-reference"
    assert material["tool_context"]["authorized_result_set"] == {
        "returned_count": 1,
        "has_more": True,
        "truncated": True,
    }


def test_query_collection_metadata_does_not_displace_tool_coordinates():
    from app.composition.school_fact_education_gateway import education_model_material

    material = education_model_material(
        {
            "status": "known",
            "items": [{"name": "获准课程"}],
            "observed_count": 1,
            "reference": "signed-selection",
            "version": 7,
            "coverage": "complete",
        },
        query_answer=True,
    )
    assert "observed_count" not in material
    assert material["tool_context"]["observed_count"] == 1
    assert material["tool_context"]["reference"] == "signed-selection"
    assert material["tool_context"]["version"] == 7
    assert material["status"] == "known"
    assert material["collection_scope"]["population_total"] is None


@pytest.mark.parametrize("status", ["denied", "unavailable", "source_unavailable"])
def test_query_collection_failure_is_not_projected_as_observed_empty_set(status):
    from app.composition.school_fact_education_gateway import education_model_material

    material = education_model_material(
        {"status": status, "items": [], "message": "本次未取得所问资料"},
        query_answer=True,
    )
    assert material == {
        "status": status,
        "items": [],
        "message": "本次未取得所问资料",
    }


def test_query_count_preserves_its_authorized_scope_without_claiming_whole_register():
    from app.composition.school_fact_education_gateway import education_model_material

    raw = {
        "status": "known",
        "items": [{"name": "可见对象甲"}, {"name": "可见对象乙"}],
        "matched_count": 5,
        "observed_count": 2,
        "scope": "当前身份获准读取的指定关系",
        "coverage": "complete",
        "has_more": True,
        "next_cursor": "authorized-next-page",
    }
    material = education_model_material(raw, query_answer=True)
    assert material["matched_count"] == 5
    assert material["scope"] == raw["scope"]
    assert material["tool_context"]["observed_count"] == 2
    assert material["tool_context"]["authorized_result_set"]["returned_count"] == 2
    assert material["collection_scope"]["whole_register_total"] is None
    assert material["collection_scope"]["population_total"] is None
    assert material["reality_coverage"] == {"status": "complete"}
    assert material["has_more"] is True
    assert material["next_cursor"] == raw["next_cursor"]
