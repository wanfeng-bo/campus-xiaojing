# ruff: noqa: E501, F811
"""Real role-separated Reader tests against the explicitly owned F4 fixture DB."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.school_facts.test_f4_writer_postgres import apply, entry, writer  # noqa: F401

pytestmark = pytest.mark.skipif(
    not os.environ.get("F4_WRITER_PG"), reason="explicit owned F4 PG target required"
)


def test_segmented_planner_preview_is_applied_by_sole_writer_and_rejects_stale_version(writer):
    import json

    from app.composition.school_fact_education_product import EducationBatchInput
    from app.domains.students.education_contracts import EDUCATION_CONTENT_FIELDS
    from app.platform.school_facts.application import SchoolFactChangeRejected
    from tests.school_facts.test_f4_education_maintenance import environment

    key = "class:planner-" + uuid4().hex
    now = datetime.now(UTC)
    start, stop, future = (
        now - timedelta(days=20),
        now + timedelta(days=10),
        now + timedelta(days=30),
    )
    apply(writer, [entry(key, "Original", start)])
    apply(writer, [entry(key, "Independent future", future, expected=1)])
    raw = query(
        writer, "education_class", [key], list(EDUCATION_CONTENT_FIELDS["education_class"]), stop
    )
    coordinator, _ = environment(raw["records"], raw["school_fact_version"])
    payload = EducationBatchInput.model_validate_json(
        json.dumps(
            {
                "fact_kind": "education_class",
                "target_ref": "f4:education_class:" + key,
                "operation": "end",
                "fields": {},
                "reason": "End current interval and preserve independent future",
            }
        )
    )
    result = coordinator.resolve_change(
        actor=writer[1], payload=payload, request_id=uuid4(), effective_at=stop
    )
    assert len(result.summary.before_segments) == 2 and len(result.summary.after_segments) == 2
    assert result.summary.after_segments[1]["effective_at"] == future.isoformat()
    entries = [c.model_dump(mode="json", exclude_unset=True) for c in result.change_set.changes]
    apply(writer, entries)
    with writer[0]["migration"].connect() as c:
        rows = c.execute(
            text(
                "SELECT f.version,v.valid_from,v.valid_to FROM school_education_facts f JOIN school_education_versions v ON v.fact_id=f.id WHERE f.school_id=:s AND f.source_key=:key AND v.superseded_at IS NULL ORDER BY v.valid_from"
            ),
            {"s": writer[1].school_id, "key": key},
        ).all()
    assert [(r.valid_from, r.valid_to) for r in rows] == [(start, stop), (future, None)]
    assert all(r.version == 3 for r in rows)
    with pytest.raises(SchoolFactChangeRejected):
        apply(writer, entries)


def test_same_root_future_cancel_preview_matches_writer_restored_revision(writer):
    import json

    from app.composition.school_fact_education_product import EducationBatchInput
    from app.domains.students.education_contracts import EDUCATION_CONTENT_FIELDS
    from tests.school_facts.test_f4_education_maintenance import environment

    key = "class:future-cancel-" + uuid4().hex
    now = datetime.now(UTC)
    start = now - timedelta(days=5)
    future = now + timedelta(days=20)
    apply(writer, [entry(key, "Original", start)])
    apply(writer, [entry(key, "Future replacement", future, expected=1)])
    raw = query(
        writer, "education_class", [key], list(EDUCATION_CONTENT_FIELDS["education_class"]), now
    )
    coordinator, _ = environment(raw["records"], raw["school_fact_version"])
    payload = EducationBatchInput.model_validate_json(
        json.dumps(
            {
                "fact_kind": "education_class",
                "target_ref": "f4:education_class:" + key,
                "operation": "cancel",
                "fields": {},
                "reason": "Cancel exact future replacement",
            }
        )
    )
    result = coordinator.resolve_change(
        actor=writer[1], payload=payload, request_id=uuid4(), effective_at=now
    )
    assert result.summary.after_segments[0]["fields"]["display_name"] == "Original"
    assert result.summary.after_segments[0]["valid_to"] is None
    apply(
        writer, [c.model_dump(mode="json", exclude_unset=True) for c in result.change_set.changes]
    )
    raw = query(writer, "education_class", [key], ["display_name"], now)
    assert raw["records"][0]["status"] == "available"
    assert len(raw["records"][0]["segments"]) == 1
    assert raw["records"][0]["segments"][0]["content"]["display_name"] == "Original"


def test_historical_two_student_transfer_then_graduation_are_complete_atomic_batches(writer):
    import json
    from types import SimpleNamespace

    from app.composition.school_fact_education_product import EducationBatchInput
    from app.domains.students.education_contracts import EDUCATION_CONTENT_FIELDS
    from tests.school_facts.test_f4_education_maintenance import environment

    token = uuid4().hex
    now = datetime.now(UTC)
    start, middle, resumed = (now - timedelta(seconds=n) for n in (60, 40, 20))
    keys = {
        name: "f4:scenario:" + name + ":" + token
        for name in (
            "a",
            "b",
            "person1",
            "person2",
            "student1",
            "student2",
            "ssr1",
            "ssr2",
            "member1",
            "member2",
            "definition",
            "role1",
            "role2",
        )
    }
    with writer[0]["migration"].connect() as c:
        campus = c.scalar(
            text(
                "SELECT l.source_key FROM campus_locations l JOIN campus_location_versions v ON v.location_id=l.id WHERE v.kind='campus' AND v.valid_to IS NULL LIMIT 1"
            )
        )

    def fact(kind, key, content, at=start, version=0, operation="upsert"):
        return {
            "fact_kind": kind,
            "source_key": keys[key],
            "content": content,
            "effective_at": at,
            "expected_version": version,
            "operation": operation,
        }

    entries = []
    for n in (1, 2):
        entries += [
            fact(
                "person",
                "person" + str(n),
                {
                    "display_name": "Scenario student " + str(n),
                    "person_kind": "other",
                    "lifecycle_status": "active",
                },
            ),
            fact(
                "student",
                "student" + str(n),
                {"person_source_key": keys["person" + str(n)], "status": "active"},
            ),
            fact(
                "student_school_relationship",
                "ssr" + str(n),
                {
                    "student_source_key": keys["student" + str(n)],
                    "student_number": token[:10] + str(n),
                    "enrollment_status": "enrolled",
                    "residence_mode": "day",
                    "primary_campus_source_key": campus,
                    "administrative_campus_source_key": campus,
                },
            ),
        ]
    apply(writer, entries)
    entries = [
        fact(
            "education_class",
            key,
            {
                "display_name": key,
                "teacher_mode": "single_primary",
                "teacher_role_limits": {"primary": 1, "acting": 1},
                "primary_classroom_limit": 1,
            },
        )
        for key in ("a", "b")
    ]
    entries += [
        fact(
            "class_position_definition",
            "definition",
            {
                "class_source_key": keys["a"],
                "display_name": "monitor",
                "allow_multiple": True,
                "allow_concurrent": True,
                "max_holders": 2,
            },
        )
    ]
    for n in (1, 2):
        entries += [
            fact(
                "student_class_membership",
                "member" + str(n),
                {
                    "student_source_key": keys["student" + str(n)],
                    "school_relationship_source_key": keys["ssr" + str(n)],
                    "class_source_key": keys["a"],
                    "membership_status": "assigned",
                },
            ),
            fact(
                "student_class_position",
                "role" + str(n),
                {
                    "student_source_key": keys["student" + str(n)],
                    "class_source_key": keys["a"],
                    "position_source_key": keys["definition"],
                },
            ),
        ]
    apply(writer, entries)

    def read_all(at):
        records = []
        version = None
        for kind, names in [
            ("student_class_membership", ("member1", "member2")),
            ("student_class_position", ("role1", "role2")),
        ]:
            raw = query(
                writer, kind, [keys[k] for k in names], list(EDUCATION_CONTENT_FIELDS[kind]), at
            )
            records.extend(raw["records"])
            version = raw["school_fact_version"]
        return records, version

    def change(n):
        return {
            "fact_kind": "student_class_membership",
            "target_ref": "f4:student_class_membership:" + keys["member" + str(n)],
            "operation": "correct",
            "fields": {
                "class_ref": "f4:education_class:" + keys["b"],
                "valid_to": resumed.isoformat(),
            },
            "reason": "Verified bounded historical transfer",
        }

    records, version = read_all(middle)
    coordinator, _ = environment(records, version)
    payload = EducationBatchInput.model_validate_json(
        json.dumps(
            {
                **change(1),
                "purpose": "historical_correction",
                "corrected_purpose": "transfer",
                "additional_changes": [change(2)],
            }
        )
    )
    result = coordinator.resolve_change(
        actor=writer[1], payload=payload, request_id=uuid4(), effective_at=middle
    )
    assert len(result.change_set.changes) == 4
    assert all(
        len(i["after_segments"]) == 2
        for i in result.summary.impacts
        if i["fact_kind"] == "student_class_position"
    )
    apply(
        writer, [c.model_dump(mode="json", exclude_unset=True) for c in result.change_set.changes]
    )
    records, version = read_all(now)
    for row in records:
        assert len(row["segments"]) == (3 if row["fact_kind"] == "student_class_membership" else 2)
    # Graduation uses the actual SSR timeline and ends only this student's membership/role.
    coordinator, _ = environment(records, version)

    class Family:
        def source_for_reference(self, *args):
            return "student_school_relationship", keys["ssr1"]

        def read_education_timeline(self, **kw):
            with writer[0]["reader"].connect() as c:
                return c.scalar(
                    text(
                        "SELECT app.school_fact_f4_ssr_timeline(:s,:key,ARRAY['student_source_key','student_number','enrollment_status','residence_mode','primary_campus_source_key','administrative_campus_source_key','valid_to'])"
                    ),
                    {"s": writer[1].school_id, "key": keys["ssr1"]},
                )

        def resolve_field_references(self, **kw):
            return kw["fields"]

        def public_field_references(self, **kw):
            return kw["fields"]

    coordinator.family = SimpleNamespace(repository=Family())
    payload = EducationBatchInput.model_validate_json(
        json.dumps(
            {
                "fact_kind": "student_school_relationship",
                "target_ref": "family:relation",
                "fields": {"enrollment_status": "graduated"},
                "reason": "Verified graduation",
            }
        )
    )
    result = coordinator.resolve_change(
        actor=writer[1], payload=payload, request_id=uuid4(), effective_at=now
    )
    assert {c.source_key for c in result.change_set.changes} == {
        keys[k] for k in ("ssr1", "member1", "role1")
    }
    apply(
        writer, [c.model_dump(mode="json", exclude_unset=True) for c in result.change_set.changes]
    )
    records, _ = read_all(now)
    assert all(
        row["status"] == "no_current_records"
        for row in records
        if row["source_key"] in {keys["member1"], keys["role1"]}
    )
    assert all(
        row["status"] == "available"
        for row in records
        if row["source_key"] in {keys["member2"], keys["role2"]}
    )


def test_two_class_teacher_replacement_capacity_rejection_and_class_closure_are_atomic(writer):
    import json

    from app.composition.school_fact_education_product import EducationBatchInput
    from app.domains.students.education_contracts import EDUCATION_CONTENT_FIELDS
    from app.platform.school_facts.application import SchoolFactChangeRejected
    from tests.school_facts.test_f4_education_maintenance import environment

    now = datetime.now(UTC)
    start = now - timedelta(seconds=40)
    token = uuid4().hex
    keys = {
        n: "f4:teachers:" + n + ":" + token
        for n in ("a", "b", "c", "old-a", "old-b", "new-a", "new-b", "excess", "race1", "race2")
    }
    with writer[0]["migration"].connect() as c:
        assignment = c.scalar(
            text(
                "SELECT a.source_key FROM staff_assignments a WHERE a.school_id=:s AND a.valid_from<=:at AND (a.valid_to IS NULL OR a.valid_to>:at) ORDER BY a.valid_from LIMIT 1"
            ),
            {"s": writer[1].school_id, "at": start},
        )
    assert assignment

    def item(kind, key, content, at=start, expected=0):
        return {
            "fact_kind": kind,
            "source_key": keys[key],
            "content": content,
            "effective_at": at,
            "expected_version": expected,
            "operation": "upsert",
        }

    classes = {
        k: {
            "display_name": k,
            "teacher_mode": "single_primary",
            "teacher_role_limits": {"primary": 1, "acting": 1},
            "primary_classroom_limit": 1,
        }
        for k in ("a", "b", "c")
    }
    def teacher(k):
        return {"class_source_key": keys[k], "assignment_source_key": assignment,
                "teacher_role": "primary"}
    apply(
        writer,
        [
            *(item("education_class", k, classes[k]) for k in ("a", "b", "c")),
            *(item("class_teacher_assignment", "old-" + k, teacher(k)) for k in ("a", "b")),
        ],
    )

    def read_all(at):
        records = []
        version = None
        for kind, names in [
            ("education_class", ("a", "b")),
            ("class_teacher_assignment", ("old-a", "old-b", "new-a", "new-b")),
        ]:
            raw = query(
                writer, kind, [keys[k] for k in names], list(EDUCATION_CONTENT_FIELDS[kind]), at
            )
            records.extend(raw["records"])
            version = raw["school_fact_version"]
        return records, version

    def replacement(k):
        return {
            "fact_kind": "class_teacher_assignment",
            "creation_ref": "f4create:class_teacher_assignment:" + keys["new-" + k],
            "fields": {
                "class_ref": "f4:education_class:" + keys[k],
                "assignment_ref": "f4:assignment:" + assignment,
                "teacher_role": "primary",
            },
            "reason": "Approved replacement",
        }

    records, version = read_all(now)
    coordinator, _ = environment(records, version)
    payload = EducationBatchInput.model_validate_json(
        json.dumps(
            {
                **replacement("a"),
                "purpose": "replace_assignment",
                "replace_refs": [
                    "f4:class_teacher_assignment:" + keys["old-" + k] for k in ("a", "b")
                ],
                "additional_changes": [replacement("b")],
            }
        )
    )
    result = coordinator.resolve_change(
        actor=writer[1], payload=payload, request_id=uuid4(), effective_at=now
    )
    assert len(result.change_set.changes) == 4
    apply(
        writer, [c.model_dump(mode="json", exclude_unset=True) for c in result.change_set.changes]
    )
    # An over-capacity teacher cannot commit the unrelated class rename in the same batch.
    with pytest.raises(SchoolFactChangeRejected):
        apply(
            writer,
            [
                item(
                    "education_class",
                    "b",
                    {**classes["b"], "display_name": "Must roll back"},
                    now,
                    1,
                ),
                item("class_teacher_assignment", "excess", teacher("a"), now),
            ],
        )
    raw = query(writer, "education_class", [keys["b"]], ["display_name"], now)
    assert (
        raw["records"][0]["version"] == 1
        and raw["records"][0]["segments"][0]["content"]["display_name"] == "b"
    )
    # Two confirmations race for one vacant primary position from the same school version.
    from concurrent.futures import ThreadPoolExecutor
    from hashlib import sha256

    from app.platform.school_facts.contracts import CanonicalFactChangeSetV3

    plans = []
    for name in ("race1", "race2"):
        plan = CanonicalFactChangeSetV3.model_validate_json(
            json.dumps(
                {
                    "schema_version": 3,
                    "base_version": raw["school_fact_version"],
                    "source_sha256": sha256(uuid4().bytes).hexdigest(),
                    "changes": [
                        {
                            "schema_version": 3,
                            "origin": {"sheet_code": "education", "row_number": 1},
                            **item("class_teacher_assignment", name, teacher("c"), now),
                        }
                    ],
                },
                default=str,
            )
        )
        prepared = writer[4].prepare_confirmation(
            change_set=plan, context=writer[3], session_binding=writer[2].session_nonce_hash
        )
        plans.append((plan, prepared))

    def confirm(pair):
        plan, prepared = pair
        try:
            writer[4].confirm(
                credential=prepared.credential,
                change_set=plan,
                context=writer[3],
                session_binding=writer[2].session_nonce_hash,
            )
            return "applied"
        except SchoolFactChangeRejected:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(confirm, plans))
    assert sorted(outcomes) == ["applied", "rejected"]
    raced = query(
        writer,
        "class_teacher_assignment",
        [keys["race1"], keys["race2"]],
        ["class_source_key"],
        now,
    )
    assert sum(row["status"] == "available" for row in raced["records"]) == 1
    closing = now + timedelta(seconds=5)
    records, version = read_all(closing)
    coordinator, _ = environment(records, version)

    def end_class(k):
        return {
            "fact_kind": "education_class",
            "target_ref": "f4:education_class:" + keys[k],
            "operation": "end",
            "fields": {},
            "reason": "Approved closure",
        }

    payload = EducationBatchInput.model_validate_json(
        json.dumps({**end_class("a"), "additional_changes": [end_class("b")]})
    )
    result = coordinator.resolve_change(
        actor=writer[1], payload=payload, request_id=uuid4(), effective_at=closing
    )
    assert {c.source_key for c in result.change_set.changes} == {
        keys[k] for k in ("a", "b", "new-a", "new-b")
    }
    apply(
        writer, [c.model_dump(mode="json", exclude_unset=True) for c in result.change_set.changes]
    )
    records, _ = read_all(closing)
    assert all(r["status"] == "no_current_records" for r in records if r["version"])


def test_dependencies_require_exact_owner_field_position_campus_and_year(writer):
    from tests.school_facts.test_f4_writer_postgres import (
        test_education_quality_and_policy_scope_use_same_formal_owner,
    )

    test_education_quality_and_policy_scope_use_same_formal_owner(writer)
    with writer[0]["migration"].connect() as c:
        row = (
            c.execute(
                text("""SELECT q.source_key quality_key,e.source_key owner_key,pos.source_key position_key,
        s.source_key scope_key,p.source_key policy_key,loc.source_key campus_key
        FROM school_fact_qualities q JOIN school_education_facts e ON e.id=q.education_target_id
        JOIN school_education_facts pos ON pos.id=q.education_position_id
        JOIN school_policy_scopes s ON s.education_target_id=e.id JOIN school_policies p ON p.id=s.policy_id
        JOIN campus_locations loc ON loc.id=s.campus_location_id
        WHERE q.school_id=:school AND q.source_key LIKE 'quality:f4-%' ORDER BY q.valid_from DESC LIMIT 1"""),
                {"school": writer[1].school_id},
            )
            .mappings()
            .one()
        )
    params = {
        "s": writer[1].school_id,
        "kind": "fact_quality",
        "keys": [row["quality_key"]],
        "owners": [row["owner_key"]],
        "positions": [row["position_key"]],
        "year": 2026,
        "policies": [row["policy_key"]],
        "campuses": [row["campus_key"]],
    }
    sql = text("""SELECT app.school_fact_f4_dependencies(:s,:kind,CAST(:keys AS text[]),'education_class',CAST(:owners AS text[]),
    'student_class_position',ARRAY[]::text[],ARRAY['position_source_key'],now(),:year,
    CAST(:policies AS text[]),CAST(:campuses AS text[]),CAST(:positions AS text[]))""")
    with writer[0]["reader"].connect() as c:
        selection = c.scalar(
            text("SELECT app.school_fact_f4_dependency_selection(:s,'fact_quality',:key,now())"),
            {"s": writer[1].school_id, "key": row["quality_key"]},
        )
        assert len(selection["records"]) == 1
        assert selection["records"][0]["owner_key"] == row["owner_key"]
        assert "content" not in selection["records"][0]
        raw = c.scalar(sql, params)
        assert len(raw["records"]) == 1
        assert raw["records"][0]["content"]["negative_code"] == "vacant_position"
        assert raw["records"][0]["source"]["batch_id"]
        assert c.scalar(sql, params | {"positions": []})["records"] == []
        policy = params | {"kind": "policy_scope", "keys": [row["scope_key"]]}
        assert len(c.scalar(sql, policy)["records"]) == 1
        assert c.scalar(sql, policy | {"year": 2025})["records"] == []
        assert c.scalar(sql, policy | {"campuses": []})["records"] == []


def test_ssr_corrected_reader_preserves_three_periods_and_projects_only_authorized_cutoff(writer):
    with writer[0]["migration"].connect() as c:
        live = c.scalar(
            text(
                "SELECT source_key FROM student_school_relationships WHERE school_id=:s AND superseded_at IS NULL AND source_key IS NOT NULL ORDER BY version DESC LIMIT 1"
            ),
            {"s": writer[1].school_id},
        )
    assert live
    with writer[0]["reader"].connect() as c:
        timeline = c.scalar(
            text("SELECT app.school_fact_f4_ssr_timeline(:s,:key,ARRAY['valid_to'])"),
            {"s": writer[1].school_id, "key": live},
        )
    assert timeline["segments"]
    assert all(
        set(s["content"]) == {"valid_to"} and s["source"]["batch_id"] for s in timeline["segments"]
    )
    with writer[0]["migration"].connect() as c:
        rows = (
            c.execute(
                text("""SELECT source_key,valid_from,valid_to,enrollment_status,version
        FROM student_school_relationships WHERE school_id=:s AND source_key LIKE 'enrollment:f4-ssr-%'
        AND superseded_at IS NULL ORDER BY source_key,valid_from"""),
                {"s": writer[1].school_id},
            )
            .mappings()
            .all()
        )
    if not rows:
        with writer[0]["migration"].connect() as c:
            key = c.scalar(
                text(
                    "SELECT source_key FROM student_school_relationships WHERE school_id=:s AND source_key LIKE 'enrollment:f4-ssr-%' ORDER BY version DESC LIMIT 1"
                ),
                {"s": writer[1].school_id},
            )
        assert key
        with writer[0]["reader"].connect() as c:
            assert (
                c.scalar(
                    text(
                        "SELECT app.school_fact_f3_scope_is_current(:s,'student_school_relationship',ARRAY[:key],now())"
                    ),
                    {"s": writer[1].school_id, "key": key},
                )
                is True
            )
            raw = c.scalar(
                text(
                    "SELECT app.school_fact_f3_staff_target(:s,'student_school_relationship',:key,false,now(),ARRAY['valid_to'])"
                ),
                {"s": writer[1].school_id, "key": key},
            )
            assert (
                raw["status"] == "no_current_records"
                and raw["content"] == {}
                and raw["expected_version"] > 0
            )
        with writer[0]["reader"].connect() as c, pytest.raises(DBAPIError):
            c.scalar(
                text("SELECT app_private.f4_ssr_coordinate_exists(:s,:key,1)"),
                {"s": writer[1].school_id, "key": key},
            )
        return
    assert len(rows) >= 3
    for row in rows:
        at = row["valid_from"] + timedelta(seconds=1)
        if at > datetime.now(UTC):
            continue
        with writer[0]["reader"].connect() as c:
            assert (
                c.scalar(
                    text(
                        "SELECT app.school_fact_f3_scope_is_current(:s,'student_school_relationship',ARRAY[:key],now())"
                    ),
                    {"s": writer[1].school_id, "key": row["source_key"]},
                )
                is True
            )
            raw = c.scalar(
                text(
                    "SELECT app.school_fact_f3_staff_target(:s,'student_school_relationship',:key,false,:at,ARRAY['valid_to','enrollment_status'])"
                ),
                {"s": writer[1].school_id, "key": row["source_key"], "at": at},
            )
            narrow = c.scalar(
                text(
                    "SELECT app.school_fact_f3_staff_target(:s,'student_school_relationship',:key,false,:at,ARRAY['enrollment_status'])"
                ),
                {"s": writer[1].school_id, "key": row["source_key"], "at": at},
            )
        assert raw["status"] == "available"
        assert raw["content"]["enrollment_status"] == row["enrollment_status"]
        actual_end = raw["content"]["valid_to"]
        assert (None if actual_end is None else datetime.fromisoformat(actual_end)) == row[
            "valid_to"
        ]
        assert set(raw["content"]) == {"valid_to", "enrollment_status"}
        assert set(narrow["content"]) == {"enrollment_status"}
        assert raw["expected_version"] >= row["version"]


@pytest.fixture(autouse=True)
def safe_database_diagnostic(monkeypatch):
    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence

    def safe_error(exc):
        diag = getattr(exc.orig, "diag", None)
        raise AssertionError(
            "database_failure:"
            + str(getattr(exc.orig, "sqlstate", None))
            + ":"
            + str(getattr(diag, "message_primary", None))
            + ":"
            + str(exc.statement)
        ) from None

    monkeypatch.setattr(
        SqlAlchemySchoolFactPersistence, "_raise_db_error", staticmethod(safe_error)
    )


def query(w, kind, keys, fields, at, known=None, filters=None):
    import json

    with w[0]["reader"].connect() as c:
        return c.scalar(
            text(
                "SELECT app.school_fact_f4_targets(:s,:k,:keys,:fields,:at,:known,CAST(:filters AS jsonb))"
            ),
            {
                "s": w[1].school_id,
                "k": kind,
                "keys": keys,
                "fields": fields,
                "at": at,
                "known": known,
                "filters": json.dumps(filters or {}),
            },
        )


def test_staff_current_corrected_and_known_at_views_preserve_three_segments(writer):
    now = datetime.now(UTC)
    start = now - timedelta(days=30)
    middle = now - timedelta(days=20)
    later = now - timedelta(days=10)
    key = "class:reader-" + uuid4().hex
    apply(writer, [entry(key, "original", start)])
    known = datetime.now(UTC)
    apply(writer, [entry(key, "later", later, expected=1)])
    apply(
        writer,
        [
            entry(
                key,
                "corrected",
                middle,
                operation="correct",
                expected=2,
                until=later,
                reason="correct history",
            )
        ],
    )
    current = query(writer, "education_class", [key], ["display_name"], now)
    assert [s["content"] for s in current["records"][0]["segments"]] == [
        {"display_name": "original"},
        {"display_name": "corrected"},
        {"display_name": "later"},
    ]
    assert current["records"][0]["version"] == 3
    historical = query(writer, "education_class", [key], ["display_name"], now, known)
    assert [s["content"] for s in historical["records"][0]["segments"]] == [
        {"display_name": "original"}
    ]
    with writer[0]["reader"].connect() as c:
        assert (
            c.scalar(
                text("SELECT app.school_fact_f4_scope_is_current(:s,'education_class',:keys,:at)"),
                {"s": writer[1].school_id, "keys": [key], "at": now},
            )
            is True
        )


def test_cancelled_root_is_distinct_from_absence_and_invalid_projection_denies(writer):
    now = datetime.now(UTC)
    future = now + timedelta(days=10)
    key = "class:cancel-reader-" + uuid4().hex
    apply(writer, [entry(key, "future", future)])
    apply(
        writer,
        [
            entry(
                key,
                "",
                future,
                operation="cancel",
                expected=1,
                reason="future arrangement withdrawn",
            )
        ],
    )
    result = query(
        writer, "education_class", [key, "class:never-created"], ["display_name"], future
    )
    states = {r["source_key"]: r["status"] for r in result["records"]}
    assert states == {key: "cancelled", "class:never-created": "not_recorded"}
    for fields, known in [
        (["student_source_key"], None),
        (["display_name"], now + timedelta(days=1)),
    ]:
        with pytest.raises(DBAPIError):
            query(writer, "education_class", [key], fields, now, known)
    with pytest.raises(DBAPIError):
        query(
            writer,
            "education_class",
            [key],
            ["display_name"],
            now,
            filters={"class_source_key": "class:one"},
        )


def test_runtime_cannot_call_reader_and_reader_cannot_select_tables(writer):
    for role, sql in [
        (
            "runtime",
            "SELECT app.school_fact_f4_targets(NULL,'education_class',ARRAY[]::text[],ARRAY['display_name'],now())",
        ),
        ("reader", "SELECT payload FROM school_education_versions"),
    ]:
        with writer[0][role].connect() as c, pytest.raises(DBAPIError):
            c.execute(text(sql))


def test_field_changed_uses_exact_revision_and_merges_unchanged_field_slices(writer):
    now = datetime.now(UTC)
    start = now - timedelta(days=30)
    middle = now - timedelta(days=20)
    later = now - timedelta(days=10)
    key = "class:field-diff-" + uuid4().hex
    initial = entry(key, "same name", start)
    initial["content"]["short_name"] = "old"
    apply(writer, [initial])
    changed = entry(
        key,
        "same name",
        middle,
        operation="correct",
        expected=1,
        until=later,
        reason="short name corrected",
    )
    changed["content"]["short_name"] = "new"
    apply(writer, [changed])
    with writer[0]["writer"].connect() as c:
        params = {"s": writer[1].school_id, "key": key}
        assert (
            c.scalar(
                text(
                    "SELECT app.school_fact_f3_field_changed(:s,'education_class',:key,2,'display_name')"
                ),
                params,
            )
            is False
        )
        assert (
            c.scalar(
                text(
                    "SELECT app.school_fact_f3_field_changed(:s,'education_class',:key,2,'short_name')"
                ),
                params,
            )
            is True
        )
        assert (
            c.scalar(
                text(
                    "SELECT app.school_fact_f3_field_changed(:s,'education_class','class:wrong',2,'short_name')"
                ),
                params,
            )
            is False
        )


def test_guardian_requires_current_verified_relationship_and_can_read_future_education_only(writer):
    now = datetime.now(UTC)
    with writer[0]["migration"].connect() as c:
        relation = c.execute(
            text(
                "SELECT id,guardian_person_id,version,valid_from FROM guardian_relationships WHERE relationship_kind='verified_guardian' AND status='confirmed' AND revoked_at IS NULL AND valid_from<=now() AND (valid_to IS NULL OR valid_to>now()) ORDER BY id LIMIT 1"
            )
        ).one()
    params = {
        "s": writer[1].school_id,
        "adult": relation.guardian_person_id,
        "relation": relation.id,
        "version": relation.version,
        "at": now + timedelta(hours=1),
    }
    with writer[0]["reader"].connect() as c:
        result = c.scalar(
            text(
                "SELECT app.school_fact_family_education(:s,:adult,:relation,:version,:at,ARRAY['class','teachers'])"
            ),
            params,
        )
        assert result["status"] in {"available", "no_current_records"}
    params["version"] += 1
    with writer[0]["reader"].connect() as c, pytest.raises(DBAPIError):
        c.execute(
            text(
                "SELECT app.school_fact_family_education(:s,:adult,:relation,:version,:at,ARRAY['class'])"
            ),
            params,
        )


def test_guardian_published_class_teacher_and_own_position_exclude_other_students(writer):
    now = datetime.now(UTC)
    at = now - timedelta(seconds=2)
    suffix = uuid4().hex
    with writer[0]["migration"].connect() as c:
        row = c.execute(
            text(
                "SELECT r.id,r.guardian_person_id,r.version,s.source_key AS student_key,l.fact_source_key AS relation_key FROM guardian_relationships r JOIN students s ON s.school_id=r.school_id AND s.id=r.student_id JOIN student_school_relationships ss ON ss.school_id=s.school_id AND ss.student_id=s.id JOIN school_fact_change_lineage l ON l.school_id=ss.school_id AND l.fact_source_key=ss.source_key AND l.fact_kind='student_school_relationship' AND l.target_version=ss.version WHERE r.relationship_kind='verified_guardian' AND r.status='confirmed' AND r.revoked_at IS NULL AND r.valid_from<=now() AND (r.valid_to IS NULL OR r.valid_to>now()) AND ss.superseded_at IS NULL AND ss.valid_from<=now() AND (ss.valid_to IS NULL OR ss.valid_to>now()) ORDER BY r.id LIMIT 1"
            )
        ).one()
        teacher = c.execute(
            text(
                "SELECT source_key FROM staff_assignments WHERE status='confirmed' AND valid_from<=now() AND (valid_to IS NULL OR valid_to>now()) AND revoked_at IS NULL ORDER BY id LIMIT 1"
            )
        ).scalar_one()
    # Retain and formally end only this test's earlier failed/successful fixtures.
    # This keeps repeated runs independent without deleting published evidence.
    with writer[0]["migration"].connect() as c:
        prior = c.execute(
            text(
                "SELECT f.kind,f.source_key,f.version FROM school_education_facts f JOIN school_education_versions v ON v.school_id=f.school_id AND v.fact_id=f.id WHERE f.kind IN ('student_class_membership','student_class_position') AND v.superseded_at IS NULL AND v.valid_to IS NULL AND v.payload->>'student_source_key'=:student AND v.payload->>'class_source_key' LIKE 'education-class:%' AND f.source_key LIKE ANY(ARRAY['member:%','own-position:%'])"
            ),
            {"student": row.student_key},
        ).all()
    if prior:
        apply(
            writer,
            [
                {
                    "fact_kind": x.kind,
                    "source_key": x.source_key,
                    "operation": "end",
                    "expected_version": x.version,
                    "effective_at": at,
                    "content": None,
                }
                for x in prior
            ],
        )
    keys = {
        k: k + ":" + suffix
        for k in ("education-class", "member", "teacher", "position-definition", "own-position")
    }

    def item(kind, key, content):
        return {
            "fact_kind": kind,
            "source_key": key,
            "operation": "upsert",
            "expected_version": 0,
            "effective_at": at,
            "content": content,
        }

    apply(
        writer,
        [
            item(
                "education_class",
                keys["education-class"],
                {
                    "display_name": "guardian isolated class",
                    "teacher_mode": "single_primary",
                    "teacher_role_limits": {"primary": 1, "acting": 1},
                    "primary_classroom_limit": 1,
                },
            ),
            item(
                "student_class_membership",
                keys["member"],
                {
                    "student_source_key": row.student_key,
                    "school_relationship_source_key": row.relation_key,
                    "membership_status": "assigned",
                    "class_source_key": keys["education-class"],
                },
            ),
            item(
                "class_teacher_assignment",
                keys["teacher"],
                {
                    "class_source_key": keys["education-class"],
                    "assignment_source_key": teacher,
                    "teacher_role": "primary",
                },
            ),
            item(
                "class_position_definition",
                keys["position-definition"],
                {
                    "class_source_key": keys["education-class"],
                    "display_name": "monitor",
                    "allow_multiple": True,
                    "allow_concurrent": True,
                    "max_holders": 3,
                },
            ),
            item(
                "student_class_position",
                keys["own-position"],
                {
                    "class_source_key": keys["education-class"],
                    "student_source_key": row.student_key,
                    "position_source_key": keys["position-definition"],
                },
            ),
        ],
    )
    params = {
        "s": writer[1].school_id,
        "adult": row.guardian_person_id,
        "relation": row.id,
        "version": row.version,
        "at": now,
    }
    with writer[0]["reader"].connect() as c:
        result = c.scalar(
            text(
                "SELECT app.school_fact_family_education(:s,:adult,:relation,:version,:at,ARRAY['class','teachers','positions'])"
            ),
            params,
        )
    returned = {r["source_key"] for r in result["records"]}
    assert set(keys.values()) <= returned
    assert result["related_teachers"] and result["related_teachers"][0]["display_name"]
    from app.composition.school_fact_family_education_query import _public

    public = _public(result, ("class", "teachers", "positions"), now)
    assert public["profile"]["class"] and public["profile"]["class"][0]
    assert public["profile"]["teachers"][0]["display_name"]
    assert public["profile"]["positions"] and public["profile"]["positions"][0]
    assert "student_id" not in public and "records" not in public
    for record in result["records"]:
        if record["fact_kind"] in {
            "student_class_membership",
            "student_class_position",
            "student_cohort_membership",
        }:
            assert all(
                v["content"]["student_source_key"] == row.student_key for v in record["segments"]
            )
    with writer[0]["writer"].connect() as c:
        targets = c.execute(
            text(
                "SELECT fact_kind,fact_source_key,field_name FROM app.school_fact_family_correction_targets(:s,:adult,:relation,:version)"
            ),
            params,
        ).all()
    assert ("student_class_membership", keys["member"], "class_source_key") in targets
    filtered = query(
        writer,
        "class_teacher_assignment",
        [keys["teacher"]],
        ["class_source_key", "teacher_role"],
        now,
        filters={"class_source_key": "class:wrong"},
    )
    assert filtered["status"] == "no_current_records" and filtered["records"] == []
    matched = query(
        writer,
        "class_teacher_assignment",
        [keys["teacher"]],
        ["class_source_key", "teacher_role"],
        now,
        filters={"class_source_key": [keys["education-class"]]},
    )
    assert len(matched["records"]) == 1


def test_creation_coordinates_and_causal_prior_content_are_bounded(writer):
    now = datetime.now(UTC)
    key = "class:causal-" + uuid4().hex
    future = now + timedelta(days=3)
    apply(writer, [entry(key, "retained", now - timedelta(days=3))])
    ending = entry(key, "", future, operation="end", expected=1)
    ending.update(cause_source_key=key, cause_version=2)
    apply(writer, [ending])
    result = query(writer, "education_class", [key], ["display_name", "valid_to"], now)
    record = result["records"][0]
    assert record["segments"][0]["cause_source_key"] == key
    assert record["segments"][0]["content"]["valid_to"] is not None
    assert record["prior_segments"][0]["content"]["valid_to"] is None
    with writer[0]["reader"].connect() as c:
        coordinates = c.scalar(
            text("SELECT app.school_fact_f4_coordinates(:s,'education_class',:keys)"),
            {"s": writer[1].school_id, "keys": [key, "class:unknown"]},
        )
    assert all(set(r) == {"source_key", "version", "status"} for r in coordinates["records"])
    assert {r["source_key"]: r["status"] for r in coordinates["records"]} == {
        key: "exists",
        "class:unknown": "not_recorded",
    }
    with writer[0]["reader"].connect() as c, pytest.raises(DBAPIError):
        c.execute(
            text("SELECT app_private.f4_child_keys(:s,:student,now(),ARRAY['positions'])"),
            {"s": writer[1].school_id, "student": uuid4()},
        )
