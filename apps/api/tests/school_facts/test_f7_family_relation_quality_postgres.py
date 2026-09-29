"""Formal self-owned quality reaches only the selected child's exact relation.

Runs only in the named isolated F7 fixture, with its existing real Writer and
Guardian Reader. No production configuration, fact, or permission is used.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.composition.school_fact_family_education_query import _public
from app.composition.school_fact_family_teaching_query import _quality
from app.composition.school_fact_teaching_query import teaching_view
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import CanonicalFactChangeSetV4
from tests.school_facts import test_f3_family_postgres, test_f3_writer_postgres
from tests.school_facts.test_f3_family_postgres import existing_fixture_writer, safe_test_engine
from tests.school_facts.test_f4_reader_postgres import (
    test_guardian_published_class_teacher_and_own_position_exclude_other_students as seed_guardian,
)
from tests.school_facts.test_f4_writer_postgres import apply

pytestmark = pytest.mark.skipif(
    not os.environ.get("F7_TEST_DATABASE_CONFIG"), reason="owned F7 PostgreSQL required"
)


@pytest.fixture
def case(monkeypatch):
    config = Path(os.environ["F7_TEST_DATABASE_CONFIG"])
    metadata = json.loads(config.read_text(encoding="utf-8"))
    assert metadata["database"].startswith("f7_") and metadata["database"].endswith("_test")
    engines = {
        role: safe_test_engine(config, role)
        for role in ("migration", "reader", "writer", "runtime")
    }
    try:
        with engines["migration"].connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20260924_56"
            )
        monkeypatch.setattr(test_f3_family_postgres, "KEY", metadata["context"].encode())
        monkeypatch.setattr(test_f3_writer_postgres, "KEY", metadata["context"].encode())
        writer = (engines, *existing_fixture_writer(engines))
        seed_guardian(writer)
        with engines["migration"].connect() as connection:
            selected = (
                connection.execute(
                    text("""
                SELECT g.id AS relation,g.guardian_person_id AS adult,g.version,
                    c.source_key AS class_key,t.source_key AS teacher_key,
                    tv.payload->>'assignment_source_key' AS assignment_key
                FROM guardian_relationships g
                JOIN students s ON s.school_id=g.school_id AND s.id=g.student_id
                JOIN school_education_versions mv ON mv.school_id=s.school_id
                    AND mv.payload->>'student_source_key'=s.source_key
                    AND mv.superseded_at IS NULL AND mv.valid_from<=now()
                    AND (mv.valid_to IS NULL OR now()<mv.valid_to)
                JOIN school_education_facts m ON m.school_id=mv.school_id AND m.id=mv.fact_id
                    AND m.kind='student_class_membership'
                JOIN school_education_facts c ON c.school_id=m.school_id
                    AND c.kind='education_class'
                    AND c.source_key=mv.payload->>'class_source_key'
                JOIN school_education_versions tv ON tv.school_id=c.school_id
                    AND tv.payload->>'class_source_key'=c.source_key
                    AND tv.superseded_at IS NULL AND tv.valid_from<=now()
                    AND (tv.valid_to IS NULL OR now()<tv.valid_to)
                JOIN school_education_facts t ON t.school_id=tv.school_id AND t.id=tv.fact_id
                    AND t.kind='class_teacher_assignment'
                WHERE g.school_id=:school AND g.relationship_kind='verified_guardian'
                    AND g.status='confirmed' AND g.revoked_at IS NULL
                    AND g.valid_from<=now() AND (g.valid_to IS NULL OR now()<g.valid_to)
                    AND m.source_key LIKE 'member:%'
                ORDER BY mv.valid_from DESC LIMIT 1
            """),
                    {"school": writer[1].school_id},
                )
                .mappings()
                .one()
            )
        yield writer, dict(selected)
    finally:
        for engine in engines.values():
            engine.dispose()


def publish_quality(writer, *, kind, owner, at, source_status="available", until=None, target=None):
    engines, actor, identity, context, service = writer
    with engines["reader"].connect() as connection:
        base = connection.scalar(
            text("SELECT app.school_fact_current_version(:school)"), {"school": actor.school_id}
        )
    content = {
        "quality_kind": "source",
        "source_status": source_status,
        "owner_kind": "education",
        "education_target_kind": kind,
        "education_target_source_key": owner,
        "target_fact_kind": kind,
        "responsibility": "隔离测试核查员",
        "basis_reference": "正式关系来源核查",
        "verified_at": at,
    }
    if until is not None:
        content["valid_to"] = until
    if target is not None:
        content["target_source_key"] = target
    change = CanonicalFactChangeSetV4.model_validate_json(
        json.dumps(
            {
                "schema_version": 4,
                "base_version": base,
                "source_sha256": sha256(uuid4().bytes).hexdigest(),
                "changes": [
                    {
                        "schema_version": 4,
                        "fact_kind": "fact_quality",
                        "source_key": "quality:f7-own-relation-" + uuid4().hex,
                        "operation": "upsert",
                        "expected_version": 0,
                        "effective_at": at,
                        "origin": {"sheet_code": "quality", "row_number": 1},
                        "content": content,
                    }
                ],
            },
            default=str,
        )
    )
    report = service.preflight(
        school_id=actor.school_id,
        change_set=change,
        context=context,
        session_binding=identity.session_nonce_hash,
    )
    assert report.is_clean, [issue.error_code for issue in report.issues]
    prepared = service.prepare_confirmation(
        change_set=change, context=context, session_binding=identity.session_nonce_hash
    )
    result = service.confirm(
        credential=prepared.credential,
        change_set=change,
        context=context,
        session_binding=identity.session_nonce_hash,
    )
    assert result.changed_count == 1
    assert service.confirm(
        credential=prepared.credential,
        change_set=change,
        context=context,
        session_binding=identity.session_nonce_hash,
    ).replayed


def test_actual_f4_self_owned_quality_is_item_scoped_and_not_all_teachers(case):
    writer, selected = case
    at = datetime.now(UTC)
    publish_quality(writer, kind="class_teacher_assignment", owner=selected["teacher_key"], at=at)
    future = at + timedelta(hours=1)
    publish_quality(
        writer,
        kind="class_teacher_assignment",
        owner=selected["teacher_key"],
        at=future,
        source_status="unavailable",
    )
    params = {**selected, "school": writer[1].school_id, "at": datetime.now(UTC)}
    sql = text(
        "SELECT app.school_fact_family_education("
        ":school,:adult,:relation,:version,:at,ARRAY['teachers'])"
    )
    with writer[0]["reader"].connect() as connection:
        raw = connection.scalar(sql, params)
    exact = [
        q for q in raw["quality"] if q.get("education_target_source_key") == selected["teacher_key"]
    ]
    assert len(exact) == 1 and exact[0]["scope"] == "item"
    assert exact[0]["target_source_key"] == selected["teacher_key"]
    public = _public(raw, ("teachers",), params["at"], school_id=writer[1].school_id)
    assert public["quality"]["teachers"]["source_availability"] == "unknown"
    assert any(
        q.get("source_status") == "available"
        for t in public["profile"]["teachers"]
        for q in t.get("quality_declarations", [])
    )
    with writer[0]["reader"].connect() as connection:
        later = connection.scalar(sql, params | {"at": future + timedelta(seconds=1)})
    assert any(q.get("source_status") == "unavailable" for q in later["quality"])
    with writer[0]["reader"].connect() as connection, pytest.raises(DBAPIError):
        connection.scalar(sql, params | {"adult": uuid4()})


def test_actual_f5_self_owned_quality_is_visible_without_expanding_child_scope(case):
    writer, selected = case
    at = datetime.now(UTC)
    prefix = "f7-family-relation:" + uuid4().hex + ":"
    with writer[0]["migration"].connect() as connection:
        second = connection.scalar(
            text(
                "SELECT source_key FROM staff_assignments "
                "WHERE school_id=:school AND source_key<>:first AND status='confirmed' "
                "AND revoked_at IS NULL ORDER BY source_key LIMIT 1"
            ),
            {"school": writer[1].school_id, "first": selected["assignment_key"]},
        )
    assert second is not None

    def entry(kind, key, content):
        return {
            "fact_kind": kind,
            "source_key": prefix + key,
            "operation": "upsert",
            "expected_version": 0,
            "effective_at": at,
            "content": content,
        }

    apply(
        writer,
        [
            entry("teaching_course", "course", {"display_name": "自属质量隔离课程"}),
            entry(
                "teaching_offering",
                "offering",
                {
                    "display_name": "隔离班任课",
                    "course_source_key": prefix + "course",
                    "offering_status": "active",
                },
            ),
            entry(
                "offering_member_source",
                "members",
                {
                    "offering_source_key": prefix + "offering",
                    "source_kind": "administrative_class",
                    "class_source_key": selected["class_key"],
                },
            ),
            entry(
                "teaching_assignment",
                "lead",
                {
                    "offering_source_key": prefix + "offering",
                    "assignment_source_key": selected["assignment_key"],
                    "teaching_role": "lead",
                },
            ),
            entry(
                "teaching_assignment",
                "co",
                {
                    "offering_source_key": prefix + "offering",
                    "assignment_source_key": second,
                    "teaching_role": "co_teacher",
                },
            ),
        ],
    )
    publish_quality(writer, kind="teaching_assignment", owner=prefix + "lead", at=at)
    future = at + timedelta(hours=1)
    publish_quality(
        writer,
        kind="teaching_assignment",
        owner=prefix + "co",
        at=future,
        source_status="unavailable",
    )
    params = {
        **selected,
        "school": writer[1].school_id,
        "at": datetime.now(UTC),
        "course": "自属质量隔离课程",
    }
    sql = text(
        "SELECT app.school_fact_family_teaching("
        ":school,:adult,:relation,:version,:at,'current',NULL,:course,false)"
    )
    with writer[0]["reader"].connect() as connection:
        raw = connection.scalar(sql, params)
    assert raw["status"] == "available"
    claims = [q for q in raw["quality"] if q.get("quality_owner_source_key") == prefix + "lead"]
    assert len(claims) == 1
    assert claims[0]["target_source_key"] == prefix + "lead"
    assert claims[0]["offering_source_key"] == prefix + "offering"
    assert not any(q.get("quality_owner_source_key") == prefix + "co" for q in raw["quality"])
    assert raw["next_boundary"] is not None
    assert datetime.fromisoformat(raw["next_boundary"]) <= future
    assert all(r["fact_kind"] not in {"student", "teaching_group_member"} for r in raw["records"])
    cleaned = _quality(
        raw["quality"],
        school_id=writer[1].school_id,
        records=raw["records"],
        at=params["at"],
        school_fact_version=raw["school_fact_version"],
    )
    assert "quality_owner_source_key" not in str(cleaned)
    # Read the shared view through its normal shape; exact owner stays on the
    # corresponding relation rather than becoming an offering-wide statement.
    public = teaching_view(
        records=raw["records"],
        at=params["at"],
        teachers={row["assignment_source_key"]: row for row in raw["related_teachers"]},
        quality=cleaned,
    )
    target = next(item for item in public["items"] if item["offering"] == "隔离班任课")
    assert "quality" not in target
    assert sum(bool(t.get("quality_declarations")) for t in target["teachers"]) == 1
    with writer[0]["reader"].connect() as connection:
        assert connection.scalar(sql, params | {"course": "不存在的隔离课程"})["quality"] == []
    with writer[0]["reader"].connect() as connection, pytest.raises(DBAPIError):
        connection.scalar(sql, params | {"adult": uuid4()})
    # Merely sharing an offering does not make the second teacher a fact owned
    # by the first relation. The final Writer, not the model, rejects this.
    with pytest.raises(SchoolFactChangeRejected, match="质量目标不属于明确教育对象"):
        publish_quality(
            writer, kind="teaching_assignment", owner=prefix + "lead", target=prefix + "co", at=at
        )
    with writer[0]["reader"].connect() as connection:
        inconsistent = connection.scalar(sql, params)
    assert (
        sum(q.get("quality_owner_source_key") == prefix + "lead" for q in inconsistent["quality"])
        == 1
    )
    assert all(q.get("target_source_key") != prefix + "co" for q in inconsistent["quality"])
    assert inconsistent["school_fact_version"] == raw["school_fact_version"]
