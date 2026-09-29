"""Separate policy record revisions from official business applicability dates."""

from alembic import op

revision = "20260906_25"
down_revision = "20260906_24"
branch_labels = None
depends_on = None

# Frozen forward migration: derive record coordinates only from formal lineage.
# Unlineaged legacy rows retain their previously materialized interval.
_BACKFILL = r"""
WITH formal_records AS (
    SELECT f.school_id, f.id, l.fact_source_key, l.target_version,
           (u.payload ->> 'effective_at')::timestamptz AS record_from
    FROM public.school_policy_versions f
    JOIN public.school_fact_change_lineage l
      ON l.school_id=f.school_id AND l.target_id=f.id
     AND l.fact_kind='policy_version' AND l.operation='upsert'
    JOIN public.master_data_import_units u ON u.school_id=l.school_id AND u.id=l.unit_id
), periods AS (
    SELECT r.*, (
        SELECT min((u.payload ->> 'effective_at')::timestamptz)
        FROM public.school_fact_change_lineage l
        JOIN public.master_data_import_units u ON u.school_id=l.school_id AND u.id=l.unit_id
        WHERE l.school_id=r.school_id AND l.fact_kind='policy_version'
          AND l.fact_source_key=r.fact_source_key
          AND (u.payload ->> 'effective_at')::timestamptz > r.record_from
          AND (l.operation='upsert' OR
               (l.operation IN ('end','revoke') AND l.target_version>r.target_version))
    ) AS record_to
    FROM formal_records r
)
UPDATE public.school_policy_versions AS f
SET valid_from=p.record_from, valid_to=LEAST(p.record_to, f.revoked_at)
FROM periods p WHERE f.school_id=p.school_id AND f.id=p.id;
"""

_APPLICABILITY_RANGE = (
    "tstzrange(GREATEST(valid_from, effective_from), "
    "GREATEST(GREATEST(valid_from, effective_from), "
    "COALESCE(LEAST(valid_to, effective_to, revoked_at), "
    "'infinity'::timestamptz)), '[)')"
)


_AMBIGUOUS_ORIGIN = r"""
SELECT f.id FROM public.school_policy_versions f
WHERE EXISTS (
    SELECT 1 FROM public.school_fact_change_lineage any_l
    WHERE any_l.school_id=f.school_id AND any_l.target_id=f.id
      AND any_l.fact_kind='policy_version'
) AND (
    SELECT count(*) FROM public.school_fact_change_lineage l
    JOIN public.master_data_import_units u ON u.school_id=l.school_id AND u.id=l.unit_id
    WHERE l.school_id=f.school_id AND l.target_id=f.id
      AND l.fact_kind='policy_version' AND l.operation='upsert'
      AND NULLIF(u.payload ->> 'effective_at', '') IS NOT NULL
) <> 1
"""


def upgrade() -> None:
    # Terminal lineage may advance a physical row's version. Match its unique
    # original upsert by target identity, not by the mutable current version.
    op.execute(
        "DO $origin_guard$ BEGIN IF EXISTS (" + _AMBIGUOUS_ORIGIN + ") THEN "
        "RAISE EXCEPTION 'schema25 policy record origin is ambiguous' USING ERRCODE='55000'; "
        "END IF; END $origin_guard$"
    )
    # The transaction rolls back if contradictory history violates non-overlap.
    op.execute(
        "ALTER TABLE public.school_policy_versions "
        "DROP CONSTRAINT excl_school_policy_versions_period"
    )
    op.execute(
        "ALTER TABLE public.school_policy_versions "
        "ALTER COLUMN valid_from DROP EXPRESSION, ALTER COLUMN valid_to DROP EXPRESSION"
    )
    op.execute(_BACKFILL)
    op.execute(
        "ALTER TABLE public.school_policy_versions ADD CONSTRAINT "
        "ck_school_policy_versions_business_interval "
        "CHECK (effective_to IS NULL OR effective_to > effective_from)"
    )
    op.execute(
        "ALTER TABLE public.school_policy_versions ADD CONSTRAINT "
        "excl_school_policy_versions_period EXCLUDE USING gist "
        "(school_id WITH =, policy_id WITH =, (" + _APPLICABILITY_RANGE + ") WITH &&)"
    )
    # The schema24 Writer has table-level UPDATE, verified on the clone.
    # All current Writer updates now touch only the two terminal coordinates.
    op.execute(
        "REVOKE UPDATE ON TABLE public.school_policy_versions FROM campus_ai_school_fact_writer"
    )
    op.execute(
        "REVOKE UPDATE (effective_to) ON TABLE public.school_policy_versions "
        "FROM campus_ai_school_fact_writer"
    )
    op.execute(
        "GRANT UPDATE (valid_to, revoked_at) ON TABLE public.school_policy_versions "
        "TO campus_ai_school_fact_writer"
    )


def downgrade() -> None:
    raise RuntimeError(
        "Restore the approved schema24 backup; policy record history is not lossy-downgradable."
    )
