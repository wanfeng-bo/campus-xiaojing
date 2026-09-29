"""Compose teaching cancellation with proven independent interval changes."""

from alembic import op

revision = "20260914_41"
down_revision = "20260914_40"
branch_labels = None
depends_on = None

CANCELLATION_SQL = r"""
CREATE OR REPLACE FUNCTION app_private.f5_cancellation_prior(
 p_school uuid,p_root uuid,p_revision bigint,p_start timestamptz,p_end timestamptz,
 p_withdrawn bigint[],p_withdrawals bigint[],p_observed bigint)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $prior$
DECLARE result jsonb; unproven boolean;
BEGIN
WITH RECURSIVE history AS MATERIALIZED (
 SELECT l.target_version AS revision,l.operation,
 (u.payload->>'effective_at')::timestamptz AS effective_at
 FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_units u ON u.school_id=l.school_id AND u.id=l.unit_id
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id
 AND b.status='applied'
 WHERE l.school_id=p_school AND l.target_id=p_root AND l.target_version<=p_observed
), points AS (
 SELECT valid_from AS point FROM public.school_education_versions
 WHERE school_id=p_school AND fact_id=p_root AND revision<=p_observed
 UNION SELECT valid_to FROM public.school_education_versions
 WHERE school_id=p_school AND fact_id=p_root AND revision<=p_observed AND valid_to IS NOT NULL
 UNION SELECT p_start UNION SELECT p_end WHERE p_end IS NOT NULL
), spans AS MATERIALIZED (
 SELECT point,lead(point) OVER(ORDER BY point) AS endpoint FROM points
), decisions AS MATERIALIZED (
 -- Coordinates and the actual revision establish identity. Payload equality is
 -- checked only after selecting an identity, never used to search for an ancestor.
 SELECT h.revision,s.point AS valid_from,s.endpoint AS valid_to,
 v.valid_from AS origin_start,v.payload
 FROM history h JOIN public.school_education_versions v
 ON v.school_id=p_school AND v.fact_id=p_root AND v.revision=h.revision
 JOIN spans s ON v.valid_from<=s.point AND (v.valid_to IS NULL OR s.point<v.valid_to)
 LEFT JOIN public.school_education_versions old ON old.school_id=p_school
 AND old.fact_id=p_root AND old.revision=h.revision-1 AND old.valid_from<=s.point
 AND (old.valid_to IS NULL OR s.point<old.valid_to)
 WHERE (h.operation='upsert' AND v.valid_from=h.effective_at)
 OR (h.operation='correct' AND NOT h.revision=ANY(p_withdrawals)
 AND (v.valid_from=h.effective_at OR v.payload IS DISTINCT FROM old.payload))
), ancestors AS (
 SELECT greatest(s.point,p_start) AS valid_from,least(s.endpoint,p_end) AS valid_to,
 v.payload,v.revision
 FROM public.school_education_versions v
 JOIN spans s ON v.valid_from<=s.point AND (v.valid_to IS NULL OR s.point<v.valid_to)
 WHERE v.school_id=p_school AND v.fact_id=p_root AND v.revision=p_revision
 AND (s.endpoint IS NULL OR s.endpoint>p_start) AND (p_end IS NULL OR s.point<p_end)
 UNION ALL
 SELECT a.valid_from,a.valid_to,v.payload,v.revision FROM ancestors a
 CROSS JOIN LATERAL (SELECT i.revision,i.payload FROM decisions i
 WHERE i.revision<=a.revision AND i.valid_from<=a.valid_from
 AND (i.valid_to IS NULL OR a.valid_from<i.valid_to)
 AND NOT EXISTS (SELECT 1 FROM unnest(p_withdrawn,p_withdrawals) w(intro,rev)
 WHERE w.intro=i.revision AND w.rev<=a.revision)
 ORDER BY i.revision DESC LIMIT 1) source
 JOIN public.school_education_versions v ON v.school_id=p_school AND v.fact_id=p_root
 AND v.revision=source.revision-1
 AND v.valid_from<=a.valid_from AND (v.valid_to IS NULL OR a.valid_from<v.valid_to)
 WHERE source.revision=ANY(p_withdrawn) AND source.payload=a.payload
), resolved AS MATERIALIZED (
 SELECT a.*,source.revision AS origin,source.origin_start,source.payload AS original_payload
 FROM ancestors a LEFT JOIN LATERAL (SELECT i.* FROM decisions i
 WHERE i.revision<=a.revision AND i.valid_from<=a.valid_from
 AND (i.valid_to IS NULL OR a.valid_from<i.valid_to)
 AND NOT EXISTS (SELECT 1 FROM unnest(p_withdrawn,p_withdrawals) w(intro,rev)
 WHERE w.intro=i.revision AND w.rev<=a.revision)
 ORDER BY i.revision DESC LIMIT 1) source ON true
), surviving AS MATERIALIZED (
 SELECT * FROM resolved WHERE NOT origin=ANY(p_withdrawn)
), uncertain_ends AS (
 -- A later independent edit reaching an ancestor's visible tail cannot prove
 -- that the old open tail may return beyond the intervening arrangement. The
 -- stored complete correction has no end-vs-gap intent; require full correction.
 SELECT 1 FROM history h JOIN public.school_education_versions old
 ON old.school_id=p_school AND old.fact_id=p_root AND old.revision=h.revision-1
 JOIN spans s ON old.valid_from<=s.point AND old.valid_to=s.endpoint
 LEFT JOIN public.school_education_versions new ON new.school_id=p_school
 AND new.fact_id=p_root AND new.revision=h.revision AND new.valid_from<=s.point
 AND (new.valid_to IS NULL OR s.point<new.valid_to)
 CROSS JOIN LATERAL (SELECT i.revision FROM decisions i
 WHERE i.revision<h.revision AND i.valid_from<=s.point
 AND (i.valid_to IS NULL OR s.point<i.valid_to)
 AND NOT EXISTS (SELECT 1 FROM unnest(p_withdrawn,p_withdrawals) w(intro,rev)
 WHERE w.intro=i.revision AND w.rev<h.revision)
 ORDER BY i.revision DESC LIMIT 1) source
 WHERE h.revision>p_revision AND h.operation<>'upsert'
 AND NOT h.revision=ANY(p_withdrawals) AND old.valid_to<=p_start
 AND (new.payload IS DISTINCT FROM old.payload OR new.valid_from=h.effective_at)
 AND EXISTS (SELECT 1 FROM surviving a WHERE a.origin=source.revision)
)
SELECT COALESCE(jsonb_agg(jsonb_build_object('effective_at',valid_from,'valid_to',valid_to,
 'origin_start',origin_start,'content',payload) ORDER BY valid_from),'[]'::jsonb),
 EXISTS(SELECT 1 FROM resolved WHERE origin IS NULL OR payload IS DISTINCT FROM original_payload)
 OR EXISTS(SELECT 1 FROM uncertain_ends) INTO result,unproven
FROM surviving;
IF unproven THEN RAISE EXCEPTION
 'prior arrangement independently changed or lacks interval proof; complete correction required'
 USING ERRCODE='22023'; END IF;
RETURN result;
END;
$prior$;
REVOKE ALL ON FUNCTION
 app_private.f5_cancellation_prior(uuid,uuid,bigint,timestamptz,timestamptz,bigint[],bigint[],bigint)
 FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_reader,campus_ai_school_fact_writer,
 campus_ai_migration,campus_ai_db_owner;

CREATE OR REPLACE FUNCTION app.school_fact_f5_cancellation_proof(
 p_school uuid,p_kind text,p_source text,p_fields text[],p_start timestamptz,p_version bigint)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE root_id uuid; introduction bigint; publication uuid; current_payload jsonb;
 old_payload jsonb; prior jsonb; scope_end timestamptz; original_end timestamptz;
 history jsonb; withdrawn bigint[]; withdrawals bigint[]; cancellation_segments jsonb;
 candidate record; candidate_prior jsonb; candidate_end timestamptz;
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
 IF root_id IS NULL THEN RAISE EXCEPTION 'teaching cancellation version changed' USING
 ERRCODE='40001'; END IF;
 SELECT v.payload INTO current_payload
 FROM public.school_education_versions v JOIN public.master_data_import_batches b
 ON b.school_id=v.school_id AND b.id=v.import_batch_id AND b.status='applied'
 WHERE v.school_id=p_school AND v.fact_id=root_id AND v.superseded_at IS NULL AND
 v.valid_from=p_start;
 IF current_payload IS NULL THEN RAISE EXCEPTION 'selected future slice absent' USING
 ERRCODE='22023'; END IF;
 SELECT jsonb_agg(jsonb_build_object('version',l.target_version,'operation',l.operation,
 'batch_id',l.batch_id,'change',u.payload) ORDER BY l.target_version) INTO history
 FROM public.school_fact_change_lineage l
 JOIN public.master_data_import_batches b ON b.school_id=l.school_id AND b.id=l.batch_id AND
 b.status='applied'
 JOIN public.master_data_import_units u ON u.school_id=l.school_id AND u.id=l.unit_id
 WHERE l.school_id=p_school AND l.target_id=root_id AND l.fact_kind=p_kind AND
 l.fact_source_key=p_source;
 SELECT (h->>'version')::bigint,(h->>'batch_id')::uuid INTO introduction,publication
 FROM jsonb_array_elements(history) h WHERE h->>'operation'='upsert'
 AND (h->'change'->>'effective_at')::timestamptz=p_start
 ORDER BY (h->>'version')::bigint DESC LIMIT 1;
 IF introduction IS NULL THEN RAISE EXCEPTION 'selected slice has no original arrangement proof'
 USING ERRCODE='22023'; END IF;
 SELECT payload,valid_to INTO old_payload,original_end FROM public.school_education_versions
 WHERE school_id=p_school AND fact_id=root_id AND revision=introduction AND valid_from=p_start;

 -- A self-cause pointing to an earlier upsert records withdrawal of that precise
 -- introduction. Later independent corrections have no such causal declaration.
 withdrawn:='{}'::bigint[]; withdrawals:='{}'::bigint[];
 FOR candidate IN
 SELECT (i->>'version')::bigint AS introduction,(c->>'version')::bigint AS revision,
 (i->'change'->>'effective_at')::timestamptz AS start
 FROM jsonb_array_elements(history) c JOIN jsonb_array_elements(history) i
 ON i->>'operation'='upsert' AND (i->>'version')::bigint<(c->>'version')::bigint
 AND (i->'change'->>'effective_at')::timestamptz=(c->'change'->>'effective_at')::timestamptz
 WHERE c->>'operation'='correct' AND (
 (c->'change'->>'cause_source_key'=p_source
 AND (c->'change'->>'cause_version')::bigint=(i->>'version')::bigint)
 OR (NOT (c->'change' ? 'cause_source_key') AND (c->>'version')::bigint=(
  SELECT min(v.revision) FROM public.school_education_versions v
  WHERE v.school_id=p_school AND v.fact_id=root_id AND v.cause_source_key=p_source
  AND v.cause_version=(i->>'version')::bigint)))
 ORDER BY (c->>'version')::bigint
 LOOP
  SELECT least(v.valid_to,(SELECT min((h->'change'->>'effective_at')::timestamptz)
   FROM jsonb_array_elements(history) h WHERE h->>'operation'='upsert'
   AND (h->>'version')::bigint>candidate.introduction
   AND (h->>'version')::bigint<candidate.revision
   AND NOT (h->>'version')::bigint=ANY(withdrawn)
   AND (h->'change'->>'effective_at')::timestamptz>candidate.start)) INTO candidate_end
  FROM public.school_education_versions v WHERE v.school_id=p_school AND v.fact_id=root_id
  AND v.revision=candidate.introduction AND v.valid_from=candidate.start;
  candidate_prior:=app_private.f5_cancellation_prior(p_school,root_id,
   candidate.introduction-1,candidate.start,candidate_end,withdrawn,withdrawals,
   candidate.revision-1);
  -- A claimed withdrawal is evidence only when its actual changed intervals are
  -- exactly restorations of the proven prior, never a third independent value.
  IF EXISTS (
   WITH points AS (
    SELECT v.valid_from AS point FROM public.school_education_versions v
    WHERE v.school_id=p_school AND v.fact_id=root_id
    AND v.revision IN(candidate.revision-1,candidate.revision)
    UNION SELECT v.valid_to FROM public.school_education_versions v
    WHERE v.school_id=p_school AND v.fact_id=root_id
    AND v.revision IN(candidate.revision-1,candidate.revision) AND v.valid_to IS NOT NULL
    UNION SELECT (p->>'effective_at')::timestamptz FROM jsonb_array_elements(candidate_prior) p
    UNION SELECT (p->>'valid_to')::timestamptz FROM jsonb_array_elements(candidate_prior) p
    WHERE p->>'valid_to' IS NOT NULL
   )
   SELECT 1 FROM points s
   LEFT JOIN LATERAL (SELECT v.payload FROM public.school_education_versions v
    WHERE v.school_id=p_school AND v.fact_id=root_id AND v.revision=candidate.revision-1
    AND v.valid_from<=s.point AND (v.valid_to IS NULL OR s.point<v.valid_to)) old ON true
   LEFT JOIN LATERAL (SELECT v.payload FROM public.school_education_versions v
    WHERE v.school_id=p_school AND v.fact_id=root_id AND v.revision=candidate.revision
    AND v.valid_from<=s.point AND (v.valid_to IS NULL OR s.point<v.valid_to)) new ON true
   LEFT JOIN LATERAL (SELECT p->'content' AS payload
    FROM jsonb_array_elements(candidate_prior) p WHERE
    (p->>'effective_at')::timestamptz<=s.point AND
    (p->>'valid_to' IS NULL OR s.point<(p->>'valid_to')::timestamptz)) expected ON true
   WHERE (old.payload IS DISTINCT FROM new.payload AND (
    s.point<candidate.start OR (candidate_end IS NOT NULL AND s.point>=candidate_end)
    OR old.payload IS DISTINCT FROM (SELECT v.payload FROM public.school_education_versions v
     WHERE v.school_id=p_school AND v.fact_id=root_id AND v.revision=candidate.introduction
     AND v.valid_from=candidate.start)))
   OR (s.point>=candidate.start AND (candidate_end IS NULL OR s.point<candidate_end)
    AND old.payload=(SELECT v.payload FROM public.school_education_versions v
     WHERE v.school_id=p_school AND v.fact_id=root_id AND v.revision=candidate.introduction
     AND v.valid_from=candidate.start)
    AND new.payload IS DISTINCT FROM expected.payload)
  ) THEN RAISE EXCEPTION 'invalid teaching cancellation cause' USING ERRCODE='22023'; END IF;
  withdrawn:=array_append(withdrawn,candidate.introduction);
  withdrawals:=array_append(withdrawals,candidate.revision);
 END LOOP;
 IF introduction=ANY(withdrawn) OR old_payload IS DISTINCT FROM current_payload THEN
 RAISE EXCEPTION 'selected arrangement independently changed; review required' USING
 ERRCODE='22023'; END IF;
 SELECT least(original_end,min((h->'change'->>'effective_at')::timestamptz)) INTO scope_end
 FROM jsonb_array_elements(history) h WHERE h->>'operation'='upsert'
 AND (h->>'version')::bigint>introduction AND NOT (h->>'version')::bigint=ANY(withdrawn)
 AND (h->'change'->>'effective_at')::timestamptz>p_start;

 -- Compare actual before/after intervals, not root operation names. Independent
 -- deletions remain gaps. Any later replacement inside this introduction is a
 -- conflict, including a change that was subsequently changed back to the same value.
 IF EXISTS (
 WITH later AS (
  SELECT (h->>'version')::bigint AS rev,
  (h->'change'->>'effective_at')::timestamptz AS effective_at
  FROM jsonb_array_elements(history) h
  WHERE (h->>'version')::bigint>introduction AND h->>'operation'<>'upsert'
  AND NOT (h->>'version')::bigint=ANY(withdrawals)
 ), points AS (
  SELECT l.rev,v.valid_from AS point FROM later l JOIN public.school_education_versions v
  ON v.school_id=p_school AND v.fact_id=root_id AND v.revision IN(l.rev,l.rev-1)
  UNION SELECT l.rev,v.valid_to FROM later l JOIN public.school_education_versions v
  ON v.school_id=p_school AND v.fact_id=root_id AND v.revision IN(l.rev,l.rev-1)
  WHERE v.valid_to IS NOT NULL
 ), spans AS (SELECT rev,point,lead(point) OVER(PARTITION BY rev ORDER BY point) AS endpoint
 FROM points)
 SELECT 1 FROM spans s
 LEFT JOIN LATERAL (SELECT v.payload FROM public.school_education_versions v
 WHERE v.school_id=p_school AND v.fact_id=root_id AND v.revision=s.rev-1
 AND v.valid_from<=s.point AND (v.valid_to IS NULL OR s.point<v.valid_to)) old ON true
 LEFT JOIN LATERAL (SELECT v.payload,v.valid_from FROM public.school_education_versions v
 WHERE v.school_id=p_school AND v.fact_id=root_id AND v.revision=s.rev
 AND v.valid_from<=s.point AND (v.valid_to IS NULL OR s.point<v.valid_to)) new ON true
 WHERE (scope_end IS NULL OR s.point<scope_end) AND (s.endpoint IS NULL OR s.endpoint>p_start)
 AND new.payload IS NOT NULL AND (new.payload IS DISTINCT FROM old.payload
 OR new.valid_from=(SELECT l.effective_at FROM later l WHERE l.rev=s.rev))
 ) OR EXISTS (
 SELECT 1 FROM public.school_education_versions v
 WHERE v.school_id=p_school AND v.fact_id=root_id AND v.superseded_at IS NULL
 AND v.valid_from>=p_start AND (scope_end IS NULL OR v.valid_from<scope_end)
 AND (v.payload IS DISTINCT FROM old_payload OR
 (scope_end IS NOT NULL AND (v.valid_to IS NULL OR v.valid_to>scope_end)))
 ) THEN RAISE EXCEPTION 'selected arrangement independently changed; review required' USING
 ERRCODE='22023'; END IF;
 SELECT jsonb_agg(jsonb_build_object('effective_at',v.valid_from,'valid_to',v.valid_to)
 ORDER BY v.valid_from) INTO cancellation_segments FROM public.school_education_versions v
 WHERE v.school_id=p_school AND v.fact_id=root_id AND v.superseded_at IS NULL
 AND v.valid_from>=p_start AND (scope_end IS NULL OR v.valid_from<scope_end);
 SELECT COALESCE(jsonb_agg(p||jsonb_build_object('content',
  (SELECT COALESCE(jsonb_object_agg(e.key,e.value),'{}') FROM jsonb_each(p->'content') e
   WHERE e.key=ANY(p_fields)))),'[]') INTO prior FROM jsonb_array_elements(
  app_private.f5_cancellation_prior(p_school,root_id,introduction-1,p_start,scope_end,
   withdrawn,withdrawals,p_version)) p;
 RETURN
 jsonb_build_object('school_fact_version',app.school_fact_current_version(p_school),'records',
 jsonb_build_array(jsonb_build_object('fact_kind',p_kind,'source_key',p_source,'version',p_version,
 'selected_start',p_start,'introduction_version',introduction,'next_valid_boundary',p_start,
 'publication_reference','publication:'||publication::text,'prior_segments',prior,
 'cancellation_segments',cancellation_segments,
 'introduced_content',(SELECT COALESCE(jsonb_object_agg(e.key,e.value),'{}') FROM
 jsonb_each(old_payload) e WHERE e.key=ANY(p_fields)))));
END;
$f$;
REVOKE ALL ON FUNCTION
 app.school_fact_f5_cancellation_proof(uuid,text,text,text[],timestamptz,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
 app.school_fact_f5_cancellation_proof(uuid,text,text,text[],timestamptz,bigint)
 TO campus_ai_school_fact_reader;"""


def upgrade():
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("GRANT CREATE ON SCHEMA app,app_private TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(CANCELLATION_SQL)
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app,app_private FROM campus_ai_context_definer")


def downgrade():
    raise RuntimeError("Teaching cancellation intervals require a preserving forward repair")
