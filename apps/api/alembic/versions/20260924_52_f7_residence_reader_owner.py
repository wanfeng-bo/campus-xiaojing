"""Run the F7 exact residence proof with the retained narrow Reader definer.

Revision ID: 20260924_52
Revises: 20260924_51

The first projection was created by the migration owner. Its call to the
existing private F6 publication helper needs the established context-definer
role. Keep revision 51 immutable and correct only this new function owner.
"""

from sqlalchemy import text

from alembic import op

revision = "20260924_52"
down_revision = "20260924_51"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    owner, definition, definition_hash = bind.execute(
        text(
            "SELECT pg_get_userbyid(proowner),pg_get_functiondef(oid),"
            "md5(pg_get_functiondef(oid)) FROM pg_proc WHERE oid="
            "'app.school_fact_f7_residence_exact_coverage(uuid,text,text,timestamptz)'"
            "::regprocedure"
        )
    ).one()
    if owner != "campus_ai_db_owner" or definition_hash != "18c913da3310c2bc1e5ae1c4e29a6c47":
        raise RuntimeError("F7 exact Reader function differs from reviewed revision 51")
    had_create = bind.execute(
        text("SELECT has_schema_privilege('campus_ai_context_definer','app','CREATE')")
    ).scalar_one()
    if not had_create:
        op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    # PostgreSQL cannot transfer this function directly through the migration
    # role's memberships. Re-create the exact reviewed catalog definition in
    # one migration transaction under the retained narrow definer instead.
    op.execute(
        "DROP FUNCTION app.school_fact_f7_residence_exact_coverage("
        "uuid,text,text,timestamptz)"
    )
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(definition)
    op.execute(
        "REVOKE ALL ON FUNCTION app.school_fact_f7_residence_exact_coverage("
        "uuid,text,text,timestamptz) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.school_fact_f7_residence_exact_coverage("
        "uuid,text,text,timestamptz) TO campus_ai_school_fact_reader"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    if not had_create:
        op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")


def downgrade() -> None:
    raise RuntimeError("Retain F7 Reader authority; use a reviewed forward repair")
