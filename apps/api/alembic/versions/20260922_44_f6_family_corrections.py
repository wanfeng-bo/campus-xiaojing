"""Own-child residence reports and exact applied-field evidence; no fact writes."""

from alembic import op

revision = "20260922_44"
down_revision = "20260922_43"
branch_labels = None
depends_on = None

SQL = r"""
CREATE FUNCTION app.school_fact_family_residence_targets(p_school uuid,p_adult uuid,p_relation
 uuid,p_version bigint)
RETURNS TABLE(field_name text,label text,fact_kind text,fact_source_key text,target_version
 bigint,student_id uuid)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE child uuid;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_writer' OR p_school IS DISTINCT FROM
 app.require_school_database_anchor()
 OR NOT app_private.family_relationship_is_authoritative(p_school,p_adult,p_relation,p_version,
 statement_timestamp()) THEN
 RAISE EXCEPTION 'residence correction denied' USING ERRCODE='28000'; END IF;
 SELECT r.student_id INTO child FROM public.guardian_relationships r WHERE
 r.school_id=p_school AND r.id=p_relation
 AND r.guardian_person_id=p_adult AND r.relationship_kind='verified_guardian';
 IF child IS NULL THEN RETURN; END IF;
 RETURN QUERY WITH RECURSIVE own AS MATERIALIZED (
 SELECT r.*,l.fact_source_key,l.target_version FROM public.student_bed_residences r
 JOIN LATERAL (SELECT x.fact_source_key,x.target_version FROM public.school_fact_change_lineage x
 JOIN public.master_data_import_batches b ON b.school_id=x.school_id AND b.id=x.batch_id AND
 b.status='applied'
 WHERE x.school_id=r.school_id AND x.fact_kind='student_bed_residence' AND x.target_id=r.id
 ORDER BY x.target_version DESC LIMIT 1) l ON true
 WHERE r.school_id=p_school AND r.student_id=child AND r.revoked_at IS NULL
 AND r.valid_from<=statement_timestamp() AND (r.valid_to IS NULL OR
 statement_timestamp()<r.valid_to)
 ), beds AS MATERIALIZED (
 SELECT DISTINCT d.* FROM own o JOIN public.dorm_beds d ON d.school_id=p_school AND d.id=o.bed_id
 ), ancestors(id,path) AS (
 SELECT b.room_location_id,ARRAY[b.room_location_id] FROM beds b
 UNION ALL SELECT v.parent_location_id,a.path||v.parent_location_id FROM ancestors a
 JOIN public.campus_location_versions v ON v.school_id=p_school AND v.location_id=a.id
 WHERE v.valid_from<=statement_timestamp() AND (v.valid_to IS NULL OR
 statement_timestamp()<v.valid_to)
 AND v.parent_location_id IS NOT NULL AND NOT v.parent_location_id=ANY(a.path) AND
 cardinality(a.path)<64
 ), targets(kind,source,version,title,fields) AS (
 SELECT 'student_bed_residence',o.fact_source_key,o.target_version,'孩子住宿安排',
 ARRAY['bed_source_key','effective_at','valid_to'] FROM own o
 UNION ALL SELECT 'dorm_bed',b.source_key,v.version,'孩子床位',
 ARRAY['display_name','room_source_key']
 FROM beds b JOIN public.dorm_bed_versions v ON v.school_id=p_school AND v.bed_id=b.id AND
 v.superseded_at IS NULL
 WHERE v.valid_from<=statement_timestamp() AND (v.valid_to IS NULL OR
 statement_timestamp()<v.valid_to)
 UNION ALL SELECT 'dorm_room',COALESCE(v.source_key,l.fact_source_key),v.version,
 '孩子住宿房间',ARRAY['room_source_key']
 FROM public.dorm_room_profile_versions v LEFT JOIN LATERAL (
 SELECT x.fact_source_key FROM public.school_fact_change_lineage x
 JOIN public.master_data_import_batches b ON b.school_id=x.school_id AND b.id=x.batch_id
 AND b.status='applied' WHERE x.school_id=v.school_id AND x.fact_kind='dorm_room'
 AND x.target_id=v.id AND x.target_version=v.version ORDER BY x.created_at DESC LIMIT 1
 ) l ON true WHERE v.school_id=p_school AND COALESCE(v.source_key,l.fact_source_key) IS NOT NULL
 AND v.room_location_id IN (SELECT b.room_location_id FROM beds b) AND v.superseded_at IS NULL
 AND v.valid_from<=statement_timestamp() AND (v.valid_to IS NULL OR
 statement_timestamp()<v.valid_to)
 UNION ALL SELECT 'dormitory_building_profile',v.source_key,v.version,'孩子宿舍楼或住宿区域',
 ARRAY['display_name','location_source_key']
 FROM public.dormitory_building_profile_versions v WHERE v.school_id=p_school AND
 v.location_id IN (SELECT a.id FROM ancestors a)
 AND v.superseded_at IS NULL AND v.valid_from<=statement_timestamp() AND (v.valid_to IS NULL
 OR statement_timestamp()<v.valid_to)
 ) SELECT DISTINCT f.name,t.title,t.kind,t.source::text,t.version,child FROM targets t CROSS JOIN
 LATERAL unnest(t.fields) f(name);
END; $f$;
REVOKE ALL ON FUNCTION app.school_fact_family_residence_targets(uuid,uuid,uuid,bigint) FROM
 PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_family_residence_targets(uuid,uuid,uuid,bigint) TO
 campus_ai_school_fact_writer;

CREATE FUNCTION app.school_fact_f6_field_changed(p_school uuid,p_kind text,p_source text,
 p_version bigint,p_field text)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE old_version bigint; before_value jsonb; after_value jsonb; rev bigint; value jsonb;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_writer' OR p_school IS DISTINCT FROM
 app.require_school_database_anchor() THEN
 RAISE EXCEPTION 'residence difference denied' USING ERRCODE='28000'; END IF;
 IF p_version<2 OR NOT COALESCE((p_kind='student_bed_residence' AND p_field IN
 ('bed_source_key','effective_at','valid_to'))
 OR (p_kind='dorm_bed' AND p_field IN ('display_name','room_source_key')) OR
 (p_kind='dorm_room' AND p_field='room_source_key')
 OR (p_kind='dormitory_building_profile' AND p_field IN ('display_name',
 'location_source_key')),false) THEN RETURN false; END IF;
 IF (SELECT count(*) FROM public.school_fact_change_lineage l JOIN
 public.master_data_import_batches b
 ON b.school_id=l.school_id AND b.id=l.batch_id AND b.status='applied' WHERE
 l.school_id=p_school AND l.fact_kind=p_kind
 AND l.fact_source_key=p_source AND l.target_version=p_version)<>1 THEN RETURN false; END IF;
 SELECT max(l.target_version) INTO old_version FROM public.school_fact_change_lineage l JOIN
 public.master_data_import_batches b
 ON b.school_id=l.school_id AND b.id=l.batch_id AND b.status='applied' WHERE
 l.school_id=p_school AND l.fact_kind=p_kind
 AND l.fact_source_key=p_source AND l.target_version<p_version;
 IF old_version IS NULL THEN RETURN false; END IF;
 FOREACH rev IN ARRAY ARRAY[old_version,p_version] LOOP
  IF p_kind='student_bed_residence' THEN
   -- Reconstruct each introduced row from immutable applied units. End/revoke mutate
   -- physical rows, so comparing their current values would not prove a past change.
   WITH events AS MATERIALIZED (
    SELECT l.target_id,l.target_version,l.operation,u.payload FROM
 public.school_fact_change_lineage l
    JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
 AND b.status='applied'
    JOIN public.master_data_import_units u ON u.school_id=l.school_id AND u.id=l.unit_id
    WHERE l.school_id=p_school AND l.fact_kind=p_kind AND l.fact_source_key=p_source AND
 l.target_version<=rev
   ), slices AS (
    SELECT e.payload->>'effective_at' AS start,
     COALESCE(terminal.payload->>'effective_at',e.payload->'content'->>'valid_to') AS finish,
     CASE p_field WHEN 'bed_source_key' THEN e.payload->'content'->'bed_source_key'
       WHEN 'effective_at' THEN e.payload->'effective_at'
       ELSE COALESCE(terminal.payload->'effective_at',e.payload->'content'->'valid_to',
 'null'::jsonb) END AS field_value
    FROM events e LEFT JOIN LATERAL (SELECT t.* FROM events t WHERE t.target_id=e.target_id
    AND t.target_version>e.target_version AND t.operation IN ('end','revoke') ORDER BY
 t.target_version DESC LIMIT 1) terminal ON true
    WHERE e.operation='upsert' AND terminal.operation IS DISTINCT FROM 'revoke'
   ) SELECT jsonb_agg(jsonb_build_array(start,finish,field_value) ORDER BY start,finish,
 field_value) INTO value FROM slices;
  ELSIF p_kind='dorm_bed' THEN
   SELECT jsonb_agg(jsonb_build_array(v.valid_from,v.valid_to,CASE p_field WHEN 'display_name'
 THEN to_jsonb(v.display_name) ELSE to_jsonb(b.room_location_id) END)
     ORDER BY v.valid_from,v.valid_to) INTO value FROM public.dorm_beds b JOIN
 public.dorm_bed_versions v ON v.school_id=b.school_id AND v.bed_id=b.id
     WHERE b.school_id=p_school AND b.source_key=p_source AND v.version=rev;
  ELSIF p_kind='dorm_room' THEN
   SELECT jsonb_agg(jsonb_build_array(v.valid_from,v.valid_to,v.room_location_id) ORDER BY
 v.valid_from,v.valid_to) INTO value
    FROM public.dorm_room_profile_versions v WHERE v.school_id=p_school AND
 v.source_key=p_source AND v.version=rev;
  ELSE
   SELECT jsonb_agg(jsonb_build_array(v.valid_from,v.valid_to,CASE p_field WHEN 'display_name'
 THEN to_jsonb(v.display_name) ELSE to_jsonb(v.location_id) END)
    ORDER BY v.valid_from,v.valid_to) INTO value FROM
 public.dormitory_building_profile_versions v WHERE v.school_id=p_school AND
 v.source_key=p_source AND v.version=rev;
  END IF;
  IF rev=old_version THEN before_value:=value; ELSE after_value:=value; END IF;
 END LOOP;
 RETURN before_value IS NOT NULL AND before_value IS DISTINCT FROM after_value;
END; $f$;
REVOKE ALL ON FUNCTION app.school_fact_f6_field_changed(uuid,text,text,bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f6_field_changed(uuid,text,text,bigint,text) TO
 campus_ai_school_fact_writer;
"""


def upgrade():
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(SQL)
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")
    op.execute("""ALTER TABLE public.family_fact_correction_reports
      ADD CONSTRAINT ck_f6_correction_field
      CHECK (fact_kind NOT IN
        ('student_bed_residence','dorm_bed','dorm_room','dormitory_building_profile')
      OR (fact_kind='student_bed_residence'
          AND field_name IN ('bed_source_key','effective_at','valid_to'))
      OR (fact_kind='dorm_bed' AND field_name IN ('display_name','room_source_key'))
      OR (fact_kind='dorm_room' AND field_name='room_source_key')
      OR (fact_kind='dormitory_building_profile'
          AND field_name IN ('display_name','location_source_key')))""")


def downgrade():
    raise RuntimeError("Residence correction evidence requires a preserving forward repair")
