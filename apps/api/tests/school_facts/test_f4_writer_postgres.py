"""Opt-in F4 unique Writer tests in a newly cloned owned fixture DB only.

These prove persistence/confirmation and RLS, not the separately tested F4 grant policy.
Never imports or invokes the global postgres_roles/bootstrap fixture.
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

from app.platform.school_facts.contracts import CanonicalFactChangeSetV3
from tests.school_facts.test_f3_family_postgres import existing_fixture_writer, safe_test_engine

pytestmark = pytest.mark.skipif(
    not os.environ.get("F4_WRITER_PG"), reason="explicit owned F4 PG target required"
)


@pytest.fixture
def writer(monkeypatch):
    assert os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1"
    metadata = json.loads(Path(os.environ["F3_TEST_TARGET_METADATA"]).read_text())
    assert metadata["owner"] == "F4 persistence and reader controlled tests"
    engines = {
        role: safe_test_engine(Path(os.environ["F3_TEST_DATABASE_CONFIG"]), role)
        for role in ("migration", "runtime", "reader", "writer")
    }
    with engines["migration"].connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) in {
            "20260913_28",
            "20260913_29",
            "20260913_30",
            "20260913_31",
        }
        assert connection.scalar(text("SELECT source_key FROM schools")) == "school:f3-pg-fixture"
    actor, identity, context, service = existing_fixture_writer(engines)
    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence

    original = SqlAlchemySchoolFactPersistence._raise_db_error

    def diagnostic(exc):
        cause = getattr(exc, "orig", None)
        detail = getattr(cause, "diag", None)
        print(
            "F4 SQL diagnostic",
            getattr(cause, "sqlstate", None),
            getattr(detail, "constraint_name", None),
            getattr(detail, "message_primary", None),
        )
        original(exc)

    monkeypatch.setattr(
        SqlAlchemySchoolFactPersistence, "_raise_db_error", staticmethod(diagnostic)
    )
    yield engines, actor, identity, context, service
    for engine in engines.values():
        engine.dispose()


def apply(writer, entries):
    engines, actor, identity, context, service = writer
    with engines["migration"].connect() as connection:
        base = connection.scalar(
            text(
                "SELECT COALESCE(MAX(target_fact_version),1) FROM "
                "master_data_import_batches WHERE status='applied'"
            )
        )
    plan = CanonicalFactChangeSetV3.model_validate_json(
        json.dumps(
            {
                "schema_version": 3,
                "base_version": base,
                "source_sha256": sha256(uuid4().bytes).hexdigest(),
                "changes": [
                    {
                        "schema_version": 3,
                        "origin": {"sheet_code": "education", "row_number": index + 1},
                        **entry,
                    }
                    for index, entry in enumerate(entries)
                ],
            },
            default=str,
        )
    )
    prepared = service.prepare_confirmation(
        change_set=plan, context=context, session_binding=identity.session_nonce_hash
    )
    result = service.confirm(
        credential=prepared.credential,
        change_set=plan,
        context=context,
        session_binding=identity.session_nonce_hash,
    )
    replay = service.confirm(
        credential=prepared.credential,
        change_set=plan,
        context=context,
        session_binding=identity.session_nonce_hash,
    )
    assert replay.batch_id == result.batch_id and replay.replayed
    return result


def entry(key, name, at, *, operation="upsert", expected=0, until=None, reason=None):
    value = {
        "fact_kind": "education_class",
        "source_key": key,
        "operation": operation,
        "expected_version": expected,
        "effective_at": at,
        "content": None if operation in {"end", "cancel"} else {"display_name": name},
    }
    if until is not None:
        value["content"]["valid_to"] = until
    if reason:
        value["correction_reason"] = reason
    return value


def test_education_quality_and_policy_scope_use_same_formal_owner(writer):
    token = uuid4().hex
    now = datetime.now(UTC)
    class_key, position_key = "class:quality-" + token, "position:quality-" + token
    policy_key, scope_key, quality_key = (
        "policy:f4-" + token,
        "scope:f4-" + token,
        "quality:f4-" + token,
    )
    with writer[0]["migration"].connect() as connection:
        campus = connection.scalar(
            text(
                "SELECT l.source_key FROM campus_locations l JOIN "
                "campus_location_versions v ON v.location_id=l.id WHERE "
                "v.kind='campus' AND v.valid_to IS NULL LIMIT 1"
            )
        )

    def fact(kind, key, content):
        return {
            "fact_kind": kind,
            "source_key": key,
            "operation": "upsert",
            "expected_version": 0,
            "effective_at": now,
            "content": content,
        }

    apply(
        writer,
        [
            entry(class_key, "质量事实合成班", now),
            fact(
                "class_position_definition",
                position_key,
                {
                    "class_source_key": class_key,
                    "display_name": "班长",
                    "allow_multiple": False,
                    "allow_concurrent": False,
                    "max_holders": 1,
                },
            ),
            fact("policy", policy_key, {}),
            fact(
                "policy_scope",
                scope_key,
                {
                    "policy_source_key": policy_key,
                    "education_target_kind": "education_class",
                    "education_target_source_key": class_key,
                    "campus_source_key": campus,
                    "academic_year": 2026,
                },
            ),
            fact(
                "fact_quality",
                quality_key,
                {
                    "quality_kind": "negative",
                    "negative_code": "vacant_position",
                    "owner_kind": "education",
                    "education_target_kind": "education_class",
                    "education_target_source_key": class_key,
                    "target_fact_kind": "student_class_position",
                    "position_source_key": position_key,
                    "responsibility": "正式登记",
                    "basis_reference": "合成确认资料",
                    "verified_at": now,
                },
            ),
        ],
    )
    with writer[0]["migration"].connect() as connection:
        scope = connection.execute(
            text(
                "SELECT education_target_id,academic_year,campus_location_id FROM "
                "school_policy_scopes WHERE source_key=:key"
            ),
            {"key": scope_key},
        ).one()
        quality = connection.execute(
            text(
                "SELECT education_target_id,education_position_id,negative_code "
                "FROM school_fact_qualities WHERE source_key=:key"
            ),
            {"key": quality_key},
        ).one()
    assert scope.education_target_id == quality.education_target_id
    assert scope.academic_year == 2026 and scope.campus_location_id is not None
    assert quality.education_position_id is not None and quality.negative_code == "vacant_position"


def test_real_writer_versions_correction_and_original_receipt(writer):
    key = "class:f4-pg-" + uuid4().hex
    now = datetime.now(UTC)
    start = now - timedelta(days=90)
    corrected = now - timedelta(days=60)
    later = now - timedelta(days=30)
    apply(writer, [entry(key, "原名", start)])
    apply(writer, [entry(key, "新名", later, expected=1)])
    apply(
        writer,
        [
            entry(
                key,
                "补正名",
                corrected,
                operation="correct",
                expected=2,
                until=later,
                reason="历史登记纠正",
            )
        ],
    )
    with writer[0]["migration"].connect() as connection:
        rows = connection.execute(
            text(
                "SELECT v.valid_from,v.valid_to,v.payload->>'display_name' AS "
                "name,v.revision FROM school_education_versions v JOIN "
                "school_education_facts f ON f.id=v.fact_id WHERE "
                "f.source_key=:key AND v.superseded_at IS NULL ORDER BY "
                "v.valid_from"
            ),
            {"key": key},
        ).all()
        assert [r.name for r in rows] == ["原名", "补正名", "新名"]
        assert all(r.revision == 3 for r in rows)
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM school_education_versions v JOIN "
                    "school_education_facts f ON f.id=v.fact_id WHERE "
                    "f.source_key=:key AND v.superseded_at IS NOT NULL"
                ),
                {"key": key},
            )
            == 3
        )


def test_batch_failure_rolls_back_preceding_education_unit(writer, monkeypatch):
    key = "class:f4-rollback-" + uuid4().hex
    existing = "class:f4-conflict-" + uuid4().hex
    now = datetime.now(UTC)
    from app.platform.school_facts.application import SchoolFactChangeRejected

    apply(writer, [entry(existing, "既有", now)])
    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence

    # Deliberately bypass the read-only semantic preview to prove the transactional guard too.
    monkeypatch.setattr(
        SqlAlchemySchoolFactPersistence, "education_preflight_codes", lambda *args: ()
    )
    with pytest.raises(SchoolFactChangeRejected):
        apply(
            writer,
            [entry(key, "应回滚", now), entry(existing, "未明确补正不得覆盖", now, expected=1)],
        )
    with writer[0]["migration"].connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM school_education_facts WHERE source_key=:key"),
                {"key": key},
            )
            == 0
        )


def test_original_school_relationship_owner_correction_preserves_three_segments(writer):
    token = uuid4().hex
    person = "person:f4-ssr-" + token
    student = "student:f4-ssr-" + token
    key = "enrollment:f4-ssr-" + token
    now = datetime.now(UTC)
    start = now - timedelta(seconds=30)
    middle = now - timedelta(seconds=20)
    end = now - timedelta(seconds=10)
    with writer[0]["migration"].connect() as connection:
        campus = connection.scalar(
            text(
                "SELECT l.source_key FROM campus_locations l JOIN "
                "campus_location_versions v ON v.location_id=l.id WHERE "
                "v.kind='campus' AND v.valid_to IS NULL LIMIT 1"
            )
        )
    profile = {
        "student_source_key": student,
        "student_number": "F4-" + token[:12],
        "enrollment_status": "enrolled",
        "residence_mode": "day",
        "primary_campus_source_key": campus,
        "administrative_campus_source_key": campus,
    }
    entries = [
        {
            "fact_kind": "person",
            "source_key": person,
            "operation": "upsert",
            "expected_version": 0,
            "effective_at": start,
            "content": {
                "display_name": "F4时态合成学生",
                "person_kind": "other",
                "lifecycle_status": "active",
            },
        },
        {
            "fact_kind": "student",
            "source_key": student,
            "operation": "upsert",
            "expected_version": 0,
            "effective_at": start,
            "content": {"person_source_key": person, "status": "active"},
        },
        {
            "fact_kind": "student_school_relationship",
            "source_key": key,
            "operation": "upsert",
            "expected_version": 0,
            "effective_at": start,
            "content": profile,
        },
    ]
    apply(writer, entries)
    apply(
        writer,
        [
            {
                "fact_kind": "student_school_relationship",
                "source_key": key,
                "operation": "correct",
                "expected_version": 1,
                "effective_at": middle,
                "correction_reason": "纠正已核实短期休学时段",
                "content": {**profile, "enrollment_status": "suspended", "valid_to": end},
            }
        ],
    )
    with writer[0]["migration"].connect() as connection:
        rows = connection.execute(
            text(
                "SELECT valid_from,valid_to,enrollment_status FROM "
                "student_school_relationships WHERE source_key=:key AND "
                "superseded_at IS NULL ORDER BY valid_from"
            ),
            {"key": key},
        ).all()
        assert [r.enrollment_status for r in rows] == ["enrolled", "suspended", "enrolled"]
        assert rows[0].valid_from == start and rows[0].valid_to == middle
        assert rows[1].valid_from == middle and rows[1].valid_to == end
        assert rows[2].valid_from == end and rows[2].valid_to is None
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM student_school_relationships WHERE "
                    "source_key=:key AND superseded_at IS NOT NULL"
                ),
                {"key": key},
            )
            == 1
        )

    correction = {
        "fact_kind": "student_school_relationship",
        "source_key": key,
        "operation": "correct",
        "expected_version": 2,
        "effective_at": middle,
        "correction_reason": "明确移除中段，保留其他在校段",
        "content": None,
        "replacement_segments": [
            {"effective_at": start, "valid_to": middle, "content": profile},
            {"effective_at": end, "valid_to": None, "content": profile},
        ],
    }
    apply(writer, [correction])
    with writer[0]["migration"].connect() as connection:
        segments = connection.execute(
            text(
                "SELECT valid_from,valid_to FROM student_school_relationships WHERE source_key=:key AND superseded_at IS NULL ORDER BY valid_from"
            ),
            {"key": key},
        ).all()
        assert [(r.valid_from, r.valid_to) for r in segments] == [(start, middle), (end, None)]
    apply(
        writer,
        [
            {
                **correction,
                "expected_version": 3,
                "replacement_segments": [],
                "correction_reason": "明确取消该独立合成学生的全部安排",
            }
        ],
    )
    with writer[0]["migration"].connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM student_school_relationships WHERE source_key=:key AND superseded_at IS NULL"
                ),
                {"key": key},
            )
            == 0
        )
        assert (
            connection.scalar(
                text("SELECT max(version) FROM student_school_relationships WHERE source_key=:key"),
                {"key": key},
            )
            == 4
        )


def test_future_replacement_cancel_restores_only_its_original_cutoff(writer):
    key = "class:f4-cancel-" + uuid4().hex
    now = datetime.now(UTC)
    future = now + timedelta(days=20)
    apply(writer, [entry(key, "现在班名", now)])
    apply(writer, [entry(key, "未来班名", future, expected=1)])
    apply(
        writer,
        [entry(key, None, future, operation="cancel", expected=2, reason="正式取消未来更名")],
    )
    with writer[0]["migration"].connect() as connection:
        rows = connection.execute(
            text(
                "SELECT v.payload->>'display_name' AS name,v.valid_to FROM "
                "school_education_versions v JOIN school_education_facts f ON "
                "f.id=v.fact_id WHERE f.source_key=:key AND v.superseded_at IS "
                "NULL"
            ),
            {"key": key},
        ).all()
        assert len(rows) == 1 and rows[0].name == "现在班名" and rows[0].valid_to is None


def test_explicit_timeline_ends_current_period_and_preserves_independent_future(writer):
    key = "class:full-timeline-" + uuid4().hex
    now = datetime.now(UTC)
    start, stop, future = (
        now - timedelta(days=20),
        now + timedelta(days=10),
        now + timedelta(days=30),
    )
    apply(writer, [entry(key, "原班", start)])
    apply(writer, [entry(key, "独立未来", future, expected=1)])
    value = entry(
        key, "", stop, operation="correct", expected=2, reason="结束当前任期，明确保留独立未来段"
    )
    value["content"] = None
    value["replacement_segments"] = [
        {"effective_at": start, "valid_to": stop, "content": {"display_name": "原班"}},
        {"effective_at": future, "valid_to": None, "content": {"display_name": "独立未来"}},
    ]
    apply(writer, [value])
    with writer[0]["migration"].connect() as connection:
        rows = connection.execute(
            text(
                "SELECT f.version,v.valid_from,v.valid_to,v.payload->>'display_name' AS name,v.cause_source_key FROM school_education_facts f JOIN school_education_versions v ON v.fact_id=f.id WHERE f.source_key=:key AND v.superseded_at IS NULL ORDER BY v.valid_from"
            ),
            {"key": key},
        ).all()
    assert [(r.valid_from, r.valid_to, r.name) for r in rows] == [
        (start, stop, "原班"),
        (future, None, "独立未来"),
    ]
    assert all(r.version == 3 and r.cause_source_key is None for r in rows)


def test_ssr_ordinary_graduation_after_bounded_correction_uses_whole_source_timeline(
    writer, monkeypatch
):
    token = uuid4().hex
    person, student, key = (
        prefix + token
        for prefix in ("person:ssr-upsert:", "student:ssr-upsert:", "enrollment:ssr-upsert:")
    )
    now = datetime.now(UTC)
    start, middle, resumed = (now - timedelta(seconds=n) for n in (40, 30, 20))
    with writer[0]["migration"].connect() as connection:
        campus = connection.scalar(
            text(
                "SELECT l.source_key FROM campus_locations l JOIN campus_location_versions v ON v.location_id=l.id WHERE v.kind='campus' AND v.valid_to IS NULL LIMIT 1"
            )
        )
    profile = {
        "student_source_key": student,
        "student_number": "F4-U-" + token[:12],
        "enrollment_status": "enrolled",
        "residence_mode": "day",
        "primary_campus_source_key": campus,
        "administrative_campus_source_key": campus,
    }

    def fact(kind, source, content, at=start, expected=0, operation="upsert", reason=None):
        result = {
            "fact_kind": kind,
            "source_key": source,
            "operation": operation,
            "expected_version": expected,
            "effective_at": at,
            "content": content,
        }
        if reason:
            result["correction_reason"] = reason
        return result

    apply(
        writer,
        [
            fact(
                "person",
                person,
                {
                    "display_name": "独立普通更新学生",
                    "person_kind": "other",
                    "lifecycle_status": "active",
                },
            ),
            fact("student", student, {"person_source_key": person, "status": "active"}),
            fact("student_school_relationship", key, profile),
        ],
    )
    apply(
        writer,
        [
            fact(
                "student_school_relationship",
                key,
                {**profile, "enrollment_status": "suspended", "valid_to": resumed},
                middle,
                1,
                "correct",
                "核实有界休学",
            )
        ],
    )
    apply(
        writer,
        [
            fact(
                "student_school_relationship",
                key,
                {**profile, "enrollment_status": "graduated"},
                now,
                2,
            )
        ],
    )
    with writer[0]["migration"].connect() as connection:
        rows = connection.execute(
            text(
                "SELECT valid_from,valid_to,enrollment_status,version FROM student_school_relationships WHERE source_key=:key AND superseded_at IS NULL ORDER BY valid_from"
            ),
            {"key": key},
        ).all()
        old = connection.scalar(
            text(
                "SELECT count(*) FROM student_school_relationships WHERE source_key=:key AND version=2 AND superseded_at IS NOT NULL"
            ),
            {"key": key},
        )
    assert [(r.valid_from, r.valid_to, r.enrollment_status) for r in rows] == [
        (start, middle, "enrolled"),
        (middle, resumed, "suspended"),
        (resumed, now, "enrolled"),
        (now, None, "graduated"),
    ]
    assert all(r.version == 3 for r in rows) and old == 3
    future = now + timedelta(days=30)
    apply(
        writer,
        [
            fact(
                "student_school_relationship",
                key,
                {**profile, "enrollment_status": "enrolled"},
                future,
                3,
            )
        ],
    )
    from app.platform.school_facts.application import SchoolFactChangeRejected

    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence

    monkeypatch.setattr(
        SqlAlchemySchoolFactPersistence, "education_preflight_codes", lambda *args: ()
    )
    with pytest.raises(SchoolFactChangeRejected):
        apply(
            writer,
            [
                fact(
                    "student_school_relationship",
                    key,
                    {**profile, "enrollment_status": "withdrawn"},
                    now + timedelta(days=1),
                    4,
                )
            ],
        )
    with writer[0]["migration"].connect() as connection:
        tail = connection.execute(
            text(
                "SELECT valid_from,enrollment_status,version FROM student_school_relationships WHERE source_key=:key AND superseded_at IS NULL ORDER BY valid_from DESC LIMIT 1"
            ),
            {"key": key},
        ).one()
    assert (tail.valid_from, tail.enrollment_status, tail.version) == (future, "enrolled", 4)
