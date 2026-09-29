from pathlib import Path

import pytest

from app.composition.school_fact_education_gateway import education_model_material
from app.composition.school_fact_family_teaching_query import _teachers


def test_period_material_uses_formal_zone_and_exclusive_query_window():
    view = {
        "kind": "family_teaching",
        "fact_timezone_name": "Asia/Shanghai",
        "query_window": {
            "start": "2027-02-14T16:00:00+00:00",
            "end_exclusive": "2027-07-14T16:00:00+00:00",
        },
        "items": [{"effective_from": "2026-09-01T00:00:00+00:00", "effective_to": None}],
    }
    actual = education_model_material(view)
    assert "2027-02-15 00:00:00" in actual["query_window"]["time_labels"]["start"]
    assert "2027-07-15 00:00:00" in actual["query_window"]["time_labels"]["end_exclusive"]
    assert actual["items"][0]["effective_to"] is None
    assert "effective_to是事实截止" in actual["interpretation"]["time"]
    assert "查询窗口结束不代表任教或开课结束" in actual["interpretation"]["time"]


def test_audience_material_keeps_membership_facts_without_maintenance_guidance():
    view = {
        "items": [
            {
                "audiences": [
                    {"name": "2023班", "source": "行政班实时成员"},
                    {"name": "跨班组", "source": "独立教学名单，需单独维护"},
                ]
            }
        ]
    }
    for item in education_model_material(view)["items"]:
        for audience in item.get("audiences", []):
            assert "source" not in audience
            assert audience["membership_basis"] in {
                "随行政班成员关系变化",
                "独立登记的教学成员关系",
            }


def test_parent_contact_source_is_narrow_and_cannot_smuggle_internal_metadata():
    contact = {
        "owner": "甲校区教育组",
        "owner_kind": "organization",
        "contact_kind": "email",
        "contact_value": "office@example.test",
        "source": {
            "kind": "formal_publication",
            "label": "学校正式发布的官方渠道登记",
            "published_at": "2026-08-01T00:00:00+00:00",
            "approval_reference": "internal",
        },
    }
    with pytest.raises(ValueError):
        _teachers(
            [
                {
                    "assignment_source_key": "staff:1",
                    "name": "陈老师",
                    "official_contacts": [contact],
                }
            ],
            True,
        )


def test_model_material_preserves_channel_own_basis_without_duplicate_education_sources():
    channel = {
        "owner": "甲校区教育组",
        "contact_kind": "email",
        "contact_value": "office@example.test",
        "source": {
            "kind": "formal_publication",
            "label": "学校正式发布的官方渠道登记",
            "published_at": "2026-08-01T00:00:00+00:00",
        },
    }
    view = {
        "kind": "family_teaching",
        "fact_timezone_name": "Asia/Shanghai",
        "sources": [
            {"fact": "teaching_assignment", "source": {"published_at": "2026-09-13T20:47:00+00:00"}}
        ],
        "items": [
            {
                "course": "数学",
                "teachers": [
                    {
                        "name": "陈老师",
                        "basis": {
                            "label": "正式任教登记",
                            "published_at": "2026-09-13T20:47:00+00:00",
                            "recorded_at": None,
                        },
                        "official_contacts": [channel],
                    }
                ],
                "quality": [{"meaning": "主讲记录存在争议", "source": {"label": "正式质量依据"}}],
            }
        ],
    }
    actual = education_model_material(view)
    assert "sources" not in actual
    teacher = actual["items"][0]["teachers"][0]
    assert teacher["official_contacts"][0]["source"]["published_at"] == "2026-08-01T00:00:00+00:00"
    assert (
        "2026-08-01 08:00:00"
        in teacher["official_contacts"][0]["source"]["time_labels"]["published_at"]
    )
    assert actual["items"][0]["quality"]
    assert (
        teacher["来源时间"]["published_at"]
        != teacher["official_contacts"][0]["source"]["published_at"]
    )


def test_migration36_replaces_only_existing_child_projection_without_new_grants():
    p = Path(__file__).resolve().parents[2] / "alembic/versions/20260914_36_f5_channel_sources.py"
    source = p.read_text(encoding="utf-8")
    assert 'down_revision = "20260914_35"' in source
    assert "CREATE OR REPLACE FUNCTION app_private.f5_child_projection" in source
    assert "渠道登记" in source and "channel_publication.published_at" in source
    assert "GRANT SELECT" not in source and "CREATE TABLE" not in source


def test_publication_alone_never_creates_verification_or_coverage_assertions():
    from tests.school_facts.test_f5_teaching_query import render

    view = render()
    assert "coverage" not in view
    assert "quality" not in view["items"][0] or not view["items"][0]["quality"]
    for item in view["items"]:
        for teacher in item["teachers"]:
            assert set(teacher["basis"]) == {"label", "published_at", "recorded_at"}


def test_explicit_quality_unknown_conflict_and_source_failure_are_not_suppressed():
    from app.composition.school_fact_teaching_query import teaching_quality
    from tests.school_facts.test_f5_teaching_query import NOW

    rows = [
        {
            "offering_source_key": "offer",
            "quality_kind": "dispute",
            "dispute_status": "open",
            "source_status": "unavailable",
            "coverage_status": "unknown",
            "effective_at": "2026-01-01T00:00:00+00:00",
            "source": {"label": "正式争议声明"},
        },
        {
            "offering_source_key": "offer",
            "quality_kind": "coverage",
            "coverage_status": "unknown",
            "effective_at": "2026-01-01T00:00:00+00:00",
            "source": {"label": "正式范围声明"},
        },
    ]
    _, notices, evidence, conflict = teaching_quality(
        rows, offering_key="offer", teachers=[], at=NOW
    )
    assert conflict and len(notices) == 2
    assert "coverage" not in evidence[0]  # unrelated DB default is not a coverage statement
    assert evidence[1]["coverage"] == "unknown"  # a real scoped declaration is retained
    material = education_model_material(
        {"kind": "family_teaching", "status": "conflict", "quality": evidence, "notices": notices}
    )
    assert material["status"] == "conflict" and material["notices"] == notices
    assert material["quality"][1]["coverage"] == "unknown"


@pytest.mark.parametrize("kind", ["teaching_assignment", "class_teacher_assignment"])
def test_maintenance_source_uses_actual_record_publication_coordinates(kind):
    from app.composition.school_fact_education_views import maintenance_publication_source

    segment = {
        "recorded_at": "2026-08-01T00:00:01+00:00",
        "source": {
            "publication_reference": "publication:actual",
            "source_ref": "internal-input-path",
        },
    }
    row = {
        "fact_kind": kind,
        "publication_reference": "publication:actual",
        "source_published_at": "2026-08-01T00:00:02+00:00",
    }
    source = maintenance_publication_source(row, segment)
    assert source["reference"] == "publication:actual"
    assert source["published_at"] == row["source_published_at"]
    assert "source_ref" not in source
    prior = maintenance_publication_source(
        row, {**segment, "source": {"publication_reference": "publication:prior"}}
    )
    assert prior["published_at"] is None
    assert prior["recorded_at"] == segment["recorded_at"]
