"""A relationship's published status cannot describe every related teacher."""

from datetime import UTC, datetime, timedelta

import pytest

from app.composition.school_fact_family_education_query import _quality

AT = datetime(2026, 9, 24, tzinfo=UTC)


@pytest.mark.parametrize(
    "kind,status_key,status",
    [
        ("source", "source_status", "unavailable"),
        ("source", "source_status", "available"),
        ("dispute", "dispute_status", "open"),
    ],
)
def test_self_teacher_quality_stays_on_the_exact_teacher(kind, status_key, status):
    raw = {
        "records": [
            {
                "fact_kind": "class_teacher_assignment",
                "source_key": key,
                "segments": [
                    {
                        "revision": 1,
                        "effective_at": (AT - timedelta(days=1)).isoformat(),
                        "valid_to": None,
                        "content": {},
                    }
                ],
            }
            for key in ("teacher:a", "teacher:b")
        ],
        "quality": [
            {
                "field": "teachers",
                "scope": "item",
                "target_source_key": "teacher:a",
                "target_fact_kind": "class_teacher_assignment",
                "education_target_kind": "class_teacher_assignment",
                "education_target_source_key": "teacher:a",
                "quality_kind": kind,
                status_key: status,
                "source": {},
                "effective_at": (AT - timedelta(days=1)).isoformat(),
                "verified_at": (AT - timedelta(days=1)).isoformat(),
                "valid_to": None,
            }
        ],
    }
    profile = {"teachers": [{"display_name": "陈老师"}, {"display_name": "赵老师"}]}
    value, _ = _quality(raw, ("teachers",), profile, AT)
    declaration = profile["teachers"][0]["quality_declarations"][0]
    assert declaration[status_key] == status
    assert "quality_declarations" not in profile["teachers"][1]
    assert value["teachers"]["source_availability"] == "unknown"
    assert value["teachers"]["statements"][0]["item_index"] == 0
    assert "teacher:a" not in str(value)
    if status == "open":
        assert profile["teachers"][0]["status"] == "conflict"
    if status == "unavailable":
        assert profile["teachers"][0]["status"] == "source_unavailable"
    assert value["teachers"]["status"] != "source_unavailable"


def test_wrong_or_stale_exact_teacher_is_not_attached_to_a_current_item():
    raw = {
        "records": [],
        "quality": [
            {
                "field": "teachers",
                "scope": "item",
                "target_source_key": "teacher:old",
                "target_fact_kind": "class_teacher_assignment",
                "quality_kind": "source",
                "source_status": "unavailable",
                "source": {},
                "effective_at": (AT - timedelta(days=1)).isoformat(),
                "verified_at": (AT - timedelta(days=1)).isoformat(),
                "valid_to": None,
            }
        ],
    }
    profile = {"teachers": [{"display_name": "陈老师"}]}
    value, _ = _quality(raw, ("teachers",), profile, AT)
    assert "quality_declarations" not in profile["teachers"][0]
    assert value["teachers"]["source_availability"] == "unknown"
    assert value["teachers"]["status"] == "known"
