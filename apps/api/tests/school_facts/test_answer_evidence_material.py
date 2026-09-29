"""Answer materials separate evidence without changing facts or their authorization."""

from copy import deepcopy
from importlib import import_module, util

import pytest


def project(value):
    name = "app.composition.school_fact_answer_evidence"
    assert util.find_spec(name), "Authorized query evidence needs a shared presentation boundary"
    return import_module(name).answer_evidence_material(value)


def test_unknown_quality_is_evidence_not_a_default_warning_or_untrusted_fact():
    raw = {
        "status": "known",
        "name": "独立教师",
        "coverage": {"status": "unknown"},
        "quality": {"coverage": "unknown", "conflict": "unknown", "freshness": "unknown"},
        "source": {"label": "学校登记", "published_at": "2031-02-03T00:00:00Z"},
    }
    original = deepcopy(raw)
    result = project(raw)
    assert raw == original
    assert result["status"] == "known" and result["name"] == "独立教师"
    assert "quality" not in result and "source" not in result
    assert result["supporting_evidence"]["quality"] == raw["quality"]
    assert result["coverage"] == raw["coverage"]
    assert "answer_constraints" not in result


def test_negative_fact_keeps_exact_object_field_and_effective_time():
    claim = {
        "quality_kind": "negative",
        "negative_code": "no_residence",
        "target_fact_kind": "student_bed_residence",
        "field_name": None,
        "valid_from": "2031-02-03T00:00:00Z",
        "valid_to": None,
    }
    result = project({"status": "known_negative", "name": "孩子乙", "quality": [claim]})
    assert result["status"] == "known_negative" and result["name"] == "孩子乙"
    assert result["answer_constraints"]["established_absences"] == [claim]


@pytest.mark.parametrize(
    "field,value",
    [
        ("dispute_status", "open"),
        ("source_status", "unavailable"),
        ("evidence_status", "denied"),
        ("freshness", "review_overdue"),
        ("evidence_truncated", True),
        ("conflict", "contradictory_evidence"),
    ],
)
def test_actual_risks_remain_with_their_owner_not_siblings(field, value):
    claim = {field: value, "field_name": "phone", "target_ref": "allowed-target"}
    raw = {
        "items": [
            {"name": "甲", "quality": claim, "valid_to": "2031-03-01T00:00:00Z"},
            {"name": "乙", "quality": {"conflict": "unknown"}},
        ]
    }
    result = project(raw)
    first, second = result["items"]
    assert first["answer_constraints"]["issues_affecting_answer"] == [claim]
    assert first["valid_to"] == raw["items"][0]["valid_to"]
    assert "answer_constraints" not in second and "answer_constraints" not in result


def test_field_scoped_quality_and_readable_directory_claims_preserve_binding():
    claim = {
        "kind": "正式否定声明",
        "scope": {"target_name": "甲办公室"},
        "details": {"negative_code": "未设办公地点"},
        "tool_context": {"target_ref": "ref"},
    }
    raw = {
        "quality": {"phone": {"source_status": "unavailable"}},
        "readable_quality": {"statements": [claim], "conflict": "正式资料存在待核实争议"},
    }
    result = project(raw)
    constraints = result["answer_constraints"]
    assert any(item.get("field") == "phone" for item in constraints["issues_affecting_answer"])
    assert result["supporting_evidence"]["readable_quality"]["statements"] == [claim]


def test_opaque_handles_pagination_and_restrictions_stay_usable():
    raw = {
        "status": "partial",
        "query_scope": {"reference": "selected", "as_of": "2031-01-01"},
        "tool_context": {"targets": [{"reference": "same-object", "version": 2}]},
        "has_more": True,
        "next_cursor": "signed-cursor",
        "truncated": True,
        "field_restrictions": {"private_phone": "denied"},
        "limitations": ["source_quality_unavailable"],
        "quality": [],
    }
    result = project(raw)
    for key in raw.keys() - {"quality"}:
        assert result[key] == raw[key]


def test_formal_fields_and_unrecognized_payloads_are_not_recursively_rewritten():
    raw = {
        "records": [
            {
                "fields": {
                    "quality": "原文",
                    "source": "原文来源",
                    "items": [{"quality": {"conflict": "open"}}],
                },
                "source": {"label": "正式发布"},
            }
        ],
        "custom_payload": {"quality": {"conflict": "open"}},
    }
    result = project(raw)
    assert result["records"][0]["fields"] == raw["records"][0]["fields"]
    assert result["custom_payload"] == raw["custom_payload"]
    assert result["records"][0]["supporting_evidence"]["source"] == {"label": "正式发布"}


def test_existing_supporting_evidence_and_quality_profiles_are_not_lost():
    raw = {
        "supporting_evidence": {"source": {"label": "已有"}},
        "source": {"label": "新来源"},
        "quality_profile_index": 0,
        "quality_profiles": [{"readable_quality": {"coverage": "覆盖未确认"}}],
    }
    result = project(raw)
    assert result["source"] == raw["source"]  # Do not overwrite an ambiguous existing slot.
    assert result["supporting_evidence"] == raw["supporting_evidence"]
    assert result["quality_profiles"] == raw["quality_profiles"]
    assert result["quality_profile_index"] == 0


def test_existing_separated_evidence_keeps_negative_and_explicit_family_risks_primary():
    raw = {
        "items": [
            {
                "name": "甲",
                "supporting_evidence": {
                    "quality": [{"negative_code": "no_residence", "valid_from": "2031-01-01"}]
                },
            },
            {"name": "乙", "quality": {"phone": {"status": "claims", "risks": ["学校登记争议"]}}},
        ]
    }
    result = project(raw)
    assert result["items"][0]["answer_constraints"]["established_absences"]
    assert result["items"][1]["answer_constraints"]["issues_affecting_answer"] == [
        {"field": "phone", "evidence": raw["items"][1]["quality"]["phone"]}
    ]


