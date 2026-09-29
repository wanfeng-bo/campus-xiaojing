# ruff: noqa: E501
"""Real v30 selected-child suspension/resumption dependency projection."""

import os
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.composition.school_fact_family_education_query import _public
from tests.school_facts.test_f4_writer_postgres import apply
from tests.school_facts.test_f4_writer_postgres import writer as writer

pytestmark = pytest.mark.skipif(not os.environ.get("F4_WRITER_PG"), reason="owned PG only")


def test_guardian_grade_arrangement_tracks_formal_ssr_and_remains_child_scoped(writer):
    from datetime import UTC, datetime

    start = datetime.now(UTC) - timedelta(seconds=90)
    with writer[0]["migration"].connect() as c:
        row = c.execute(
            text(
                "SELECT r.id,r.guardian_person_id,r.version,ss.source_key,GREATEST(r.valid_from,ss.valid_from) AS start FROM guardian_relationships r JOIN student_school_relationships ss ON ss.school_id=r.school_id AND ss.student_id=r.student_id WHERE r.relationship_kind='verified_guardian' AND r.status='confirmed' AND r.revoked_at IS NULL AND r.valid_from<=now() AND (r.valid_to IS NULL OR r.valid_to>now()) AND ss.superseded_at IS NULL AND ss.valid_to IS NULL ORDER BY r.id LIMIT 1"
            )
        ).one()
    start = row.start + timedelta(seconds=1)
    with writer[0]["reader"].connect() as c:
        axis = c.scalar(
            text(
                "SELECT app.school_fact_f4_ssr_timeline(:s,:key,ARRAY['student_source_key','student_number','enrollment_status','residence_mode','primary_campus_source_key','administrative_campus_source_key','valid_to'])"
            ),
            {"s": writer[1].school_id, "key": row.source_key},
        )
    original = next(s["content"] for s in axis["segments"] if s["valid_to"] is None)
    params = {
        "s": writer[1].school_id,
        "adult": row.guardian_person_id,
        "relation": row.id,
        "version": row.version,
        "at": start + timedelta(seconds=60),
    }

    def read(fields):
        with writer[0]["reader"].connect() as c:
            return c.scalar(
                text(
                    "SELECT app.school_fact_family_education(:s,:adult,:relation,:version,:at,CAST(:fields AS text[]))"
                ),
                params | {"fields": fields},
            )

    before = read(["grade"])
    assert before["grade_arrangement"]["enrollment_status"] == "enrolled"
    assert "grade_arrangement" not in read(["class"])
    suspended, resumed = start + timedelta(seconds=20), start + timedelta(seconds=40)
    correction = dict(
        fact_kind="student_school_relationship",
        source_key=row.source_key,
        content=original | {"enrollment_status": "suspended", "valid_to": resumed},
        effective_at=suspended,
        expected_version=axis["version"],
        operation="correct",
    )
    correction["correction_reason"] = "Explicit bounded school suspension"
    apply(writer, [correction])
    after = read(["grade"])
    assert after["school_fact_version"] > before["school_fact_version"]
    assert after["grade_arrangement"]["enrollment_status"] == "enrolled"
    assert after["grade_arrangement"]["last_suspended_until"] is not None
    assert _public(after, ("grade",), params["at"])["profile"]["grade"][0]["status"] == "unknown"
    params["at"] = suspended + timedelta(seconds=1)
    assert read(["grade"])["grade_arrangement"]["enrollment_status"] == "suspended"
    params["version"] += 1
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        read(["grade"])
