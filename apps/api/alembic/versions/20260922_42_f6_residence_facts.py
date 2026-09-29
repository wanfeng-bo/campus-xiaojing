"""Formal residence facilities and continuous interval constraints."""

from alembic import op

revision = "20260922_42"
down_revision = "20260914_41"
branch_labels = None
depends_on = None

SCHEMA_SQL = r"""
ALTER TABLE public.student_school_relationships ALTER COLUMN residence_mode DROP NOT NULL;
ALTER TABLE public.dorm_room_profile_versions ADD COLUMN source_key varchar(255);
ALTER TABLE public.dorm_room_profile_versions ADD COLUMN superseded_at timestamptz;
ALTER TABLE public.dorm_bed_versions ADD COLUMN superseded_at timestamptz;
-- Only an unambiguous recorded identity may be backfilled. Legacy rows remain readable.
UPDATE public.dorm_room_profile_versions p SET source_key=l.source_key
FROM (SELECT school_id,target_id,min(fact_source_key) source_key
      FROM public.school_fact_change_lineage WHERE fact_kind='dorm_room'
      GROUP BY school_id,target_id HAVING count(DISTINCT fact_source_key)=1) l
WHERE p.school_id=l.school_id AND p.id=l.target_id;
ALTER TABLE public.dorm_room_profile_versions
 DROP CONSTRAINT excl_dorm_room_profile_versions_period;
ALTER TABLE public.dorm_room_profile_versions ADD CONSTRAINT excl_dorm_room_profile_versions_period
EXCLUDE USING gist (school_id WITH =,room_location_id WITH =,
 tstzrange(valid_from,coalesce(valid_to,'infinity'::timestamptz),'[)') WITH &&)
WHERE (superseded_at IS NULL);
ALTER TABLE public.dorm_bed_versions DROP CONSTRAINT excl_dorm_bed_versions_period;
ALTER TABLE public.dorm_bed_versions ADD CONSTRAINT excl_dorm_bed_versions_period
EXCLUDE USING gist (school_id WITH =,bed_id WITH =,
 tstzrange(valid_from,coalesce(valid_to,'infinity'::timestamptz),'[)') WITH &&)
WHERE (superseded_at IS NULL);
CREATE TABLE public.dormitory_building_profile_versions (
 id uuid PRIMARY KEY,school_id uuid NOT NULL,source_key varchar(255) NOT NULL,
 location_id uuid NOT NULL,usage_kind varchar(40) NOT NULL,
 display_name varchar(1000) NOT NULL,description varchar(1000),is_available boolean NOT NULL,
 valid_from timestamptz NOT NULL,valid_to timestamptz,superseded_at timestamptz,
 version bigint NOT NULL DEFAULT 1,source_id uuid NOT NULL,import_batch_id uuid,
 created_at timestamptz NOT NULL DEFAULT now(),
 CONSTRAINT fk_dormitory_profiles_location FOREIGN KEY(school_id,location_id)
 REFERENCES public.campus_locations(school_id,id) ON DELETE RESTRICT,
 CONSTRAINT fk_dormitory_profiles_source FOREIGN KEY(school_id,source_id)
 REFERENCES public.school_fact_sources(school_id,id) ON DELETE RESTRICT,
 CONSTRAINT fk_dormitory_profiles_batch FOREIGN KEY(school_id,import_batch_id)
 REFERENCES public.master_data_import_batches(school_id,id) ON DELETE RESTRICT,
 CONSTRAINT ck_dormitory_profiles_usage CHECK(usage_kind IN ('building','residential_area')),
 CONSTRAINT ck_dormitory_profiles_interval CHECK(valid_to IS NULL OR valid_to>valid_from),
 CONSTRAINT ck_dormitory_profiles_version CHECK(version>=1),
 CONSTRAINT excl_dormitory_profiles_period EXCLUDE USING gist
 (school_id WITH =,location_id WITH =,
 tstzrange(valid_from,coalesce(valid_to,'infinity'::timestamptz),'[)') WITH &&)
 WHERE(superseded_at IS NULL)
);
CREATE INDEX ix_dormitory_profiles_source ON public.dormitory_building_profile_versions
 (school_id,source_key,version);
ALTER TABLE public.dormitory_building_profile_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.dormitory_building_profile_versions FORCE ROW LEVEL SECURITY;
REVOKE ALL ON public.dormitory_building_profile_versions
 FROM PUBLIC,campus_ai_runtime,campus_ai_school_fact_reader;
GRANT SELECT,INSERT,UPDATE ON public.dormitory_building_profile_versions
 TO campus_ai_school_fact_writer;
GRANT SELECT ON public.dormitory_building_profile_versions TO campus_ai_context_definer;
CREATE POLICY f6_writer ON public.dormitory_building_profile_versions FOR ALL
 TO campus_ai_school_fact_writer USING(school_id=app.require_school_context())
 WITH CHECK(school_id=app.require_school_context());
CREATE POLICY f6_owner ON public.dormitory_building_profile_versions FOR ALL
 TO campus_ai_db_owner,campus_ai_migration USING(true) WITH CHECK(true);
CREATE POLICY f6_context_read ON public.dormitory_building_profile_versions FOR SELECT
 TO campus_ai_context_definer USING(school_id=app.require_school_database_anchor());
CREATE POLICY f6_anchor ON public.dormitory_building_profile_versions AS RESTRICTIVE FOR ALL
 TO PUBLIC USING(school_id=app.require_school_database_anchor())
 WITH CHECK(school_id=app.require_school_database_anchor());
CREATE TRIGGER trg_f6_profile_no_delete BEFORE DELETE ON public.dormitory_building_profile_versions
 FOR EACH ROW EXECUTE FUNCTION public.sf1_reject_history_delete();
CREATE TRIGGER trg_f6_profile_source_key BEFORE UPDATE OF source_key
 ON public.dormitory_building_profile_versions
 FOR EACH ROW EXECUTE FUNCTION public.sf1_reject_source_key_update();
CREATE TRIGGER trg_f6_profile_location_lock BEFORE INSERT OR UPDATE
 ON public.dormitory_building_profile_versions
 FOR EACH ROW EXECUTE FUNCTION public.sf18_lock_location_residence_change();
"""

