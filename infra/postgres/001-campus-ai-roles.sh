#!/usr/bin/env sh
set -eu

: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${POSTGRES_BOOTSTRAP_USER:?POSTGRES_BOOTSTRAP_USER is required}"
: "${POSTGRES_BOOTSTRAP_PASSWORD:?POSTGRES_BOOTSTRAP_PASSWORD is required}"
: "${POSTGRES_RUNTIME_PASSWORD:?POSTGRES_RUNTIME_PASSWORD is required}"
: "${POSTGRES_MIGRATION_PASSWORD:?POSTGRES_MIGRATION_PASSWORD is required}"
: "${POSTGRES_SCHOOL_FACT_WRITER_PASSWORD:?POSTGRES_SCHOOL_FACT_WRITER_PASSWORD is required}"
: "${POSTGRES_SCHOOL_FACT_READER_PASSWORD:?POSTGRES_SCHOOL_FACT_READER_PASSWORD is required}"

# Reject database credential reuse before invoking psql.  Never print a value.
if [ "$POSTGRES_BOOTSTRAP_PASSWORD" = "$POSTGRES_RUNTIME_PASSWORD" ] ||
   [ "$POSTGRES_BOOTSTRAP_PASSWORD" = "$POSTGRES_MIGRATION_PASSWORD" ] ||
   [ "$POSTGRES_BOOTSTRAP_PASSWORD" = "$POSTGRES_SCHOOL_FACT_WRITER_PASSWORD" ] ||
   [ "$POSTGRES_BOOTSTRAP_PASSWORD" = "$POSTGRES_SCHOOL_FACT_READER_PASSWORD" ] ||
   [ "$POSTGRES_RUNTIME_PASSWORD" = "$POSTGRES_MIGRATION_PASSWORD" ] ||
   [ "$POSTGRES_RUNTIME_PASSWORD" = "$POSTGRES_SCHOOL_FACT_WRITER_PASSWORD" ] ||
   [ "$POSTGRES_RUNTIME_PASSWORD" = "$POSTGRES_SCHOOL_FACT_READER_PASSWORD" ] ||
   [ "$POSTGRES_MIGRATION_PASSWORD" = "$POSTGRES_SCHOOL_FACT_WRITER_PASSWORD" ] ||
   [ "$POSTGRES_MIGRATION_PASSWORD" = "$POSTGRES_SCHOOL_FACT_READER_PASSWORD" ] ||
   [ "$POSTGRES_SCHOOL_FACT_WRITER_PASSWORD" = "$POSTGRES_SCHOOL_FACT_READER_PASSWORD" ]
then
    printf '%s\n' 'database role passwords must be pairwise distinct' >&2
    exit 1
fi

export PGDATABASE="${POSTGRES_DB}"
export PGUSER="${POSTGRES_BOOTSTRAP_USER}"
export PGPASSWORD="${POSTGRES_BOOTSTRAP_PASSWORD}"
export POSTGRES_EXPECTED_SYSTEM_IDENTIFIER="${POSTGRES_EXPECTED_SYSTEM_IDENTIFIER:-}"

psql -X --quiet -v ON_ERROR_STOP=1 <<'SQL'
\getenv target_database POSTGRES_DB
\getenv runtime_password POSTGRES_RUNTIME_PASSWORD
\getenv migration_password POSTGRES_MIGRATION_PASSWORD
\getenv school_fact_writer_password POSTGRES_SCHOOL_FACT_WRITER_PASSWORD
\getenv school_fact_reader_password POSTGRES_SCHOOL_FACT_READER_PASSWORD
\getenv expected_system_identifier POSTGRES_EXPECTED_SYSTEM_IDENTIFIER

SET ROLE NONE;
SET search_path = pg_catalog;

SELECT :'expected_system_identifier' <> '' AS verify_cluster_identity
\gset
\if :verify_cluster_identity
SELECT 1 / (
    (control.system_identifier::text = :'expected_system_identifier')::integer
) AS cluster_identity_guard
FROM pg_catalog.pg_control_system() AS control
\gset
SELECT 1 / (
    (NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_database AS database
        WHERE NOT database.datistemplate
          AND database.datname <> 'postgres'
          AND database.datname <> :'target_database'
          AND database.datname !~ '_test$'
    ))::integer
) AS cluster_inventory_guard
\gset
\endif

BEGIN;

DO $bootstrap$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'campus_ai_db_owner'
    ) THEN
        CREATE ROLE campus_ai_db_owner;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'campus_ai_context_definer'
    ) THEN
        CREATE ROLE campus_ai_context_definer;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'campus_ai_audit_definer'
    ) THEN
        CREATE ROLE campus_ai_audit_definer;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'campus_ai_outbox_probe_definer'
    ) THEN
        CREATE ROLE campus_ai_outbox_probe_definer;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'campus_ai_takeover_definer'
    ) THEN
        CREATE ROLE campus_ai_takeover_definer;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'campus_ai_migration'
    ) THEN
        CREATE ROLE campus_ai_migration;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'campus_ai_runtime'
    ) THEN
        CREATE ROLE campus_ai_runtime;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'campus_ai_school_fact_writer'
    ) THEN
        CREATE ROLE campus_ai_school_fact_writer;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'campus_ai_school_fact_reader'
    ) THEN
        CREATE ROLE campus_ai_school_fact_reader;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'campus_ai_identity_definer'
    ) THEN
        CREATE ROLE campus_ai_identity_definer;
    END IF;
END
$bootstrap$;

ALTER ROLE campus_ai_db_owner
    NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD NULL;
ALTER ROLE campus_ai_context_definer
    NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD NULL;
ALTER ROLE campus_ai_audit_definer
    NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD NULL;
ALTER ROLE campus_ai_outbox_probe_definer
    NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD NULL;
ALTER ROLE campus_ai_takeover_definer
    NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD NULL;
ALTER ROLE campus_ai_migration
    LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE campus_ai_runtime
    LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE campus_ai_school_fact_writer
    LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE campus_ai_school_fact_reader
    LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE campus_ai_identity_definer
    NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD NULL;

ALTER DEFAULT PRIVILEGES FOR ROLE campus_ai_outbox_probe_definer
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE campus_ai_takeover_definer
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

ALTER DEFAULT PRIVILEGES FOR ROLE campus_ai_identity_definer
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER ROLE campus_ai_db_owner RESET role;
ALTER ROLE campus_ai_context_definer RESET role;
ALTER ROLE campus_ai_audit_definer RESET role;
ALTER ROLE campus_ai_outbox_probe_definer RESET role;
ALTER ROLE campus_ai_takeover_definer RESET role;
ALTER ROLE campus_ai_migration RESET role;
ALTER ROLE campus_ai_runtime RESET role;
ALTER ROLE campus_ai_school_fact_writer RESET role;
ALTER ROLE campus_ai_school_fact_reader RESET role;
ALTER ROLE campus_ai_identity_definer RESET role;

SELECT pg_catalog.format('ALTER DATABASE %I RESET role', :'target_database')
\gexec
SELECT pg_catalog.format(
    'ALTER ROLE %I IN DATABASE %I RESET role',
    application_role,
    :'target_database'
)
FROM pg_catalog.unnest(ARRAY[
    'campus_ai_db_owner',
    'campus_ai_context_definer',
    'campus_ai_audit_definer',
    'campus_ai_outbox_probe_definer',
    'campus_ai_takeover_definer',
    'campus_ai_migration',
    'campus_ai_runtime'
]) AS application_roles(application_role)
\gexec

SELECT pg_catalog.format('ALTER ROLE campus_ai_migration PASSWORD %L', :'migration_password')
\gexec
SELECT pg_catalog.format('ALTER ROLE campus_ai_runtime PASSWORD %L', :'runtime_password')
\gexec
SELECT pg_catalog.format('ALTER ROLE campus_ai_school_fact_writer PASSWORD %L', :'school_fact_writer_password')
\gexec
SELECT pg_catalog.format('ALTER ROLE campus_ai_school_fact_reader PASSWORD %L', :'school_fact_reader_password')
\gexec

SELECT pg_catalog.format('REVOKE %I FROM %I CASCADE', parent.rolname, member.rolname)
FROM pg_catalog.pg_auth_members AS membership
JOIN pg_catalog.pg_roles AS parent ON parent.oid = membership.roleid
JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member
WHERE parent.rolname IN (
    'campus_ai_db_owner',
    'campus_ai_context_definer',
    'campus_ai_audit_definer',
    'campus_ai_outbox_probe_definer',
    'campus_ai_takeover_definer',
    'campus_ai_migration',
    'campus_ai_runtime'
)
OR member.rolname IN (
    'campus_ai_db_owner',
    'campus_ai_context_definer',
    'campus_ai_audit_definer',
    'campus_ai_outbox_probe_definer',
    'campus_ai_takeover_definer',
    'campus_ai_migration',
    'campus_ai_runtime'
)
\gexec

GRANT campus_ai_db_owner TO campus_ai_migration
    WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
GRANT campus_ai_context_definer TO campus_ai_migration
    WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
GRANT campus_ai_audit_definer TO campus_ai_migration
    WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
GRANT campus_ai_outbox_probe_definer TO campus_ai_migration
    WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
GRANT campus_ai_takeover_definer TO campus_ai_migration
    WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;

REASSIGN OWNED BY campus_ai_runtime TO campus_ai_db_owner;
REASSIGN OWNED BY campus_ai_migration TO campus_ai_db_owner;

SELECT pg_catalog.format(
    'ALTER %s %I.%I OWNER TO campus_ai_db_owner',
    CASE relation.relkind
        WHEN 'r' THEN 'TABLE'
        WHEN 'p' THEN 'TABLE'
        WHEN 'S' THEN 'SEQUENCE'
        WHEN 'v' THEN 'VIEW'
        WHEN 'm' THEN 'MATERIALIZED VIEW'
        WHEN 'f' THEN 'FOREIGN TABLE'
    END,
    namespace.nspname,
    relation.relname
)
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
JOIN pg_catalog.pg_roles AS owner ON owner.oid = relation.relowner
WHERE owner.rolname IN (
    'campus_ai_context_definer',
    'campus_ai_audit_definer',
    'campus_ai_outbox_probe_definer',
    'campus_ai_takeover_definer'
)
  AND namespace.nspname <> 'information_schema'
  AND namespace.nspname NOT LIKE 'pg_%'
  AND relation.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
ORDER BY namespace.nspname, relation.relname
\gexec

SELECT pg_catalog.format('ALTER DATABASE %I OWNER TO campus_ai_db_owner', :'target_database')
\gexec

SELECT DISTINCT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON DATABASE %I FROM %I CASCADE',
    :'target_database',
    grantee.rolname
)
FROM pg_catalog.pg_database AS database
CROSS JOIN LATERAL pg_catalog.aclexplode(database.datacl) AS acl
JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
WHERE database.datname = :'target_database'
  AND grantee.rolname NOT IN (
      'campus_ai_db_owner',
      'campus_ai_runtime',
      'campus_ai_migration'
  )
\gexec

