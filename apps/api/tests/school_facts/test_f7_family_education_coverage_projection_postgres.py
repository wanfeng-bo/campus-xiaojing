"""Formal exact F4 quality reaches only the selected child's authorized item."""

import json
import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.composition.school_fact_family_education_query import _public
from app.platform.school_facts.contracts import CanonicalFactChangeSetV4
from app.platform.school_facts.f7_coverage_basis import mother_set_digest
from tests.school_facts import test_f3_family_postgres, test_f3_writer_postgres
from tests.school_facts.test_f3_family_postgres import existing_fixture_writer, safe_test_engine
from tests.school_facts.test_f4_reader_postgres import (
    test_guardian_published_class_teacher_and_own_position_exclude_other_students as seed_guardian,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("F7_TEST_DATABASE_CONFIG"), reason="owned F7 PG config required"
)


def test_formal_exact_teacher_quality_reaches_selected_child_without_whole_class_claim():
    config_path = Path(os.environ["F7_TEST_DATABASE_CONFIG"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    engines = {
        role: safe_test_engine(config_path, role)
        for role in ("migration", "runtime", "reader", "writer")
    }
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(test_f3_family_postgres, "KEY", config["context"].encode())
            patch.setattr(test_f3_writer_postgres, "KEY", config["context"].encode())
            writer = (engines, *existing_fixture_writer(engines))
            seed_guardian(writer)
            school = writer[1].school_id
            at = datetime.now(UTC)
            with engines["migration"].connect() as connection:
                selected = connection.execute(text("""
                    SELECT g.id,g.guardian_person_id,g.version,
                        c.source_key class_key,t.source_key teacher_key,t.version teacher_version
                    FROM guardian_relationships g
                    JOIN students s ON s.school_id=g.school_id AND s.id=g.student_id
                    JOIN school_education_versions mv ON mv.school_id=s.school_id
                        AND mv.payload->>'student_source_key'=s.source_key
                        AND mv.superseded_at IS NULL AND mv.valid_from<=:at
                        AND (mv.valid_to IS NULL OR :at<mv.valid_to)
                    JOIN school_education_facts m ON m.school_id=mv.school_id AND m.id=mv.fact_id
                        AND m.kind='student_class_membership'
                    JOIN school_education_facts c ON c.school_id=m.school_id
                        AND c.kind='education_class'
                        AND c.source_key=mv.payload->>'class_source_key'
                    JOIN school_education_versions tv ON tv.school_id=c.school_id
                        AND tv.payload->>'class_source_key'=c.source_key
                        AND tv.superseded_at IS NULL AND tv.valid_from<=:at
                        AND (tv.valid_to IS NULL OR :at<tv.valid_to)
                    JOIN school_education_facts t ON t.school_id=tv.school_id AND t.id=tv.fact_id
                        AND t.kind='class_teacher_assignment'
                    WHERE g.school_id=:school AND g.relationship_kind='verified_guardian'
                        AND g.status='confirmed' AND g.revoked_at IS NULL
                        AND g.valid_from<=:at AND (g.valid_to IS NULL OR :at<g.valid_to)
                        AND m.source_key LIKE 'member:%'
                    ORDER BY mv.valid_from DESC LIMIT 1
                """), {"school": school, "at": at}).one()
            with engines["reader"].connect() as connection:
                base = connection.scalar(
                    text("SELECT app.school_fact_current_version(:school)"),
                    {"school": school},
                )
            quality_key = "quality:f7-parent-exact-" + uuid4().hex
            basis = {
                "schema_version": 1, "school_id": str(school),
                "owner_kind": "education", "owner_source_key": selected.class_key,
                "target_fact_kind": "class_teacher_assignment",
                "target_source_key": selected.teacher_key, "field_name": "teacher_role",
                "purpose": None, "contact_kind": None,
                "valid_from": at.isoformat(), "valid_to": None,
                "school_fact_version": base, "member_count": 1,
                "member_sha256": mother_set_digest(((
                    selected.teacher_key, selected.teacher_version
                ),))[1],
            }
            changes = CanonicalFactChangeSetV4.model_validate_json(json.dumps({
                "schema_version": 4,
                "base_version": base,
                "source_sha256": sha256(uuid4().bytes).hexdigest(),
                "changes": [{
                    "schema_version": 4, "fact_kind": "fact_quality",
                    "source_key": quality_key, "operation": "upsert", "expected_version": 0,
                    "effective_at": at.isoformat(),
                    "origin": {"sheet_code": "f7-parent-exact", "row_number": 1},
                    "content": {
                        "quality_kind": "coverage", "coverage_status": "complete",
                        "owner_kind": "education", "education_target_kind": "education_class",
                        "education_target_source_key": selected.class_key,
                        "target_fact_kind": "class_teacher_assignment",
                        "target_source_key": selected.teacher_key,
                        "field_name": "teacher_role", "responsibility": "隔离核查员",
                        "basis_reference": "隔离正式核查记录", "verified_at": at.isoformat(),
                        "coverage_basis": basis,
                    },
                }],
            }, ensure_ascii=False))
            report = writer[4].preflight(
                school_id=school, change_set=changes, context=writer[3],
                session_binding=writer[2].session_nonce_hash,
            )
            assert report.is_clean
            prepared = writer[4].prepare_confirmation(
                change_set=changes, context=writer[3],
                session_binding=writer[2].session_nonce_hash,
            )
            assert writer[4].confirm(
                credential=prepared.credential, change_set=changes,
                context=writer[3], session_binding=writer[2].session_nonce_hash,
            ).changed_count == 1
            at = datetime.now(UTC)
            with engines["reader"].connect() as connection:
                raw = connection.scalar(text("""
                    SELECT app.school_fact_family_education(
                        :school,:adult,:relation,:version,:at,ARRAY['teachers'])
                """), {
                    "school": school, "adult": selected.guardian_person_id,
                    "relation": selected.id, "version": selected.version, "at": at,
                })
            exact = [
                row for row in raw["quality"]
                if row.get("target_source_key") == selected.teacher_key
                and row.get("quality_kind") == "coverage"
            ]
            assert len(exact) == 1
            assert exact[0]["coverage_basis"]["member_sha256"] == basis["member_sha256"]
            public = _public(raw, ("teachers",), at, school_id=school)
            assert public["quality"]["teachers"]["coverage"] == "unknown"
            assert any(
                item.get("quality") == {"record_coverage": "complete"}
                for item in public["profile"]["teachers"]
            )
            assert "member_sha256" not in str(public)
    finally:
        for engine in engines.values():
            engine.dispose()
