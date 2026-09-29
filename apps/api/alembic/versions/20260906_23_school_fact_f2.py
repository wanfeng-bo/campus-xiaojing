"""F2 temporal domain records and exact role privileges; preserves revision22 anchor."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260906_23"
down_revision = "20260905_22"
branch_labels = None
depends_on = None

_F2_DDL = r"""
CREATE TABLE organization_profiles (
        organization_unit_id UUID NOT NULL,
        organization_type VARCHAR(80) NOT NULL,
        function_summary VARCHAR(1000) NOT NULL,
        id UUID NOT NULL,
        school_id UUID NOT NULL,
        source_key VARCHAR(255) NOT NULL,
        version BIGINT NOT NULL,
        valid_from TIMESTAMP WITH TIME ZONE NOT NULL,
        valid_to TIMESTAMP WITH TIME ZONE,
        revoked_at TIMESTAMP WITH TIME ZONE,
        source_id UUID NOT NULL,
        import_batch_id UUID NOT NULL,
        basis_reference VARCHAR(1000) NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT fk_organization_profiles_school FOREIGN KEY(school_id) REFERENCES schools
(id) ON DELETE RESTRICT,
        CONSTRAINT fk_organization_profiles_source_id FOREIGN KEY(school_id, source_id)
REFERENCES school_fact_sources (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_organization_profiles_import_batch_id FOREIGN KEY(school_id,
import_batch_id) REFERENCES master_data_import_batches (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT uq_organization_profiles_school_id UNIQUE (school_id, id),
        CONSTRAINT uq_organization_profiles_source_version UNIQUE (school_id, source_key,
version),
        CONSTRAINT ck_organization_profiles_version CHECK (version >= 1),
        CONSTRAINT ck_organization_profiles_interval CHECK (valid_to IS NULL OR valid_to >
valid_from),
        CONSTRAINT excl_organization_profiles_source_period EXCLUDE USING gist (school_id WITH
=, source_key WITH =, tstzrange(valid_from, GREATEST(valid_from, COALESCE(LEAST(valid_to,
revoked_at), 'infinity'::timestamptz)), '[)') WITH &&) WHERE (true),
        CONSTRAINT fk_organization_profiles_organization_unit_id FOREIGN KEY(school_id,
organization_unit_id) REFERENCES organization_units (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT ck_organization_profiles_type CHECK (organization_type IN
('department','grade_group','teaching_research_group','lesson_preparation_group','committee','other')),
        CONSTRAINT excl_organization_profiles_organization_period EXCLUDE USING gist (school_id
WITH =, organization_unit_id WITH =, tstzrange(valid_from, GREATEST(valid_from,
COALESCE(LEAST(valid_to, revoked_at), 'infinity'::timestamptz)), '[)') WITH &&) WHERE (true)
);

CREATE TABLE organization_memberships (
        organization_unit_id UUID NOT NULL,
        person_id UUID NOT NULL,
        membership_kind VARCHAR(20) NOT NULL,
        id UUID NOT NULL,
        school_id UUID NOT NULL,
        source_key VARCHAR(255) NOT NULL,
        version BIGINT NOT NULL,
        valid_from TIMESTAMP WITH TIME ZONE NOT NULL,
        valid_to TIMESTAMP WITH TIME ZONE,
        revoked_at TIMESTAMP WITH TIME ZONE,
        source_id UUID NOT NULL,
        import_batch_id UUID NOT NULL,
        basis_reference VARCHAR(1000) NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT fk_organization_memberships_school FOREIGN KEY(school_id) REFERENCES schools
(id) ON DELETE RESTRICT,
        CONSTRAINT fk_organization_memberships_source_id FOREIGN KEY(school_id, source_id)
REFERENCES school_fact_sources (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_organization_memberships_import_batch_id FOREIGN KEY(school_id,
import_batch_id) REFERENCES master_data_import_batches (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT uq_organization_memberships_school_id UNIQUE (school_id, id),
        CONSTRAINT uq_organization_memberships_source_version UNIQUE (school_id, source_key,
version),
        CONSTRAINT ck_organization_memberships_version CHECK (version >= 1),
        CONSTRAINT ck_organization_memberships_interval CHECK (valid_to IS NULL OR valid_to >
valid_from),
        CONSTRAINT excl_organization_memberships_source_period EXCLUDE USING gist (school_id
WITH =, source_key WITH =, tstzrange(valid_from, GREATEST(valid_from, COALESCE(LEAST(valid_to,
revoked_at), 'infinity'::timestamptz)), '[)') WITH &&) WHERE (true),
        CONSTRAINT fk_organization_memberships_organization_unit_id FOREIGN KEY(school_id,
organization_unit_id) REFERENCES organization_units (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_organization_memberships_person_id FOREIGN KEY(school_id, person_id)
REFERENCES school_people (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT ck_organization_memberships_kind CHECK (membership_kind IN
('member','participant')),
        CONSTRAINT excl_organization_memberships_member_period EXCLUDE USING gist (school_id
WITH =, organization_unit_id WITH =, person_id WITH =, membership_kind WITH =,
tstzrange(valid_from, GREATEST(valid_from, COALESCE(LEAST(valid_to, revoked_at),
'infinity'::timestamptz)), '[)') WITH &&) WHERE (true)
);

CREATE TABLE organization_locations (
        organization_unit_id UUID NOT NULL,
        location_id UUID NOT NULL,
        purpose VARCHAR(20) NOT NULL,
        is_primary BOOLEAN NOT NULL,
        id UUID NOT NULL,
        school_id UUID NOT NULL,
        source_key VARCHAR(255) NOT NULL,
        version BIGINT NOT NULL,
        valid_from TIMESTAMP WITH TIME ZONE NOT NULL,
        valid_to TIMESTAMP WITH TIME ZONE,
        revoked_at TIMESTAMP WITH TIME ZONE,
        source_id UUID NOT NULL,
        import_batch_id UUID NOT NULL,
        basis_reference VARCHAR(1000) NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT fk_organization_locations_school FOREIGN KEY(school_id) REFERENCES schools
(id) ON DELETE RESTRICT,
        CONSTRAINT fk_organization_locations_source_id FOREIGN KEY(school_id, source_id)
REFERENCES school_fact_sources (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_organization_locations_import_batch_id FOREIGN KEY(school_id,
import_batch_id) REFERENCES master_data_import_batches (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT uq_organization_locations_school_id UNIQUE (school_id, id),
        CONSTRAINT uq_organization_locations_source_version UNIQUE (school_id, source_key,
version),
        CONSTRAINT ck_organization_locations_version CHECK (version >= 1),
        CONSTRAINT ck_organization_locations_interval CHECK (valid_to IS NULL OR valid_to >
valid_from),
        CONSTRAINT excl_organization_locations_source_period EXCLUDE USING gist (school_id WITH
=, source_key WITH =, tstzrange(valid_from, GREATEST(valid_from, COALESCE(LEAST(valid_to,
revoked_at), 'infinity'::timestamptz)), '[)') WITH &&) WHERE (true),
        CONSTRAINT fk_organization_locations_organization_unit_id FOREIGN KEY(school_id,
organization_unit_id) REFERENCES organization_units (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_organization_locations_location_id FOREIGN KEY(school_id, location_id)
REFERENCES campus_locations (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT ck_organization_locations_purpose CHECK (purpose IN ('office','reception')),
        CONSTRAINT excl_organization_locations_relation_period EXCLUDE USING gist (school_id
WITH =, organization_unit_id WITH =, location_id WITH =, purpose WITH =, tstzrange(valid_from,
GREATEST(valid_from, COALESCE(LEAST(valid_to, revoked_at), 'infinity'::timestamptz)), '[)') WITH
&&) WHERE (true),
        CONSTRAINT excl_organization_locations_primary_period EXCLUDE USING gist (school_id WITH
=, organization_unit_id WITH =, purpose WITH =, tstzrange(valid_from, GREATEST(valid_from,
COALESCE(LEAST(valid_to, revoked_at), 'infinity'::timestamptz)), '[)') WITH &&) WHERE
(is_primary)
);

CREATE TABLE person_workplaces (
        person_id UUID,
        assignment_id UUID,
        location_id UUID NOT NULL,
        purpose VARCHAR(20) NOT NULL,
        is_primary BOOLEAN NOT NULL,
        id UUID NOT NULL,
        school_id UUID NOT NULL,
        source_key VARCHAR(255) NOT NULL,
        version BIGINT NOT NULL,
        valid_from TIMESTAMP WITH TIME ZONE NOT NULL,
        valid_to TIMESTAMP WITH TIME ZONE,
        revoked_at TIMESTAMP WITH TIME ZONE,
        source_id UUID NOT NULL,
        import_batch_id UUID NOT NULL,
        basis_reference VARCHAR(1000) NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT fk_person_workplaces_school FOREIGN KEY(school_id) REFERENCES schools (id) ON
DELETE RESTRICT,
        CONSTRAINT fk_person_workplaces_source_id FOREIGN KEY(school_id, source_id) REFERENCES
school_fact_sources (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_person_workplaces_import_batch_id FOREIGN KEY(school_id, import_batch_id)
REFERENCES master_data_import_batches (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT uq_person_workplaces_school_id UNIQUE (school_id, id),
        CONSTRAINT uq_person_workplaces_source_version UNIQUE (school_id, source_key, version),
        CONSTRAINT ck_person_workplaces_version CHECK (version >= 1),
        CONSTRAINT ck_person_workplaces_interval CHECK (valid_to IS NULL OR valid_to >
valid_from),
        CONSTRAINT excl_person_workplaces_source_period EXCLUDE USING gist (school_id WITH =,
source_key WITH =, tstzrange(valid_from, GREATEST(valid_from, COALESCE(LEAST(valid_to,
revoked_at), 'infinity'::timestamptz)), '[)') WITH &&) WHERE (true),
        CONSTRAINT fk_person_workplaces_person_id FOREIGN KEY(school_id, person_id) REFERENCES
school_people (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_person_workplaces_assignment_id FOREIGN KEY(school_id, assignment_id)
REFERENCES staff_assignments (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_person_workplaces_location_id FOREIGN KEY(school_id, location_id)
REFERENCES campus_locations (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT ck_person_workplaces_subject CHECK ((person_id IS NOT NULL)::int +
(assignment_id IS NOT NULL)::int = 1),
        CONSTRAINT ck_person_workplaces_purpose CHECK (purpose IN ('office','reception')),
        CONSTRAINT excl_person_workplaces_person_period EXCLUDE USING gist (school_id WITH =,
person_id WITH =, location_id WITH =, purpose WITH =, tstzrange(valid_from, GREATEST(valid_from,
COALESCE(LEAST(valid_to, revoked_at), 'infinity'::timestamptz)), '[)') WITH &&) WHERE (true),
        CONSTRAINT excl_person_workplaces_assignment_period EXCLUDE USING gist (school_id WITH
=, assignment_id WITH =, location_id WITH =, purpose WITH =, tstzrange(valid_from,
GREATEST(valid_from, COALESCE(LEAST(valid_to, revoked_at), 'infinity'::timestamptz)), '[)') WITH
&&) WHERE (true),
        CONSTRAINT excl_person_workplaces_person_primary EXCLUDE USING gist (school_id WITH =,
person_id WITH =, purpose WITH =, tstzrange(valid_from, GREATEST(valid_from,
COALESCE(LEAST(valid_to, revoked_at), 'infinity'::timestamptz)), '[)') WITH &&) WHERE
(is_primary),
        CONSTRAINT excl_person_workplaces_assignment_primary EXCLUDE USING gist (school_id WITH
=, assignment_id WITH =, purpose WITH =, tstzrange(valid_from, GREATEST(valid_from,
COALESCE(LEAST(valid_to, revoked_at), 'infinity'::timestamptz)), '[)') WITH &&) WHERE
(is_primary)
);

CREATE TABLE school_services (
        display_name VARCHAR(1000) NOT NULL,
        organization_unit_id UUID NOT NULL,
        audience_description VARCHAR(1000) NOT NULL,
        service_summary VARCHAR(1000) NOT NULL,
        official_contact_id UUID,
        policy_id UUID,
        id UUID NOT NULL,
        school_id UUID NOT NULL,
        source_key VARCHAR(255) NOT NULL,
        version BIGINT NOT NULL,
        valid_from TIMESTAMP WITH TIME ZONE NOT NULL,
        valid_to TIMESTAMP WITH TIME ZONE,
        revoked_at TIMESTAMP WITH TIME ZONE,
        source_id UUID NOT NULL,
        import_batch_id UUID NOT NULL,
        basis_reference VARCHAR(1000) NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT fk_school_services_school FOREIGN KEY(school_id) REFERENCES schools (id) ON
DELETE RESTRICT,
        CONSTRAINT fk_school_services_source_id FOREIGN KEY(school_id, source_id) REFERENCES
school_fact_sources (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_school_services_import_batch_id FOREIGN KEY(school_id, import_batch_id)
REFERENCES master_data_import_batches (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT uq_school_services_school_id UNIQUE (school_id, id),
        CONSTRAINT uq_school_services_source_version UNIQUE (school_id, source_key, version),
        CONSTRAINT ck_school_services_version CHECK (version >= 1),
        CONSTRAINT ck_school_services_interval CHECK (valid_to IS NULL OR valid_to >
valid_from),
        CONSTRAINT excl_school_services_source_period EXCLUDE USING gist (school_id WITH =,
source_key WITH =, tstzrange(valid_from, GREATEST(valid_from, COALESCE(LEAST(valid_to,
revoked_at), 'infinity'::timestamptz)), '[)') WITH &&) WHERE (true),
        CONSTRAINT fk_school_services_organization_unit_id FOREIGN KEY(school_id,
organization_unit_id) REFERENCES organization_units (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_school_services_official_contact_id FOREIGN KEY(school_id,
official_contact_id) REFERENCES official_contact_points (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_school_services_policy_id FOREIGN KEY(school_id, policy_id) REFERENCES
school_policies (school_id, id) ON DELETE RESTRICT
);

CREATE TABLE school_fact_qualities (
        quality_kind VARCHAR(20) NOT NULL,
        target_fact_kind VARCHAR(80) NOT NULL,
        owner_kind VARCHAR(20) NOT NULL,
        organization_unit_id UUID,
        person_id UUID,
        location_id UUID,
        target_source_key VARCHAR(255),
        replacement_source_key VARCHAR(255),
        responsibility VARCHAR(1000) NOT NULL,
        evidence_reference VARCHAR(1000),
        purpose VARCHAR(20),
        contact_kind VARCHAR(20),
        coverage_status VARCHAR(20),
        negative_code VARCHAR(40),
        dispute_status VARCHAR(20),
        source_status VARCHAR(20),
        last_success_at TIMESTAMP WITH TIME ZONE,
        verified_at TIMESTAMP WITH TIME ZONE NOT NULL,
        review_due_at TIMESTAMP WITH TIME ZONE,
        id UUID NOT NULL,
        school_id UUID NOT NULL,
        source_key VARCHAR(255) NOT NULL,
        version BIGINT NOT NULL,
        valid_from TIMESTAMP WITH TIME ZONE NOT NULL,
        valid_to TIMESTAMP WITH TIME ZONE,
        revoked_at TIMESTAMP WITH TIME ZONE,
        source_id UUID NOT NULL,
        import_batch_id UUID NOT NULL,
        basis_reference VARCHAR(1000) NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT fk_school_fact_qualities_school FOREIGN KEY(school_id) REFERENCES schools
(id) ON DELETE RESTRICT,
        CONSTRAINT fk_school_fact_qualities_source_id FOREIGN KEY(school_id, source_id)
REFERENCES school_fact_sources (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_school_fact_qualities_import_batch_id FOREIGN KEY(school_id,
import_batch_id) REFERENCES master_data_import_batches (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT uq_school_fact_qualities_school_id UNIQUE (school_id, id),
        CONSTRAINT uq_school_fact_qualities_source_version UNIQUE (school_id, source_key,
version),
        CONSTRAINT ck_school_fact_qualities_version CHECK (version >= 1),
        CONSTRAINT ck_school_fact_qualities_interval CHECK (valid_to IS NULL OR valid_to >
valid_from),
        CONSTRAINT excl_school_fact_qualities_source_period EXCLUDE USING gist (school_id WITH
=, source_key WITH =, tstzrange(valid_from, GREATEST(valid_from, COALESCE(LEAST(valid_to,
revoked_at), 'infinity'::timestamptz)), '[)') WITH &&) WHERE (true),
        CONSTRAINT fk_school_fact_qualities_organization_unit_id FOREIGN KEY(school_id,
organization_unit_id) REFERENCES organization_units (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_school_fact_qualities_person_id FOREIGN KEY(school_id, person_id)
REFERENCES school_people (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT fk_school_fact_qualities_location_id FOREIGN KEY(school_id, location_id)
REFERENCES campus_locations (school_id, id) ON DELETE RESTRICT,
        CONSTRAINT ck_school_fact_qualities_kind CHECK (quality_kind IN
('source','coverage','negative','dispute','review','correction')),
        CONSTRAINT ck_school_fact_qualities_target CHECK (target_fact_kind IN
('school','person','organization','assignment','organization_campus_relationship','location','location_alias','person_contact','official_contact','policy','policy_scope','policy_version','organization_profile','organization_membership','organization_location','person_workplace','school_service')),
        CONSTRAINT ck_school_fact_qualities_owner CHECK ((owner_kind = 'school' AND
organization_unit_id IS NULL AND person_id IS NULL AND location_id IS NULL) OR (owner_kind =
'organization' AND organization_unit_id IS NOT NULL AND person_id IS NULL AND location_id IS
NULL) OR (owner_kind = 'person' AND person_id IS NOT NULL AND organization_unit_id IS NULL AND
location_id IS NULL) OR (owner_kind = 'location' AND location_id IS NOT NULL AND
organization_unit_id IS NULL AND person_id IS NULL)),
        CONSTRAINT ck_school_fact_qualities_coverage CHECK (coverage_status IS NULL OR
coverage_status IN ('partial','complete')),
        CONSTRAINT ck_school_fact_qualities_source CHECK (source_status IS NULL OR source_status
IN ('available','unavailable')),
        CONSTRAINT ck_school_fact_qualities_dispute CHECK (dispute_status IS NULL OR
dispute_status IN ('open','resolved')),
        CONSTRAINT ck_school_fact_qualities_exclusive_fields CHECK ((coverage_status IS NULL OR
quality_kind = 'coverage') AND (negative_code IS NULL OR quality_kind = 'negative') AND
(dispute_status IS NULL OR quality_kind = 'dispute') AND (source_status IS NULL OR quality_kind
= 'source') AND (replacement_source_key IS NULL OR quality_kind = 'correction')),
        CONSTRAINT ck_school_fact_qualities_contact_kind CHECK (contact_kind IS NULL OR
contact_kind IN ('phone','email')),
        CONSTRAINT ck_school_fact_qualities_review CHECK (review_due_at IS NULL OR review_due_at
> verified_at),
        CONSTRAINT ck_school_fact_qualities_required CHECK ((quality_kind = 'source' AND
source_status IS NOT NULL) OR (quality_kind = 'coverage' AND coverage_status IS NOT NULL) OR
(quality_kind = 'negative' AND negative_code IS NOT NULL) OR (quality_kind = 'dispute' AND
dispute_status IS NOT NULL AND evidence_reference IS NOT NULL) OR (quality_kind = 'review' AND
review_due_at IS NOT NULL) OR (quality_kind = 'correction' AND target_source_key IS NOT NULL AND
replacement_source_key IS NOT NULL AND target_source_key <> replacement_source_key AND
evidence_reference IS NOT NULL)),
        CONSTRAINT ck_school_fact_qualities_negative CHECK (negative_code IS NULL OR
((negative_code = 'no_office' AND target_fact_kind IN
('organization_location','person_workplace') AND purpose = 'office') OR (negative_code =
'no_reception' AND target_fact_kind IN ('organization_location','person_workplace') AND purpose
= 'reception') OR (negative_code = 'no_official_channel' AND target_fact_kind =
'official_contact' AND contact_kind IN ('phone','email')) OR (negative_code = 'no_service' AND
target_fact_kind = 'school_service')) IS TRUE)
);
"""

_F2_TABLES = (
    "organization_profiles",
    "organization_memberships",
    "organization_locations",
    "person_workplaces",
    "school_services",
    "school_fact_qualities",
)
_GOVERNANCE_TABLES = (
    "governance_duties",
    "governance_duty_versions",
    "governance_scopes",
    "governance_scope_versions",
    "governance_grants",
    "governance_grant_versions",
    "governance_assignment_epochs",
    "governance_command_receipts",
    "governance_audit_events",
)


_F2_READER_ROOT_SIGNATURE = (
    "app.school_fact_f2_reader_root_is_effective_at(uuid,text,uuid,timestamptz)"
)


def _create_f2_reader_root_authority() -> None:
    # Keep the historical F1 wrapper's person/organization contract unchanged.
    op.execute("GRANT CREATE ON SCHEMA app TO campus_ai_context_definer")
    op.execute("SET LOCAL ROLE campus_ai_context_definer")
    op.execute(
        sa.text("""
        CREATE FUNCTION app.school_fact_f2_reader_root_is_effective_at(
            p_school_id uuid,
            p_fact_kind text,
            p_target_id uuid,
            p_as_of timestamptz
        )
        RETURNS boolean
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $f2_root$
        BEGIN
            IF SESSION_USER <> 'campus_ai_school_fact_reader' THEN
                RAISE EXCEPTION 'F2 reader root authority rejected'
                    USING ERRCODE = '28000';
            END IF;
            IF p_school_id IS NULL OR p_target_id IS NULL OR p_as_of IS NULL
               OR p_fact_kind IS NULL OR p_fact_kind NOT IN (
                   'school', 'person', 'organization', 'location'
               ) THEN
                RAISE EXCEPTION 'F2 reader root arguments rejected'
                    USING ERRCODE = '22023';
            END IF;
            IF app.require_school_database_anchor() IS DISTINCT FROM p_school_id THEN
                RAISE EXCEPTION 'F2 reader root school rejected'
                    USING ERRCODE = '28000';
            END IF;
            RETURN app.school_fact_root_is_effective_at(
                p_school_id, p_fact_kind, p_target_id, p_as_of
            );
        END
        $f2_root$;
        """)
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {_F2_READER_ROOT_SIGNATURE} FROM PUBLIC, "
        "campus_ai_runtime, campus_ai_school_fact_writer, campus_ai_governance_writer, "
        "campus_ai_migration, campus_ai_db_owner, campus_ai_identity_definer, "
        "campus_ai_school_fact_reader"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {_F2_READER_ROOT_SIGNATURE} TO campus_ai_school_fact_reader"
    )
    op.execute("SET LOCAL ROLE campus_ai_db_owner")
    op.execute("REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer")


def upgrade() -> None:
    _create_f2_reader_root_authority()
    op.create_unique_constraint(
        "uq_official_contact_points_school_id",
        "official_contact_points",
        ["school_id", "id"],
    )
    op.execute(sa.text(_F2_DDL))
    op.execute(
        sa.text("""
        CREATE FUNCTION app.enforce_f2_temporal_history() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $f2_history$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'F2 published history cannot be deleted' USING ERRCODE='55000';
            END IF;
            IF (to_jsonb(NEW) - 'valid_to' - 'revoked_at') IS DISTINCT FROM
               (to_jsonb(OLD) - 'valid_to' - 'revoked_at') THEN
                RAISE EXCEPTION 'F2 content versions are immutable' USING ERRCODE='55000';
            END IF;
            IF OLD.revoked_at IS NOT NULL OR
               (OLD.valid_to IS NOT NULL AND
                (NEW.valid_to IS NULL OR NEW.valid_to > OLD.valid_to)) THEN
                RAISE EXCEPTION 'F2 terminal history cannot be extended' USING ERRCODE='55000';
            END IF;
            RETURN NEW;
        END $f2_history$;
        REVOKE ALL ON FUNCTION app.enforce_f2_temporal_history() FROM PUBLIC,
            campus_ai_runtime, campus_ai_school_fact_reader, campus_ai_governance_writer;
    """)
    )
    for table in _F2_TABLES:
        op.execute(
            f"CREATE TRIGGER f2_temporal_history BEFORE UPDATE OR DELETE ON "
            f"public.{table} FOR EACH ROW EXECUTE FUNCTION "
            f"app.enforce_f2_temporal_history()"
        )
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY sf5b_owner_migration_access ON public.{table} AS "
            f"PERMISSIVE FOR ALL TO campus_ai_db_owner, campus_ai_migration USING "
            f"(true) WITH CHECK (true)"
        )
        op.execute(
            f"CREATE POLICY sf5b_writer_context_access ON public.{table} AS "
            f"PERMISSIVE FOR ALL TO campus_ai_school_fact_writer USING (school_id = "
            f"app.require_school_context()) WITH CHECK (school_id = "
            f"app.require_school_context())"
        )
        op.execute(
            f"CREATE POLICY sf5b_reader_anchor_access ON public.{table} AS "
            f"PERMISSIVE FOR SELECT TO campus_ai_school_fact_reader USING (school_id"
            f" = app.require_school_database_anchor())"
        )
        op.execute(
            f"CREATE POLICY sf5b_anchor_guard ON public.{table} AS RESTRICTIVE FOR "
            f"ALL TO PUBLIC USING (CURRENT_USER IN "
            f"('campus_ai_db_owner','campus_ai_migration') OR school_id = "
            f"app.require_school_database_anchor()) WITH CHECK (CURRENT_USER IN "
            f"('campus_ai_db_owner','campus_ai_migration') OR school_id = "
            f"app.require_school_database_anchor())"
        )
        op.execute(
            f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, campus_ai_runtime, "
            f"campus_ai_school_fact_reader, campus_ai_school_fact_writer, "
            f"campus_ai_governance_writer"
        )
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE (valid_to, revoked_at) ON TABLE "
            f"public.{table} TO campus_ai_school_fact_writer"
        )
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO campus_ai_school_fact_reader")
    for table in (
        "school_versions",
        "school_person_profile_versions",
        "school_person_names",
        "organization_unit_versions",
        "organization_units",
        "staff_assignment_versions",
        "campus_location_versions",
        "campus_location_aliases",
        "organization_campus_relationships",
        "person_contact_points",
        "official_contact_points",
        "school_policies",
        "school_policy_scopes",
        "school_policy_versions",
        "school_fact_change_lineage",
    ):
        op.execute(f"DROP POLICY IF EXISTS sf5b_reader_anchor_access ON public.{table}")
        op.execute(
            f"CREATE POLICY sf5b_reader_anchor_access ON public.{table} AS "
            f"PERMISSIVE FOR SELECT TO campus_ai_school_fact_reader USING (school_id"
            f" = app.require_school_database_anchor())"
        )
        op.execute(f"GRANT SELECT ON TABLE public.{table} TO campus_ai_school_fact_reader")
    op.execute("GRANT USAGE ON SCHEMA public, app TO campus_ai_governance_writer")
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.require_school_database_anchor() TO "
        "campus_ai_governance_writer"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "app.assert_school_database_anchor(uuid,uuid,text) TO "
        "campus_ai_governance_writer"
    )
    # Governance is an independently authenticated writer, never a fact writer or
    # bootstrap holder. Role provisioning is controlled deployment infrastructure.
    for table in _GOVERNANCE_TABLES:
        op.execute(
            f"CREATE POLICY f2_governance_writer_anchor_access ON public.{table} AS "
            f"PERMISSIVE FOR ALL TO campus_ai_governance_writer USING (school_id = "
            f"app.require_school_database_anchor()) WITH CHECK (school_id = "
            f"app.require_school_database_anchor())"
        )
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM campus_ai_governance_writer")
        op.execute(f"GRANT SELECT, INSERT ON TABLE public.{table} TO campus_ai_governance_writer")
    for table in (
        "governance_grants",
        "governance_assignment_epochs",
        "governance_command_receipts",
    ):
        op.execute(f"GRANT UPDATE ON TABLE public.{table} TO campus_ai_governance_writer")
    op.execute(
        "REVOKE ALL ON TABLE public.governance_bootstrap_states FROM campus_ai_governance_writer"
    )


def downgrade() -> None:
    raise RuntimeError(
        "F2 history and role isolation cannot be downgraded destructively; "
        "restore the matching verified backup."
    )