SELECT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON DATABASE %I FROM PUBLIC CASCADE',
    :'target_database'
)
\gexec
SELECT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON DATABASE %I FROM campus_ai_runtime, campus_ai_migration CASCADE',
    :'target_database'
)
\gexec
SELECT pg_catalog.format(
    'GRANT CONNECT ON DATABASE %I TO campus_ai_runtime, campus_ai_migration',
    :'target_database'
)
\gexec

SELECT DISTINCT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON SCHEMA public FROM %I CASCADE',
    grantee.rolname
)
FROM pg_catalog.pg_namespace AS namespace
CROSS JOIN LATERAL pg_catalog.aclexplode(namespace.nspacl) AS acl
JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
WHERE namespace.nspname = 'public'
  AND grantee.rolname NOT IN (
      'pg_database_owner',
      'campus_ai_db_owner',
      'campus_ai_context_definer',
      'campus_ai_audit_definer',
      'campus_ai_outbox_probe_definer',
      'campus_ai_takeover_definer',
      'campus_ai_runtime',
      'campus_ai_migration'
  )
\gexec

REVOKE ALL PRIVILEGES ON SCHEMA public FROM campus_ai_context_definer CASCADE;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM campus_ai_audit_definer CASCADE;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM campus_ai_outbox_probe_definer CASCADE;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM campus_ai_takeover_definer CASCADE;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM campus_ai_runtime, campus_ai_migration CASCADE;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC CASCADE;
GRANT USAGE ON SCHEMA public TO campus_ai_runtime, campus_ai_migration;
GRANT USAGE ON SCHEMA public TO campus_ai_context_definer;
GRANT USAGE ON SCHEMA public TO campus_ai_audit_definer;
GRANT USAGE ON SCHEMA public TO campus_ai_outbox_probe_definer;
GRANT USAGE ON SCHEMA public TO campus_ai_takeover_definer;

SELECT DISTINCT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON SCHEMA app FROM %I CASCADE',
    grantee.rolname
)
FROM pg_catalog.pg_namespace AS namespace
CROSS JOIN LATERAL pg_catalog.aclexplode(namespace.nspacl) AS acl
JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
WHERE namespace.nspname = 'app'
  AND grantee.rolname <> 'campus_ai_db_owner'
\gexec
SELECT 'REVOKE ALL PRIVILEGES ON SCHEMA app FROM PUBLIC CASCADE'
WHERE pg_catalog.to_regnamespace('app') IS NOT NULL
\gexec
SELECT 'GRANT USAGE ON SCHEMA app TO campus_ai_context_definer, campus_ai_audit_definer, campus_ai_outbox_probe_definer, campus_ai_takeover_definer, campus_ai_runtime, campus_ai_migration'
WHERE pg_catalog.to_regnamespace('app') IS NOT NULL
\gexec
SELECT 'GRANT CREATE ON SCHEMA app TO campus_ai_outbox_probe_definer'
WHERE pg_catalog.to_regprocedure(
    'app.verify_staff_duty_binding_outbox(uuid,uuid,uuid,bigint)'
) IS NOT NULL
\gexec
SELECT 'ALTER FUNCTION app.verify_staff_duty_binding_outbox(uuid,uuid,uuid,bigint) '
       'OWNER TO campus_ai_outbox_probe_definer'
WHERE pg_catalog.to_regprocedure(
    'app.verify_staff_duty_binding_outbox(uuid,uuid,uuid,bigint)'
) IS NOT NULL
\gexec
SELECT 'REVOKE CREATE ON SCHEMA app FROM campus_ai_outbox_probe_definer'
WHERE pg_catalog.to_regnamespace('app') IS NOT NULL
\gexec
SELECT 'GRANT CREATE ON SCHEMA app TO campus_ai_takeover_definer'
WHERE pg_catalog.to_regprocedure(
    'app.lock_master_data_import_takeover_source(uuid,uuid,uuid)'
) IS NOT NULL
\gexec
WITH expected(function_name, argument_types) AS (
    VALUES
        ('lock_master_data_import_takeover_source', 'uuid, uuid, uuid'),
        ('lock_master_data_import_takeover_assignment', 'uuid, uuid, uuid, uuid'),
        (
            'supersede_master_data_import_takeover_source',
            'uuid, uuid, uuid, text, bigint'
        ),
        (
            'assert_master_data_import_takeover_integrity',
            'uuid, uuid, uuid'
        ),
        (
            'read_master_data_import_attempt_lineage',
            'uuid, uuid'
        ),
        (
            'read_master_data_import_succeeded_attempt',
            'uuid, uuid, uuid'
        )
)
SELECT pg_catalog.format(
    'ALTER FUNCTION app.%I(%s) OWNER TO campus_ai_takeover_definer',
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM expected
JOIN pg_catalog.pg_proc AS procedure
  ON procedure.proname = expected.function_name
 AND pg_catalog.oidvectortypes(procedure.proargtypes) = expected.argument_types
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app'
\gexec
SELECT 'REVOKE CREATE ON SCHEMA app FROM campus_ai_takeover_definer'
WHERE pg_catalog.to_regnamespace('app') IS NOT NULL
\gexec

SELECT DISTINCT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON SCHEMA app_private FROM %I CASCADE',
    grantee.rolname
)
FROM pg_catalog.pg_namespace AS namespace
CROSS JOIN LATERAL pg_catalog.aclexplode(namespace.nspacl) AS acl
JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
WHERE namespace.nspname = 'app_private'
  AND grantee.rolname <> 'campus_ai_db_owner'
\gexec
SELECT 'REVOKE ALL PRIVILEGES ON SCHEMA app_private FROM PUBLIC CASCADE'
WHERE pg_catalog.to_regnamespace('app_private') IS NOT NULL
\gexec
SELECT 'GRANT USAGE ON SCHEMA app_private TO campus_ai_context_definer, campus_ai_audit_definer, campus_ai_takeover_definer'
WHERE pg_catalog.to_regnamespace('app_private') IS NOT NULL
\gexec

SELECT pg_catalog.format(
    'ALTER FUNCTION %I.%I(%s) OWNER TO campus_ai_context_definer',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app_private'
  AND procedure.proname = 'master_data_import_effective_assignment'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) =
      'uuid, uuid, timestamp with time zone'
\gexec
SELECT DISTINCT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON FUNCTION %I.%I(%s) FROM %I CASCADE',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes),
    grantee.rolname
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
CROSS JOIN LATERAL pg_catalog.aclexplode(
    COALESCE(procedure.proacl, pg_catalog.acldefault('f', procedure.proowner))
) AS acl
JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
WHERE namespace.nspname = 'app_private'
  AND procedure.proname = 'master_data_import_effective_assignment'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) =
      'uuid, uuid, timestamp with time zone'
  AND grantee.rolname NOT IN (
      'campus_ai_context_definer', 'campus_ai_audit_definer',
      'campus_ai_takeover_definer'
  )
\gexec
SELECT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON FUNCTION %I.%I(%s) FROM PUBLIC CASCADE',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app_private'
  AND procedure.proname = 'master_data_import_effective_assignment'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) =
      'uuid, uuid, timestamp with time zone'
\gexec
SELECT pg_catalog.format(
    'GRANT EXECUTE ON FUNCTION %I.%I(%s) TO campus_ai_audit_definer, campus_ai_takeover_definer',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app_private'
  AND procedure.proname = 'master_data_import_effective_assignment'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) =
      'uuid, uuid, timestamp with time zone'
\gexec

SELECT pg_catalog.format(
    'ALTER FUNCTION %I.%I(%s) OWNER TO campus_ai_audit_definer',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app_private'
  AND procedure.proname = 'append_master_data_import_audit_xid'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) =
      'integer, uuid, uuid, uuid, bigint, text, text, text, uuid, uuid, uuid, jsonb, jsonb, uuid, text'
\gexec
SELECT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON FUNCTION %I.%I(%s) FROM PUBLIC, campus_ai_runtime, campus_ai_db_owner, campus_ai_context_definer, campus_ai_outbox_probe_definer, campus_ai_takeover_definer CASCADE',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app_private'
  AND procedure.proname = 'append_master_data_import_audit_xid'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) =
      'integer, uuid, uuid, uuid, bigint, text, text, text, uuid, uuid, uuid, jsonb, jsonb, uuid, text'
\gexec

SELECT pg_catalog.format(
    'ALTER FUNCTION %I.%I(%s) OWNER TO campus_ai_audit_definer',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app_private'
  AND procedure.proname = 'append_business_audit_revision_12'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) =
      'integer, uuid, uuid, uuid, bigint, uuid, bigint, text, text, text, text, uuid, jsonb, jsonb, uuid, text'
\gexec
SELECT pg_catalog.format(
    'ALTER FUNCTION %I.%I(%s) SECURITY DEFINER',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app_private'
  AND procedure.proname = 'append_business_audit_revision_12'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) =
      'integer, uuid, uuid, uuid, bigint, uuid, bigint, text, text, text, text, uuid, jsonb, jsonb, uuid, text'
\gexec
SELECT pg_catalog.format(
    'ALTER FUNCTION %I.%I(%s) SET search_path = pg_catalog',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app_private'
  AND procedure.proname = 'append_business_audit_revision_12'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) =
      'integer, uuid, uuid, uuid, bigint, uuid, bigint, text, text, text, text, uuid, jsonb, jsonb, uuid, text'
\gexec
SELECT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON FUNCTION %I.%I(%s) FROM PUBLIC, campus_ai_runtime, campus_ai_db_owner, campus_ai_migration, campus_ai_context_definer, campus_ai_outbox_probe_definer, campus_ai_takeover_definer CASCADE',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app_private'
  AND procedure.proname = 'append_business_audit_revision_12'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) =
      'integer, uuid, uuid, uuid, bigint, uuid, bigint, text, text, text, text, uuid, jsonb, jsonb, uuid, text'
\gexec
-- Revision 20 creates the two Identity capabilities under the DB-owner migration
-- role.  A post-migration bootstrap repeat transfers only these exact signatures.
SELECT 'GRANT CREATE ON SCHEMA app TO campus_ai_identity_definer'
WHERE pg_catalog.to_regprocedure('app.lock_existing_identity_user_id(text,text,text)')
      IS NOT NULL
\gexec

