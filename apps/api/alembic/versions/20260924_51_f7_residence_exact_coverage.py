"""F7 narrow exact F6 coverage proof projection; old F6 target Reader stays unchanged.

Revision ID: 20260924_51
Revises: 20260924_50
"""

from alembic import op

revision = "20260924_51"
down_revision = "20260924_50"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION app.school_fact_f7_residence_exact_coverage(
            p_school uuid, p_kind text, p_target text, p_at timestamptz
        ) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path=pg_catalog AS $f$
        DECLARE result jsonb; count_rows integer;
        BEGIN
            IF SESSION_USER<>'campus_ai_school_fact_reader'
                OR p_school IS DISTINCT FROM app.require_school_database_anchor()
                OR p_kind NOT IN ('dormitory_building_profile','dorm_room','dorm_bed',
                                  'student_bed_residence')
                OR p_target IS NULL OR length(p_target)=0 OR length(p_target)>255
                OR p_at IS NULL
            THEN
                RAISE EXCEPTION 'F7 exact residence coverage denied' USING ERRCODE='28000';
            END IF;
            SELECT count(*), COALESCE(jsonb_agg(jsonb_build_object(
                'quality_source_key', selected.source_key,
                'quality_version', selected.version,
                'target_fact_kind', selected.target_fact_kind,
                'target_source_key', selected.target_source_key,
                'field_name', selected.field_name,
                'owner_kind', selected.owner_kind,
                'owner_source_key', selected.owner_source_key,
                'valid_from', selected.valid_from,
                'valid_to', selected.valid_to,
                'coverage_basis', selected.coverage_basis,
                'source', app_private.f6_publication(
                    p_school,'fact_quality',selected.id,selected.version
                )
            ) ORDER BY selected.source_key),'[]'::jsonb)
            INTO count_rows,result
            FROM (
                SELECT q.id,q.source_key,q.version,q.target_fact_kind,
                       q.target_source_key,q.field_name,q.owner_kind,
                       CASE q.owner_kind
                         WHEN 'person' THEN (
                           SELECT p.source_key FROM public.school_people p
                           WHERE p.school_id=q.school_id AND p.id=q.person_id
                         )
                         WHEN 'location' THEN (
                           SELECT l.source_key FROM public.campus_locations l
                           WHERE l.school_id=q.school_id AND l.id=q.location_id
                         )
                         ELSE NULL
                       END AS owner_source_key,
                       q.valid_from,q.valid_to,q.coverage_basis
                FROM public.school_fact_qualities q
                JOIN public.master_data_import_batches b
                  ON b.school_id=q.school_id AND b.id=q.import_batch_id
                 AND b.status='applied'
                WHERE q.school_id=p_school AND q.target_fact_kind=p_kind
                  AND q.target_source_key=p_target
                  AND q.quality_kind='coverage' AND q.coverage_status='complete'
                  AND q.coverage_basis IS NOT NULL
                  AND q.valid_from<=p_at AND (q.valid_to IS NULL OR p_at<q.valid_to)
                  AND (q.revoked_at IS NULL OR p_at<q.revoked_at)
                  AND q.verified_at<=statement_timestamp()
                ORDER BY q.source_key LIMIT 101
            ) selected;
            IF count_rows>100 THEN
                RAISE EXCEPTION 'F7 exact quality set is unbounded' USING ERRCODE='22023';
            END IF;
            RETURN result;
        END; $f$;
        REVOKE ALL ON FUNCTION app.school_fact_f7_residence_exact_coverage(
            uuid,text,text,timestamptz) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION app.school_fact_f7_residence_exact_coverage(
            uuid,text,text,timestamptz) TO campus_ai_school_fact_reader;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS app.school_fact_f7_residence_exact_coverage("
        "uuid,text,text,timestamptz)"
    )
