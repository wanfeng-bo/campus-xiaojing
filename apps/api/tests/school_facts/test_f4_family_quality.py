"""Own-child quality distinguishes missing data from formal statements."""

from copy import deepcopy
from datetime import timedelta

import pytest

from tests.school_facts.test_f4_family_education import fixture
from tests.test_f3_family_read import NOW


def quality(**updates):
    return {
        "verified_at": NOW.isoformat(),
        "field": "teachers",
        "quality_kind": "negative",
        "negative_code": "no_class_teacher",
        "effective_at": (NOW - timedelta(days=1)).isoformat(),
        "valid_to": None,
        "version": 2,
        "source": {"publication_reference": "publication:test", "published_at": NOW.isoformat()},
        **updates,
    }


def test_official_negative_is_not_missing_and_keeps_factual_source_evidence():
    service, actor, state = fixture()
    state.payload["quality"] = [quality()]
    result = service.query(actor=actor, fields=("teachers",))
    value = result.public()
    assert value["profile"]["teachers"] == []
    assert value["quality"]["teachers"]["status"] == "known_negative"
    assert value["quality"]["teachers"]["coverage"] == "unknown"
    assert value["quality"]["teachers"]["statements"][0]["negative_code"] == "no_class_teacher"
    assert value["sources"][0]["fact"] == "fact_quality"
    state.payload["quality"] = []
    assert not result.evidence.valid(actor)


@pytest.mark.parametrize(
    "rows,status",
    [
        ([], "unknown"),
        ([quality(valid_to=NOW.isoformat())], "unknown"),
        ([quality(effective_at=(NOW + timedelta(days=1)).isoformat())], "unknown"),
        ([quality(quality_kind="dispute", negative_code=None, dispute_status="open")], "conflict"),
        (
            [
                quality(),
                quality(quality_kind="source", negative_code=None, source_status="unavailable"),
            ],
            "source_unavailable",
        ),
    ],
)
def test_quality_state_requires_current_explicit_statement(rows, status):
    service, actor, state = fixture()
    state.payload["quality"] = deepcopy(rows)
    value = service.query(actor=actor, fields=("teachers",)).public()
    assert value["quality"]["teachers"]["status"] == status


def test_quality_for_unrequested_field_is_rejected_without_disclosure():
    service, actor, state = fixture()
    state.payload["quality"] = [quality(field="positions")]
    value = service.query(actor=actor, fields=("teachers",)).public()
    assert value["status"] == "unavailable" and "profile" not in value


def test_partial_column_statement_never_claims_whole_teacher_set_negative():
    service, actor, state = fixture()
    state.payload["quality"] = [quality(field_name="teacher_role")]
    value = service.query(actor=actor, fields=("teachers",)).public()
    assert value["quality"]["teachers"]["status"] == "unknown"
    assert value["quality"]["teachers"]["statements"]


def test_assignment_and_official_negative_are_reported_as_conflict():
    service, actor, state = fixture()
    state.payload["quality"] = [quality()]
    state.payload["records"] = [
        {
            "fact_kind": "class_teacher_assignment",
            "source_key": "private:t",
            "segments": [
                {
                    "effective_at": (NOW - timedelta(days=1)).isoformat(),
                    "valid_to": None,
                    "content": {"teacher_role": "primary", "assignment_source_key": "private:a"},
                }
            ],
        }
    ]
    value = service.query(actor=actor, fields=("teachers",)).public()
    assert value["quality"]["teachers"]["status"] == "conflict"
    assert "private:" not in str(value)


def test_only_unique_authorized_calendar_timezone_is_reported():
    service, actor, state = fixture()

    def calendar(key, zone):
        return {
            "fact_kind": "education_calendar_rule",
            "source_key": key,
            "segments": [
                {
                    "effective_at": (NOW - timedelta(days=2)).isoformat(),
                    "valid_to": None,
                    "content": {"timezone_name": zone},
                }
            ],
        }

    state.payload["records"] = [calendar("private:cal", "Asia/Shanghai")]
    value = service.query(actor=actor, fields=("teachers",)).public()
    assert value["fact_timezone_name"] == "Asia/Shanghai"
    assert value["as_of"] == NOW.isoformat()
    state.payload["records"].append(calendar("private:other", "UTC"))
    assert service.query(actor=actor, fields=("teachers",)).public()["fact_timezone_name"] is None


def test_unverified_or_future_verified_statement_does_not_establish_negative():
    service, actor, state = fixture()
    for verification in (None, (NOW + timedelta(days=1)).isoformat()):
        state.payload["quality"] = [quality(verified_at=verification)]
        assert (
            service.query(actor=actor, fields=("teachers",)).public()["quality"]["teachers"][
                "status"
            ]
            == "unknown"
        )


def test_academic_year_requires_matching_authorized_owner_calendar():
    service, actor, state = fixture()
    state.payload["quality"] = [
        quality(
            academic_year=2026,
            education_target_source_key="class:a",
            education_target_kind="education_class",
        )
    ]
    assert (
        service.query(actor=actor, fields=("teachers",)).public()["quality"]["teachers"]["status"]
        == "unknown"
    )

    def rec(kind, key, content):
        return {
            "fact_kind": kind,
            "source_key": key,
            "segments": [
                {
                    "revision": 1,
                    "effective_at": "2026-04-01T00:00:00+00:00",
                    "valid_to": None,
                    "recorded_at": "2026-03-01T00:00:00+00:00",
                    "content": content,
                }
            ],
        }

    state.payload["records"] = [
        rec(
            "class_placement",
            "placement:a",
            {
                "class_source_key": "class:a",
                "calendar_source_key": "cal:a",
                "grade_source_key": "g1",
            },
        ),
        rec(
            "education_calendar_rule",
            "cal:a",
            {
                "start_cycle": 2026,
                "month": 4,
                "day": 1,
                "timezone_name": "UTC",
                "grade_source_keys": ["g1", "g2"],
            },
        ),
    ]
    assert (
        service.query(actor=actor, fields=("teachers",)).public()["quality"]["teachers"]["status"]
        == "known_negative"
    )
    state.payload["quality"][0]["academic_year"] = 2027
    assert (
        service.query(actor=actor, fields=("teachers",)).public()["quality"]["teachers"]["status"]
        == "unknown"
    )