WITH revision20_identity_function(function_name, argument_types) AS (
    VALUES
        (
            'lock_existing_identity_user_id',
            'text, text, text'
        ),
        (
            'claim_legacy_identity_user_id',
            'text, text, text, text'
        )
)
SELECT pg_catalog.format(
    'ALTER FUNCTION %I.%I(%s) OWNER TO campus_ai_identity_definer',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM revision20_identity_function AS expected
JOIN pg_catalog.pg_proc AS procedure
  ON procedure.proname = expected.function_name
 AND pg_catalog.oidvectortypes(procedure.proargtypes) = expected.argument_types
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app'
\gexec

SELECT 'REVOKE CREATE ON SCHEMA app FROM campus_ai_identity_definer'
WHERE pg_catalog.to_regnamespace('app') IS NOT NULL
\gexec

SELECT 'GRANT CREATE ON SCHEMA app TO campus_ai_context_definer'
WHERE pg_catalog.to_regprocedure(
    'app.school_fact_school_reactivation_preflight(uuid,integer,uuid,uuid,bigint,bigint,uuid,text,text,timestamptz,text,text,bigint,timestamptz,bigint,text,text)'
) IS NOT NULL
\gexec
WITH revision20_school_reactivation_function(function_name, argument_types) AS (
    VALUES
        (
            'school_fact_school_reactivation_preflight',
            'uuid, integer, uuid, uuid, bigint, bigint, uuid, text, text, '
            'timestamp with time zone, text, text, bigint, timestamp with time zone, '
            'bigint, text, text'
        ),
        (
            'school_fact_school_reactivation_prepare',
            'uuid, integer, uuid, uuid, bigint, bigint, uuid, text, text, '
            'timestamp with time zone, text, text, bigint, timestamp with time zone, '
            'bigint, text, text, uuid, uuid, text, text, timestamp with time zone, text, text, '
            'text, text, text, text, integer'
        )
)
SELECT pg_catalog.format(
    'ALTER FUNCTION %I.%I(%s) OWNER TO campus_ai_context_definer',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM revision20_school_reactivation_function AS expected
JOIN pg_catalog.pg_proc AS procedure
  ON procedure.proname = expected.function_name
 AND pg_catalog.oidvectortypes(procedure.proargtypes) = expected.argument_types
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app'
\gexec
SELECT 'REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer'
WHERE pg_catalog.to_regnamespace('app') IS NOT NULL
\gexec

SELECT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON FUNCTION %I.%I(%s) FROM PUBLIC CASCADE',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app'
\gexec

SELECT DISTINCT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON FUNCTION %I.%I(%s) FROM %I CASCADE',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes),
    grantee.rolname
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
CROSS JOIN LATERAL pg_catalog.aclexplode(
    COALESCE(
        procedure.proacl,
        pg_catalog.acldefault('f', procedure.proowner)
    )
) AS acl
JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
WHERE namespace.nspname = 'app'
  AND acl.grantee <> procedure.proowner
\gexec

WITH expected(function_name, argument_types) AS (
    VALUES
        ('context_signing_key_fingerprint', ''),
        (
            'list_execution_context_options',
            'integer, bigint, text, timestamp with time zone, text'
        ),
        (
            'bind_execution_context',
            'uuid, integer, uuid, text, uuid, bigint, uuid, bigint, bigint, uuid, '
            'text, text, text, timestamp with time zone, text'
        ),
        ('require_school_context', ''),
        (
            'append_business_audit',
            'integer, uuid, uuid, uuid, bigint, uuid, bigint, text, text, text, text, '
            'uuid, jsonb, jsonb, uuid, text'
        ),
        (
            'append_master_data_import_audit',
            'integer, uuid, uuid, uuid, bigint, text, text, text, uuid, uuid, uuid, jsonb, jsonb, '
            'uuid, text'
        ),
        (
            'append_legacy_audit',
            'integer, text, text, text, jsonb, jsonb, text, text'
        ),
        (
            'verify_staff_duty_binding_outbox',
            'uuid, uuid, uuid, bigint'
        ),
        (
            'read_master_data_import_attempt_lineage',
            'uuid, uuid'
        ),
        (
            'read_master_data_import_succeeded_attempt',
            'uuid, uuid, uuid'
        )
)
SELECT pg_catalog.format(
    'GRANT EXECUTE ON FUNCTION %I.%I(%s) TO campus_ai_runtime',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM expected
JOIN pg_catalog.pg_proc AS procedure
  ON procedure.proname = expected.function_name
 AND pg_catalog.oidvectortypes(procedure.proargtypes) = expected.argument_types
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app'
\gexec

-- Revision 18 adds narrow School Facts temporal readers and explicit-reactivation
-- entry points.  The bootstrap is intentionally idempotent and first revokes
-- every non-owner function ACL above, so it must restore this exact allowlist
-- after an already-migrated database is bootstrapped again.
WITH revision18_school_fact_function(function_name, argument_types) AS (
    VALUES
        (
            'school_fact_content_version_terminal',
            'uuid, text, uuid, bigint'
        ),
        (
            'school_fact_root_is_effective_at',
            'uuid, text, uuid, timestamp with time zone'
        ),
        (
            'school_fact_location_history_is_resolvable',
            'uuid, uuid, timestamp with time zone'
        ),
        (
            'school_fact_location_history_interval_is_resolvable',
            'uuid, uuid, timestamp with time zone, timestamp with time zone'
        ),
        (
            'school_fact_school_snapshot_as_of',
            'uuid, timestamp with time zone'
        ),
        (
            'list_school_reactivation_candidates',
            'integer, bigint, text, timestamp with time zone, text'
        ),
        (
            'validate_school_reactivation_candidate',
            'uuid, integer, uuid, text, uuid, bigint, uuid, bigint, bigint, uuid, '
            'text, text, text, timestamp with time zone, text'
        ),
        (
            'bind_school_reactivation_context',
            'uuid, integer, uuid, text, uuid, bigint, uuid, bigint, bigint, uuid, '
            'text, text, text, timestamp with time zone, text, text, text, text, '
            'bigint, text, text, text, bigint'
        ),
        ('require_school_reactivation_context', ''),
        ('school_reactivation_replay_result', ''),
        (
            'bind_root_reactivation_context',
            'text, text, text, text, bigint'
        ),
        ('require_root_reactivation_context', '')
)
SELECT pg_catalog.format(
    'GRANT EXECUTE ON FUNCTION %I.%I(%s) TO campus_ai_runtime, campus_ai_db_owner',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM revision18_school_fact_function AS expected
JOIN pg_catalog.pg_proc AS procedure
  ON procedure.proname = expected.function_name
 AND pg_catalog.oidvectortypes(procedure.proargtypes) = expected.argument_types
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app'
\gexec

-- Revision 20 anchor, Writer and Identity capabilities.  The global revoke
-- above is deliberate; this exact allowlist is the only repeat-safe recovery.
WITH revision20_function(function_name, argument_types, grantees) AS (
    VALUES
        (
            'require_school_database_anchor',
            '',
            'campus_ai_runtime, campus_ai_school_fact_writer, '
            'campus_ai_school_fact_reader, campus_ai_migration, '
            'campus_ai_context_definer, campus_ai_audit_definer, '
            'campus_ai_outbox_probe_definer, campus_ai_takeover_definer'
        ),
        (
            'assert_school_database_anchor',
            'uuid, uuid, text',
            'campus_ai_runtime, campus_ai_school_fact_writer, '
            'campus_ai_school_fact_reader, campus_ai_migration'
        ),
        (
            'provision_school_database_anchor',
            'uuid, uuid, text',
            'campus_ai_migration'
        ),
        (
            'lock_existing_identity_user_id',
            'text, text, text',
            'campus_ai_school_fact_writer'
        ),
        (
            'claim_legacy_identity_user_id',
            'text, text, text, text',
            'campus_ai_runtime'
        ),
        (
            'school_fact_school_reactivation_preflight',
            'uuid, integer, uuid, uuid, bigint, bigint, uuid, text, text, '
            'timestamp with time zone, text, text, bigint, timestamp with time zone, '
            'bigint, text, text',
            'campus_ai_school_fact_writer'
        ),
        (
            'school_fact_school_reactivation_prepare',
            'uuid, integer, uuid, uuid, bigint, bigint, uuid, text, text, '
            'timestamp with time zone, text, text, bigint, timestamp with time zone, '
            'bigint, text, text, uuid, uuid, text, text, timestamp with time zone, text, text, '
            'text, text, text, text, integer',
            'campus_ai_school_fact_writer'
        ),
        (
            'purge_expired_school_fact_preparations',
            '',
            'campus_ai_school_fact_writer'
        ),
        ('require_school_context', '', 'campus_ai_school_fact_writer'),
        (
            'bind_execution_context',
            'uuid, integer, uuid, text, uuid, bigint, uuid, bigint, bigint, uuid, '
            'text, text, text, timestamp with time zone, text',
            'campus_ai_school_fact_writer'
        ),
        (
            'bind_school_reactivation_context',
            'uuid, integer, uuid, text, uuid, bigint, uuid, bigint, bigint, uuid, '
            'text, text, text, timestamp with time zone, text, text, text, text, '
            'bigint, text, text, text, bigint',
            'campus_ai_school_fact_writer'
        ),
        ('require_school_reactivation_context', '', 'campus_ai_school_fact_writer'),
        ('school_reactivation_replay_result', '', 'campus_ai_school_fact_writer'),
        (
            'bind_root_reactivation_context',
            'text, text, text, text, bigint',
            'campus_ai_school_fact_writer'
        ),
        ('require_root_reactivation_context', '', 'campus_ai_school_fact_writer'),
        (
            'school_fact_content_version_terminal',
            'uuid, text, uuid, bigint',
            'campus_ai_school_fact_writer'
        ),
        (
            'school_fact_root_is_effective_at',
            'uuid, text, uuid, timestamp with time zone',
            'campus_ai_school_fact_writer'
        ),
        (
            'school_fact_location_history_is_resolvable',
            'uuid, uuid, timestamp with time zone',
            'campus_ai_school_fact_writer, campus_ai_school_fact_reader'
        ),
        (
            'school_fact_location_history_interval_is_resolvable',
            'uuid, uuid, timestamp with time zone, timestamp with time zone',
            'campus_ai_school_fact_writer'
        )
)
SELECT pg_catalog.format(
    'GRANT EXECUTE ON FUNCTION %I.%I(%s) TO %s',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes),
    expected.grantees
)
FROM revision20_function AS expected
JOIN pg_catalog.pg_proc AS procedure
  ON procedure.proname = expected.function_name
 AND pg_catalog.oidvectortypes(procedure.proargtypes) = expected.argument_types
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app'
\gexec

SELECT pg_catalog.format(
    'GRANT EXECUTE ON FUNCTION %I.%I(%s) TO campus_ai_db_owner, campus_ai_audit_definer, campus_ai_outbox_probe_definer, campus_ai_takeover_definer',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app'
  AND procedure.proname = 'require_school_context'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) = ''
\gexec

SELECT pg_catalog.format(
    'GRANT EXECUTE ON FUNCTION %I.%I(%s) TO campus_ai_db_owner',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app'
  AND procedure.proname = 'bind_execution_context'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) =
      'uuid, integer, uuid, text, uuid, bigint, uuid, bigint, bigint, uuid, text, text, text, timestamp with time zone, text'
\gexec

WITH expected(function_name, argument_types) AS (
    VALUES
        (
            'append_business_audit',
            'integer, uuid, uuid, uuid, bigint, uuid, bigint, text, text, text, text, '
            'uuid, jsonb, jsonb, uuid, text'
        ),
        (
            'append_master_data_import_audit',
            'integer, uuid, uuid, uuid, bigint, text, text, text, uuid, uuid, uuid, jsonb, jsonb, '
            'uuid, text'
        ),
        (
            'append_legacy_audit',
            'integer, text, text, text, jsonb, jsonb, text, text'
        )
)
SELECT pg_catalog.format(
    'GRANT EXECUTE ON FUNCTION %I.%I(%s) TO campus_ai_db_owner',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM expected
JOIN pg_catalog.pg_proc AS procedure
  ON procedure.proname = expected.function_name
 AND pg_catalog.oidvectortypes(procedure.proargtypes) = expected.argument_types
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app'
\gexec

SELECT pg_catalog.format(
    'GRANT EXECUTE ON FUNCTION %I.%I(%s) TO campus_ai_takeover_definer',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app'
  AND procedure.proname = 'append_master_data_import_audit'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) =
      'integer, uuid, uuid, uuid, bigint, text, text, text, uuid, uuid, uuid, jsonb, jsonb, uuid, text'
\gexec

SELECT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON FUNCTION %I.%I(%s) FROM PUBLIC, campus_ai_runtime CASCADE',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app_private'
  AND procedure.proname = 'is_safe_business_audit_summary'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) = 'jsonb'
\gexec

SELECT pg_catalog.format(
    'ALTER FUNCTION %I.%I(%s) OWNER TO campus_ai_takeover_definer',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app_private'
  AND procedure.proname = 'master_data_import_takeover_chain_is_valid'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) = 'uuid, uuid, uuid, bigint'
\gexec
SELECT DISTINCT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON FUNCTION %I.%I(%s) FROM %I CASCADE',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes),
    grantee.rolname
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
CROSS JOIN LATERAL pg_catalog.aclexplode(procedure.proacl) AS acl
JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
WHERE namespace.nspname = 'app_private'
  AND procedure.proname = 'master_data_import_takeover_chain_is_valid'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) = 'uuid, uuid, uuid, bigint'
  AND grantee.rolname <> 'campus_ai_takeover_definer'
\gexec
SELECT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON FUNCTION %I.%I(%s) FROM PUBLIC CASCADE',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app_private'
  AND procedure.proname = 'master_data_import_takeover_chain_is_valid'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) = 'uuid, uuid, uuid, bigint'
\gexec
SELECT pg_catalog.format(
    'GRANT EXECUTE ON FUNCTION %I.%I(%s) TO campus_ai_takeover_definer',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app_private'
  AND procedure.proname = 'master_data_import_takeover_chain_is_valid'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) = 'uuid, uuid, uuid, bigint'
\gexec

WITH expected(function_name) AS (
    VALUES
        ('populate_master_data_import_unit_outcome_snapshot'),
        ('reject_master_data_import_outcome_mutation')
)
SELECT pg_catalog.format(
    'ALTER FUNCTION %I.%I(%s) OWNER TO campus_ai_takeover_definer',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM expected
JOIN pg_catalog.pg_proc AS procedure ON procedure.proname = expected.function_name
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app_private'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) = ''
\gexec
WITH expected(function_name) AS (
    VALUES
        ('populate_master_data_import_unit_outcome_snapshot'),
        ('reject_master_data_import_outcome_mutation')
)
SELECT DISTINCT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON FUNCTION %I.%I(%s) FROM %I CASCADE',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes),
    grantee.rolname
)
FROM expected
JOIN pg_catalog.pg_proc AS procedure ON procedure.proname = expected.function_name
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
CROSS JOIN LATERAL pg_catalog.aclexplode(procedure.proacl) AS acl
JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
WHERE namespace.nspname = 'app_private'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) = ''
  AND grantee.rolname <> 'campus_ai_takeover_definer'
