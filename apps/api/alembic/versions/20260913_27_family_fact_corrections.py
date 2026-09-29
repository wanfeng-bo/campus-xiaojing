# ruff: noqa: E501
"""Non-authoritative family fact correction intake owned by the isolated Writer."""

import sqlalchemy as sa

from alembic import op

revision = "20260913_27"
down_revision = "20260913_26"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "school_fact_change_lineage",
        sa.Column("changed_fields", sa.ARRAY(sa.String(64)), nullable=False, server_default="{}"),
    )
    op.create_table(
        "family_fact_correction_reports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("reporter_person_id", sa.Uuid(), nullable=False),
        sa.Column("guardian_relationship_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("fact_kind", sa.String(64), nullable=False),
        sa.Column("fact_source_key", sa.String(255), nullable=False),
        sa.Column("field_name", sa.String(64), nullable=False),
        sa.Column("fact_target_version", sa.BigInteger(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("idempotency_key_sha256", sa.String(64), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="submitted"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolution_locator_sha256", sa.String(64)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('submitted','resolved')", name="ck_family_correction_status"
        ),
        sa.CheckConstraint(
            "char_length(statement) BETWEEN 1 AND 1000", name="ck_family_correction_statement"
        ),
        sa.CheckConstraint(
            "idempotency_key_sha256 ~ '^[0-9a-f]{64}$' AND request_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_family_correction_hashes",
        ),
        sa.CheckConstraint(
            "(status='submitted' AND resolution_locator_sha256 IS NULL AND resolved_at IS NULL) OR (status='resolved' AND resolution_locator_sha256 ~ '^[0-9a-f]{64}$' AND resolved_at IS NOT NULL)",
            name="ck_family_correction_resolution",
        ),
        sa.UniqueConstraint(
            "school_id",
            "reporter_person_id",
            "idempotency_key_sha256",
            name="uq_family_correction_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "reporter_person_id"],
            ["school_people.school_id", "school_people.id"],
            name="fk_family_correction_reporter",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "guardian_relationship_id"],
            ["guardian_relationships.school_id", "guardian_relationships.id"],
            name="fk_family_correction_relationship",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "student_id"],
            ["students.school_id", "students.id"],
            name="fk_family_correction_student",
        ),
    )
    op.execute("""
GRANT CREATE ON SCHEMA app TO campus_ai_context_definer;
SET LOCAL ROLE campus_ai_context_definer;
CREATE FUNCTION app.school_fact_family_correction_targets(
 p_school uuid,p_adult uuid,p_relation uuid,p_version bigint)
RETURNS TABLE(field_name text,label text,fact_kind text,fact_source_key text,
 target_version bigint,student_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
WITH basis AS (
 SELECT r.school_id,r.guardian_person_id AS adult_person_id,r.id AS relationship_id,
   r.version AS relationship_version,r.student_id,s.version AS student_version,
   r.relationship_kind::text AS relationship_kind,r.valid_from,r.valid_to
 FROM public.guardian_relationships r
 JOIN public.students s ON s.school_id=r.school_id AND s.id=r.student_id
 WHERE app_private.family_relationship_is_authoritative(
   p_school,p_adult,p_relation,p_version,statement_timestamp())
   AND r.school_id=p_school AND r.id=p_relation AND r.guardian_person_id=p_adult
), raw_targets(field_name,label,fact_kind,fact_source_key,target_version,student_id) AS (
 SELECT 'relationship'::text,'孩子关联'::text,'guardian_relationship'::text,
   l.fact_source_key,l.target_version,b.student_id
 FROM basis b JOIN public.school_fact_change_lineage l
   ON l.school_id=b.school_id AND l.fact_kind='guardian_relationship'
  AND l.target_id=b.relationship_id AND l.target_version=b.relationship_version
  AND l.operation IN ('upsert','reactivate')
 JOIN public.master_data_import_batches mb ON mb.school_id=l.school_id
  AND mb.id=l.batch_id AND mb.status='applied'
 UNION ALL
 SELECT 'name','姓名','person',l.fact_source_key,l.target_version,b.student_id
 FROM basis b JOIN public.students s ON s.school_id=b.school_id AND s.id=b.student_id
 JOIN public.school_person_names n ON n.school_id=b.school_id AND n.person_id=s.person_id
   AND n.status='confirmed' AND n.valid_from<=statement_timestamp()
   AND (n.valid_to IS NULL OR statement_timestamp()<n.valid_to)
 JOIN public.school_fact_change_lineage l ON l.school_id=b.school_id AND l.fact_kind='person'
   AND l.target_id=s.person_id AND l.target_version=n.version
   AND l.operation IN ('upsert','reactivate')
 JOIN public.master_data_import_batches mbn ON mbn.school_id=l.school_id
   AND mbn.id=l.batch_id AND mbn.status='applied'
 UNION ALL
 SELECT f.field_name,f.label,'student_school_relationship',l.fact_source_key,l.target_version,b.student_id
 FROM basis b JOIN public.student_school_relationships e ON e.school_id=b.school_id
   AND e.student_id=b.student_id AND e.valid_from<=statement_timestamp()
   AND (e.valid_to IS NULL OR statement_timestamp()<e.valid_to)
 JOIN public.school_fact_change_lineage l ON l.school_id=b.school_id
   AND l.fact_kind='student_school_relationship' AND l.target_id=e.id AND l.target_version=e.version
   AND l.operation IN ('upsert','reactivate')
 JOIN public.master_data_import_batches mbe ON mbe.school_id=l.school_id
   AND mbe.id=l.batch_id AND mbe.status='applied'
 CROSS JOIN (VALUES ('enrollment_status','在校状态'),('campus','校区'),
   ('student_number','学号'),('residence_mode','住宿方式')) f(field_name,label)
 WHERE b.relationship_kind='verified_guardian'
    OR f.field_name IN ('enrollment_status','campus')
 UNION ALL
 SELECT 'region','地区','person_region',l.fact_source_key,l.target_version,b.student_id
 FROM basis b JOIN public.students s ON s.school_id=b.school_id AND s.id=b.student_id
 JOIN public.person_regions r ON r.school_id=b.school_id AND r.person_id=s.person_id
   AND r.region_purpose='student_profile' AND r.valid_from<=statement_timestamp()
   AND (r.valid_to IS NULL OR statement_timestamp()<r.valid_to)
   AND (r.revoked_at IS NULL OR statement_timestamp()<r.revoked_at)
 JOIN public.school_fact_change_lineage l ON l.school_id=b.school_id
   AND l.fact_kind='person_region' AND l.target_id=r.id AND l.target_version=r.version
   AND l.operation IN ('upsert','reactivate')
 JOIN public.master_data_import_batches mbr ON mbr.school_id=l.school_id
   AND mbr.id=l.batch_id AND mbr.status='applied'
 WHERE b.relationship_kind='verified_guardian'
)
SELECT r.field_name,r.label,r.fact_kind,r.fact_source_key,r.target_version,r.student_id
FROM raw_targets r
WHERE p_school=app.require_school_database_anchor()
  AND SESSION_USER='campus_ai_school_fact_writer'
  AND (SELECT count(*) FROM raw_targets c WHERE c.field_name=r.field_name)=1;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_family_correction_targets(uuid,uuid,uuid,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_family_correction_targets(uuid,uuid,uuid,bigint)
 TO campus_ai_school_fact_writer;
SET LOCAL ROLE campus_ai_db_owner;
REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer;
CREATE FUNCTION app.school_fact_f3_field_changed(
 p_school uuid,p_kind text,p_source text,p_version bigint,p_field text)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE new_id uuid; old_id uuid; answer boolean := false;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_writer'
    OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
  RAISE EXCEPTION 'correction difference denied' USING ERRCODE='28000';
 END IF;
 SELECT target_id INTO new_id FROM public.school_fact_change_lineage
 WHERE school_id=p_school AND fact_kind=p_kind AND fact_source_key=p_source
   AND target_version=p_version AND operation='upsert';
 SELECT target_id INTO old_id FROM public.school_fact_change_lineage
 WHERE school_id=p_school AND fact_kind=p_kind AND fact_source_key=p_source
   AND target_version<p_version AND operation IN ('upsert','reactivate')
 ORDER BY target_version DESC LIMIT 1;
 IF new_id IS NULL OR old_id IS NULL THEN RETURN false; END IF;
 IF p_kind='student_school_relationship' THEN
  SELECT CASE p_field WHEN 'enrollment_status' THEN n.enrollment_status IS DISTINCT FROM o.enrollment_status
   WHEN 'student_number' THEN n.student_number IS DISTINCT FROM o.student_number
   WHEN 'residence_mode' THEN n.residence_mode IS DISTINCT FROM o.residence_mode
   WHEN 'campus' THEN n.primary_campus_location_id IS DISTINCT FROM o.primary_campus_location_id
   ELSE false END INTO answer FROM public.student_school_relationships n
   JOIN public.student_school_relationships o ON o.school_id=n.school_id
   WHERE n.school_id=p_school AND n.id=new_id AND o.id=old_id;
 ELSIF p_kind='person_region' AND p_field='region' THEN
  SELECT (n.district_code,n.district_name) IS DISTINCT FROM (o.district_code,o.district_name)
  INTO answer FROM public.person_regions n JOIN public.person_regions o ON o.school_id=n.school_id
  WHERE n.school_id=p_school AND n.id=new_id AND o.id=old_id;
 ELSIF p_kind='person' AND p_field='name' THEN
  SELECT n.display_name IS DISTINCT FROM o.display_name INTO answer
  FROM public.school_person_names n JOIN public.school_person_names o ON o.school_id=n.school_id
  WHERE n.school_id=p_school AND n.person_id=new_id AND o.person_id=old_id
    AND n.version=p_version AND o.version<n.version ORDER BY o.version DESC LIMIT 1;
 END IF;
 RETURN COALESCE(answer,false);
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_f3_field_changed(uuid,text,text,bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f3_field_changed(uuid,text,text,bigint,text)
 TO campus_ai_school_fact_writer;
""")
    op.execute("ALTER TABLE family_fact_correction_reports ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE family_fact_correction_reports FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY family_correction_writer ON family_fact_correction_reports FOR ALL TO campus_ai_school_fact_writer USING (school_id=app.require_school_database_anchor()) WITH CHECK (school_id=app.require_school_database_anchor())"
    )
    op.execute(
        "REVOKE ALL ON family_fact_correction_reports FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_reader,campus_ai_migration,campus_ai_db_owner"
    )
    op.execute(
        "GRANT SELECT,INSERT,UPDATE ON family_fact_correction_reports TO campus_ai_school_fact_writer"
    )


def downgrade():
    raise RuntimeError("F3 correction intake downgrade requires a data-preserving plan")