GUARDS_SQL = r"""
-- Immediate triggers retain identity and row locks. Eligibility is checked against
-- the final transaction state, allowing complete timeline replacement atomically.
CREATE OR REPLACE FUNCTION public.sf1_validate_room_profile() RETURNS trigger
 LANGUAGE plpgsql SET search_path=pg_catalog AS $f$
BEGIN
 IF TG_OP='UPDATE' AND (NEW.school_id IS DISTINCT FROM OLD.school_id
 OR NEW.room_location_id IS DISTINCT FROM OLD.room_location_id
 OR (OLD.source_key IS NOT NULL AND NEW.source_key IS DISTINCT FROM OLD.source_key)) THEN
  RAISE EXCEPTION 'dorm room stable identity is immutable' USING ERRCODE='55000';
 END IF;
 PERFORM 1 FROM public.campus_locations
 WHERE school_id=NEW.school_id AND id=NEW.room_location_id FOR UPDATE;
 RETURN NEW;
END $f$;
CREATE OR REPLACE FUNCTION public.sf1_validate_bed_version() RETURNS trigger
 LANGUAGE plpgsql SET search_path=pg_catalog AS $f$
DECLARE room uuid;
BEGIN
 IF TG_OP='UPDATE' AND (NEW.school_id IS DISTINCT FROM OLD.school_id
 OR NEW.bed_id IS DISTINCT FROM OLD.bed_id) THEN
  RAISE EXCEPTION 'dorm bed version stable identity is immutable' USING ERRCODE='55000';
 END IF;
 SELECT room_location_id INTO room FROM public.dorm_beds
 WHERE school_id=NEW.school_id AND id=NEW.bed_id;
 PERFORM 1 FROM public.campus_locations WHERE school_id=NEW.school_id AND id=room FOR UPDATE;
 PERFORM 1 FROM public.dorm_beds WHERE school_id=NEW.school_id AND id=NEW.bed_id FOR UPDATE;
 RETURN NEW;
END $f$;

CREATE FUNCTION public.f6_assert_room(checked_school_id uuid,checked_room_id uuid)
 RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $f$
DECLARE row record; coverage tstzmultirange;
BEGIN
 FOR row IN SELECT * FROM public.dorm_room_profile_versions
 WHERE school_id=checked_school_id AND room_location_id=checked_room_id
 AND superseded_at IS NULL LOOP
  SELECT range_agg(tstzrange(v.valid_from,coalesce(v.valid_to,'infinity'::timestamptz),'[)'))
  INTO coverage FROM public.campus_location_versions v
  WHERE v.school_id=row.school_id AND v.location_id=row.room_location_id
  AND v.kind='dorm_room' AND v.status='confirmed';
  IF NOT coalesce(coverage @> tstzrange(row.valid_from,
     coalesce(row.valid_to,'infinity'::timestamptz),'[)'),false) THEN
   RAISE EXCEPTION 'room profile requires a dorm room over its entire interval'
   USING ERRCODE='23514',CONSTRAINT='ck_dorm_room_profile_versions_room_kind';
  END IF;
 END LOOP;
 FOR row IN SELECT v.* FROM public.dorm_bed_versions v JOIN public.dorm_beds b
 ON b.school_id=v.school_id AND b.id=v.bed_id
 WHERE b.school_id=checked_school_id AND b.room_location_id=checked_room_id
 AND v.superseded_at IS NULL AND v.availability='available' LOOP
  SELECT range_agg(tstzrange(p.valid_from,coalesce(p.valid_to,'infinity'::timestamptz),'[)'))
  INTO coverage FROM public.dorm_room_profile_versions p
  WHERE p.school_id=checked_school_id AND p.room_location_id=checked_room_id
  AND p.superseded_at IS NULL AND p.is_available;
  IF NOT coalesce(coverage @> tstzrange(row.valid_from,
     coalesce(row.valid_to,'infinity'::timestamptz),'[)'),false) THEN
   RAISE EXCEPTION 'available bed requires continuous available room coverage'
   USING ERRCODE='23514',CONSTRAINT='ck_dorm_bed_versions_room_available';
  END IF;
 END LOOP;
 -- Every capacity or availability increase begins at one of these boundaries.
 -- Adjacent, sequential bed versions therefore never count as simultaneous.
 IF EXISTS (
  WITH beds AS MATERIALIZED (
   SELECT v.* FROM public.dorm_bed_versions v JOIN public.dorm_beds b
   ON b.school_id=v.school_id AND b.id=v.bed_id
   WHERE b.school_id=checked_school_id AND b.room_location_id=checked_room_id
   AND v.superseded_at IS NULL AND v.availability='available'),
  profiles AS MATERIALIZED (
   SELECT * FROM public.dorm_room_profile_versions
   WHERE school_id=checked_school_id AND room_location_id=checked_room_id
   AND superseded_at IS NULL),
  points AS (SELECT valid_from AS point FROM beds UNION SELECT valid_from FROM profiles)
  SELECT 1 FROM points t JOIN profiles p
  ON p.valid_from<=t.point AND (p.valid_to IS NULL OR t.point<p.valid_to)
  WHERE (SELECT count(DISTINCT b.bed_id) FROM beds b WHERE b.valid_from<=t.point
  AND (b.valid_to IS NULL OR t.point<b.valid_to))>p.authorized_bed_capacity
 ) THEN
  RAISE EXCEPTION 'simultaneously available beds exceed authorized room capacity'
  USING ERRCODE='23514',CONSTRAINT='ck_dorm_bed_versions_room_capacity';
 END IF;
END $f$;

CREATE OR REPLACE FUNCTION public.sf1_assert_student_residences_valid(
 checked_school_id uuid,checked_student_id uuid) RETURNS void
 LANGUAGE plpgsql SET search_path=pg_catalog AS $f$
DECLARE row record; coverage tstzmultirange; required tstzrange; room uuid;
BEGIN
 FOR row IN SELECT * FROM public.student_bed_residences
 WHERE school_id=checked_school_id AND student_id=checked_student_id AND revoked_at IS NULL LOOP
  required:=tstzrange(row.valid_from,coalesce(row.valid_to,'infinity'::timestamptz),'[)');
  SELECT range_agg(tstzrange(r.valid_from,coalesce(r.valid_to,'infinity'::timestamptz),'[)'))
  INTO coverage FROM public.student_school_relationships r
  WHERE r.school_id=row.school_id AND r.student_id=row.student_id
  AND r.superseded_at IS NULL AND r.enrollment_status IN ('enrolled','suspended')
  AND r.residence_mode='boarding';
  IF NOT coalesce(coverage @> required,false) THEN
   RAISE EXCEPTION 'residence requires continuous formal boarding eligibility'
   USING ERRCODE='23514',CONSTRAINT='ck_student_bed_residences_enrolled_boarder';
  END IF;
  SELECT range_agg(tstzrange(v.valid_from,coalesce(v.valid_to,'infinity'::timestamptz),'[)'))
  INTO coverage FROM public.dorm_bed_versions v
  WHERE v.school_id=row.school_id AND v.bed_id=row.bed_id
  AND v.superseded_at IS NULL AND v.availability='available';
  IF NOT coalesce(coverage @> required,false) THEN
   RAISE EXCEPTION 'residence requires continuous available bed coverage'
   USING ERRCODE='23514',CONSTRAINT='ck_student_bed_residences_bed_available';
  END IF;
  SELECT room_location_id INTO room FROM public.dorm_beds
  WHERE school_id=row.school_id AND id=row.bed_id;
  SELECT range_agg(tstzrange(p.valid_from,coalesce(p.valid_to,'infinity'::timestamptz),'[)'))
  INTO coverage FROM public.dorm_room_profile_versions p
  WHERE p.school_id=row.school_id AND p.room_location_id=room
  AND p.superseded_at IS NULL AND p.is_available;
  IF NOT coalesce(coverage @> required,false) THEN
   RAISE EXCEPTION 'residence requires continuous available room coverage'
   USING ERRCODE='23514',CONSTRAINT='ck_student_bed_residences_room_available';
  END IF;
 END LOOP;
END $f$;
CREATE OR REPLACE FUNCTION public.sf1_validate_student_residence() RETURNS trigger
 LANGUAGE plpgsql SET search_path=pg_catalog AS $f$
BEGIN
 PERFORM public.sf1_assert_student_residences_valid(NEW.school_id,NEW.student_id);
 RETURN NULL;
END $f$;
CREATE OR REPLACE FUNCTION public.sf1_assert_bed_residences_valid(
 checked_school_id uuid,checked_bed_id uuid) RETURNS void
 LANGUAGE plpgsql SET search_path=pg_catalog AS $f$
DECLARE row record; room uuid;
BEGIN
 SELECT room_location_id INTO room FROM public.dorm_beds
 WHERE school_id=checked_school_id AND id=checked_bed_id;
 PERFORM public.f6_assert_room(checked_school_id,room);
 FOR row IN SELECT DISTINCT student_id FROM public.student_bed_residences
 WHERE school_id=checked_school_id AND bed_id=checked_bed_id AND revoked_at IS NULL LOOP
  PERFORM public.sf1_assert_student_residences_valid(checked_school_id,row.student_id);
 END LOOP;
END $f$;
CREATE OR REPLACE FUNCTION public.sf1_assert_room_residences_valid(
 checked_school_id uuid,checked_room_id uuid) RETURNS void
 LANGUAGE plpgsql SET search_path=pg_catalog AS $f$
DECLARE row record;
BEGIN
 PERFORM public.f6_assert_room(checked_school_id,checked_room_id);
 FOR row IN SELECT DISTINCT r.student_id FROM public.student_bed_residences r
 JOIN public.dorm_beds b ON b.school_id=r.school_id AND b.id=r.bed_id
 WHERE b.school_id=checked_school_id AND b.room_location_id=checked_room_id
 AND r.revoked_at IS NULL LOOP
  PERFORM public.sf1_assert_student_residences_valid(checked_school_id,row.student_id);
 END LOOP;
END $f$;

CREATE FUNCTION public.f6_profile_identity() RETURNS trigger
 LANGUAGE plpgsql SET search_path=pg_catalog AS $f$
BEGIN
 IF TG_OP='UPDATE' AND (NEW.school_id IS DISTINCT FROM OLD.school_id
 OR NEW.location_id IS DISTINCT FROM OLD.location_id) THEN
  RAISE EXCEPTION 'dormitory profile identity is immutable' USING ERRCODE='55000';
 END IF;
 PERFORM 1 FROM public.campus_locations
 WHERE school_id=NEW.school_id AND id=NEW.location_id FOR UPDATE;
 RETURN NEW;
END $f$;
CREATE TRIGGER trg_f6_profile_identity BEFORE INSERT OR UPDATE
 ON public.dormitory_building_profile_versions FOR EACH ROW
 EXECUTE FUNCTION public.f6_profile_identity();

CREATE FUNCTION public.f6_validate_facility_locations() RETURNS trigger
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $f$
DECLARE row record; coverage tstzmultirange;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended(
  'campus-ai:sf18:school-campus:'||NEW.school_id::text,0));
 FOR row IN SELECT * FROM public.dormitory_building_profile_versions
 WHERE school_id=NEW.school_id AND superseded_at IS NULL LOOP
  SELECT range_agg(tstzrange(v.valid_from,coalesce(v.valid_to,'infinity'::timestamptz),'[)'))
  INTO coverage FROM public.campus_location_versions v
  WHERE v.school_id=row.school_id AND v.location_id=row.location_id
  AND v.status='confirmed' AND
  ((row.usage_kind='building' AND v.kind='building') OR
   (row.usage_kind='residential_area' AND v.kind IN ('building','floor','zone','area')));
  IF NOT coalesce(coverage @> tstzrange(row.valid_from,
   coalesce(row.valid_to,'infinity'::timestamptz),'[)'),false) THEN
   RAISE EXCEPTION 'dormitory use requires compatible formal location history'
   USING ERRCODE='23514',CONSTRAINT='ck_dormitory_profiles_location_kind';
  END IF;
  IF row.is_available AND NOT app.school_fact_location_history_interval_is_resolvable(
   row.school_id,row.location_id,row.valid_from,row.valid_to) THEN
   RAISE EXCEPTION 'available dormitory use requires continuous active ancestors'
   USING ERRCODE='23514',CONSTRAINT='ck_dormitory_profiles_location_available';
  END IF;
 END LOOP;
 FOR row IN SELECT DISTINCT room_location_id FROM public.dorm_room_profile_versions
 WHERE school_id=NEW.school_id AND superseded_at IS NULL LOOP
  PERFORM public.f6_assert_room(NEW.school_id,row.room_location_id);
 END LOOP;
 RETURN NULL;
END $f$;
CREATE CONSTRAINT TRIGGER trg_f6_validate_profile_locations
 AFTER INSERT OR UPDATE ON public.dormitory_building_profile_versions
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION public.f6_validate_facility_locations();
CREATE CONSTRAINT TRIGGER trg_f6_validate_location_facilities
 AFTER INSERT OR UPDATE ON public.campus_location_versions
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION public.f6_validate_facility_locations();
CREATE CONSTRAINT TRIGGER trg_f6_validate_location_terminal_facilities
 AFTER UPDATE OF status ON public.campus_locations
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION public.f6_validate_facility_locations();
CREATE CONSTRAINT TRIGGER trg_f6_validate_lineage_facilities
 AFTER INSERT OR UPDATE ON public.school_fact_change_lineage
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 WHEN(NEW.fact_kind='location' AND NEW.operation IN ('end','revoke','correct'))
 EXECUTE FUNCTION public.f6_validate_facility_locations();
"""