\gexec
WITH expected(function_name) AS (
    VALUES
        ('populate_master_data_import_unit_outcome_snapshot'),
        ('reject_master_data_import_outcome_mutation')
)
SELECT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON FUNCTION %I.%I(%s) FROM PUBLIC CASCADE',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM expected
JOIN pg_catalog.pg_proc AS procedure ON procedure.proname = expected.function_name
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app_private'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) = ''
\gexec
WITH expected(function_name) AS (
    VALUES
        ('populate_master_data_import_unit_outcome_snapshot'),
        ('reject_master_data_import_outcome_mutation')
)
SELECT pg_catalog.format(
    'GRANT EXECUTE ON FUNCTION %I.%I(%s) TO campus_ai_takeover_definer',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM expected
JOIN pg_catalog.pg_proc AS procedure ON procedure.proname = expected.function_name
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app_private'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) = ''
\gexec

SELECT pg_catalog.format(
    'ALTER FUNCTION %I.%I(%s) OWNER TO campus_ai_takeover_definer',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app_private'
  AND procedure.proname = 'master_data_import_takeover_chain_is_valid_for_audit'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) = 'uuid, uuid, uuid, bigint'
\gexec
SELECT DISTINCT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON FUNCTION %I.%I(%s) FROM %I CASCADE',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes),
    grantee.rolname
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
CROSS JOIN LATERAL pg_catalog.aclexplode(procedure.proacl) AS acl
JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
WHERE namespace.nspname = 'app_private'
  AND procedure.proname = 'master_data_import_takeover_chain_is_valid_for_audit'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) = 'uuid, uuid, uuid, bigint'
  AND grantee.rolname NOT IN ('campus_ai_takeover_definer', 'campus_ai_audit_definer')
\gexec
SELECT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON FUNCTION %I.%I(%s) FROM PUBLIC CASCADE',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app_private'
  AND procedure.proname = 'master_data_import_takeover_chain_is_valid_for_audit'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) = 'uuid, uuid, uuid, bigint'
\gexec
SELECT pg_catalog.format(
    'GRANT EXECUTE ON FUNCTION %I.%I(%s) TO campus_ai_audit_definer',
    namespace.nspname,
    procedure.proname,
    pg_catalog.oidvectortypes(procedure.proargtypes)
)
FROM pg_catalog.pg_proc AS procedure
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
WHERE namespace.nspname = 'app_private'
  AND procedure.proname = 'master_data_import_takeover_chain_is_valid_for_audit'
  AND pg_catalog.oidvectortypes(procedure.proargtypes) = 'uuid, uuid, uuid, bigint'
\gexec

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM campus_ai_audit_definer;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM campus_ai_audit_definer;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM campus_ai_takeover_definer;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM campus_ai_takeover_definer;

WITH expected(table_name, column_list) AS (
    VALUES
        (
            'staff_assignments',
            'school_id, id, person_id, semantic_role_code, valid_from, valid_to, status, version'
        ),
        (
            'master_data_import_batches',
            'school_id, id, mode, source_kind, source_ref, status, active_idempotency_record_id, '
            'active_action_code, version'
        ),
        (
            'master_data_import_units',
            'school_id, id, batch_id, unit_kind, status, target_id, target_version, '
            'soft_anomalies, decision_reason, '
            'last_attempt_idempotency_record_id, last_attempt_completed'
        ),
        (
            'master_data_import_command_outcomes',
            'school_id, idempotency_record_id, batch_id, action_code, status, batch_version'
        ),
        (
            'master_data_import_takeovers',
            'school_id, id, batch_id, previous_idempotency_record_id, '
            'new_idempotency_record_id, original_actor_user_id, original_actor_person_id, '
            'original_actor_assignment_id, original_actor_assignment_version, '
            'new_actor_user_id, new_actor_person_id, new_actor_assignment_id, '
            'new_actor_assignment_version, request_id, batch_version_before, batch_version_after'
        ),
        (
            'command_idempotency_records',
            'school_id, id, action_code, actor_user_id, actor_person_id, actor_assignment_id, '
            'actor_assignment_version, resource_type, resource_id, idempotency_key_hash, '
            'status, result_type, result_payload'
        ),
        ('school_people', 'school_id, id, status, version'),
        (
            'school_calendar_versions',
            'school_id, id, status, version, source_kind, source_ref'
        )
)
SELECT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON TABLE public.%I FROM campus_ai_audit_definer CASCADE',
    table_name
)
FROM expected
WHERE pg_catalog.to_regclass('public.' || table_name) IS NOT NULL
\gexec

WITH expected(table_name, column_list) AS (
    VALUES
        (
            'staff_assignments',
            'school_id, id, person_id, semantic_role_code, valid_from, valid_to, status, version'
        ),
        (
            'master_data_import_batches',
            'school_id, id, mode, source_kind, source_ref, status, active_idempotency_record_id, '
            'active_action_code, version'
        ),
        (
            'master_data_import_units',
            'school_id, id, batch_id, unit_kind, status, target_id, target_version, '
            'soft_anomalies, decision_reason, '
            'last_attempt_idempotency_record_id, last_attempt_completed'
        ),
        (
            'master_data_import_command_outcomes',
            'school_id, idempotency_record_id, batch_id, action_code, status, batch_version'
        ),
        (
            'master_data_import_takeovers',
            'school_id, id, batch_id, previous_idempotency_record_id, '
            'new_idempotency_record_id, original_actor_user_id, original_actor_person_id, '
            'original_actor_assignment_id, original_actor_assignment_version, '
            'new_actor_user_id, new_actor_person_id, new_actor_assignment_id, '
            'new_actor_assignment_version, request_id, batch_version_before, batch_version_after'
        ),
        (
            'command_idempotency_records',
            'school_id, id, action_code, actor_user_id, actor_person_id, actor_assignment_id, '
            'actor_assignment_version, resource_type, resource_id, idempotency_key_hash, '
            'status, result_type, result_payload'
        ),
        ('school_people', 'school_id, id, status, version'),
        (
            'school_calendar_versions',
            'school_id, id, status, version, source_kind, source_ref'
        )
)
SELECT pg_catalog.format(
    'GRANT SELECT (%s) ON TABLE public.%I TO campus_ai_audit_definer',
    column_list,
    table_name
)
FROM expected
WHERE pg_catalog.to_regclass('public.' || table_name) IS NOT NULL
\gexec

WITH expected(table_name) AS (
    VALUES
        ('master_data_import_batches'),
        ('master_data_import_units'),
        ('master_data_import_command_outcomes'),
        ('command_idempotency_records'),
        ('school_people'),
        ('school_calendar_versions')
)
SELECT pg_catalog.format(
    'GRANT SELECT (xmin) ON TABLE public.%I TO campus_ai_audit_definer',
    table_name
)
FROM expected
WHERE pg_catalog.to_regclass('public.' || table_name) IS NOT NULL
\gexec

SELECT 'GRANT SELECT ('
       'school_id, id, action_code, actor_user_id, actor_person_id, actor_context_type, '
       'actor_assignment_id, actor_assignment_version, resource_type, resource_id, '
       'idempotency_key_hash, request_sha256, status, result_type, result_payload, '
       'created_at, updated_at'
       ') ON TABLE public.command_idempotency_records TO campus_ai_takeover_definer'
WHERE pg_catalog.to_regclass('public.command_idempotency_records') IS NOT NULL
\gexec
SELECT 'GRANT SELECT ('
       'school_id, idempotency_record_id, batch_id, action_code, status, batch_version, '
       'succeeded_units, failed_units, executed_units, unit_outcome_snapshots'
       ') ON TABLE public.master_data_import_command_outcomes '
       'TO campus_ai_takeover_definer'
