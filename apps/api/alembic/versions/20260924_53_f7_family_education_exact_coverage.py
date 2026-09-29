"""Expose typed proof for the parent Reader's already-authorized exact F4 rows.

This revision adds no new selection or parent permission. It only carries the
published quality claim's coordinates and proof through the retained bounded
selected-child projection, for current target/version verification in Product.
"""

from __future__ import annotations

import hashlib
import runpy
from pathlib import Path

from sqlalchemy import text

from alembic import op

revision = "20260924_53"
down_revision = "20260924_52"
branch_labels = None
depends_on = None

_PRIOR_SHA256 = "052abb5f76aa7ce594d2aa69f047d99f4f1ceeecf3118143a8411d1da001986e"


def _replace_once(value: str, before: str, after: str) -> str:
    if value.count(before) != 1:
        raise RuntimeError("Retained parent Reader no longer matches reviewed shape")
    return value.replace(before, after, 1)


def _function_sql() -> str:
    original = runpy.run_path(str(
        Path(__file__).with_name("20260914_31_family_education_quality.py")
    ))["FAMILY_QUALITY_SQL"]
    if hashlib.sha256(original.encode()).hexdigest() != _PRIOR_SHA256:
        raise RuntimeError("Retained selected-child quality Reader changed")
    function = original[:original.index("\n$f$;") + len("\n$f$;")]
    function = _replace_once(
        function, "CREATE FUNCTION app.school_fact_family_education(",
        "CREATE OR REPLACE FUNCTION app.school_fact_family_education(",
    )
    function = _replace_once(
        function, "q.academic_year,q.target_source_key,q.education_position_id",
        "q.academic_year,q.target_fact_kind,q.target_source_key,"
        "q.coverage_basis,q.education_position_id",
    )
    function = _replace_once(
        function,
        "'academic_year',academic_year,'education_target_kind',education_target_kind,",
        "'academic_year',academic_year,'education_target_kind',education_target_kind,"
        "'target_fact_kind',target_fact_kind,'target_source_key',target_source_key,"
    )
    function = _replace_once(
        function, "'quality_kind',quality_kind,'negative_code',negative_code,"
        "'coverage_status',coverage_status,",
        "'quality_kind',quality_kind,'negative_code',negative_code,"
        "'coverage_status',coverage_status,'coverage_basis',coverage_basis,",
    )
    return function


def upgrade() -> None:
    bind = op.get_bind()
    temporary_create = not bind.execute(text(
        "SELECT has_schema_privilege('campus_ai_context_definer','app','CREATE')"
    )).scalar_one()
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    if temporary_create:
        op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(_function_sql())
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    if temporary_create:
        op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")


def downgrade() -> None:
    raise RuntimeError("Published selected-child quality proof is append-only")