QUALITY_SQL = r"""
ALTER TABLE public.school_fact_qualities DROP CONSTRAINT ck_school_fact_qualities_negative;
ALTER TABLE public.school_fact_qualities
 ADD CONSTRAINT ck_school_fact_qualities_negative
 CHECK (negative_code IS NULL
 OR ((negative_code = 'no_office'
 AND target_fact_kind IN ('organization_location',
'person_workplace')
 AND purpose = 'office')
 OR (negative_code = 'no_reception'
 AND target_fact_kind IN ('organization_location',
'person_workplace')
 AND purpose = 'reception')
 OR (negative_code = 'no_official_channel'
 AND target_fact_kind = 'official_contact'
 AND contact_kind IN ('phone',
'email'))
 OR (negative_code = 'no_service'
 AND target_fact_kind = 'school_service')
 OR (negative_code = 'vacant_position'
 AND target_fact_kind = 'student_class_position'
 AND education_position_id IS NOT NULL)
 OR (negative_code = 'no_class_teacher'
 AND target_fact_kind = 'class_teacher_assignment')
 OR (negative_code = 'no_fixed_location'
 AND target_fact_kind = 'class_location')
 OR (negative_code='no_teaching_assignment'
 AND target_fact_kind='teaching_assignment'
 AND education_target_id IS NOT NULL
 AND teaching_role IN ('lead',
'co_teacher')
 AND education_position_id IS NULL
 AND target_source_key IS NULL
 AND replacement_source_key IS NULL)
 OR (negative_code='no_residence'
 AND target_fact_kind='student_bed_residence'
 AND owner_kind='person'
 AND person_id IS NOT NULL
 AND purpose IS NULL)
 OR (negative_code='no_dormitory'
 AND target_fact_kind='dormitory_building_profile'
 AND owner_kind IN ('school',
'location')
 AND purpose IS NULL)) IS TRUE);
ALTER TABLE public.school_fact_qualities DROP CONSTRAINT ck_school_fact_qualities_target;
ALTER TABLE public.school_fact_qualities
 ADD CONSTRAINT ck_school_fact_qualities_target
 CHECK (target_fact_kind IN ('school',
'person',
'organization',
'assignment',
'organization_campus_relationship',
'location',
'location_alias',
'person_contact',
'official_contact',
'policy',
'policy_scope',
'policy_version',
'organization_profile',
'organization_membership',
'organization_location',
'person_workplace',
'school_service',
'class_lineage',
'class_location',
'class_placement',
'class_position_definition',
'class_teacher_assignment',
'course_subject',
'dorm_bed',
'dorm_room',
'dormitory_building_profile',
'education_calendar_rule',
'education_class',
'education_cohort',
'education_grade',
'education_grade_group',
'education_offering',
'education_stage',
'education_term',
'offering_member_source',
'student_bed_residence',
'student_class_membership',
'student_class_position',
'student_cohort_membership',
'student_grade_placement',
'teaching_assignment',
'teaching_course',
'teaching_group',
'teaching_group_member',
'teaching_offering',
'teaching_organization_scope',
'teaching_subject'));
"""


def upgrade():
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute(SCHEMA_SQL)
    op.execute(GUARDS_SQL)
    op.execute(QUALITY_SQL)
    for signature in (
        "public.f6_assert_room(uuid,uuid)",
        "public.f6_profile_identity()",
        "public.f6_validate_facility_locations()",
    ):
        op.execute(f"ALTER FUNCTION {signature} OWNER TO campus_ai_db_owner")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.f6_assert_room(uuid,uuid) TO campus_ai_school_fact_writer"
    )


def downgrade():
    raise RuntimeError("Residence history requires forward repair; destructive downgrade disabled.")