WHERE pg_catalog.to_regclass('public.master_data_import_command_outcomes') IS NOT NULL
  AND pg_catalog.to_regprocedure(
      'app.read_master_data_import_succeeded_attempt(uuid,uuid,uuid)'
  ) IS NOT NULL
\gexec
SELECT 'GRANT SELECT (target_id, target_version, error_code, error_field_code) '
       'ON TABLE public.master_data_import_units TO campus_ai_takeover_definer'
WHERE pg_catalog.to_regprocedure(
    'app_private.populate_master_data_import_unit_outcome_snapshot()'
) IS NOT NULL
\gexec
SELECT 'GRANT UPDATE (status, result_type, result_payload, updated_at) '
       'ON TABLE public.command_idempotency_records TO campus_ai_takeover_definer'
WHERE pg_catalog.to_regclass('public.command_idempotency_records') IS NOT NULL
\gexec
SELECT 'GRANT SELECT ('
       'school_id, id, mode, source_kind, status, active_idempotency_record_id, '
       'active_action_code, planned_units, version'
       ') ON TABLE public.master_data_import_batches TO campus_ai_takeover_definer'
WHERE pg_catalog.to_regclass('public.master_data_import_batches') IS NOT NULL
\gexec
SELECT 'GRANT SELECT ('
       'school_id, id, batch_id, status, sequence, last_attempt_idempotency_record_id, '
       'last_attempt_completed, version'
       ') ON TABLE public.master_data_import_units TO campus_ai_takeover_definer'
WHERE pg_catalog.to_regclass('public.master_data_import_units') IS NOT NULL
\gexec
SELECT 'GRANT SELECT ('
       'school_id, id, batch_id, previous_idempotency_record_id, '
       'new_idempotency_record_id, original_actor_user_id, original_actor_person_id, '
       'original_actor_assignment_id, original_actor_assignment_version, '
       'new_actor_user_id, new_actor_person_id, new_actor_assignment_id, '
       'new_actor_assignment_version, request_id, batch_version_before, batch_version_after'
       ') ON TABLE public.master_data_import_takeovers TO campus_ai_takeover_definer'
WHERE pg_catalog.to_regclass('public.master_data_import_takeovers') IS NOT NULL
\gexec
SELECT 'GRANT SELECT (transferred_sequences, transferred_unit_versions) '
       'ON TABLE public.master_data_import_takeovers TO campus_ai_takeover_definer'
WHERE (
    SELECT pg_catalog.count(*) = 2
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'master_data_import_takeovers'
      AND column_name IN ('transferred_sequences', 'transferred_unit_versions')
)
\gexec
SELECT 'GRANT SELECT ('
       'school_id, id, person_id, semantic_role_code, valid_from, valid_to, status, version'
       ') ON TABLE public.staff_assignments TO campus_ai_takeover_definer'
WHERE pg_catalog.to_regclass('public.staff_assignments') IS NOT NULL
\gexec
SELECT 'GRANT UPDATE (id) ON TABLE public.staff_assignments TO campus_ai_takeover_definer'
WHERE pg_catalog.to_regclass('public.staff_assignments') IS NOT NULL
\gexec

SELECT 'GRANT SELECT ('
       'id, actor_user_id, school_id, actor_person_id, actor_assignment_id, '
       'actor_assignment_version, actor_context_type, idempotency_key_hash, event_type, '
       'target_type, target_id, request_id, reason'
       ') ON TABLE public.audit_logs TO campus_ai_takeover_definer'
WHERE pg_catalog.to_regclass('public.audit_logs') IS NOT NULL
\gexec

SELECT 'REVOKE ALL PRIVILEGES ON TABLE public.audit_logs FROM campus_ai_runtime CASCADE'
WHERE pg_catalog.to_regclass('public.audit_logs') IS NOT NULL
\gexec
SELECT 'REVOKE INSERT ('
       'actor_user_id, school_id, actor_person_id, actor_assignment_id, '
       'actor_assignment_version, actor_guardian_relationship_id, '
       'actor_guardian_relationship_version, actor_context_type, idempotency_key_hash, '
       'event_type, target_type, target_id, before_summary, after_summary, request_id, reason'
       ') ON TABLE public.audit_logs FROM campus_ai_runtime CASCADE'
WHERE pg_catalog.to_regclass('public.audit_logs') IS NOT NULL
\gexec
SELECT 'REVOKE ALL PRIVILEGES ON SEQUENCE public.audit_logs_id_seq '
       'FROM campus_ai_runtime CASCADE'
WHERE pg_catalog.to_regclass('public.audit_logs_id_seq') IS NOT NULL
\gexec
SELECT 'REVOKE ALL PRIVILEGES ON TABLE public.audit_logs '
       'FROM campus_ai_audit_definer CASCADE'
WHERE pg_catalog.to_regclass('public.audit_logs') IS NOT NULL
\gexec
SELECT 'GRANT INSERT ('
       'actor_user_id, school_id, actor_person_id, actor_assignment_id, '
       'actor_assignment_version, actor_guardian_relationship_id, '
       'actor_guardian_relationship_version, actor_context_type, idempotency_key_hash, '
       'event_type, target_type, target_id, before_summary, after_summary, request_id, reason'
       ') ON TABLE public.audit_logs TO campus_ai_audit_definer'
WHERE pg_catalog.to_regclass('public.audit_logs') IS NOT NULL
\gexec
SELECT 'GRANT SELECT (id) ON TABLE public.audit_logs TO campus_ai_audit_definer'
WHERE pg_catalog.to_regclass('public.audit_logs') IS NOT NULL
\gexec
SELECT 'REVOKE ALL PRIVILEGES ON SEQUENCE public.audit_logs_id_seq '
       'FROM campus_ai_audit_definer CASCADE'
WHERE pg_catalog.to_regclass('public.audit_logs_id_seq') IS NOT NULL
\gexec
SELECT 'GRANT USAGE ON SEQUENCE public.audit_logs_id_seq TO campus_ai_audit_definer'
WHERE pg_catalog.to_regclass('public.audit_logs_id_seq') IS NOT NULL
\gexec
SELECT 'REVOKE ALL PRIVILEGES ON TABLE public.outbox_messages '
       'FROM campus_ai_outbox_probe_definer CASCADE'
WHERE pg_catalog.to_regclass('public.outbox_messages') IS NOT NULL
\gexec
SELECT 'GRANT SELECT ('
       'id, school_id, event_key, event_type, aggregate_type, aggregate_id, aggregate_version'
       ') ON TABLE public.outbox_messages TO campus_ai_outbox_probe_definer'
WHERE pg_catalog.to_regclass('public.outbox_messages') IS NOT NULL
\gexec

-- SF5B never authorizes PUBLIC relation access.  Table ACL and independent
-- column ACL are separate PostgreSQL grants, so both are converged.
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC CASCADE;
WITH public_relation_columns AS (
    SELECT namespace.nspname AS schema_name,
           relation.relname AS table_name,
           pg_catalog.string_agg(
               pg_catalog.format('%I', attribute.attname),
               ', ' ORDER BY attribute.attnum
           ) AS column_list
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    JOIN pg_catalog.pg_attribute AS attribute ON attribute.attrelid = relation.oid
    WHERE namespace.nspname = 'public'
      AND relation.relkind IN ('r', 'p')
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
    GROUP BY namespace.nspname, relation.relname
), column_privilege(privilege_type) AS (
    VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('REFERENCES')
)
SELECT pg_catalog.format(
    'REVOKE %s (%s) ON TABLE %I.%I FROM PUBLIC CASCADE',
    privilege.privilege_type,
    relation.column_list,
    relation.schema_name,
    relation.table_name
)
FROM public_relation_columns AS relation
CROSS JOIN column_privilege AS privilege
\gexec

-- Fixed runtime closure: School Facts apply tables and every retired legacy
-- Repairs/Responsibility table stay unavailable even after rogue table/column grants.
WITH revision20_runtime_closed_table(table_name) AS (
    VALUES
        ('account_person_binding_versions'),
        ('account_person_bindings'),
        ('campus_location_aliases'),
        ('campus_location_versions'),
        ('campus_locations'),
        ('dorm_bed_versions'),
        ('dorm_beds'),
        ('dorm_room_profile_versions'),
        ('master_data_import_batches'),
        ('master_data_import_command_outcomes'),
        ('master_data_import_takeovers'),
        ('master_data_import_units'),
        ('official_contact_points'),
        ('organization_campus_relationships'),
        ('organization_unit_versions'),
        ('organization_units'),
        ('person_contact_points'),
        ('repair_responsibility_grants'),
        ('repair_scope_bindings'),
        ('repair_tickets'),
        ('responsibility_group_memberships'),
        ('responsibility_group_versions'),
        ('responsibility_groups'),
        ('responsibility_types'),
        ('school_fact_change_lineage'),
        ('school_fact_sources'),
        ('school_people'),
        ('school_person_names'),
        ('school_person_profile_versions'),
        ('school_policies'),
        ('school_policy_scopes'),
        ('school_policy_versions'),
        ('school_versions'),
        ('schools'),
        ('staff_assignment_versions'),
        ('staff_assignments'),
        ('student_bed_residences'),
        ('student_contact_relationships'),
        ('student_school_relationships'),
        ('students')
)
SELECT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON TABLE public.%I '
    'FROM campus_ai_runtime, campus_ai_school_fact_reader CASCADE',
    expected.table_name
)
FROM revision20_runtime_closed_table AS expected
WHERE pg_catalog.to_regclass(
    pg_catalog.format('public.%I', expected.table_name)
) IS NOT NULL
\gexec

WITH revision20_runtime_closed_table(table_name) AS (
    VALUES
        ('account_person_binding_versions'),
        ('account_person_bindings'),
        ('campus_location_aliases'),
        ('campus_location_versions'),
        ('campus_locations'),
        ('dorm_bed_versions'),
        ('dorm_beds'),
        ('dorm_room_profile_versions'),
        ('master_data_import_batches'),
        ('master_data_import_command_outcomes'),
        ('master_data_import_takeovers'),
        ('master_data_import_units'),
        ('official_contact_points'),
        ('organization_campus_relationships'),
        ('organization_unit_versions'),
        ('organization_units'),
        ('person_contact_points'),
        ('repair_responsibility_grants'),
        ('repair_scope_bindings'),
        ('repair_tickets'),
        ('responsibility_group_memberships'),
        ('responsibility_group_versions'),
        ('responsibility_groups'),
        ('responsibility_types'),
        ('school_fact_change_lineage'),
        ('school_fact_sources'),
        ('school_people'),
        ('school_person_names'),
        ('school_person_profile_versions'),
        ('school_policies'),
        ('school_policy_scopes'),
        ('school_policy_versions'),
        ('school_versions'),
        ('schools'),
        ('staff_assignment_versions'),
        ('staff_assignments'),
        ('student_bed_residences'),
        ('student_contact_relationships'),
        ('student_school_relationships'),
        ('students')
), protected_relation_columns AS (
    SELECT relation.relname AS table_name,
           pg_catalog.string_agg(
               pg_catalog.format('%I', attribute.attname),
               ', ' ORDER BY attribute.attnum
           ) AS column_list
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    JOIN pg_catalog.pg_attribute AS attribute ON attribute.attrelid = relation.oid
    JOIN revision20_runtime_closed_table AS expected
      ON expected.table_name = relation.relname
    WHERE namespace.nspname = 'public'
      AND relation.relkind IN ('r', 'p')
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
    GROUP BY relation.relname
), column_privilege(privilege_type) AS (
    VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('REFERENCES')
)
SELECT pg_catalog.format(
    'REVOKE %s (%s) ON TABLE public.%I '
    'FROM campus_ai_runtime, campus_ai_school_fact_reader CASCADE',
    privilege.privilege_type,
    relation.column_list,
    relation.table_name
)
FROM protected_relation_columns AS relation
CROSS JOIN column_privilege AS privilege
\gexec

