# ruff: noqa: E501
"""Real own-child quality projection, using actual published facts and Reader role."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.composition.school_fact_family_education_query import _public
from tests.school_facts.test_f4_reader_postgres import (
    test_guardian_published_class_teacher_and_own_position_exclude_other_students as seed_guardian,
)
from tests.school_facts.test_f4_writer_postgres import apply
from tests.school_facts.test_f4_writer_postgres import writer as writer

pytestmark = pytest.mark.skipif(not os.environ.get("F4_WRITER_PG"), reason="owned PG only")


def test_guardian_quality_is_published_child_field_time_scoped_and_runtime_denied(writer):
    seed_guardian(writer)
    school = writer[1].school_id
    with writer[0]["migration"].connect() as c:
        row = c.execute(
            text("""SELECT g.id,g.guardian_person_id,g.version,
          v.payload->>'class_source_key' class_key
          FROM guardian_relationships g JOIN students s ON s.school_id=g.school_id AND s.id=g.student_id
          JOIN school_education_versions v ON v.school_id=s.school_id AND v.payload->>'student_source_key'=s.source_key
          JOIN school_education_facts f ON f.school_id=v.school_id AND f.id=v.fact_id AND f.kind='student_class_membership'
          WHERE g.relationship_kind='verified_guardian' AND g.status='confirmed' AND g.revoked_at IS NULL
          AND g.valid_from<=now() AND (g.valid_to IS NULL OR now()<g.valid_to)
          AND v.superseded_at IS NULL AND v.valid_from<=now() AND v.valid_to IS NULL
          AND f.source_key LIKE 'member:%' ORDER BY g.id,v.valid_from DESC LIMIT 1""")
        ).one()
        teachers = c.execute(
            text("""SELECT f.source_key,f.version FROM school_education_facts f
          JOIN school_education_versions v ON v.school_id=f.school_id AND v.fact_id=f.id
          WHERE f.kind='class_teacher_assignment' AND v.superseded_at IS NULL AND v.valid_to IS NULL
          AND v.payload->>'class_source_key'=:key"""),
            {"key": row.class_key},
        ).all()
    now = datetime.now(UTC)
    token = "quality:family31:" + uuid4().hex

    def fact(kind, key, content, at=now, expected=0, operation="upsert"):
        return dict(
            fact_kind=kind,
            source_key=key,
            operation=operation,
            expected_version=expected,
            effective_at=at,
            content=content,
        )

    base = dict(
        quality_kind="negative",
        negative_code="no_class_teacher",
        owner_kind="education",
        education_target_kind="education_class",
        education_target_source_key=row.class_key,
        target_fact_kind="class_teacher_assignment",
        responsibility="正式登记",
        basis_reference="Owned family quality projection test",
        verified_at=now,
    )
    other = token + ":other-class"
    apply(
        writer,
        [
            fact(
                "class_teacher_assignment", t.source_key, None, expected=t.version, operation="end"
            )
            for t in teachers
        ]
        + [
            fact("education_class", other, {"display_name": "Other class quality must not escape"}),
            fact("fact_quality", token, base),
            fact("fact_quality", token + ":dimension", base | {"purpose": "office"}),
            fact("fact_quality", token + ":other", base | {"education_target_source_key": other}),
            fact("fact_quality", token + ":future", base, now + timedelta(days=1)),
            fact("fact_quality", token + ":year", base | {"academic_year": 2099}),
        ],
    )
    params = dict(
        s=school,
        adult=row.guardian_person_id,
        relation=row.id,
        version=row.version,
        at=datetime.now(UTC),
    )

    def read(fields):
        with writer[0]["reader"].connect() as c:
            return c.scalar(
                text(
                    "SELECT app.school_fact_family_education(:s,:adult,:relation,:version,:at,CAST(:fields AS text[]))"
                ),
                params | {"fields": fields},
            )

    raw = read(["teachers"])
    assert raw["related_teachers"] == []
    qualities = raw.get("quality", [])
    assert len(qualities) == 3, (
        "schema30 loses explicit no-teacher quality; other owner/future must stay excluded"
    )
    assert all(
        q["field"] == "teachers" and q["negative_code"] == "no_class_teacher" for q in qualities
    )
    public = _public(raw, ("teachers",), params["at"])
    assert public["quality"]["teachers"]["status"] == "known_negative"
    assert len(public["quality"]["teachers"]["statements"]) == 2
    assert sum(q["scope"] == "item" for q in qualities) == 1
    assert not read(["class"])["quality"]
    assert "grade_arrangement" in read(["grade"])
    assert all("education_target_source_key" not in s for s in public["sources"])
    # F2 already owns a Reader quality-table projection; schema31 must not
    # invent a blanket denial. The host migration evidence compares its ACLs.
    with writer[0]["runtime"].connect() as c, pytest.raises(DBAPIError):
        c.execute(text("SELECT app.school_fact_family_education(:s,:adult,:relation,:version,:at,ARRAY['teachers'])"), params)
    params["version"] += 1
    with pytest.raises(DBAPIError):
        read(["teachers"])
