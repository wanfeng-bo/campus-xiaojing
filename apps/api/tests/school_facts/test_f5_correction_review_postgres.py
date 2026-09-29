# ruff: noqa: E501
"""Opt-in audit constraints; all test rows are rolled back in the owned database."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.school_facts.test_f3_family_postgres import safe_test_engine

pytestmark = pytest.mark.skipif(
    not os.environ.get("F5_LEADERSHIP_PG"), reason="explicit owned F5 PG required"
)


def test_review_is_separate_from_changed_receipt_and_immutable():
    metadata = json.loads(Path(os.environ["F3_TEST_TARGET_METADATA"]).read_text())
    assert metadata["owner"] == "F5 persistence and reader controlled tests"
    config = Path(os.environ["F3_TEST_DATABASE_CONFIG"])
    owner, writer = safe_test_engine(config, "migration"), safe_test_engine(config, "writer")
    try:
        with owner.connect() as c:
            g = c.execute(
                text(
                    "SELECT school_id,id,guardian_person_id,student_id FROM guardian_relationships WHERE relationship_kind='verified_guardian' ORDER BY id LIMIT 1"
                )
            ).one()
            a = c.execute(
                text(
                    "SELECT id,person_id FROM staff_assignments WHERE school_id=:s ORDER BY id LIMIT 1"
                ),
                {"s": g.school_id},
            ).one()
        with writer.connect() as c:
            tx = c.begin()
            try:
                params = {
                    "id": uuid4(),
                    "s": g.school_id,
                    "g": g.id,
                    "p": g.guardian_person_id,
                    "student": g.student_id,
                    "key": uuid4().hex * 2,
                    "rp": a.person_id,
                    "ra": a.id,
                }
                c.execute(
                    text("""INSERT INTO family_fact_correction_reports(id,school_id,reporter_person_id,
                    guardian_relationship_id,student_id,fact_kind,fact_source_key,field_name,fact_target_version,
                    statement,idempotency_key_sha256,request_sha256,status,submitted_at)
                    VALUES(:id,:s,:p,:g,:student,'teaching_assignment','review:test','teaching_role',1,
                    'review test',:key,:key,'submitted',now())"""),
                    params,
                )
                with pytest.raises(DBAPIError):
                    with c.begin_nested():
                        c.execute(
                            text(
                                "UPDATE family_fact_correction_reports SET status='reviewed',resolved_at=now() WHERE id=:id"
                            ),
                            params,
                        )
                c.execute(
                    text("""UPDATE family_fact_correction_reports SET status='reviewed',resolved_at=now(),
                    review_explanation='已核实原资料无误',reviewer_person_id=:rp,reviewer_assignment_id=:ra,
                    reviewer_assignment_version=1,reviewer_authorization_version=1,
                    review_command_sha256=:key,review_target_version=1 WHERE id=:id"""),
                    params,
                )
                row = c.execute(
                    text(
                        "SELECT status,resolution_locator_sha256 FROM family_fact_correction_reports WHERE id=:id"
                    ),
                    params,
                ).one()
                assert row.status == "reviewed" and row.resolution_locator_sha256 is None
                with pytest.raises(DBAPIError):
                    with c.begin_nested():
                        c.execute(
                            text(
                                "UPDATE family_fact_correction_reports SET review_explanation='rewritten' WHERE id=:id"
                            ),
                            params,
                        )
            finally:
                tx.rollback()
    finally:
        owner.dispose()
        writer.dispose()