-- Product entry retirement is stronger than ACL cleanup: remove every legacy
-- permissive policy, then reconstruct the one tenant anchor guard only.
WITH retired_repair_table(table_name) AS (
    VALUES
        ('repair_responsibility_grants'),
        ('repair_scope_bindings'),
        ('repair_tickets'),
        ('responsibility_group_memberships'),
        ('responsibility_group_versions'),
        ('responsibility_groups'),
        ('responsibility_types')
)
SELECT pg_catalog.format(
    'ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',
    expected.table_name
)
FROM retired_repair_table AS expected
WHERE pg_catalog.to_regclass(
    pg_catalog.format('public.%I', expected.table_name)
) IS NOT NULL
  AND pg_catalog.to_regclass('public.school_database_anchor') IS NOT NULL
  AND pg_catalog.to_regprocedure('app.require_school_database_anchor()') IS NOT NULL
\gexec
WITH retired_repair_table(table_name) AS (
    VALUES
        ('repair_responsibility_grants'),
        ('repair_scope_bindings'),
        ('repair_tickets'),
        ('responsibility_group_memberships'),
        ('responsibility_group_versions'),
        ('responsibility_groups'),
        ('responsibility_types')
)
SELECT pg_catalog.format(
    'ALTER TABLE public.%I FORCE ROW LEVEL SECURITY',
    expected.table_name
)
FROM retired_repair_table AS expected
WHERE pg_catalog.to_regclass(
    pg_catalog.format('public.%I', expected.table_name)
) IS NOT NULL
  AND pg_catalog.to_regclass('public.school_database_anchor') IS NOT NULL
  AND pg_catalog.to_regprocedure('app.require_school_database_anchor()') IS NOT NULL
\gexec
WITH retired_repair_table(table_name) AS (
    VALUES
        ('repair_responsibility_grants'),
        ('repair_scope_bindings'),
        ('repair_tickets'),
        ('responsibility_group_memberships'),
        ('responsibility_group_versions'),
        ('responsibility_groups'),
        ('responsibility_types')
)
SELECT pg_catalog.format(
    'DROP POLICY %I ON public.%I',
    policy.policyname,
    policy.tablename
)
FROM pg_catalog.pg_policies AS policy
JOIN retired_repair_table AS expected ON expected.table_name = policy.tablename
WHERE policy.schemaname = 'public'
  AND pg_catalog.to_regclass('public.school_database_anchor') IS NOT NULL
  AND pg_catalog.to_regprocedure('app.require_school_database_anchor()') IS NOT NULL
\gexec
WITH retired_repair_table(table_name) AS (
    VALUES
        ('repair_responsibility_grants'),
        ('repair_scope_bindings'),
        ('repair_tickets'),
        ('responsibility_group_memberships'),
        ('responsibility_group_versions'),
        ('responsibility_groups'),
        ('responsibility_types')
)
SELECT pg_catalog.format(
    'CREATE POLICY sf5b_anchor_guard ON public.%I '
    'AS RESTRICTIVE FOR ALL TO PUBLIC '
    'USING (CURRENT_USER IN (''campus_ai_db_owner'', ''campus_ai_migration'') '
    'OR (school_id = app.require_school_database_anchor())) '
    'WITH CHECK (CURRENT_USER IN (''campus_ai_db_owner'', ''campus_ai_migration'') '
    'OR (school_id = app.require_school_database_anchor()))',
    expected.table_name
)
FROM retired_repair_table AS expected
WHERE pg_catalog.to_regclass(
    pg_catalog.format('public.%I', expected.table_name)
) IS NOT NULL
  AND pg_catalog.to_regclass('public.school_database_anchor') IS NOT NULL
  AND pg_catalog.to_regprocedure('app.require_school_database_anchor()') IS NOT NULL
\gexec
-- SF5B isolated database principals.  This block runs last so a repeat
-- converges the principals and reconstructs the fixed revision-20 ACL allowlists.
SELECT pg_catalog.format(
    'ALTER ROLE %I IN DATABASE %I RESET role',
    application_role,
    :'target_database'
)
FROM pg_catalog.unnest(ARRAY[
    'campus_ai_school_fact_writer',
    'campus_ai_school_fact_reader',
    'campus_ai_identity_definer'
]) AS application_roles(application_role)
\gexec

SELECT pg_catalog.format('REVOKE %I FROM %I CASCADE', parent.rolname, member.rolname)
FROM pg_catalog.pg_auth_members AS membership
JOIN pg_catalog.pg_roles AS parent ON parent.oid = membership.roleid
JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member
WHERE parent.rolname IN (
    'campus_ai_school_fact_writer',
    'campus_ai_school_fact_reader',
    'campus_ai_identity_definer'
)
OR member.rolname IN (
    'campus_ai_school_fact_writer',
    'campus_ai_school_fact_reader',
    'campus_ai_identity_definer'
)
\gexec

REASSIGN OWNED BY campus_ai_school_fact_writer TO campus_ai_db_owner;
REASSIGN OWNED BY campus_ai_school_fact_reader TO campus_ai_db_owner;

SELECT pg_catalog.format(
    'REVOKE ALL PRIVILEGES ON DATABASE %I '
    'FROM campus_ai_school_fact_writer, campus_ai_school_fact_reader, '
    'campus_ai_identity_definer CASCADE',
    :'target_database'
)
\gexec
SELECT pg_catalog.format(
    'GRANT CONNECT ON DATABASE %I '
    'TO campus_ai_school_fact_writer, campus_ai_school_fact_reader',
    :'target_database'
)
\gexec

REVOKE ALL PRIVILEGES ON SCHEMA public
    FROM campus_ai_school_fact_writer, campus_ai_school_fact_reader,
         campus_ai_identity_definer CASCADE;
GRANT USAGE ON SCHEMA public
    TO campus_ai_school_fact_writer, campus_ai_school_fact_reader,
       campus_ai_identity_definer;

SELECT 'REVOKE ALL PRIVILEGES ON SCHEMA app '
       'FROM campus_ai_school_fact_writer, campus_ai_school_fact_reader, '
       'campus_ai_identity_definer CASCADE'
WHERE pg_catalog.to_regnamespace('app') IS NOT NULL
\gexec
SELECT 'GRANT USAGE ON SCHEMA app '
       'TO campus_ai_school_fact_writer, campus_ai_school_fact_reader, '
       'campus_ai_identity_definer'
WHERE pg_catalog.to_regnamespace('app') IS NOT NULL
\gexec


-- Clear every public relation ACL, including independent column ACLs, before
-- reconstructing the fixed revision-20 matrix below.  Catalog enumeration is
-- used only to revoke all columns; no existing grant is treated as authority.
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public
    FROM campus_ai_school_fact_writer, campus_ai_school_fact_reader,
         campus_ai_identity_definer CASCADE;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public
    FROM campus_ai_school_fact_writer, campus_ai_school_fact_reader,
         campus_ai_identity_definer CASCADE;

WITH public_relation_columns AS (
    SELECT namespace.nspname AS schema_name,
           relation.relname AS table_name,
           pg_catalog.string_agg(
               pg_catalog.format('%I', attribute.attname),
               ', ' ORDER BY attribute.attnum
           ) AS column_list
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    JOIN pg_catalog.pg_attribute AS attribute ON attribute.attrelid = relation.oid
    WHERE namespace.nspname = 'public'
      AND relation.relkind IN ('r', 'p')
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
    GROUP BY namespace.nspname, relation.relname
), column_privilege(privilege_type) AS (
    VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('REFERENCES')
)
SELECT pg_catalog.format(
    'REVOKE %s (%s) ON TABLE %I.%I '
    'FROM campus_ai_school_fact_writer, campus_ai_school_fact_reader, '
    'campus_ai_identity_definer CASCADE',
    privilege.privilege_type,
    relation.column_list,
    relation.schema_name,
    relation.table_name
)
FROM public_relation_columns AS relation
CROSS JOIN column_privilege AS privilege
\gexec

-- Fixed Writer table allowlist: never derive it from the current ACL catalog.
WITH revision20_writer_table(table_name) AS (
    VALUES
        ('schools'),
        ('school_versions'),
        ('school_people'),
        ('school_person_names'),
        ('school_person_profile_versions'),
        ('students'),
        ('student_school_relationships'),
        ('account_person_bindings'),
        ('account_person_binding_versions'),
        ('organization_units'),
        ('organization_unit_versions'),
        ('organization_campus_relationships'),
        ('staff_assignments'),
        ('staff_assignment_versions'),
        ('campus_locations'),
        ('campus_location_versions'),
        ('campus_location_aliases'),
        ('dorm_room_profile_versions'),
        ('dorm_beds'),
        ('dorm_bed_versions'),
        ('student_bed_residences'),
        ('person_contact_points'),
        ('student_contact_relationships'),
        ('official_contact_points'),
        ('school_policies'),
        ('school_policy_scopes'),
        ('school_policy_versions'),
        ('school_fact_sources'),
        ('master_data_import_batches'),
        ('master_data_import_units')
)
SELECT pg_catalog.format(
    'GRANT SELECT, INSERT, UPDATE ON TABLE public.%I '
    'TO campus_ai_school_fact_writer',
    expected.table_name
)
FROM revision20_writer_table AS expected
WHERE pg_catalog.to_regclass(
    pg_catalog.format('public.%I', expected.table_name)
) IS NOT NULL
\gexec

SELECT 'GRANT INSERT (id, school_id, action_code, actor_user_id, '
       'actor_person_id, actor_context_type, actor_context_key, '
       'actor_assignment_id, actor_assignment_version, resource_type, '
       'resource_id, idempotency_key_hash, request_sha256, status, '
       'result_type, result_payload) ON TABLE public.command_idempotency_records '
       'TO campus_ai_school_fact_writer'
WHERE pg_catalog.to_regclass('public.command_idempotency_records') IS NOT NULL
\gexec
SELECT 'GRANT INSERT (actor_user_id, school_id, actor_person_id, '
       'actor_assignment_id, actor_assignment_version, actor_context_type, '
       'idempotency_key_hash, event_type, target_type, target_id, '
       'before_summary, after_summary, request_id, reason) '
       'ON TABLE public.audit_logs TO campus_ai_school_fact_writer'
WHERE pg_catalog.to_regclass('public.audit_logs') IS NOT NULL
\gexec
SELECT 'GRANT INSERT ON TABLE public.master_data_import_command_outcomes '
       'TO campus_ai_school_fact_writer'
