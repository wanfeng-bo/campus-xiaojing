"""Add a distinct multi-source package provenance channel without changing old batches.

Revision ID: 20260924_47
Revises: 20260924_46
"""

from alembic import op

revision = "20260924_47"
down_revision = "20260924_46"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(
        "ALTER TABLE public.school_fact_sources "
        "DROP CONSTRAINT ck_school_fact_sources_kind"
    )
    op.execute(
        "ALTER TABLE public.school_fact_sources "
        "ADD CONSTRAINT ck_school_fact_sources_kind "
        "CHECK (source_kind IN ('manual','excel','system','legacy','package'))"
    )
    op.execute(
        "ALTER TABLE public.master_data_import_batches "
        "DROP CONSTRAINT ck_master_data_import_batches_source_kind"
    )
    op.execute(
        "ALTER TABLE public.master_data_import_batches "
        "ADD CONSTRAINT ck_master_data_import_batches_source_kind "
        "CHECK (source_kind IN ('manual','excel','package'))"
    )
    op.execute(
        "ALTER TABLE public.master_data_import_batches "
        "DROP CONSTRAINT ck_master_data_import_batches_source_metadata"
    )
    op.execute(
        "ALTER TABLE public.master_data_import_batches "
        "ADD CONSTRAINT ck_master_data_import_batches_source_metadata CHECK ("
        "(source_kind='manual' AND filename_summary IS NULL AND source_size_bytes IS NULL) "
        "OR (source_kind='excel' AND filename_summary IS NOT NULL "
        "AND source_size_bytes BETWEEN 1 AND 10485760) "
        "OR (source_kind='package' AND filename_summary='school-facts-package' "
        "AND source_size_bytes BETWEEN 1 AND 10485760))"
    )


def downgrade() -> None:
    raise RuntimeError("Retain F7 package source provenance; use a reviewed forward repair.")
