"""Exact teaching cancellation evidence from the selected formal publication only."""
from alembic import op

revision = '20260914_39'
down_revision = '20260914_38'
branch_labels = None
depends_on = None

CANCELLATION_SQL = r"""
CREATE FUNCTION app.school_fact_f5_cancellation_proof(
 p_school uuid,p_kind text,p_source text,p_fields text[],p_start timestamptz,p_version bigint)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE root_id uuid; introduction bigint; publication uuid; current_payload jsonb;
 old_payload jsonb; prior jsonb; next_end timestamptz;
BEGIN
 IF SESSION_USER<>'campus_ai_school_fact_reader'
 OR p_school IS DISTINCT FROM app.require_school_database_anchor() THEN
 RAISE EXCEPTION 'teaching cancellation Reader denied' USING ERRCODE='28000'; END IF;
 IF p_source IS NULL OR length(p_source) NOT BETWEEN 3 AND 255 OR btrim(p_source)<>p_source
 OR p_start IS NULL OR p_start<=statement_timestamp() OR p_version IS NULL OR p_version<1
 OR p_fields IS NULL OR cardinality(p_fields)=0 OR array_position(p_fields,NULL) IS NOT NULL
 OR app_private.f5_allowed_fields(p_kind) IS NULL
 OR NOT p_fields <@ app_private.f5_allowed_fields(p_kind) THEN
 RAISE EXCEPTION 'invalid exact teaching cancellation coordinates' USING ERRCODE='22023'; END IF;
 SELECT id INTO root_id FROM public.school_education_facts
 WHERE school_id=p_school AND kind=p_kind AND source_key=p_source AND version=p_version;
 IF root_id IS NULL THEN RAISE EXCEPTION 'teaching cancellation version changed' USING ERRCODE='40001'; END IF;
 SELECT v.payload,v.valid_to INTO current_payload,next_end
 FROM public.school_education_versions v JOIN public.master_data_import_batches b
 ON b.school_id=v.school_id AND b.id=v.import_batch_id AND b.status='applied'
 WHERE v.school_id=p_school AND v.fact_id=root_id AND v.superseded_at IS NULL AND v.valid_from=p_start;
 IF current_payload IS NULL THEN RAISE EXCEPTION 'selected future slice absent' USING ERRCODE='22023'; END IF;
 SELECT l.target_version,l.batch_id INTO introduction,publication
 FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id AND b.status='applied'
 JOIN public.master_data_import_units u ON u.school_id=l.school_id AND u.id=l.unit_id
 WHERE l.school_id=p_school AND l.target_id=root_id AND l.fact_kind=p_kind AND l.fact_source_key=p_source
 AND l.operation='upsert' AND (u.payload->>'effective_at')::timestamptz=p_start
 ORDER BY l.target_version DESC LIMIT 1;
 IF introduction IS NULL THEN RAISE EXCEPTION 'selected slice has no original arrangement proof' USING ERRCODE='22023'; END IF;
 SELECT payload INTO old_payload FROM public.school_education_versions
 WHERE school_id=p_school AND fact_id=root_id AND revision=introduction AND valid_from=p_start;
 IF old_payload IS DISTINCT FROM current_payload OR EXISTS (
 SELECT 1 FROM public.school_fact_change_lineage l JOIN public.master_data_import_batches b
 ON b.school_id=l.school_id AND b.id=l.batch_id AND b.status='applied'
 WHERE l.school_id=p_school AND l.target_id=root_id AND l.fact_kind=p_kind
 AND l.target_version>introduction AND l.operation<>'upsert') THEN
 RAISE EXCEPTION 'selected arrangement independently changed; review required' USING ERRCODE='22023'; END IF;
 SELECT COALESCE(jsonb_agg(jsonb_build_object('effective_at',v.valid_from,'valid_to',v.valid_to,
 'content',(SELECT COALESCE(jsonb_object_agg(e.key,e.value),'{}') FROM jsonb_each(v.payload) e WHERE e.key=ANY(p_fields)))
 ORDER BY v.valid_from),'[]') INTO prior FROM public.school_education_versions v
 JOIN public.master_data_import_batches b ON b.school_id=v.school_id AND b.id=v.import_batch_id AND b.status='applied'
 WHERE v.school_id=p_school AND v.fact_id=root_id AND v.revision=introduction-1
 AND (v.valid_to IS NULL OR v.valid_to>p_start) AND (next_end IS NULL OR v.valid_from<next_end);
 RETURN jsonb_build_object('school_fact_version',app.school_fact_current_version(p_school),'records',
 jsonb_build_array(jsonb_build_object('fact_kind',p_kind,'source_key',p_source,'version',p_version,
 'selected_start',p_start,'introduction_version',introduction,'next_valid_boundary',p_start,
 'publication_reference','publication:'||publication::text,'prior_segments',prior,
 'introduced_content',(SELECT COALESCE(jsonb_object_agg(e.key,e.value),'{}') FROM jsonb_each(old_payload) e WHERE e.key=ANY(p_fields)))));
END;
$f$;
REVOKE ALL ON FUNCTION app.school_fact_f5_cancellation_proof(uuid,text,text,text[],timestamptz,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.school_fact_f5_cancellation_proof(uuid,text,text,text[],timestamptz,bigint)
 TO campus_ai_school_fact_reader;
"""


def upgrade():
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(CANCELLATION_SQL)
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")


def downgrade():
    raise RuntimeError("Teaching cancellation evidence requires a preserving forward repair")