WHERE pg_catalog.to_regclass('public.master_data_import_command_outcomes') IS NOT NULL
\gexec
SELECT 'GRANT SELECT, INSERT ON TABLE public.school_fact_change_lineage '
       'TO campus_ai_school_fact_writer'
WHERE pg_catalog.to_regclass('public.school_fact_change_lineage') IS NOT NULL
\gexec
SELECT 'GRANT SELECT ON TABLE public.alembic_version '
       'TO campus_ai_runtime, campus_ai_school_fact_writer, campus_ai_school_fact_reader'
WHERE pg_catalog.to_regclass('public.alembic_version') IS NOT NULL
\gexec
SELECT 'GRANT USAGE ON SEQUENCE public.audit_logs_id_seq '
       'TO campus_ai_school_fact_writer'
WHERE pg_catalog.to_regclass('public.audit_logs_id_seq') IS NOT NULL
\gexec

-- Fixed Reader relation allowlist.
WITH revision20_reader_table(table_name) AS (
    VALUES
        ('governance_duties'),
        ('governance_duty_versions'),
        ('governance_scopes'),
        ('governance_scope_versions'),
        ('governance_grants'),
        ('governance_grant_versions'),
        ('governance_assignment_epochs'),
        ('school_people'),
        ('organization_units'),
        ('organization_unit_versions'),
        ('staff_assignments'),
        ('staff_assignment_versions'),
        ('campus_locations'),
        ('campus_location_versions'),
        ('official_contact_points')
)
SELECT pg_catalog.format(
    'GRANT SELECT ON TABLE public.%I TO campus_ai_school_fact_reader',
    expected.table_name
)
FROM revision20_reader_table AS expected
WHERE pg_catalog.to_regclass(
    pg_catalog.format('public.%I', expected.table_name)
) IS NOT NULL
\gexec

-- Fixed Identity definer column allowlist.
SELECT 'GRANT SELECT (id, provider_code, provider_tenant_key, external_subject), '
       'UPDATE (provider_code, provider_tenant_key, external_subject, updated_at) '
       'ON TABLE public.users TO campus_ai_identity_definer'
WHERE pg_catalog.to_regclass('public.users') IS NOT NULL
\gexec

-- The inactive-school reactivation bridge is owned by the context definer but
-- callable only by the isolated Writer.  Converge its two-table column ACL and
-- anchor-only INSERT policy without granting a generic context or base-table path.
WITH reactivation_prepare_table(table_name) AS (
    VALUES
        ('master_data_import_batches'),
        ('master_data_import_units')
)
SELECT pg_catalog.format(
    'REVOKE INSERT ON TABLE public.%I FROM campus_ai_context_definer CASCADE',
    expected.table_name
)
FROM reactivation_prepare_table AS expected
WHERE pg_catalog.to_regclass(
    pg_catalog.format('public.%I', expected.table_name)
) IS NOT NULL
  AND pg_catalog.to_regprocedure(
      'app.school_fact_school_reactivation_prepare(uuid,integer,uuid,uuid,bigint,bigint,uuid,text,text,timestamptz,text,text,bigint,timestamptz,bigint,text,text,uuid,uuid,text,text,timestamptz,text,text,text,text,text,text,integer)'
  ) IS NOT NULL
\gexec
WITH reactivation_prepare_table(table_name) AS (
    VALUES
        ('master_data_import_batches'),
        ('master_data_import_units')
), relation_columns AS (
    SELECT relation.relname AS table_name,
           pg_catalog.string_agg(
               pg_catalog.format('%I', attribute.attname),
               ', ' ORDER BY attribute.attnum
           ) AS column_list
      FROM pg_catalog.pg_class AS relation
      JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
      JOIN pg_catalog.pg_attribute AS attribute ON attribute.attrelid = relation.oid
      JOIN reactivation_prepare_table AS expected ON expected.table_name = relation.relname
     WHERE namespace.nspname = 'public'
       AND attribute.attnum > 0
       AND NOT attribute.attisdropped
     GROUP BY relation.relname
)
SELECT pg_catalog.format(
    'REVOKE INSERT (%s) ON TABLE public.%I FROM campus_ai_context_definer CASCADE',
    relation.column_list,
    relation.table_name
)
FROM relation_columns AS relation
WHERE pg_catalog.to_regprocedure(
    'app.school_fact_school_reactivation_prepare(uuid,integer,uuid,uuid,bigint,bigint,uuid,text,text,timestamptz,text,text,bigint,timestamptz,bigint,text,text,uuid,uuid,text,text,timestamptz,text,text,text,text,text,text,integer)'
) IS NOT NULL
\gexec
SELECT 'GRANT SELECT (source_kind, source_ref, filename_summary, source_size_bytes, '
       'worksheet_names, mapping_version, mapping_payload, confirmation_card_sha256, '
       'confirmed_at, created_at, version) '
       'ON TABLE public.master_data_import_batches TO campus_ai_context_definer'
WHERE pg_catalog.to_regprocedure(
    'app.school_fact_school_reactivation_prepare(uuid,integer,uuid,uuid,bigint,bigint,uuid,text,text,timestamptz,text,text,bigint,timestamptz,bigint,text,text,uuid,uuid,text,text,timestamptz,text,text,text,text,text,text,integer)'
) IS NOT NULL
\gexec
SELECT 'GRANT SELECT (row_number, soft_anomalies, initial_soft_anomalies, '
       'decision_reason, error_code, error_field_code, last_attempt_idempotency_record_id, '
       'last_attempt_completed, version) '
       'ON TABLE public.master_data_import_units TO campus_ai_context_definer'
WHERE pg_catalog.to_regprocedure(
    'app.school_fact_school_reactivation_prepare(uuid,integer,uuid,uuid,bigint,bigint,uuid,text,text,timestamptz,text,text,bigint,timestamptz,bigint,text,text,uuid,uuid,text,text,timestamptz,text,text,text,text,text,text,integer)'
) IS NOT NULL
\gexec
SELECT 'GRANT INSERT (id, school_id, mode, source_kind, source_ref, source_sha256, '
       'filename_summary, source_size_bytes, worksheet_names, mapping_version, '
       'mapping_payload, status, planned_units, succeeded_units, failed_units, '
       'pending_units, confirmation_card_sha256, request_id, idempotency_key_hash, '
       'plan_sha256, base_fact_version, target_fact_version, confirmation_token_sha256, '
       'confirmation_expires_at, confirmation_session_sha256, staged_by_user_id, '
       'staged_by_person_id, staged_by_assignment_id, staged_assignment_version, '
       'staged_authorization_version) ON TABLE public.master_data_import_batches '
       'TO campus_ai_context_definer'
WHERE pg_catalog.to_regprocedure(
    'app.school_fact_school_reactivation_prepare(uuid,integer,uuid,uuid,bigint,bigint,uuid,text,text,timestamptz,text,text,bigint,timestamptz,bigint,text,text,uuid,uuid,text,text,timestamptz,text,text,text,text,text,text,integer)'
) IS NOT NULL
\gexec
SELECT 'GRANT INSERT (id, school_id, batch_id, sequence, row_number, unit_kind, status, '
       'payload, payload_sha256, soft_anomalies, initial_soft_anomalies) '
       'ON TABLE public.master_data_import_units TO campus_ai_context_definer'
WHERE pg_catalog.to_regprocedure(
    'app.school_fact_school_reactivation_prepare(uuid,integer,uuid,uuid,bigint,bigint,uuid,text,text,timestamptz,text,text,bigint,timestamptz,bigint,text,text,uuid,uuid,text,text,timestamptz,text,text,text,text,text,text,integer)'
) IS NOT NULL
\gexec
WITH reactivation_prepare_table(table_name) AS (
    VALUES
        ('master_data_import_batches'),
        ('master_data_import_units')
)
SELECT pg_catalog.format(
    'DROP POLICY IF EXISTS sf5b_school_reactivation_prepare_insert ON public.%I',
    expected.table_name
)
FROM reactivation_prepare_table AS expected
WHERE pg_catalog.to_regclass(
    pg_catalog.format('public.%I', expected.table_name)
) IS NOT NULL
  AND pg_catalog.to_regprocedure(
      'app.school_fact_school_reactivation_prepare(uuid,integer,uuid,uuid,bigint,bigint,uuid,text,text,timestamptz,text,text,bigint,timestamptz,bigint,text,text,uuid,uuid,text,text,timestamptz,text,text,text,text,text,text,integer)'
  ) IS NOT NULL
\gexec
WITH reactivation_prepare_table(table_name) AS (
    VALUES
        ('master_data_import_batches'),
        ('master_data_import_units')
)
SELECT pg_catalog.format(
    'CREATE POLICY sf5b_school_reactivation_prepare_insert ON public.%I '
    'AS PERMISSIVE FOR INSERT TO campus_ai_context_definer '
    'WITH CHECK (school_id = app.require_school_database_anchor())',
    expected.table_name
)
FROM reactivation_prepare_table AS expected
WHERE pg_catalog.to_regclass(
    pg_catalog.format('public.%I', expected.table_name)
) IS NOT NULL
  AND pg_catalog.to_regprocedure(
      'app.school_fact_school_reactivation_prepare(uuid,integer,uuid,uuid,bigint,bigint,uuid,text,text,timestamptz,text,text,bigint,timestamptz,bigint,text,text,uuid,uuid,text,text,timestamptz,text,text,text,text,text,text,integer)'
  ) IS NOT NULL
  AND pg_catalog.to_regprocedure('app.require_school_database_anchor()') IS NOT NULL
\gexec

-- The Writer-only cleanup bridge may remove only expired, unconsumed
-- School Facts preparations for the bound school.  The definer receives no
-- generic Writer DELETE path and no caller-controlled cleanup timestamp.
WITH expired_preparation_cleanup_table(table_name) AS (
    VALUES
        ('master_data_import_batches'),
        ('master_data_import_units')
)
SELECT pg_catalog.format(
    'REVOKE DELETE ON TABLE public.%I FROM campus_ai_context_definer CASCADE',
    expected.table_name
)
FROM expired_preparation_cleanup_table AS expected
WHERE pg_catalog.to_regclass(
    pg_catalog.format('public.%I', expected.table_name)
) IS NOT NULL
\gexec
WITH expired_preparation_cleanup_table(table_name) AS (
    VALUES
        ('master_data_import_batches'),
        ('master_data_import_units')
)
SELECT pg_catalog.format(
    'GRANT DELETE ON TABLE public.%I TO campus_ai_context_definer',
    expected.table_name
)
FROM expired_preparation_cleanup_table AS expected
WHERE pg_catalog.to_regclass(
    pg_catalog.format('public.%I', expected.table_name)
) IS NOT NULL
  AND pg_catalog.to_regprocedure(
      'app.purge_expired_school_fact_preparations()'
  ) IS NOT NULL
\gexec
SELECT 'GRANT SELECT (id, school_id, mode, status, confirmation_consumed_at, '
       'confirmation_expires_at) ON TABLE public.master_data_import_batches '
       'TO campus_ai_context_definer'
