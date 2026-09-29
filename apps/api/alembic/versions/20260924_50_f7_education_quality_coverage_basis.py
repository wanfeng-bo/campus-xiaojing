"""Carry verified quality basis through the retained F4/F5 authorized projections.

The old fixed Readers remain authoritative for row selection and permission
checks. This append-only revision only adds a server-stored proof to rows they
already selected; it does not introduce another way to enumerate school facts.
"""

from __future__ import annotations

import hashlib
import runpy
from pathlib import Path

from sqlalchemy import text

from alembic import op

revision = "20260924_50"
down_revision = "20260924_49"
branch_labels = None
depends_on = None

_PRIOR = (
    ("20260913_29_school_education_read.py", "DEPENDENCY_SQL",
     "14ad9f83cfa5e1f623fc51c77734cbf2216e8cb0ffdf483c52439e87b0323de9"),
    ("20260914_35_f5_education_dependencies.py", "DEPENDENCY_SQL",
     "de09e84400109d893e4ac16df2b03ee99eab3e4ba74d311e8fc7576026b9a502"),
    ("20260914_37_f5_calendar_timezone.py", "CALENDAR_TIMEZONE_SQL",
     "84585c132d7a8a0dfa68b6b562a6bfcfca081c436d695fd9343a15b13cf24b3f"),
)


def _source(name: str, symbol: str, digest: str) -> str:
    path = Path(__file__).with_name(name)
    original = runpy.run_path(str(path))[symbol]
    if hashlib.sha256(original.encode()).hexdigest() != digest:
        raise RuntimeError("Retained authorized Reader source changed; inspect before migration")
    return original


def _replace_once(value: str, before: str, after: str) -> str:
    if value.count(before) != 1:
        raise RuntimeError("Retained quality projection no longer matches its reviewed shape")
    return value.replace(before, after, 1)


def _dependency_function(sql: str, name: str) -> str:
    marker = f"CREATE FUNCTION app.{name}("
    start = sql.index(marker)
    end = sql.index("\n$f$;", start) + len("\n$f$;")
    function = sql[start:end]
    function = _replace_once(function, marker, f"CREATE OR REPLACE FUNCTION app.{name}(")
    function = _replace_once(
        function,
        "q.purpose,q.contact_kind,q.coverage_status,q.negative_code",
        "q.purpose,q.contact_kind,q.coverage_status,q.coverage_basis,q.negative_code",
    )
    return function


def upgrade() -> None:
    f4 = _source(*_PRIOR[0])
    f5 = _source(*_PRIOR[1])
    # Retain the authorized app_private.f5_child_projection body and selection.
    child = _source(*_PRIOR[2])
    child = _replace_once(
        child,
        "q.field_name,q.target_source_key,q.replacement_source_key",
        "q.field_name,q.coverage_basis,q.target_source_key,q.replacement_source_key",
    )
    child = _replace_once(
        child,
        "'negative_code',negative_code,'coverage_status',coverage_status,'dispute_status'",
        "'negative_code',negative_code,'coverage_status',coverage_status,"
        "'coverage_basis',coverage_basis,'dispute_status'",
    )
    # The existing SECURITY DEFINER functions belong to this role. Temporarily
    # permit replacing exactly those functions; restore prior schema privileges
    # before the migration transaction commits.
    bind = op.get_bind()
    temporary_create = tuple(
        schema for schema in ("app", "app_private")
        if not bind.execute(
            text("SELECT has_schema_privilege('campus_ai_context_definer', :schema, 'CREATE')"),
            {"schema": schema},
        ).scalar_one()
    )
    for schema in temporary_create:
        op.execute(f"GRANT CREATE ON SCHEMA {schema} TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(_dependency_function(f4, "school_fact_f4_dependencies"))
    op.execute(_dependency_function(f5, "school_fact_f5_dependencies"))
    op.execute(child)
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    for schema in temporary_create:
        op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM campus_ai_context_definer")


def downgrade() -> None:
    raise RuntimeError("Published quality projection evidence is append-only")