def test_real_directory_negative_projection_retains_scoped_absence_constraint():
    from app.composition.school_fact_directory_gateway import directory_model_material

    raw = {
        "quality": {
            "existence": "formal_negative",
            "statements": [
                {
                    "kind": "negative",
                    "negative_code": "no_office",
                    "owner_kind": "organization",
                    "target_fact_kind": "organization_workplace",
                    "target_ref": "target-A",
                    "valid_from": "2031-01-01T00:00:00Z",
                    "valid_to": None,
                }
            ],
        }
    }
    result = project(directory_model_material(raw))
    claim = result["answer_constraints"]["established_absences"][0]
    assert claim["tool_context"]["target_ref"] == "target-A"
    assert claim["valid_to"] is None


def test_compact_history_claim_inherits_group_scope_without_repeating_all_claims():
    raw = {
        "readable_quality": {
            "conflict": "正式资料存在待核实争议",
            "statement_groups": [
                {
                    "scope": {"target_name": "甲对象"},
                    "tool_context": {"target_ref": "target-A"},
                    "valid_from": "2031-01-01",
                    "valid_to": None,
                    "statements": [
                        {
                            "kind": "正式否定",
                            "details": {"正式否定事项": "未设办公地点"},
                            "tool_context": {"reference": "claim-1"},
                        },
                        {"kind": "来源与责任", "details": {"责任人": "办公室"}},
                    ],
                }
            ],
        }
    }
    result = project(raw)
    constraint = result["answer_constraints"]
    absence = constraint["established_absences"][0]
    assert absence["scope"] == {"target_name": "甲对象"}
    assert absence["tool_context"] == {"target_ref": "target-A", "reference": "claim-1"}
    assert absence["valid_from"] == "2031-01-01" and absence["valid_to"] is None
    assert "statement_groups" not in constraint["issues_affecting_answer"][0]
    assert result["supporting_evidence"]["readable_quality"] == raw["readable_quality"]


def test_real_education_query_keeps_absence_and_dispute_with_own_relation():
    from app.composition.school_fact_education_gateway import education_model_material

    raw = {
        "name": "独立班级",
        "teachers": {
            "status": "known_negative",
            "items": [],
            "coverage": "unknown",
            "notice": "学校已明确登记当前未安排班主任。",
            "quality_basis": ["任教登记依据"],
        },
        "members": {
            "status": "conflict",
            "items": [{"name": "学生甲"}],
            "notice": "所问名单存在争议",
            "quality_basis": ["名单核对依据"],
        },
    }
    result = project(education_model_material(raw, query_answer=True))
    for relation in ("teachers", "members"):
        assert result[relation]["status"] == raw[relation]["status"]
        assert result[relation]["notice"] == raw[relation]["notice"]
        assert (
            result[relation]["supporting_evidence"]["独立质量声明"]
            == (raw[relation]["quality_basis"])
        )
    assert "answer_constraints" not in result


@pytest.mark.parametrize("history", [False, True])
def test_directory_local_claim_risks_survive_unknown_aggregate_without_borrowing_scope(history):
    from app.composition.school_fact_directory_gateway import (
        directory_history_material,
        directory_model_material,
    )

    def claim(target, kind, **fields):
        return {
            "kind": kind,
            "target_ref": target,
            "owner_kind": "organization",
            "target_fact_kind": "official_contact",
            "applies_to_records": [target],
            "applies_to_query": False,
            "valid_from": "2031-01-01T00:00:00Z",
            "valid_to": None,
            **fields,
        }

    raw = {
        "quality": {
            "conflict": "unknown",
            "source_availability": "unknown",
            "statements": [
                claim("target-A", "dispute", dispute_status="open"),
                claim("target-A", "source", source_status="available"),
                claim("target-B", "source", source_status="unavailable"),
                claim("target-C", "source", source_status="available"),
            ],
        }
    }
    convert = directory_history_material if history else directory_model_material
    result = project(convert(raw))
    issues = result["answer_constraints"]["issues_affecting_answer"]
    assert len(issues) == 2
    assert {item["tool_context"]["target_ref"] for item in issues} == {"target-A", "target-B"}
    for item in issues:
        assert item["scope"]["application"] == "仅限声明列明的对象与条件"
        assert item["valid_from"] == "2031-01-01T00:00:00Z" and item["valid_to"] is None
    assert result["supporting_evidence"]["readable_quality"] == convert(raw)["readable_quality"]


def test_family_education_quality_keeps_exact_teacher_item_year_and_effective_time():
    from app.composition.school_fact_education_gateway import education_model_material

    raw = {
        "teachers": [{"name": "甲"}, {"name": "乙"}],
        "quality": {
            "teachers": [
                {
                    "quality_kind": "source",
                    "source_status": "unavailable",
                    "scope": "item",
                    "item_index": 1,
                    "effective_at": "2031-01-01T00:00:00Z",
                    "valid_to": None,
                    "academic_year": 2030,
                    "field_name": "phone",
                }
            ]
        },
    }
    result = project(education_model_material(raw))
    issue = result["answer_constraints"]["issues_affecting_answer"][0]
    assert issue["field"] == "teachers"
    own = issue["evidence"]
    for key in ("scope", "item_index", "effective_at", "academic_year", "valid_to", "field_name"):
        assert own[key] == raw["quality"]["teachers"][0][key]