WHERE pg_catalog.to_regprocedure(
    'app.purge_expired_school_fact_preparations()'
) IS NOT NULL
\gexec
SELECT 'GRANT SELECT (school_id, batch_id, status, target_id, target_version, '
       'last_attempt_idempotency_record_id, last_attempt_completed) '
       'ON TABLE public.master_data_import_units TO campus_ai_context_definer'
WHERE pg_catalog.to_regprocedure(
    'app.purge_expired_school_fact_preparations()'
) IS NOT NULL
\gexec
WITH expired_preparation_cleanup_table(table_name) AS (
    VALUES
        ('master_data_import_batches'),
        ('master_data_import_units')
)
SELECT pg_catalog.format(
    'DROP POLICY IF EXISTS sf5b_expired_preparation_cleanup_read ON public.%I',
    expected.table_name
)
FROM expired_preparation_cleanup_table AS expected
WHERE pg_catalog.to_regclass(
    pg_catalog.format('public.%I', expected.table_name)
) IS NOT NULL
\gexec
WITH expired_preparation_cleanup_table(table_name) AS (
    VALUES
        ('master_data_import_batches'),
        ('master_data_import_units')
)
SELECT pg_catalog.format(
    'DROP POLICY IF EXISTS sf5b_expired_preparation_cleanup_delete ON public.%I',
    expected.table_name
)
FROM expired_preparation_cleanup_table AS expected
WHERE pg_catalog.to_regclass(
    pg_catalog.format('public.%I', expected.table_name)
) IS NOT NULL
\gexec
SELECT 'CREATE POLICY sf5b_expired_preparation_cleanup_read '
       'ON public.master_data_import_batches AS PERMISSIVE FOR SELECT '
       'TO campus_ai_context_definer USING ('
       'school_id = app.require_school_context() '
       'AND mode = ''canonical_atomic_changes'' '
       'AND status = ''ready_for_confirmation'' '
       'AND confirmation_consumed_at IS NULL '
       'AND confirmation_expires_at <= pg_catalog.statement_timestamp())'
WHERE pg_catalog.to_regprocedure(
    'app.purge_expired_school_fact_preparations()'
) IS NOT NULL
  AND pg_catalog.to_regprocedure('app.require_school_context()') IS NOT NULL
\gexec
SELECT 'CREATE POLICY sf5b_expired_preparation_cleanup_delete '
       'ON public.master_data_import_batches AS PERMISSIVE FOR DELETE '
       'TO campus_ai_context_definer USING ('
       'school_id = app.require_school_context() '
       'AND mode = ''canonical_atomic_changes'' '
       'AND status = ''ready_for_confirmation'' '
       'AND confirmation_consumed_at IS NULL '
       'AND confirmation_expires_at <= pg_catalog.statement_timestamp())'
WHERE pg_catalog.to_regprocedure(
    'app.purge_expired_school_fact_preparations()'
) IS NOT NULL
  AND pg_catalog.to_regprocedure('app.require_school_context()') IS NOT NULL
\gexec
SELECT 'CREATE POLICY sf5b_expired_preparation_cleanup_read '
       'ON public.master_data_import_units AS PERMISSIVE FOR SELECT '
       'TO campus_ai_context_definer USING ('
       'school_id = app.require_school_context() '
       'AND status = ''ready'' '
       'AND target_id IS NULL AND target_version IS NULL '
       'AND last_attempt_idempotency_record_id IS NULL '
       'AND last_attempt_completed IS NULL '
       'AND EXISTS (SELECT 1 FROM public.master_data_import_batches AS batch '
       'WHERE batch.id = master_data_import_units.batch_id '
       'AND batch.school_id = master_data_import_units.school_id '
       'AND batch.mode = ''canonical_atomic_changes'' '
       'AND batch.status = ''ready_for_confirmation'' '
       'AND batch.confirmation_consumed_at IS NULL '
       'AND batch.confirmation_expires_at <= pg_catalog.statement_timestamp()))'
WHERE pg_catalog.to_regprocedure(
    'app.purge_expired_school_fact_preparations()'
) IS NOT NULL
  AND pg_catalog.to_regprocedure('app.require_school_context()') IS NOT NULL
\gexec
SELECT 'CREATE POLICY sf5b_expired_preparation_cleanup_delete '
       'ON public.master_data_import_units AS PERMISSIVE FOR DELETE '
       'TO campus_ai_context_definer USING ('
       'school_id = app.require_school_context() '
       'AND status = ''ready'' '
       'AND target_id IS NULL AND target_version IS NULL '
       'AND last_attempt_idempotency_record_id IS NULL '
       'AND last_attempt_completed IS NULL '
       'AND EXISTS (SELECT 1 FROM public.master_data_import_batches AS batch '
       'WHERE batch.id = master_data_import_units.batch_id '
       'AND batch.school_id = master_data_import_units.school_id '
       'AND batch.mode = ''canonical_atomic_changes'' '
       'AND batch.status = ''ready_for_confirmation'' '
       'AND batch.confirmation_consumed_at IS NULL '
       'AND batch.confirmation_expires_at <= pg_catalog.statement_timestamp()))'
WHERE pg_catalog.to_regprocedure(
    'app.purge_expired_school_fact_preparations()'
) IS NOT NULL
  AND pg_catalog.to_regprocedure('app.require_school_context()') IS NOT NULL
\gexec
-- SF5B closes the legacy ordinary-takeover Writer path.  This final block is
-- deliberately after every historical allowlist so repeat/rogue reconciliation
-- cannot restore runtime DML or the old SECURITY DEFINER mutator.
SELECT 'REVOKE ALL PRIVILEGES ON TABLE public.master_data_import_takeovers '
       'FROM campus_ai_runtime, campus_ai_school_fact_reader CASCADE'
WHERE pg_catalog.to_regclass('public.master_data_import_takeovers') IS NOT NULL
\gexec
DO $sf5b_runtime_writer_close$
DECLARE
    v_signature text;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'app.lock_master_data_import_takeover_source(uuid,uuid,uuid)',
        'app.lock_master_data_import_takeover_assignment(uuid,uuid,uuid,uuid)',
        'app.supersede_master_data_import_takeover_source(uuid,uuid,uuid,text,bigint)'
    ]
    LOOP
        IF pg_catalog.to_regprocedure(v_signature) IS NOT NULL THEN
            EXECUTE pg_catalog.format(
                'REVOKE ALL PRIVILEGES ON FUNCTION %s FROM campus_ai_runtime CASCADE',
                v_signature
            );
        END IF;
    END LOOP;
END
$sf5b_runtime_writer_close$;

-- F1 schema21 additions remain absent on the untouched identity/schema20 instance.
DO $f1_capabilities$
BEGIN
    IF pg_catalog.to_regclass('public.school_fact_initializations') IS NOT NULL THEN
        REVOKE ALL ON public.school_fact_initializations FROM PUBLIC, campus_ai_runtime, campus_ai_school_fact_writer, campus_ai_school_fact_reader;
        GRANT SELECT, INSERT, UPDATE ON public.school_fact_initializations TO campus_ai_migration;
        GRANT SELECT (id,school_id,source_kind,created_at) ON public.school_fact_sources TO campus_ai_school_fact_reader;
        GRANT SELECT (id,school_id,batch_id,fact_kind,fact_source_key,operation,target_id,target_version,created_at) ON public.school_fact_change_lineage TO campus_ai_school_fact_reader;
        GRANT EXECUTE ON FUNCTION app.lock_existing_identity_user_id(text,text,text) TO campus_ai_migration, campus_ai_db_owner;
        GRANT EXECUTE ON FUNCTION app.require_school_context() TO campus_ai_db_owner;
        REVOKE ALL ON FUNCTION app.lock_school_fact_governance_epoch(uuid,uuid) FROM PUBLIC, campus_ai_runtime, campus_ai_school_fact_reader, campus_ai_migration;
        GRANT EXECUTE ON FUNCTION app.lock_school_fact_governance_epoch(uuid,uuid) TO campus_ai_school_fact_writer;
        DROP POLICY IF EXISTS f1_reader_source ON public.school_fact_sources;
        CREATE POLICY f1_reader_source ON public.school_fact_sources FOR SELECT
          TO campus_ai_school_fact_reader USING (school_id = app.require_school_database_anchor());
        DROP POLICY IF EXISTS f1_reader_source ON public.school_fact_change_lineage;
        CREATE POLICY f1_reader_source ON public.school_fact_change_lineage FOR SELECT
          TO campus_ai_school_fact_reader USING (school_id = app.require_school_database_anchor());
        REVOKE ALL ON FUNCTION app.school_fact_current_version(uuid)
          FROM PUBLIC, campus_ai_runtime, campus_ai_school_fact_writer, campus_ai_migration;
        GRANT EXECUTE ON FUNCTION app.school_fact_current_version(uuid)
          TO campus_ai_school_fact_reader;
        GRANT SELECT (source_key) ON public.campus_locations TO campus_ai_school_fact_reader;
        REVOKE ALL ON FUNCTION app.require_initial_publication_window(uuid,uuid,uuid,text)
          FROM PUBLIC, campus_ai_runtime, campus_ai_school_fact_reader, campus_ai_migration;
        GRANT EXECUTE ON FUNCTION app.require_initial_publication_window(uuid,uuid,uuid,text)
          TO campus_ai_school_fact_writer;
    END IF;
END
$f1_capabilities$;

-- Revision 22 grants only narrow Reader judgments, preserving legacy schema20.
-- The general temporal functions remain unavailable to the Reader role.
DO $f1_reader_authority$
DECLARE
    v_signature text;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'app.school_fact_reader_assignment_terminal(uuid,uuid,bigint)',
        'app.school_fact_reader_identity_root_is_effective_at(uuid,text,uuid,timestamptz)'
    ]
    LOOP
        IF pg_catalog.to_regprocedure(v_signature) IS NOT NULL THEN
            EXECUTE pg_catalog.format(
                'REVOKE ALL PRIVILEGES ON FUNCTION %s '
                'FROM PUBLIC, campus_ai_runtime, campus_ai_school_fact_writer, '
                'campus_ai_migration, campus_ai_db_owner, campus_ai_identity_definer, '
                'campus_ai_school_fact_reader CASCADE',
                v_signature
            );
            EXECUTE pg_catalog.format(
                'GRANT EXECUTE ON FUNCTION %s TO campus_ai_school_fact_reader',
                v_signature
            );
        END IF;
    END LOOP;
    -- Only schema22 has both wrappers. Keep schema20/21 policies untouched.
    IF pg_catalog.to_regprocedure(
        'app.school_fact_reader_assignment_terminal(uuid,uuid,bigint)'
    ) IS NOT NULL AND pg_catalog.to_regprocedure(
        'app.school_fact_reader_identity_root_is_effective_at(uuid,text,uuid,timestamptz)'
    ) IS NOT NULL THEN
        ALTER POLICY tenant_select ON public.school_fact_change_lineage
            TO campus_ai_runtime;
    END IF;
END
$f1_reader_authority$;

COMMIT;
SQL
