from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from sqlalchemy import text

from app.db.rls_context import set_local_execution_context
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.school_facts.application import SchoolFactChangeService
from tests.master_data.conftest import PostgresRoles
from tests.school_facts.test_checkpoint2_lifecycle_red import (
    _confirm,
    _plan,
    _terminal_change,
)
from tests.school_facts.test_sf2_transaction import NOW, context_for, sf2_environment

API_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = API_ROOT / "alembic/versions/20260819_15_school_facts_schema.py"
SEMANTICS_MIGRATION_PATH = API_ROOT / "alembic/versions/20260821_18_school_fact_semantics.py"
SF5B_MIGRATION_PATH = API_ROOT / "alembic/versions/20260824_20_school_database_boundary.py"
CURRENT_HEAD = "20260824_20"
REVISION_19 = "20260823_19"
DOWNGRADE_BLOCKED_MESSAGE = "SF1 authoritative school facts exist; downgrade is unsafe"
SEMANTICS_DOWNGRADE_BLOCKED_MESSAGE = "revision 18 contains facts not representable by revision 17"
RESIDENCE_UPGRADE_BLOCKED_MESSAGE = "revision 18 cannot discard populated Residence self-FK data"
STUDENT_CAMPUS_UPGRADE_BLOCKED_MESSAGE = "revision 18 requires an explicit student campus backfill"
INACTIVE_CAMPUS_RESIDENCE_UPGRADE_BLOCKED_MESSAGE = (
    "revision 18 cannot install with effective Residence below inactive location ancestry"
)
STRUCTURE_HISTORY_UPGRADE_BLOCKED_MESSAGE = (
    "revision 18 cannot reconstruct ambiguous location or organization structure history"
)
TERMINAL_DATE_UPGRADE_BLOCKED_MESSAGE = (
    "revision 18 cannot reconstruct the original planned terminal date after a rev17 revoke"
)
WASHED_TERMINAL_UPGRADE_BLOCKED_MESSAGE = (
    "revision 18 cannot grandfather a rev17 terminal fact reopened by ordinary upsert"
)
LEGACY_SCHOOL_UPGRADE_BLOCKED_MESSAGE = (
    "revision 18 cannot reconstruct an ambiguous legacy school content history"
)
RELATIONSHIP_CONTENT_UPGRADE_BLOCKED_MESSAGE = (
    "revision 18 cannot reconstruct ambiguous assignment or login mapping content history"
)


def _migration_environment(postgres_roles: PostgresRoles) -> dict[str, str]:
    migration = postgres_roles.migration_connection_parameters
    environment = os.environ.copy()
    environment.update(
        {
            "POSTGRES_MIGRATION_HOST": str(migration["host"]),
            "POSTGRES_MIGRATION_PORT": str(migration["port"]),
            "POSTGRES_MIGRATION_DB": str(migration["dbname"]),
            "POSTGRES_MIGRATION_USER": str(migration["user"]),
            "POSTGRES_MIGRATION_PASSWORD": str(migration["password"]),
            "POSTGRES_MIGRATION_CONTEXT_SIGNING_KEY": (
                "school-facts-schema-context-signing-key-32-bytes"
            ),
        }
    )
    return environment


def _run_alembic(
    postgres_roles: PostgresRoles,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=API_ROOT,
        env=_migration_environment(postgres_roles),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )


def _context_definer_relation_acl(
    postgres_roles: PostgresRoles,
) -> tuple[tuple[object, ...], ...]:
    """Capture every non-system table/column grant held by the context definer."""

    with postgres_roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        rows = connection.execute(
            """
            WITH explicit_acl AS (
                SELECT
                    'table'::text AS grant_scope,
                    namespace.nspname AS schema_name,
                    relation.relname AS relation_name,
                    NULL::text AS column_name,
                    privilege.privilege_type,
                    privilege.is_grantable
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(
                        relation.relacl,
                        pg_catalog.acldefault('r', relation.relowner)
                    )
                ) AS privilege
                JOIN pg_catalog.pg_roles AS grantee
                  ON grantee.oid = privilege.grantee
                WHERE grantee.rolname = 'campus_ai_context_definer'
                  AND namespace.nspname !~ '^pg_'
                  AND namespace.nspname <> 'information_schema'
                UNION ALL
                SELECT
                    'column'::text,
                    namespace.nspname,
                    relation.relname,
                    attribute.attname,
                    privilege.privilege_type,
                    privilege.is_grantable
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                JOIN pg_catalog.pg_attribute AS attribute
                  ON attribute.attrelid = relation.oid
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(
                        attribute.attacl,
                        pg_catalog.acldefault('c', relation.relowner)
                    )
                ) AS privilege
                JOIN pg_catalog.pg_roles AS grantee
                  ON grantee.oid = privilege.grantee
                WHERE grantee.rolname = 'campus_ai_context_definer'
                  AND namespace.nspname !~ '^pg_'
                  AND namespace.nspname <> 'information_schema'
            )
            SELECT
                grant_scope,
                schema_name,
                relation_name,
                column_name,
                privilege_type,
                is_grantable
            FROM explicit_acl
            ORDER BY 1, 2, 3, 4 NULLS FIRST, 5, 6
            """
        ).fetchall()
    return tuple(tuple(row) for row in rows)


def _temporal_context_function_acl(
    postgres_roles: PostgresRoles,
    *,
    revision18: bool,
) -> tuple[tuple[object, ...], ...]:
    """Capture the exact ACL of the temporal identity entry points."""

    function_names = (
        "bind_execution_context",
        "list_execution_context_options",
    )
    if revision18:
        function_names += (
            "bind_execution_context_rev07",
            "list_execution_context_options_rev07",
        )
    with postgres_roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        rows = connection.execute(
            """
            SELECT procedure.proname,
                   COALESCE(grantee.rolname, 'PUBLIC'),
                   privilege.privilege_type,
                   privilege.is_grantable
            FROM pg_catalog.pg_proc AS procedure
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    procedure.proacl,
                    pg_catalog.acldefault('f', procedure.proowner)
                )
            ) AS privilege
            LEFT JOIN pg_catalog.pg_roles AS grantee
              ON grantee.oid = privilege.grantee
            WHERE namespace.nspname = 'app'
              AND procedure.proname = ANY(%s)
            ORDER BY procedure.proname, COALESCE(grantee.rolname, 'PUBLIC'),
                     privilege.privilege_type, privilege.is_grantable
            """,
            (list(function_names),),
        ).fetchall()
    return tuple(tuple(row) for row in rows)


def _assert_revision18_hides_revision17_context_functions(
    postgres_roles: PostgresRoles,
) -> None:
    calls = (
        """
        SELECT app.list_execution_context_options_rev07(
            NULL::integer, NULL::bigint, NULL::text, NULL::timestamptz, NULL::text
        )
        """,
        """
        SELECT app.bind_execution_context_rev07(
            NULL::uuid, NULL::integer, NULL::uuid, NULL::text, NULL::uuid,
            NULL::bigint, NULL::uuid, NULL::bigint, NULL::bigint, NULL::uuid,
            NULL::text, NULL::text, NULL::text, NULL::timestamptz, NULL::text
        )
        """,
    )
    for statement in calls:
        with (
            psycopg.connect(
                **postgres_roles.runtime_connection_parameters,
                autocommit=False,
            ) as connection,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            connection.execute(statement)


def _seed_revision17_staff_context(
    postgres_roles: PostgresRoles,
) -> tuple[UUID, int, UUID, UUID]:
    school_id, person_id, organization_id, assignment_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    marker = uuid4().hex
    effective_at = datetime.now(UTC) - timedelta(days=1)
    with postgres_roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            "INSERT INTO schools "
            "(id, source_key, school_code, display_name, timezone_name, status, version) "
            "VALUES (%s, %s, %s, 'revision17 ACL school', 'Asia/Shanghai', 'active', 1)",
            (school_id, f"school:{marker}", f"ACL-{marker[:12]}"),
        )
        user_row = connection.execute(
            "INSERT INTO users "
            "(provider_code, provider_tenant_key, external_subject, display_name, "
            "is_active, security_version) VALUES "
            "('local-test', %s, %s, 'revision17 ACL actor', true, 1) RETURNING id",
            (marker, marker),
        ).fetchone()
        assert user_row is not None
        user_id = int(user_row[0])
        connection.execute(
            "INSERT INTO school_people (id, school_id, source_key, status, version) "
            "VALUES (%s, %s, %s, 'active', 1)",
            (person_id, school_id, f"person:{marker}"),
        )
        connection.execute(
            "INSERT INTO organization_units "
            "(id, school_id, source_key, kind, status, version) "
            "VALUES (%s, %s, %s, 'department', 'active', 1)",
            (organization_id, school_id, f"organization:{marker}"),
        )
        connection.execute(
            "INSERT INTO organization_unit_versions "
            "(id, school_id, organization_unit_id, business_code, display_name, "
            "display_path, valid_from, source_kind, source_ref, status, version) "
            "VALUES (%s, %s, %s, 'ACL-ORG', 'revision17 ACL organization', "
            "'/revision17 ACL organization', %s, 'manual', %s, 'confirmed', 1)",
            (
                uuid4(),
                school_id,
                organization_id,
                effective_at,
                f"organization-version:{marker}",
            ),
        )
        connection.execute(
            "INSERT INTO staff_assignments "
            "(id, school_id, source_key, person_id, organization_unit_id, "
            "position_title, semantic_role_code, assignment_kind, appointment_source, "
            "approval_reference, valid_from, status, version) VALUES "
            "(%s, %s, %s, %s, %s, '数据管理员', 'school_data_admin', "
            "'substantive', 'manual', %s, %s, 'confirmed', 1)",
            (
                assignment_id,
                school_id,
                f"assignment:{marker}",
                person_id,
                organization_id,
                f"approval:{marker}",
                effective_at,
            ),
        )
        connection.execute(
            "INSERT INTO account_person_bindings "
            "(id, school_id, source_key, account_user_id, person_id, valid_from, "
            "status, version) VALUES (%s, %s, %s, %s, %s, %s, 'confirmed', 1)",
            (
                uuid4(),
                school_id,
                f"login:{marker}",
                user_id,
                person_id,
                effective_at,
            ),
        )
    return school_id, user_id, person_id, assignment_id


def _assert_revision17_context_functions_work(
    postgres_roles: PostgresRoles,
    *,
    school_id: UUID,
    user_id: int,
    person_id: UUID,
    assignment_id: UUID,
) -> None:
    signer = ContextProofSigner(b"school-facts-schema-context-signing-key-32-bytes")
    session_nonce_hash = sha256(b"revision17-acl-round-trip").hexdigest()
    proof_expires_at = datetime.now(UTC) + timedelta(minutes=15)
    account_proof = signer.sign_account_options(
        account_user_id=user_id,
        authorization_version=1,
        session_nonce_hash=session_nonce_hash,
        proof_expires_at=proof_expires_at,
    )
    parameters = account_proof.parameters()
    with psycopg.connect(
        **postgres_roles.runtime_connection_parameters,
        autocommit=False,
    ) as connection:
        options = connection.execute(
            "SELECT * FROM app.list_execution_context_options(%s, %s, %s, %s, %s)",
            (
                parameters["account_user_id"],
                parameters["authorization_version"],
                parameters["session_nonce_hash"],
                parameters["proof_expires_at"],
                parameters["server_proof"],
            ),
        ).fetchall()
    assert options == [
        (
            "assignment",
            assignment_id,
            1,
            school_id,
            person_id,
            "revision17 ACL school · revision17 ACL organization",
        )
    ]

    command_context = context_for(
        school_id=school_id,
        user_id=user_id,
        person_id=person_id,
        assignment_id=assignment_id,
        key="revision17-acl-round-trip",
    )
    envelope = signer.sign_execution_context(
        command_context,
        session_nonce_hash=session_nonce_hash,
        proof_expires_at=proof_expires_at,
    )
    with psycopg.connect(
        **postgres_roles.runtime_connection_parameters,
        autocommit=False,
    ) as connection:
        set_local_execution_context(
            connection,  # pyright: ignore[reportArgumentType]
            envelope,
        )
        assert connection.execute("SELECT app.require_school_context()").fetchone() == (school_id,)


def test_migration_15_declares_the_expected_linear_revision() -> None:
    assert MIGRATION_PATH.is_file(), MIGRATION_PATH
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "20260819_15"' in source
    assert 'down_revision = "20260816_14"' in source


def test_checkpoint3_semantics_migration_is_the_unique_revision_18_successor() -> None:
    assert SEMANTICS_MIGRATION_PATH.is_file(), SEMANTICS_MIGRATION_PATH
    source = SEMANTICS_MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "20260821_18"' in source
    assert 'down_revision = "20260820_17"' in source
    assert "organization_campus_relationships" in source
    assert "school_policy_scopes" in source
    assert "primary_campus_location_id" in source
    assert "administrative_campus_location_id" in source
    assert "sf1_validate_residence_transfer" in source
    assert "sf18_validate_inactive_campus_residences" in source
    assert "WHERE residence.revoked_at IS NULL" in source
    assert "residence.valid_to IS NULL" in source
    assert "app.school_fact_location_history_interval_is_resolvable(" in source
    assert "CREATE FUNCTION app.bind_school_reactivation_context" in source
    assert "CREATE FUNCTION app.list_school_reactivation_candidates" in source
    assert "CREATE FUNCTION app.require_school_reactivation_context" in source
    assert "CREATE FUNCTION app.bind_root_reactivation_context" in source
    assert "CREATE FUNCTION app.require_root_reactivation_context" in source
    assert "CREATE FUNCTION app.school_reactivation_replay_result" in source
    assert "CREATE FUNCTION app.school_fact_content_version_terminal" in source
    assert "CREATE FUNCTION app.school_fact_location_history_is_resolvable" in source
    assert "CREATE FUNCTION app.school_fact_school_snapshot_as_of" in source
    assert "CREATE FUNCTION public.sf18_lock_school_campus_invariants" not in source
    assert source.count("campus-ai:sf18:school-campus:") == 4
    assert "trg_sf18_preserve_referenced_campus_target" in source
    assert "trg_sf18_require_location_version_structure_lineage" in source
    assert "trg_sf18_require_organization_version_structure_lineage" in source
    assert "trg_sf18_preserve_student_person_identity" in source
    assert "DROP FUNCTION public.sf18_require_structure_version_lineage()" in source
    assert "DROP FUNCTION public.sf18_preserve_student_person_identity()" in source
    structure_ledger_start = source.index(
        "CREATE FUNCTION public.sf18_require_structure_version_lineage()"
    )
    structure_ledger_end = source.index(
        "CREATE CONSTRAINT TRIGGER trg_sf18_require_location_version_structure_lineage",
        structure_ledger_start,
    )
    assert "session_user" not in source[structure_ledger_start:structure_ledger_end]
    assert "trg_sf18_require_school_reactivation_lineage" in source
    assert "batch.confirmation_expires_at > v_as_of" in source
    assert "PERFORM pg_advisory_xact_lock(" in source
    assert "hashtextextended(p_confirmation_token_sha256, 0)" in source
    assert "FOR UPDATE OF batch, unit" not in source
    assert "school_reactivation_context_select" in source
    assert "DROP POLICY school_reactivation_context_select" in source
    assert 'bind_signature = (\n        "app.bind_school_reactivation_context' in source
    assert 'require_signature = "app.require_school_reactivation_context()"' in source
    assert 'op.execute(f"DROP FUNCTION {signature}")' in source
    assert "CREATE OR REPLACE FUNCTION app.bind_execution_context" not in source
    assert "trg_sf1_reject_delete_organization_campus_relationships" in source
    assert "trg_sf1_reject_delete_school_policy_scopes" in source
    assert "WHERE NOT child.cycle" in source
    assert "WHERE NOT parent.location_id = ANY(child.path)" in source
    guard_blocks = tuple(source.split("RAISE EXCEPTION USING")[1:])
    guard_message_fragments = {
        "revision 18 cannot discard populated Residence self-FK data": 1,
        "revision 18 requires an explicit student campus backfill": 1,
        "revision 18 cannot reconstruct the original planned ": 1,
        "revision 18 cannot grandfather a rev17 terminal ": 1,
        "revision 18 cannot reconstruct an ambiguous legacy ": 1,
        "revision 18 cannot reconstruct ambiguous location or ": 2,
        "revision 18 cannot reconstruct ambiguous assignment or ": 1,
        "revision 18 cannot install with effective Residence ": 1,
        "revision 18 contains facts not representable by revision 17": 1,
    }
    for message_fragment, expected_count in guard_message_fragments.items():
        matching_blocks = tuple(block for block in guard_blocks if message_fragment in block)
        assert len(matching_blocks) == expected_count
        assert all("ERRCODE = '55000'" in block.split("END IF;", 1)[0] for block in matching_blocks)
    assert "revision18:legacy-school-v1:" in source
    assert "revision 18 cannot grandfather a rev17 terminal" in source
    assert "fact reopened by ordinary upsert" in source
    assert "revision 18 cannot reconstruct an ambiguous legacy" in source
    assert "school content history" in source


@pytest.mark.integration
def test_revision18_cross_table_triggers_use_fixed_privilege_boundaries(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    """Cross-table trigger reads run as the fixed owner without widening runtime ACLs."""

    function_names = (
        "sf18_lock_structure_change",
        "sf18_validate_organization_structure",
        "sf18_validate_organization_version_structure",
        "sf18_validate_location_version_structure",
        "sf18_validate_structure_parent_coverage",
        "sf18_require_structure_lineage",
        "sf18_require_structure_version_lineage",
        "sf18_require_campus_target",
        "sf18_preserve_referenced_campus_target",
        "sf18_lock_location_residence_change",
        "sf18_validate_inactive_campus_residences",
        "sf18_require_school_reactivation_lineage",
        "sf18_require_root_reactivation_lineage",
        "sf18_preserve_student_person_identity",
    )
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        rows = connection.execute(
            """
            SELECT procedure.proname,
                   procedure.prosecdef,
                   owner.rolname,
                   procedure.proconfig,
                   NOT EXISTS (
                       SELECT 1
                       FROM aclexplode(
                           COALESCE(
                               procedure.proacl,
                               acldefault('f', procedure.proowner)
                           )
                       ) AS privilege
                       WHERE privilege.grantee = 0
                         AND privilege.privilege_type = 'EXECUTE'
                   ) AS public_execute_revoked
            FROM pg_proc AS procedure
            JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
            JOIN pg_roles AS owner ON owner.oid = procedure.proowner
            WHERE namespace.nspname = 'public'
              AND procedure.proname = ANY(%s)
            ORDER BY procedure.proname
            """,
            (list(function_names),),
        ).fetchall()
        runtime_select_grants = connection.execute(
            """
            SELECT table_name
            FROM information_schema.role_table_grants
            WHERE grantee = 'campus_ai_runtime'
              AND privilege_type = 'SELECT'
              AND table_schema = 'public'
              AND table_name = ANY(%s)
            ORDER BY table_name
            """,
            (
                [
                    "organization_campus_relationships",
                    "school_policy_scopes",
                    "school_fact_change_lineage",
                ],
            ),
        ).fetchall()
        student_trigger = connection.execute(
            """
            SELECT count(*)
            FROM pg_trigger
            WHERE tgrelid = 'public.students'::regclass
              AND tgname = 'trg_sf18_preserve_student_person_identity'
              AND NOT tgisinternal
            """
        ).fetchone()

    assert tuple(row[0] for row in rows) == tuple(sorted(function_names))
    assert all(
        row[1] is True
        and row[2] == "campus_ai_db_owner"
        and row[3] == ["search_path=pg_catalog"]
        and row[4] is True
        for row in rows
    )
    assert runtime_select_grants == []
    assert student_trigger == (1,)


def _assert_revision(postgres_roles: PostgresRoles, expected: str) -> None:
    with postgres_roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        revision = connection.execute("SELECT version_num FROM public.alembic_version").fetchone()
    assert revision == (expected,)


def _assert_alembic_guard_55000(
    result: subprocess.CompletedProcess[str],
    expected_message: str,
) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "psycopg.errors.ObjectNotInPrerequisiteState" in combined
    assert expected_message in combined


def _seed_revision17_structure_history(
    roles: PostgresRoles,
    *,
    ambiguous_kind: str | None = None,
) -> tuple[object, ...]:
    """Seed revision-17 structure whose single slice is safe or whose history is ambiguous."""

    school_id = uuid4()
    organization_parent_id, organization_child_id = uuid4(), uuid4()
    campus_id, room_id = uuid4(), uuid4()
    first_at = datetime(2026, 8, 1, tzinfo=UTC)
    second_at = datetime(2026, 8, 15, tzinfo=UTC)
    organization_version = 2 if ambiguous_kind == "organization" else 1
    location_version = 2 if ambiguous_kind == "location" else 1
    with roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            "INSERT INTO schools "
            "(id, source_key, school_code, display_name, timezone_name, status, version) "
            "VALUES (%s, %s, %s, 'structure history', 'Asia/Shanghai', 'active', 1)",
            (school_id, f"school:{school_id}", f"STRUCT-{uuid4().hex[:10]}"),
        )
        connection.execute(
            "INSERT INTO organization_units "
            "(id, school_id, source_key, parent_organization_unit_id, kind, status, version) "
            "VALUES (%s, %s, %s, NULL, 'schoolwide', 'active', 1), "
            "(%s, %s, %s, %s, 'department', 'active', %s)",
            (
                organization_parent_id,
                school_id,
                f"organization:parent:{organization_parent_id}",
                organization_child_id,
                school_id,
                f"organization:child:{organization_child_id}",
                organization_parent_id,
                organization_version,
            ),
        )
        connection.execute(
            "INSERT INTO organization_unit_versions "
            "(id, school_id, organization_unit_id, business_code, display_name, "
            "display_path, valid_from, valid_to, source_kind, source_ref, status, version) "
            "VALUES (%s, %s, %s, 'ORG-P', '上级组织', '/上级组织', %s, NULL, "
            "'manual', %s, 'confirmed', 1), "
            "(%s, %s, %s, 'ORG-C', '下级组织', '/上级组织/下级组织', %s, %s, "
            "'manual', %s, 'confirmed', 1)",
            (
                uuid4(),
                school_id,
                organization_parent_id,
                first_at,
                f"organization-version:{uuid4().hex}",
                uuid4(),
                school_id,
                organization_child_id,
                first_at,
                second_at if ambiguous_kind == "organization" else None,
                f"organization-version:{uuid4().hex}",
            ),
        )
        if ambiguous_kind == "organization":
            connection.execute(
                "INSERT INTO organization_unit_versions "
                "(id, school_id, organization_unit_id, business_code, display_name, "
                "display_path, valid_from, source_kind, source_ref, status, version) "
                "VALUES (%s, %s, %s, 'ORG-C', '下级组织新版', '/上级组织/下级组织新版', %s, "
                "'manual', %s, 'confirmed', 2)",
                (
                    uuid4(),
                    school_id,
                    organization_child_id,
                    second_at,
                    f"organization-version:{uuid4().hex}",
                ),
            )
        connection.execute(
            "INSERT INTO campus_locations "
            "(id, school_id, source_key, parent_location_id, kind, status, version) "
            "VALUES (%s, %s, %s, NULL, 'campus', 'active', 1), "
            "(%s, %s, %s, %s, 'dorm_room', 'active', %s)",
            (
                campus_id,
                school_id,
                f"location:campus:{campus_id}",
                room_id,
                school_id,
                f"location:room:{room_id}",
                campus_id,
                location_version,
            ),
        )
        connection.execute(
            "INSERT INTO campus_location_versions "
            "(id, school_id, location_id, business_code, display_name, aliases, "
            "display_path, valid_from, valid_to, source_kind, source_ref, status, version) "
            "VALUES (%s, %s, %s, 'CAMPUS', '主校区', ARRAY[]::varchar[], '/主校区', "
            "%s, NULL, 'manual', %s, 'confirmed', 1), "
            "(%s, %s, %s, 'ROOM', '101', ARRAY[]::varchar[], '/主校区/101', "
            "%s, %s, 'manual', %s, 'confirmed', 1)",
            (
                uuid4(),
                school_id,
                campus_id,
                first_at,
                f"location-version:{uuid4().hex}",
                uuid4(),
                school_id,
                room_id,
                first_at,
                second_at if ambiguous_kind == "location" else None,
                f"location-version:{uuid4().hex}",
            ),
        )
        if ambiguous_kind == "location":
            connection.execute(
                "INSERT INTO campus_location_versions "
                "(id, school_id, location_id, business_code, display_name, aliases, "
                "display_path, valid_from, source_kind, source_ref, status, version) "
                "VALUES (%s, %s, %s, 'ROOM', '101新版', ARRAY[]::varchar[], "
                "'/主校区/101新版', %s, 'manual', %s, 'confirmed', 2)",
                (
                    uuid4(),
                    school_id,
                    room_id,
                    second_at,
                    f"location-version:{uuid4().hex}",
                ),
            )
    return school_id, organization_parent_id, organization_child_id, campus_id, room_id


def _seed_revision17_invalid_structure_topology(
    roles: PostgresRoles,
    *,
    fact_kind: str,
    defect: str,
) -> tuple[object, object, object]:
    """Seed one-slice rev17 roots whose backfilled temporal topology is invalid."""

    school_id, parent_id, child_id = uuid4(), uuid4(), uuid4()
    child_at = datetime(2026, 8, 1, tzinfo=UTC)
    parent_at = datetime(2026, 8, 15, tzinfo=UTC) if defect == "coverage" else child_at
    with roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            "INSERT INTO schools "
            "(id, source_key, school_code, display_name, timezone_name, status, version) "
            "VALUES (%s, %s, %s, 'invalid structure', 'Asia/Shanghai', 'active', 1)",
            (school_id, f"school:{school_id}", f"INVALID-{uuid4().hex[:8]}"),
        )
        if fact_kind == "organization":
            parent_parent_id = child_id if defect == "cycle" else None
            connection.execute(
                """
                INSERT INTO organization_units (
                    id, school_id, source_key, parent_organization_unit_id,
                    kind, status, version
                ) VALUES
                    (%s, %s, %s, %s, 'department', 'active', 1),
                    (%s, %s, %s, %s, 'department', 'active', 1)
                """,
                (
                    parent_id,
                    school_id,
                    f"organization:parent:{parent_id}",
                    parent_parent_id,
                    child_id,
                    school_id,
                    f"organization:child:{child_id}",
                    parent_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO organization_unit_versions (
                    id, school_id, organization_unit_id, business_code,
                    display_name, display_path, valid_from, valid_to,
                    source_kind, source_ref, status, version
                ) VALUES
                    (%s, %s, %s, 'PARENT', 'parent', '/parent', %s, NULL,
                     'manual', %s, 'confirmed', 1),
                    (%s, %s, %s, 'CHILD', 'child', '/parent/child', %s, NULL,
                     'manual', %s, 'confirmed', 1)
                """,
                (
                    uuid4(),
                    school_id,
                    parent_id,
                    parent_at,
                    f"organization-version:{uuid4().hex}",
                    uuid4(),
                    school_id,
                    child_id,
                    child_at,
                    f"organization-version:{uuid4().hex}",
                ),
            )
        else:
            parent_parent_id = child_id if defect == "cycle" else None
            parent_kind = "dorm_building" if defect == "cycle" else "campus"
            if defect == "cycle":
                # Emulate rows grandfathered before revision 05 installed its
                # non-backscanning stable-tree trigger.
                connection.execute(
                    "ALTER TABLE campus_locations DISABLE TRIGGER trg_campus_locations_acyclic"
                )
            connection.execute(
                """
                INSERT INTO campus_locations (
                    id, school_id, source_key, parent_location_id, kind, status, version
                ) VALUES
                    (%s, %s, %s, %s, %s, 'active', 1),
                    (%s, %s, %s, %s, 'dorm_room', 'active', 1)
                """,
                (
                    parent_id,
                    school_id,
                    f"location:parent:{parent_id}",
                    parent_parent_id,
                    parent_kind,
                    child_id,
                    school_id,
                    f"location:child:{child_id}",
                    parent_id,
                ),
            )
            if defect == "cycle":
                connection.execute(
                    "ALTER TABLE campus_locations ENABLE TRIGGER trg_campus_locations_acyclic"
                )
            connection.execute(
                """
                INSERT INTO campus_location_versions (
                    id, school_id, location_id, business_code, display_name,
                    aliases, display_path, valid_from, valid_to,
                    source_kind, source_ref, status, version
                ) VALUES
                    (%s, %s, %s, 'PARENT', 'parent', ARRAY[]::varchar[],
                     '/parent', %s, NULL, 'manual', %s, 'confirmed', 1),
                    (%s, %s, %s, 'CHILD', 'child', ARRAY[]::varchar[],
                     '/parent/child', %s, NULL, 'manual', %s, 'confirmed', 1)
                """,
                (
                    uuid4(),
                    school_id,
                    parent_id,
                    parent_at,
                    f"location-version:{uuid4().hex}",
                    uuid4(),
                    school_id,
                    child_id,
                    child_at,
                    f"location-version:{uuid4().hex}",
                ),
            )
    return school_id, parent_id, child_id


def _seed_revision17_lineage_sequence(
    roles: PostgresRoles,
    *,
    school_id: object,
    fact_kind: str,
    fact_source_key: str,
    target_id: object,
    operations: tuple[str, ...],
) -> None:
    """Insert a mechanically valid rev17 lineage stream without changing its target row."""

    effective_at = datetime(2026, 8, 10, tzinfo=UTC)
    with roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        for target_version, operation in enumerate(operations, start=1):
            batch_id, unit_id = uuid4(), uuid4()
            source_sha256 = f"{target_version:x}" * 64
            payload_sha256 = f"{target_version + 8:x}" * 64
            connection.execute(
                """
                INSERT INTO master_data_import_batches (
                    id, school_id, mode, source_kind, source_ref, source_sha256,
                    worksheet_names, status, planned_units, succeeded_units,
                    failed_units, pending_units, request_id, idempotency_key_hash, version
                ) VALUES (
                    %s, %s, 'manual_unit', 'manual', %s, %s, '[]'::jsonb,
                    'source_registered', 1, 0, 0, 1, %s, %s, 1
                )
                """,
                (
                    batch_id,
                    school_id,
                    f"rev17-washed-terminal:{fact_kind}:{batch_id}",
                    source_sha256,
                    uuid4(),
                    f"{target_version + 2:x}" * 64,
                ),
            )
            connection.execute(
                """
                INSERT INTO master_data_import_units (
                    id, school_id, batch_id, sequence, row_number, unit_kind, status,
                    payload, payload_sha256, soft_anomalies, initial_soft_anomalies,
                    target_id, target_version, version
                ) VALUES (
                    %s, %s, %s, 1, 1, 'school_fact', 'pending',
                    jsonb_build_object(
                        'fact_kind', CAST(%s AS text),
                        'source_key', CAST(%s AS text),
                        'operation', CAST(%s AS text),
                        'effective_at', CAST(%s AS text)
                    ), %s, '[]'::jsonb, '[]'::jsonb, %s, %s, 1
                )
                """,
                (
                    unit_id,
                    school_id,
                    batch_id,
                    fact_kind,
                    fact_source_key,
                    operation,
                    effective_at.isoformat(),
                    payload_sha256,
                    target_id,
                    target_version,
                ),
            )
            connection.execute(
                """
                INSERT INTO school_fact_change_lineage (
                    id, school_id, batch_id, unit_id, fact_kind, fact_source_key,
                    operation, target_id, target_version, source_sha256,
                    sheet_code, row_number
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'legacy_guard', 1
                )
                """,
                (
                    uuid4(),
                    school_id,
                    batch_id,
                    unit_id,
                    fact_kind,
                    fact_source_key,
                    operation,
                    target_id,
                    target_version,
                    source_sha256,
                ),
            )


@pytest.mark.integration
@pytest.mark.destructive_migration
@pytest.mark.parametrize(
    ("fact_kind", "defect"),
    (
        pytest.param("organization", "cycle", id="organization-cycle"),
        pytest.param("location", "cycle", id="location-cycle"),
        pytest.param("organization", "coverage", id="organization-parent-coverage"),
        pytest.param("location", "coverage", id="location-parent-coverage"),
    ),
)
def test_revision18_upgrade_rejects_invalid_backfilled_structure_topology(
    isolated_revision19_schema: PostgresRoles,
    fact_kind: str,
    defect: str,
) -> None:
    """Constraint triggers do not backscan; upgrade must scan rev17 topology itself."""

    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    school_id, parent_id, child_id = _seed_revision17_invalid_structure_topology(
        isolated_revision19_schema,
        fact_kind=fact_kind,
        defect=defect,
    )

    rejected = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )

    _assert_alembic_guard_55000(rejected, STRUCTURE_HISTORY_UPGRADE_BLOCKED_MESSAGE)
    _assert_revision(isolated_revision19_schema, "20260820_17")
    table_name = "organization_units" if fact_kind == "organization" else "campus_locations"
    parent_column = (
        "parent_organization_unit_id" if fact_kind == "organization" else "parent_location_id"
    )
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        preserved = connection.execute(
            sql.SQL(
                "SELECT id, {} FROM {} WHERE school_id = %s AND id IN (%s, %s) ORDER BY id"
            ).format(sql.Identifier(parent_column), sql.Identifier(table_name)),
            (school_id, parent_id, child_id),
        ).fetchall()
    assert len(preserved) == 2


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_revision18_round_trip_from_empty_revision17(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    _assert_revision(isolated_revision19_schema, "20260820_17")

    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        revision17_shape = connection.execute(
            """
            SELECT
                to_regclass('public.organization_campus_relationships') IS NULL,
                to_regclass('public.school_policy_scopes') IS NULL,
                EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'student_bed_residences'
                      AND column_name = 'arrived_from_residence_id'
                ),
                EXISTS (
                    SELECT 1
                    FROM pg_trigger
                    WHERE tgrelid = 'public.student_bed_residences'::regclass
                      AND tgname = 'trg_sf1_validate_residence_transfer'
                      AND NOT tgisinternal
                ),
                to_regprocedure(
                    'app.bind_school_reactivation_context(uuid,integer,uuid,text,uuid,'
                    'bigint,uuid,bigint,bigint,uuid,text,text,text,timestamptz,text,text,'
                    'text,text,bigint,text,text,text,bigint)'
                ) IS NULL,
                to_regprocedure('app.require_school_reactivation_context()') IS NULL,
                NOT EXISTS (
                    SELECT 1
                    FROM pg_trigger
                    WHERE tgrelid = 'public.schools'::regclass
                      AND tgname = 'trg_sf18_require_school_reactivation_lineage'
                      AND NOT tgisinternal
                ),
                NOT EXISTS (
                    SELECT 1
                    FROM pg_policies
                    WHERE policyname = 'school_reactivation_context_select'
                ),
                NOT has_column_privilege(
                    'campus_ai_context_definer', 'public.schools', 'source_key', 'SELECT'
                ),
                NOT has_column_privilege(
                    'campus_ai_context_definer', 'public.schools', 'status', 'UPDATE'
                ),
                NOT EXISTS (
                    SELECT 1 FROM pg_trigger
                    WHERE tgname IN (
                        'trg_sf1_reject_delete_organization_campus_relationships',
                        'trg_sf1_reject_delete_school_policy_scopes'
                    ) AND NOT tgisinternal
                ),
                to_regprocedure(
                    'app.school_fact_content_version_terminal(uuid,text,uuid,bigint)'
                ) IS NULL,
                NOT EXISTS (
                    SELECT 1 FROM pg_trigger
                    WHERE tgrelid = 'public.campus_location_versions'::regclass
                      AND tgname = 'trg_sf18_preserve_referenced_campus_target'
                      AND NOT tgisinternal
                ),
                to_regprocedure(
                    'public.sf18_lock_school_campus_invariants(uuid)'
                ) IS NULL,
                to_regprocedure(
                    'app.bind_root_reactivation_context(text,text,text,text,bigint)'
                ) IS NULL,
                to_regprocedure('app.require_root_reactivation_context()') IS NULL,
                to_regprocedure('app.school_reactivation_replay_result()') IS NULL,
                to_regprocedure(
                    'app.list_school_reactivation_candidates('
                    'integer,bigint,text,timestamptz,text)'
                ) IS NULL,
                to_regprocedure(
                    'app.school_fact_location_history_is_resolvable(uuid,uuid,timestamptz)'
                ) IS NULL,
                to_regprocedure(
                    'app.school_fact_school_snapshot_as_of(uuid,timestamptz)'
                ) IS NULL,
                NOT EXISTS (
                    SELECT 1 FROM pg_trigger
                    WHERE tgname LIKE 'trg_sf18_require_%_reactivation_lineage'
                      AND tgname <> 'trg_sf18_require_school_reactivation_lineage'
                      AND NOT tgisinternal
                ),
                to_regprocedure(
                    'public.sf18_require_structure_version_lineage()'
                ) IS NULL,
                to_regprocedure(
                    'public.sf18_preserve_student_person_identity()'
                ) IS NULL,
                NOT EXISTS (
                    SELECT 1 FROM pg_trigger
                    WHERE tgname IN (
                        'trg_sf18_require_location_version_structure_lineage',
                        'trg_sf18_require_organization_version_structure_lineage',
                        'trg_sf18_preserve_student_person_identity'
                    ) AND NOT tgisinternal
                )
            """
        ).fetchone()
    assert revision17_shape == (True,) * 24

    upgraded = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    _assert_revision(isolated_revision19_schema, "20260821_18")

    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        revision18_shape = connection.execute(
            """
            SELECT
                to_regclass('public.organization_campus_relationships') IS NOT NULL,
                to_regclass('public.school_policy_scopes') IS NOT NULL,
                NOT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'student_bed_residences'
                      AND column_name IN (
                          'arrived_from_residence_id',
                          'departed_to_residence_id'
                      )
                ),
                NOT EXISTS (
                    SELECT 1
                    FROM pg_trigger
                    WHERE tgrelid = 'public.student_bed_residences'::regclass
                      AND tgname = 'trg_sf1_validate_residence_transfer'
                      AND NOT tgisinternal
                ),
                to_regprocedure(
                    'app.bind_school_reactivation_context(uuid,integer,uuid,text,uuid,'
                    'bigint,uuid,bigint,bigint,uuid,text,text,text,timestamptz,text,text,'
                    'text,text,bigint,text,text,text,bigint)'
                ) IS NOT NULL,
                to_regprocedure('app.require_school_reactivation_context()') IS NOT NULL,
                EXISTS (
                    SELECT 1
                    FROM pg_trigger
                    WHERE tgrelid = 'public.schools'::regclass
                      AND tgname = 'trg_sf18_require_school_reactivation_lineage'
                      AND NOT tgisinternal
                ),
                (
                    SELECT count(*)
                    FROM pg_policies
                    WHERE policyname = 'school_reactivation_context_select'
                ) = 3,
                has_column_privilege(
                    'campus_ai_context_definer', 'public.schools', 'source_key', 'SELECT'
                ),
                has_column_privilege(
                    'campus_ai_context_definer', 'public.schools', 'status', 'UPDATE'
                ),
                (
                    SELECT count(*) FROM pg_trigger
                    WHERE tgname IN (
                        'trg_sf1_reject_delete_organization_campus_relationships',
                        'trg_sf1_reject_delete_school_policy_scopes'
                    ) AND NOT tgisinternal
                ) = 2,
                to_regprocedure(
                    'app.school_fact_content_version_terminal(uuid,text,uuid,bigint)'
                ) IS NOT NULL,
                EXISTS (
                    SELECT 1 FROM pg_trigger
                    WHERE tgrelid = 'public.campus_location_versions'::regclass
                      AND tgname = 'trg_sf18_preserve_referenced_campus_target'
                      AND NOT tgisinternal
                ),
                to_regprocedure(
                    'public.sf18_lock_school_campus_invariants(uuid)'
                ) IS NULL,
                to_regprocedure(
                    'app.bind_root_reactivation_context(text,text,text,text,bigint)'
                ) IS NOT NULL,
                to_regprocedure('app.require_root_reactivation_context()') IS NOT NULL,
                to_regprocedure('app.school_reactivation_replay_result()') IS NOT NULL,
                to_regprocedure(
                    'app.list_school_reactivation_candidates('
                    'integer,bigint,text,timestamptz,text)'
                ) IS NOT NULL,
                to_regprocedure(
                    'app.school_fact_location_history_is_resolvable(uuid,uuid,timestamptz)'
                ) IS NOT NULL,
                to_regprocedure(
                    'app.school_fact_school_snapshot_as_of(uuid,timestamptz)'
                ) IS NOT NULL,
                (
                    SELECT count(*) FROM pg_trigger
                    WHERE tgname LIKE 'trg_sf18_require_%_reactivation_lineage'
                      AND tgname <> 'trg_sf18_require_school_reactivation_lineage'
                      AND NOT tgisinternal
                ) = 4,
                NOT EXISTS (
                    SELECT 1
                    FROM pg_proc AS procedure
                    CROSS JOIN LATERAL aclexplode(
                        COALESCE(procedure.proacl, acldefault('f', procedure.proowner))
                    ) AS privilege
                    WHERE procedure.oid = (
                        'app.bind_root_reactivation_context(text,text,text,text,bigint)'
                    )::regprocedure
                      AND privilege.grantee = 0
                      AND privilege.privilege_type = 'EXECUTE'
                ),
                has_function_privilege(
                    'campus_ai_runtime',
                    'app.bind_root_reactivation_context(text,text,text,text,bigint)',
                    'EXECUTE'
                ),
                NOT EXISTS (
                    SELECT 1
                    FROM pg_proc AS procedure
                    CROSS JOIN LATERAL aclexplode(
                        COALESCE(procedure.proacl, acldefault('f', procedure.proowner))
                    ) AS privilege
                    WHERE procedure.oid = (
                        'app.list_school_reactivation_candidates('
                        'integer,bigint,text,timestamptz,text)'
                    )::regprocedure
                      AND privilege.grantee = 0
                      AND privilege.privilege_type = 'EXECUTE'
                ),
                has_function_privilege(
                    'campus_ai_runtime',
                    'app.list_school_reactivation_candidates('
                    'integer,bigint,text,timestamptz,text)',
                    'EXECUTE'
                ),
                to_regprocedure(
                    'public.sf18_require_structure_version_lineage()'
                ) IS NOT NULL,
                to_regprocedure(
                    'public.sf18_preserve_student_person_identity()'
                ) IS NOT NULL,
                (
                    SELECT count(*) FROM pg_trigger
                    WHERE tgname IN (
                        'trg_sf18_require_location_version_structure_lineage',
                        'trg_sf18_require_organization_version_structure_lineage',
                        'trg_sf18_preserve_student_person_identity'
                    ) AND NOT tgisinternal
                ) = 3
            """
        ).fetchone()
    assert revision18_shape == (True,) * 28


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_revision18_round_trip_preserves_revision17_context_definer_acl_and_functions(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    """Downgrade restores the exact rev17 identity-reader capability, not just its DDL."""

    to_base = _run_alembic(isolated_revision19_schema, "downgrade", "base")
    assert to_base.returncode == 0, to_base.stdout + to_base.stderr
    to_revision17 = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260820_17",
    )
    assert to_revision17.returncode == 0, to_revision17.stdout + to_revision17.stderr
    _assert_revision(isolated_revision19_schema, "20260820_17")

    baseline_acl = _context_definer_relation_acl(isolated_revision19_schema)
    baseline_function_acl = _temporal_context_function_acl(
        isolated_revision19_schema,
        revision18=False,
    )
    school_id, user_id, person_id, assignment_id = _seed_revision17_staff_context(
        isolated_revision19_schema
    )
    _assert_revision17_context_functions_work(
        isolated_revision19_schema,
        school_id=school_id,
        user_id=user_id,
        person_id=person_id,
        assignment_id=assignment_id,
    )

    upgraded = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    assert _temporal_context_function_acl(
        isolated_revision19_schema,
        revision18=True,
    ) == (
        ("bind_execution_context", "campus_ai_context_definer", "EXECUTE", False),
        ("bind_execution_context", "campus_ai_db_owner", "EXECUTE", False),
        ("bind_execution_context", "campus_ai_runtime", "EXECUTE", False),
        ("bind_execution_context_rev07", "campus_ai_context_definer", "EXECUTE", False),
        (
            "list_execution_context_options",
            "campus_ai_context_definer",
            "EXECUTE",
            False,
        ),
        ("list_execution_context_options", "campus_ai_runtime", "EXECUTE", False),
        (
            "list_execution_context_options_rev07",
            "campus_ai_context_definer",
            "EXECUTE",
            False,
        ),
    )
    _assert_revision18_hides_revision17_context_functions(isolated_revision19_schema)
    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    _assert_revision(isolated_revision19_schema, "20260820_17")

    _assert_revision17_context_functions_work(
        isolated_revision19_schema,
        school_id=school_id,
        user_id=user_id,
        person_id=person_id,
        assignment_id=assignment_id,
    )
    assert _context_definer_relation_acl(isolated_revision19_schema) == baseline_acl
    assert (
        _temporal_context_function_acl(
            isolated_revision19_schema,
            revision18=False,
        )
        == baseline_function_acl
    )


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_revision18_backfills_unambiguous_location_and_organization_structure_slices(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    """A single revision-17 content slice can inherit the matching stable structure."""

    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    school_id, organization_parent_id, organization_child_id, campus_id, room_id = (
        _seed_revision17_structure_history(isolated_revision19_schema)
    )

    upgraded = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    _assert_revision(isolated_revision19_schema, "20260821_18")
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        location_slice = connection.execute(
            "SELECT parent_location_id, kind FROM campus_location_versions "
            "WHERE school_id = %s AND location_id = %s",
            (school_id, room_id),
        ).fetchone()
        campus_slice = connection.execute(
            "SELECT parent_location_id, kind FROM campus_location_versions "
            "WHERE school_id = %s AND location_id = %s",
            (school_id, campus_id),
        ).fetchone()
        organization_slice = connection.execute(
            "SELECT parent_organization_unit_id FROM organization_unit_versions "
            "WHERE school_id = %s AND organization_unit_id = %s",
            (school_id, organization_child_id),
        ).fetchone()
        parent_slice = connection.execute(
            "SELECT parent_organization_unit_id FROM organization_unit_versions "
            "WHERE school_id = %s AND organization_unit_id = %s",
            (school_id, organization_parent_id),
        ).fetchone()
    assert location_slice == (campus_id, "dorm_room")
    assert campus_slice == (None, "campus")
    assert organization_slice == (organization_parent_id,)
    assert parent_slice == (None,)


@pytest.mark.integration
@pytest.mark.destructive_migration
@pytest.mark.parametrize("ambiguous_kind", ("location", "organization"))
def test_revision18_upgrade_rejects_ambiguous_unversioned_structure_history(
    isolated_revision19_schema: PostgresRoles,
    ambiguous_kind: str,
) -> None:
    """Revision 18 cannot copy today's structure into multiple unknown historical slices."""

    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    _seed_revision17_structure_history(
        isolated_revision19_schema,
        ambiguous_kind=ambiguous_kind,
    )

    rejected = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )
    _assert_alembic_guard_55000(rejected, STRUCTURE_HISTORY_UPGRADE_BLOCKED_MESSAGE)
    _assert_revision(isolated_revision19_schema, "20260820_17")


@pytest.mark.integration
@pytest.mark.destructive_migration
@pytest.mark.parametrize("fact_kind", ("location", "organization"))
@pytest.mark.parametrize("slice_state", ("missing", "draft", "rejected"))
def test_revision18_upgrade_requires_one_confirmed_structure_slice(
    isolated_revision19_schema: PostgresRoles,
    fact_kind: str,
    slice_state: str,
) -> None:
    """Revision 18 must not invent current structure when rev17 has no confirmed slice."""

    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    (
        _school_id,
        _organization_parent_id,
        organization_child_id,
        _campus_id,
        room_id,
    ) = _seed_revision17_structure_history(isolated_revision19_schema)
    table_name, target_column, target_id = (
        ("campus_location_versions", "location_id", room_id)
        if fact_kind == "location"
        else (
            "organization_unit_versions",
            "organization_unit_id",
            organization_child_id,
        )
    )
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        if slice_state == "missing":
            connection.execute(
                f"ALTER TABLE {table_name} DISABLE TRIGGER USER"  # noqa: S608
            )
            connection.execute(
                f"DELETE FROM {table_name} WHERE {target_column} = %s",  # noqa: S608
                (target_id,),
            )
            connection.execute(
                f"ALTER TABLE {table_name} ENABLE TRIGGER USER"  # noqa: S608
            )
        else:
            connection.execute(
                f"UPDATE {table_name} SET status = %s "  # noqa: S608
                f"WHERE {target_column} = %s",  # noqa: S608
                (slice_state, target_id),
            )

    rejected = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )
    _assert_alembic_guard_55000(rejected, STRUCTURE_HISTORY_UPGRADE_BLOCKED_MESSAGE)
    _assert_revision(isolated_revision19_schema, "20260820_17")
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        assert connection.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "AND ((table_name = 'campus_location_versions' "
            "AND column_name IN ('parent_location_id', 'kind')) "
            "OR (table_name = 'organization_unit_versions' "
            "AND column_name = 'parent_organization_unit_id'))"
        ).fetchone() == (0,)


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_revision18_upgrade_rejects_assignment_whose_revoke_overwrote_planned_expiry(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    """Rev17 revoked_at cannot reveal the planned valid_to that its writer overwrote."""

    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    school_id, organization_id, *_ = _seed_revision17_structure_history(isolated_revision19_schema)
    person_id, assignment_id = uuid4(), uuid4()
    revoked_at = datetime(2026, 8, 15, tzinfo=UTC)
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            "INSERT INTO school_people (id, school_id, source_key, status, version) "
            "VALUES (%s, %s, %s, 'active', 1)",
            (person_id, school_id, f"person:{person_id}"),
        )
        connection.execute(
            """
            INSERT INTO staff_assignments (
                id, school_id, source_key, person_id, organization_unit_id,
                position_title, semantic_role_code, assignment_kind,
                appointment_source, approval_reference, valid_from, valid_to,
                revoked_at, status, version
            ) VALUES (
                %s, %s, %s, %s, %s, 'revoked assignment', 'teacher',
                'substantive', 'manual', %s, %s, %s, %s, 'rejected', 2
            )
            """,
            (
                assignment_id,
                school_id,
                f"assignment:{assignment_id}",
                person_id,
                organization_id,
                f"approval:{assignment_id}",
                datetime(2026, 8, 1, tzinfo=UTC),
                revoked_at,
                revoked_at,
            ),
        )

    rejected = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )
    _assert_alembic_guard_55000(rejected, TERMINAL_DATE_UPGRADE_BLOCKED_MESSAGE)
    _assert_revision(isolated_revision19_schema, "20260820_17")
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        assert connection.execute(
            "SELECT valid_to, revoked_at FROM staff_assignments WHERE id = %s",
            (assignment_id,),
        ).fetchone() == (revoked_at, revoked_at)


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_revision18_upgrade_rejects_policy_revoke_even_with_lineage(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    """A rev17 revoke lineage proves the action, not the overwritten planned expiry."""

    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    school_id, organization_id, *_ = _seed_revision17_structure_history(isolated_revision19_schema)
    source_id, policy_id, policy_version_id = uuid4(), uuid4(), uuid4()
    batch_id, unit_id = uuid4(), uuid4()
    revoked_at = datetime(2026, 8, 15, tzinfo=UTC)
    source_key = f"policy-version:{policy_version_id}"
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            """
            INSERT INTO school_fact_sources (
                id, school_id, source_key, source_kind, source_ref, schema_version
            ) VALUES (%s, %s, %s, 'manual', %s, 1)
            """,
            (source_id, school_id, f"source:{source_id}", f"manual:{source_id}"),
        )
        connection.execute(
            "INSERT INTO school_policies (id, school_id, source_key) VALUES (%s, %s, %s)",
            (policy_id, school_id, f"policy:{policy_id}"),
        )
        connection.execute(
            """
            INSERT INTO school_policy_versions (
                id, school_id, policy_id, business_code, title, category_code,
                classification, publishing_organization_unit_id, published_at,
                effective_from, effective_to, content_text, version_note,
                supersedes_policy_version_id, version, source_id, import_batch_id
            ) VALUES (
                %s, %s, %s, %s, 'rev17 revoked policy', 'conduct', 'internal',
                %s, %s, %s, %s, 'legacy content', NULL, NULL, 1, %s, NULL
            )
            """,
            (
                policy_version_id,
                school_id,
                policy_id,
                f"POL-{uuid4().hex[:8]}",
                organization_id,
                datetime(2026, 8, 1, tzinfo=UTC),
                datetime(2026, 8, 1, tzinfo=UTC),
                revoked_at,
                source_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO master_data_import_batches (
                id, school_id, mode, source_kind, source_ref, source_sha256,
                worksheet_names, status, planned_units, succeeded_units,
                failed_units, pending_units, request_id, idempotency_key_hash, version
            ) VALUES (
                %s, %s, 'manual_unit', 'manual', %s, %s, '[]'::jsonb,
                'source_registered', 1, 0, 0, 1, %s, %s, 1
            )
            """,
            (
                batch_id,
                school_id,
                f"rev17-policy-revoke:{batch_id}",
                "a" * 64,
                uuid4(),
                "b" * 64,
            ),
        )
        connection.execute(
            """
            INSERT INTO master_data_import_units (
                id, school_id, batch_id, sequence, row_number, unit_kind, status,
                payload, payload_sha256, soft_anomalies, initial_soft_anomalies,
                target_id, target_version, version
            ) VALUES (
                %s, %s, %s, 1, 1, 'school_fact', 'pending',
                jsonb_build_object(
                    'fact_kind', 'policy_version', 'source_key', CAST(%s AS text),
                    'operation', 'revoke', 'effective_at', CAST(%s AS text)
                ), %s, '[]'::jsonb, '[]'::jsonb, %s, 2, 1
            )
            """,
            (
                unit_id,
                school_id,
                batch_id,
                source_key,
                revoked_at.isoformat(),
                "c" * 64,
                policy_version_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO school_fact_change_lineage (
                id, school_id, batch_id, unit_id, fact_kind, fact_source_key,
                operation, target_id, target_version, source_sha256,
                sheet_code, row_number
            ) VALUES (
                %s, %s, %s, %s, 'policy_version', %s, 'revoke', %s, 2,
                %s, 'policy_versions', 1
            )
            """,
            (
                uuid4(),
                school_id,
                batch_id,
                unit_id,
                source_key,
                policy_version_id,
                "a" * 64,
            ),
        )

    rejected = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )
    _assert_alembic_guard_55000(rejected, TERMINAL_DATE_UPGRADE_BLOCKED_MESSAGE)
    _assert_revision(isolated_revision19_schema, "20260820_17")
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        assert connection.execute(
            "SELECT effective_to FROM school_policy_versions WHERE id = %s",
            (policy_version_id,),
        ).fetchone() == (revoked_at,)


@pytest.mark.integration
@pytest.mark.destructive_migration
@pytest.mark.parametrize(
    ("fact_kind", "terminal_operation"),
    (
        pytest.param("person", "revoke", id="root-revoke-then-upsert"),
        pytest.param("assignment", "end", id="assignment-end-then-upsert"),
        pytest.param("login_mapping", "revoke", id="login-revoke-then-upsert"),
    ),
)
def test_revision18_upgrade_rejects_rev17_terminal_stream_reopened_by_upsert(
    isolated_revision19_schema: PostgresRoles,
    fact_kind: str,
    terminal_operation: str,
) -> None:
    """A later ordinary upsert cannot grandfather a washed rev17 terminal stream."""

    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    school_id, organization_id, *_ = _seed_revision17_structure_history(isolated_revision19_schema)
    person_id = uuid4()
    target_id = person_id
    fact_source_key = f"person:{person_id}"
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            "INSERT INTO school_people (id, school_id, source_key, status, version) "
            "VALUES (%s, %s, %s, 'active', 3)",
            (person_id, school_id, fact_source_key),
        )
        if fact_kind == "assignment":
            target_id = uuid4()
            fact_source_key = f"assignment:{target_id}"
            connection.execute(
                """
                INSERT INTO staff_assignments (
                    id, school_id, source_key, person_id, organization_unit_id,
                    position_title, semantic_role_code, assignment_kind,
                    appointment_source, approval_reference, valid_from, valid_to,
                    revoked_at, status, version
                ) VALUES (
                    %s, %s, %s, %s, %s, 'legacy teacher', 'teacher',
                    'substantive', 'manual', %s, %s, NULL, NULL, 'confirmed', 3
                )
                """,
                (
                    target_id,
                    school_id,
                    fact_source_key,
                    person_id,
                    organization_id,
                    f"approval:{target_id}",
                    datetime(2026, 8, 1, tzinfo=UTC),
                ),
            )
        elif fact_kind == "login_mapping":
            target_id = uuid4()
            fact_source_key = f"login:{target_id}"
            user_row = connection.execute(
                """
                INSERT INTO users (external_subject, display_name)
                VALUES (%s, 'legacy login')
                RETURNING id
                """,
                (f"legacy-login-{uuid4()}",),
            ).fetchone()
            assert user_row is not None
            user_id = user_row[0]
            connection.execute(
                """
                INSERT INTO account_person_bindings (
                    id, school_id, source_key, account_user_id, person_id,
                    valid_from, valid_to, status, version
                ) VALUES (%s, %s, %s, %s, %s, %s, NULL, 'confirmed', 3)
                """,
                (
                    target_id,
                    school_id,
                    fact_source_key,
                    user_id,
                    person_id,
                    datetime(2026, 8, 1, tzinfo=UTC),
                ),
            )

    _seed_revision17_lineage_sequence(
        isolated_revision19_schema,
        school_id=school_id,
        fact_kind=fact_kind,
        fact_source_key=fact_source_key,
        target_id=target_id,
        operations=("upsert", terminal_operation, "upsert"),
    )

    rejected = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )

    _assert_alembic_guard_55000(rejected, WASHED_TERMINAL_UPGRADE_BLOCKED_MESSAGE)
    _assert_revision(isolated_revision19_schema, "20260820_17")
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        assert connection.execute(
            "SELECT array_agg(operation ORDER BY target_version) "
            "FROM school_fact_change_lineage "
            "WHERE school_id = %s AND fact_kind = %s AND fact_source_key = %s",
            (school_id, fact_kind, fact_source_key),
        ).fetchone() == (["upsert", terminal_operation, "upsert"],)
        target_table = {
            "person": "school_people",
            "assignment": "staff_assignments",
            "login_mapping": "account_person_bindings",
        }[fact_kind]
        expected_status = "active" if fact_kind == "person" else "confirmed"
        assert connection.execute(
            sql.SQL("SELECT status, version FROM {} WHERE id = %s").format(
                sql.Identifier(target_table)
            ),
            (target_id,),
        ).fetchone() == (expected_status, 3)


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_revision18_backfills_one_unambiguous_stable_only_legacy_school(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    """A sole active v1 stable school gains one identifiable migration content slice."""

    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    school_id = uuid4()
    source_key = f"school:{school_id}"
    created_at = datetime(2026, 7, 1, tzinfo=UTC)
    legacy_source_key = f"revision18:legacy-school-v1:{school_id}"
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            """
            INSERT INTO schools (
                id, source_key, school_code, display_name, timezone_name,
                status, version, created_at, updated_at
            ) VALUES (%s, %s, 'LEGACY-V1', 'Legacy School', 'Asia/Shanghai',
                      'active', 1, %s, %s)
            """,
            (school_id, source_key, created_at, created_at),
        )

    upgraded = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    _assert_revision(isolated_revision19_schema, "20260821_18")
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        version_and_source = connection.execute(
            """
            SELECT version.school_id, version.business_code, version.display_name,
                   version.timezone_name, version.lifecycle_status, version.valid_from,
                   version.valid_to, version.version, version.import_batch_id,
                   source.source_key, source.source_kind, source.source_ref
            FROM school_versions AS version
            JOIN school_fact_sources AS source
              ON source.school_id = version.school_id
             AND source.id = version.source_id
            WHERE version.school_id = %s
            """,
            (school_id,),
        ).fetchone()
        snapshot = connection.execute(
            "SELECT school_id, display_name, timezone_name, source_version, "
            "valid_from, valid_to "
            "FROM app.school_fact_school_snapshot_as_of(%s, %s)",
            (school_id, datetime(2026, 7, 2, tzinfo=UTC)),
        ).fetchone()
        assert connection.execute(
            "SELECT count(*) FROM school_fact_change_lineage "
            "WHERE school_id = %s AND fact_kind = 'school'",
            (school_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT "
            "(SELECT count(*) FROM master_data_import_batches WHERE school_id = %s), "
            "(SELECT count(*) FROM master_data_import_units WHERE school_id = %s), "
            "(SELECT count(*) FROM audit_logs WHERE school_id = %s)",
            (school_id, school_id, school_id),
        ).fetchone() == (0, 0, 0)
    assert version_and_source == (
        school_id,
        "LEGACY-V1",
        "Legacy School",
        "Asia/Shanghai",
        "active",
        created_at,
        None,
        1,
        None,
        legacy_source_key,
        "legacy",
        legacy_source_key,
    )
    assert snapshot == (
        school_id,
        "Legacy School",
        "Asia/Shanghai",
        1,
        created_at,
        None,
    )

    downgraded_again = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded_again.returncode == 0, downgraded_again.stdout + downgraded_again.stderr
    _assert_revision(isolated_revision19_schema, "20260820_17")
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        assert connection.execute(
            "SELECT count(*) FROM school_versions WHERE school_id = %s",
            (school_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM school_fact_sources WHERE school_id = %s AND source_key = %s",
            (school_id, legacy_source_key),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT source_key, school_code, display_name, timezone_name, status, version "
            "FROM schools WHERE id = %s",
            (school_id,),
        ).fetchone() == (
            source_key,
            "LEGACY-V1",
            "Legacy School",
            "Asia/Shanghai",
            "active",
            1,
        )
        assert connection.execute(
            "SELECT tgenabled FROM pg_trigger "
            "WHERE tgrelid = 'public.school_versions'::regclass "
            "AND tgname = 'trg_sf1_reject_delete_school_versions' "
            "AND NOT tgisinternal"
        ).fetchone() == ("O",)


@pytest.mark.integration
@pytest.mark.destructive_migration
@pytest.mark.parametrize(
    ("status", "version", "with_lineage"),
    (
        pytest.param("active", 2, False, id="stable-version-two"),
        pytest.param("inactive", 1, False, id="inactive-stable-only"),
        pytest.param("active", 1, True, id="lineage-without-content-slice"),
    ),
)
def test_revision18_upgrade_rejects_ambiguous_stable_only_school_history(
    isolated_revision19_schema: PostgresRoles,
    status: str,
    version: int,
    with_lineage: bool,
) -> None:
    """Migration refuses to invent v1 content for a non-unique legacy school shape."""

    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    school_id = uuid4()
    source_key = f"school:{school_id}"
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            "INSERT INTO schools "
            "(id, source_key, school_code, display_name, timezone_name, status, version) "
            "VALUES (%s, %s, 'AMBIGUOUS', 'Ambiguous School', 'Asia/Shanghai', %s, %s)",
            (school_id, source_key, status, version),
        )
    if with_lineage:
        _seed_revision17_lineage_sequence(
            isolated_revision19_schema,
            school_id=school_id,
            fact_kind="school",
            fact_source_key=source_key,
            target_id=school_id,
            operations=("upsert",),
        )

    rejected = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )

    _assert_alembic_guard_55000(rejected, LEGACY_SCHOOL_UPGRADE_BLOCKED_MESSAGE)
    _assert_revision(isolated_revision19_schema, "20260820_17")
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        assert connection.execute(
            "SELECT status, version FROM schools WHERE id = %s",
            (school_id,),
        ).fetchone() == (status, version)
        assert connection.execute(
            "SELECT count(*) FROM school_versions WHERE school_id = %s",
            (school_id,),
        ).fetchone() == (0,)


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_revision18_downgrade_rejects_distinct_structure_slice_history(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    """Dropping structural slice columns cannot erase a prior parent or kind."""

    first_at = datetime(2026, 8, 1, tzinfo=UTC)
    second_at = datetime(2026, 8, 15, tzinfo=UTC)
    organization_parent_key = f"organization:first:{uuid4().hex}"
    second_parent_key = f"organization:second:{uuid4().hex}"
    organization_child_key = f"organization:child:{uuid4().hex}"
    campus_key = f"location:first:{uuid4().hex}"
    second_campus_key = f"location:second:{uuid4().hex}"
    room_key = f"location:child:{uuid4().hex}"
    with sf2_environment(isolated_revision19_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(
            reader=persistence,
            writer=persistence,
            clock=lambda: NOW,
        )
        initial = _plan(
            label="migration-distinct-structure-v1",
            base_version=1,
            changes=(
                {
                    "operation": "upsert",
                    "fact_kind": "organization",
                    "source_key": organization_parent_key,
                    "expected_version": 0,
                    "effective_at": first_at,
                    "origin": {"sheet_code": "organization_units", "row_number": 2},
                    "content": {
                        "kind": "schoolwide",
                        "display_name": "first parent",
                        "display_path": "/first parent",
                        "status": "active",
                    },
                },
                {
                    "operation": "upsert",
                    "fact_kind": "organization",
                    "source_key": second_parent_key,
                    "expected_version": 0,
                    "effective_at": first_at,
                    "origin": {"sheet_code": "organization_units", "row_number": 3},
                    "content": {
                        "kind": "schoolwide",
                        "display_name": "second parent",
                        "display_path": "/second parent",
                        "status": "active",
                    },
                },
                {
                    "operation": "upsert",
                    "fact_kind": "organization",
                    "source_key": organization_child_key,
                    "expected_version": 0,
                    "effective_at": first_at,
                    "origin": {"sheet_code": "organization_units", "row_number": 4},
                    "content": {
                        "parent_source_key": organization_parent_key,
                        "kind": "department",
                        "display_name": "child",
                        "display_path": "/first parent/child",
                        "status": "active",
                    },
                },
                {
                    "operation": "upsert",
                    "fact_kind": "location",
                    "source_key": campus_key,
                    "expected_version": 0,
                    "effective_at": first_at,
                    "origin": {"sheet_code": "campus_locations", "row_number": 2},
                    "content": {
                        "kind": "campus",
                        "display_name": "first campus",
                        "display_path": "/first campus",
                        "status": "active",
                    },
                },
                {
                    "operation": "upsert",
                    "fact_kind": "location",
                    "source_key": second_campus_key,
                    "expected_version": 0,
                    "effective_at": first_at,
                    "origin": {"sheet_code": "campus_locations", "row_number": 3},
                    "content": {
                        "kind": "campus",
                        "display_name": "second campus",
                        "display_path": "/second campus",
                        "status": "active",
                    },
                },
                {
                    "operation": "upsert",
                    "fact_kind": "location",
                    "source_key": room_key,
                    "expected_version": 0,
                    "effective_at": first_at,
                    "origin": {"sheet_code": "campus_locations", "row_number": 4},
                    "content": {
                        "parent_source_key": campus_key,
                        "kind": "dorm_room",
                        "display_name": "101",
                        "display_path": "/first campus/101",
                        "status": "active",
                    },
                },
            ),
        )
        created = _confirm(
            service=service,
            change_set=initial,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="migration-distinct-structure-v1",
        )
        changed = _plan(
            label="migration-distinct-structure-v2",
            base_version=created.target_version,
            changes=(
                {
                    "operation": "upsert",
                    "fact_kind": "organization",
                    "source_key": organization_child_key,
                    "expected_version": 1,
                    "effective_at": second_at,
                    "origin": {"sheet_code": "organization_units", "row_number": 5},
                    "content": {
                        "parent_source_key": second_parent_key,
                        "kind": "department",
                        "display_name": "child",
                        "display_path": "/second parent/child",
                        "status": "active",
                    },
                },
                {
                    "operation": "upsert",
                    "fact_kind": "location",
                    "source_key": room_key,
                    "expected_version": 1,
                    "effective_at": second_at,
                    "origin": {"sheet_code": "campus_locations", "row_number": 5},
                    "content": {
                        "parent_source_key": second_campus_key,
                        "kind": "dorm_building",
                        "display_name": "A building",
                        "display_path": "/second campus/A building",
                        "status": "active",
                    },
                },
            ),
        )
        _confirm(
            service=service,
            change_set=changed,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="migration-distinct-structure-v2",
        )
        with factory() as session:
            organization_child_id = session.execute(
                text(
                    "SELECT id FROM organization_units "
                    "WHERE school_id = :school_id AND source_key = :source_key"
                ),
                {"school_id": school_id, "source_key": organization_child_key},
            ).scalar_one()
            room_id = session.execute(
                text(
                    "SELECT id FROM campus_locations "
                    "WHERE school_id = :school_id AND source_key = :source_key"
                ),
                {"school_id": school_id, "source_key": room_key},
            ).scalar_one()

        rejected = _run_alembic(
            isolated_revision19_schema,
            "downgrade",
            "20260820_17",
        )
        _assert_alembic_guard_55000(rejected, SEMANTICS_DOWNGRADE_BLOCKED_MESSAGE)
        _assert_revision(isolated_revision19_schema, REVISION_19)
        with isolated_revision19_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            preserved = connection.execute(
                "SELECT "
                "(SELECT count(*) FROM campus_location_versions WHERE location_id = %s), "
                "(SELECT count(*) FROM organization_unit_versions "
                " WHERE organization_unit_id = %s)",
                (room_id, organization_child_id),
            ).fetchone()
    assert preserved == (2, 2)


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_fresh_revision14_to_head_applies_revision19_after_revision18(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260816_14",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    _assert_revision(isolated_revision19_schema, "20260816_14")

    upgraded = _run_alembic(isolated_revision19_schema, "upgrade", "20260824_20")
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    _assert_revision(isolated_revision19_schema, CURRENT_HEAD)


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_revision18_downgrade_guard_preserves_new_semantics_data(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    effective_at = datetime(2026, 8, 21, tzinfo=UTC)
    organization_key = f"organization:downgrade:{uuid4().hex}"
    campus_key = f"location:downgrade:{uuid4().hex}"
    relationship_key = f"organization-campus:downgrade:{uuid4().hex}"
    with sf2_environment(isolated_revision19_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(
            reader=persistence,
            writer=persistence,
            clock=lambda: NOW,
        )
        plan = _plan(
            label="migration-revision18-new-semantics",
            base_version=1,
            changes=(
                {
                    "operation": "upsert",
                    "fact_kind": "organization",
                    "source_key": organization_key,
                    "expected_version": 0,
                    "effective_at": effective_at,
                    "origin": {"sheet_code": "organization_units", "row_number": 2},
                    "content": {
                        "kind": "department",
                        "display_name": "revision 18 guard organization",
                        "display_path": "/revision 18 guard organization",
                        "status": "active",
                    },
                },
                {
                    "operation": "upsert",
                    "fact_kind": "location",
                    "source_key": campus_key,
                    "expected_version": 0,
                    "effective_at": effective_at,
                    "origin": {"sheet_code": "campus_locations", "row_number": 2},
                    "content": {
                        "kind": "campus",
                        "display_name": "revision 18 guard campus",
                        "display_path": "/revision 18 guard campus",
                        "status": "active",
                    },
                },
                {
                    "operation": "upsert",
                    "fact_kind": "organization_campus_relationship",
                    "source_key": relationship_key,
                    "expected_version": 0,
                    "effective_at": effective_at,
                    "origin": {"sheet_code": "organization_campuses", "row_number": 2},
                    "content": {
                        "organization_source_key": organization_key,
                        "campus_source_key": campus_key,
                    },
                },
            ),
        )
        _confirm(
            service=service,
            change_set=plan,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="migration-revision18-new-semantics",
        )
        with factory() as session:
            organization_id, campus_id, relationship_id = session.execute(
                text(
                    "SELECT organization.id, campus.id, relationship.id "
                    "FROM organization_units AS organization "
                    "JOIN campus_locations AS campus ON campus.school_id = organization.school_id "
                    "JOIN organization_campus_relationships AS relationship "
                    "ON relationship.school_id = organization.school_id "
                    "AND relationship.organization_unit_id = organization.id "
                    "AND relationship.campus_location_id = campus.id "
                    "WHERE organization.school_id = :school_id "
                    "AND organization.source_key = :organization_key "
                    "AND campus.source_key = :campus_key "
                    "AND relationship.source_key = :relationship_key"
                ),
                {
                    "school_id": school_id,
                    "organization_key": organization_key,
                    "campus_key": campus_key,
                    "relationship_key": relationship_key,
                },
            ).one()

        rejected = _run_alembic(
            isolated_revision19_schema,
            "downgrade",
            "20260820_17",
        )
        _assert_alembic_guard_55000(rejected, SEMANTICS_DOWNGRADE_BLOCKED_MESSAGE)
        _assert_revision(isolated_revision19_schema, REVISION_19)

        with isolated_revision19_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            preserved = connection.execute(
                """
                SELECT organization_unit_id, campus_location_id, valid_from
                FROM organization_campus_relationships
                WHERE id = %s
                """,
                (relationship_id,),
            ).fetchone()
    assert preserved == (organization_id, campus_id, effective_at)


def _seed_revision17_student_base(
    postgres_roles: PostgresRoles,
) -> tuple[object, ...]:
    school_id, source_id = uuid4(), uuid4()
    person_id, student_id = uuid4(), uuid4()
    with postgres_roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            """
            INSERT INTO schools (
                id, source_key, school_code, display_name, timezone_name, status, version
            ) VALUES (%s, %s, 'rev17-guard', 'revision 17 guard',
                      'Asia/Shanghai', 'active', 1)
            """,
            (school_id, f"school:{school_id}"),
        )
        connection.execute(
            """
            INSERT INTO school_fact_sources (
                id, school_id, source_key, source_kind, source_ref, schema_version
            ) VALUES (%s, %s, %s, 'manual', %s, 1)
            """,
            (source_id, school_id, f"source:{source_id}", f"manual:{source_id}"),
        )
        connection.execute(
            """
            INSERT INTO school_people (id, school_id, source_key, status, version)
            VALUES (%s, %s, %s, 'active', 1)
            """,
            (person_id, school_id, f"person:{person_id}"),
        )
        connection.execute(
            """
            INSERT INTO students (id, school_id, person_id, source_key, status, version)
            VALUES (%s, %s, %s, %s, 'active', 1)
            """,
            (student_id, school_id, person_id, f"student:{student_id}"),
        )
    return school_id, source_id, person_id, student_id


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_revision18_upgrade_guard_preserves_unattributed_student_relationship(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    school_id, source_id, _person_id, student_id = _seed_revision17_student_base(
        isolated_revision19_schema
    )
    relationship_id = uuid4()
    effective_at = datetime(2026, 8, 21, tzinfo=UTC)
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            """
            INSERT INTO student_school_relationships (
                id, school_id, student_id, student_number,
                enrollment_status, residence_mode, valid_from, valid_to,
                version, source_id, import_batch_id
            ) VALUES (%s, %s, %s, '20260001', 'enrolled', 'day',
                      %s, NULL, 1, %s, NULL)
            """,
            (relationship_id, school_id, student_id, effective_at, source_id),
        )

    rejected = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )
    _assert_alembic_guard_55000(rejected, STUDENT_CAMPUS_UPGRADE_BLOCKED_MESSAGE)
    _assert_revision(isolated_revision19_schema, "20260820_17")

    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        preserved = connection.execute(
            """
            SELECT student_id, student_number, residence_mode
            FROM student_school_relationships
            WHERE id = %s
            """,
            (relationship_id,),
        ).fetchone()
    assert preserved == (student_id, "20260001", "day")


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_revision18_upgrade_guard_preserves_populated_residence_edges(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    school_id, source_id, _person_id, student_id = _seed_revision17_student_base(
        isolated_revision19_schema
    )
    campus_id, room_id = uuid4(), uuid4()
    first_bed_id, second_bed_id = uuid4(), uuid4()
    first_residence_id, second_residence_id = uuid4(), uuid4()
    first_at = datetime(2026, 8, 1, tzinfo=UTC)
    transferred_at = datetime(2026, 8, 21, tzinfo=UTC)
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            """
            INSERT INTO campus_locations (
                id, school_id, source_key, parent_location_id, kind, status, version
            ) VALUES
                (%s, %s, %s, NULL, 'campus', 'active', 1),
                (%s, %s, %s, %s, 'dorm_room', 'active', 1)
            """,
            (
                campus_id,
                school_id,
                f"campus:{campus_id}",
                room_id,
                school_id,
                f"room:{room_id}",
                campus_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO campus_location_versions (
                id, school_id, location_id, business_code, display_name, aliases,
                display_path, valid_from, valid_to, source_kind, source_ref, status, version
            ) VALUES
                (%s, %s, %s, 'EDGE-CAMPUS', 'edge campus', ARRAY[]::varchar[],
                 '/edge campus', %s, NULL, 'manual', %s, 'confirmed', 1),
                (%s, %s, %s, 'EDGE-ROOM', 'edge room', ARRAY[]::varchar[],
                 '/edge campus/room', %s, NULL, 'manual', %s, 'confirmed', 1)
            """,
            (
                uuid4(),
                school_id,
                campus_id,
                first_at,
                f"revision17-location:{uuid4().hex}",
                uuid4(),
                school_id,
                room_id,
                first_at,
                f"revision17-location:{uuid4().hex}",
            ),
        )
        connection.execute(
            """
            INSERT INTO student_school_relationships (
                id, school_id, student_id, student_number, enrollment_status,
                residence_mode, valid_from, valid_to, version, source_id, import_batch_id
            ) VALUES (%s, %s, %s, %s, 'enrolled', 'boarding', %s, NULL, 1, %s, NULL)
            """,
            (
                uuid4(),
                school_id,
                student_id,
                f"EDGE-{uuid4().hex[:8]}",
                first_at,
                source_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO dorm_room_profile_versions (
                id, school_id, room_location_id, authorized_bed_capacity,
                accommodation_zone, is_available, accessibility_tags,
                valid_from, valid_to, version, source_id, import_batch_id
            ) VALUES (%s, %s, %s, 2, 'boarding', true, ARRAY[]::varchar[],
                      %s, NULL, 1, %s, NULL)
            """,
            (uuid4(), school_id, room_id, first_at, source_id),
        )
        connection.execute(
            """
            INSERT INTO dorm_beds (id, school_id, room_location_id, source_key)
            VALUES
                (%s, %s, %s, %s),
                (%s, %s, %s, %s)
            """,
            (
                first_bed_id,
                school_id,
                room_id,
                f"bed:{first_bed_id}",
                second_bed_id,
                school_id,
                room_id,
                f"bed:{second_bed_id}",
            ),
        )
        connection.execute(
            """
            INSERT INTO dorm_bed_versions (
                id, school_id, bed_id, business_code, display_name, availability,
                valid_from, valid_to, version, source_id, import_batch_id
            ) VALUES
                (%s, %s, %s, 'EDGE-A', 'edge bed A', 'available',
                 %s, NULL, 1, %s, NULL),
                (%s, %s, %s, 'EDGE-B', 'edge bed B', 'available',
                 %s, NULL, 1, %s, NULL)
            """,
            (
                uuid4(),
                school_id,
                first_bed_id,
                first_at,
                source_id,
                uuid4(),
                school_id,
                second_bed_id,
                first_at,
                source_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO student_bed_residences (
                id, school_id, student_id, bed_id,
                valid_from, valid_to, revoked_at, version,
                source_id, import_batch_id,
                arrived_from_residence_id, departed_to_residence_id
            ) VALUES
                (%s, %s, %s, %s, %s, %s, NULL, 1, %s, NULL, NULL, %s),
                (%s, %s, %s, %s, %s, NULL, NULL, 1, %s, NULL, %s, NULL)
            """,
            (
                first_residence_id,
                school_id,
                student_id,
                first_bed_id,
                first_at,
                transferred_at,
                source_id,
                second_residence_id,
                second_residence_id,
                school_id,
                student_id,
                second_bed_id,
                transferred_at,
                source_id,
                first_residence_id,
            ),
        )

    rejected = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )
    _assert_alembic_guard_55000(rejected, RESIDENCE_UPGRADE_BLOCKED_MESSAGE)
    _assert_revision(isolated_revision19_schema, "20260820_17")

    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        preserved = connection.execute(
            """
            SELECT arrived_from_residence_id, departed_to_residence_id
            FROM student_bed_residences
            WHERE id IN (%s, %s)
            ORDER BY valid_from
            """,
            (first_residence_id, second_residence_id),
        ).fetchall()
    assert preserved == [
        (None, second_residence_id),
        (first_residence_id, None),
    ]


def _seed_revision17_inactive_campus_residence(
    postgres_roles: PostgresRoles,
    *,
    valid_to: datetime | None,
    attributed_terminal: bool = True,
) -> tuple[object, ...]:
    school_id, source_id, _person_id, student_id = _seed_revision17_student_base(postgres_roles)
    campus_id, room_id, bed_id, residence_id = uuid4(), uuid4(), uuid4(), uuid4()
    valid_from = datetime(2026, 8, 1, tzinfo=UTC)
    with postgres_roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            """
            INSERT INTO campus_locations (
                id, school_id, source_key, parent_location_id, kind, status, version
            ) VALUES
                (%s, %s, %s, NULL, 'campus', 'inactive', 2),
                (%s, %s, %s, %s, 'dorm_room', 'active', 1)
            """,
            (
                campus_id,
                school_id,
                f"campus:{campus_id}",
                room_id,
                school_id,
                f"room:{room_id}",
                campus_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO campus_location_versions (
                id, school_id, location_id, business_code, display_name, aliases,
                display_path, valid_from, valid_to, source_kind, source_ref, status, version
            ) VALUES
                (%s, %s, %s, 'INACTIVE-CAMPUS', 'inactive campus',
                 ARRAY[]::varchar[], '/inactive campus', %s, NULL,
                 'manual', %s, 'confirmed', 1),
                (%s, %s, %s, 'INACTIVE-ROOM', 'inactive campus room',
                 ARRAY[]::varchar[], '/inactive campus/room', %s, NULL,
                 'manual', %s, 'confirmed', 1)
            """,
            (
                uuid4(),
                school_id,
                campus_id,
                valid_from,
                f"revision17-location:{uuid4().hex}",
                uuid4(),
                school_id,
                room_id,
                valid_from,
                f"revision17-location:{uuid4().hex}",
            ),
        )
        connection.execute(
            """
            INSERT INTO dorm_beds (id, school_id, room_location_id, source_key)
            VALUES (%s, %s, %s, %s)
            """,
            (bed_id, school_id, room_id, f"bed:{bed_id}"),
        )
        # Build the exact pre-revision-18 state the new upgrade backscan must audit.
        # Revision 17's deferred writer trigger also requires unrelated enrollment/profile
        # fixtures, so disable only that trigger while installing this migration diagnostic.
        connection.execute(
            "ALTER TABLE student_bed_residences DISABLE TRIGGER trg_sf1_validate_student_residence"
        )
        connection.execute(
            """
            INSERT INTO student_bed_residences (
                id, school_id, student_id, bed_id, valid_from, valid_to,
                revoked_at, version, source_id, import_batch_id,
                arrived_from_residence_id, departed_to_residence_id
            ) VALUES (%s, %s, %s, %s, %s, %s, NULL, 1, %s, NULL, NULL, NULL)
            """,
            (
                residence_id,
                school_id,
                student_id,
                bed_id,
                valid_from,
                valid_to,
                source_id,
            ),
        )
        connection.execute(
            "ALTER TABLE student_bed_residences ENABLE TRIGGER trg_sf1_validate_student_residence"
        )
    campus_source_key = f"campus:{campus_id}"
    _seed_revision17_lineage_sequence(
        postgres_roles,
        school_id=school_id,
        fact_kind="location",
        fact_source_key=campus_source_key,
        target_id=campus_id,
        operations=("upsert", "end"),
    )
    if not attributed_terminal:
        with postgres_roles.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            connection.execute(
                """
                UPDATE master_data_import_units AS unit
                SET payload = unit.payload - 'effective_at'
                FROM school_fact_change_lineage AS lineage
                WHERE lineage.school_id = %s
                  AND lineage.fact_kind = 'location'
                  AND lineage.fact_source_key = %s
                  AND lineage.operation = 'end'
                  AND lineage.target_version = 2
                  AND unit.school_id = lineage.school_id
                  AND unit.id = lineage.unit_id
                """,
                (school_id, campus_source_key),
            )
    return school_id, campus_id, residence_id, valid_from, valid_to


def _seed_formal_location_terminal_before_revision17(
    postgres_roles: PostgresRoles,
    *,
    operation: str,
    terminal_at: datetime,
) -> tuple[object, ...]:
    source_key = f"location:migration-terminal-{uuid4().hex}"
    with sf2_environment(postgres_roles) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(
            reader=persistence,
            writer=persistence,
            clock=lambda: NOW,
        )
        with factory() as session:
            actor_organization_source_key = str(
                session.execute(
                    text("SELECT source_key FROM organization_units WHERE school_id = :school_id"),
                    {"school_id": school_id},
                ).scalar_one()
            )
        actor_organization = _plan(
            label=f"migration-actor-organization-{operation}",
            base_version=1,
            changes=(
                {
                    "operation": "upsert",
                    "fact_kind": "organization",
                    "source_key": actor_organization_source_key,
                    "expected_version": 1,
                    "effective_at": datetime(2026, 8, 1, tzinfo=UTC),
                    "origin": {"sheet_code": "organization_units", "row_number": 2},
                    "content": {
                        "kind": "department",
                        "business_code": f"ACTOR-{uuid4().hex[:8]}",
                        "display_name": "migration actor organization",
                        "display_path": "/migration actor organization",
                        "status": "active",
                    },
                },
            ),
        )
        actor_created = _confirm(
            service=service,
            change_set=actor_organization,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label=f"migration-actor-organization-{operation}",
        )
        upsert = _plan(
            label=f"migration-location-upsert-{operation}",
            base_version=actor_created.target_version,
            changes=(
                {
                    "operation": "upsert",
                    "fact_kind": "location",
                    "source_key": source_key,
                    "expected_version": 0,
                    "effective_at": datetime(2026, 8, 1, tzinfo=UTC),
                    "origin": {"sheet_code": "campus_locations", "row_number": 2},
                    "content": {
                        "kind": "campus",
                        "business_code": f"MIG-{source_key[-8:]}",
                        "display_name": "migration terminal campus",
                        "display_path": "/migration terminal campus",
                        "status": "active",
                    },
                },
            ),
        )
        created = _confirm(
            service=service,
            change_set=upsert,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label=f"migration-location-upsert-{operation}",
        )
        terminal_payload = _terminal_change(
            "location",
            source_key,
            1,
            terminal_at,
            3,
        )
        terminal_payload["operation"] = operation
        terminal = _plan(
            label=f"migration-location-{operation}",
            base_version=created.target_version,
            changes=(terminal_payload,),
        )
        _confirm(
            service=service,
            change_set=terminal,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label=f"migration-location-{operation}",
        )
        with factory() as session:
            campus_id = session.execute(
                text(
                    "SELECT id FROM campus_locations "
                    "WHERE school_id = :school_id AND source_key = :source_key"
                ),
                {"school_id": school_id, "source_key": source_key},
            ).scalar_one()
    return school_id, campus_id


def _seed_revision17_residence_below_existing_campus(
    postgres_roles: PostgresRoles,
    *,
    school_id: object,
    campus_id: object,
    valid_from: datetime,
    valid_to: datetime | None,
) -> object:
    source_id, person_id, student_id = uuid4(), uuid4(), uuid4()
    room_id, bed_id, residence_id = uuid4(), uuid4(), uuid4()
    with postgres_roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            """
            INSERT INTO school_fact_sources (
                id, school_id, source_key, source_kind, source_ref, schema_version
            ) VALUES (%s, %s, %s, 'manual', %s, 1)
            """,
            (source_id, school_id, f"source:{source_id}", f"manual:{source_id}"),
        )
        connection.execute(
            """
            INSERT INTO school_people (id, school_id, source_key, status, version)
            VALUES (%s, %s, %s, 'active', 1)
            """,
            (person_id, school_id, f"person:{person_id}"),
        )
        connection.execute(
            """
            INSERT INTO students (id, school_id, person_id, source_key, status, version)
            VALUES (%s, %s, %s, %s, 'active', 1)
            """,
            (student_id, school_id, person_id, f"student:{student_id}"),
        )
        connection.execute(
            """
            INSERT INTO campus_locations (
                id, school_id, source_key, parent_location_id, kind, status, version
            ) VALUES (%s, %s, %s, %s, 'dorm_room', 'active', 1)
            """,
            (room_id, school_id, f"room:{room_id}", campus_id),
        )
        connection.execute(
            """
            INSERT INTO campus_location_versions (
                id, school_id, location_id, business_code, display_name, aliases,
                display_path, valid_from, valid_to, source_kind, source_ref, status, version
            ) VALUES (%s, %s, %s, %s, 'migration residence room',
                      ARRAY[]::varchar[], '/migration residence room', %s, NULL,
                      'manual', %s, 'confirmed', 1)
            """,
            (
                uuid4(),
                school_id,
                room_id,
                f"ROOM-{uuid4().hex[:8]}",
                valid_from,
                f"migration-residence-room:{uuid4().hex}",
            ),
        )
        connection.execute(
            """
            INSERT INTO dorm_beds (id, school_id, room_location_id, source_key)
            VALUES (%s, %s, %s, %s)
            """,
            (bed_id, school_id, room_id, f"bed:{bed_id}"),
        )
        connection.execute(
            "ALTER TABLE student_bed_residences DISABLE TRIGGER trg_sf1_validate_student_residence"
        )
        connection.execute(
            """
            INSERT INTO student_bed_residences (
                id, school_id, student_id, bed_id, valid_from, valid_to,
                revoked_at, version, source_id, import_batch_id,
                arrived_from_residence_id, departed_to_residence_id
            ) VALUES (%s, %s, %s, %s, %s, %s, NULL, 1, %s, NULL, NULL, NULL)
            """,
            (
                residence_id,
                school_id,
                student_id,
                bed_id,
                valid_from,
                valid_to,
                source_id,
            ),
        )
        connection.execute(
            "ALTER TABLE student_bed_residences ENABLE TRIGGER trg_sf1_validate_student_residence"
        )
    return residence_id


def _assert_revision17_structure_is_unambiguous(postgres_roles: PostgresRoles) -> None:
    """Keep the terminal-history migration fixtures honest about every stable root."""

    with postgres_roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        rows = connection.execute(
            """
            SELECT 'location', stable.source_key, stable.status, stable.version,
                   count(DISTINCT version.id),
                   count(DISTINCT version.id) FILTER (WHERE version.status = 'confirmed'),
                   min(version.version),
                   array_remove(array_agg(lineage.operation || ':' || lineage.target_version), NULL)
            FROM campus_locations AS stable
            LEFT JOIN campus_location_versions AS version
              ON version.school_id = stable.school_id
             AND version.location_id = stable.id
            LEFT JOIN school_fact_change_lineage AS lineage
              ON lineage.school_id = stable.school_id
             AND lineage.fact_kind = 'location'
             AND lineage.fact_source_key = stable.source_key
             AND lineage.target_id = stable.id
            GROUP BY stable.id
            UNION ALL
            SELECT 'organization', stable.source_key, stable.status, stable.version,
                   count(DISTINCT version.id),
                   count(DISTINCT version.id) FILTER (WHERE version.status = 'confirmed'),
                   min(version.version),
                   array_remove(array_agg(lineage.operation || ':' || lineage.target_version), NULL)
            FROM organization_units AS stable
            LEFT JOIN organization_unit_versions AS version
              ON version.school_id = stable.school_id
             AND version.organization_unit_id = stable.id
            LEFT JOIN school_fact_change_lineage AS lineage
              ON lineage.school_id = stable.school_id
             AND lineage.fact_kind = 'organization'
             AND lineage.fact_source_key = stable.source_key
             AND lineage.target_id = stable.id
            GROUP BY stable.id
            ORDER BY 1, 2
            """
        ).fetchall()
    assert all(
        row[4] == 1
        and row[5] == 1
        and (
            (row[2] == "active" and row[6] == row[3])
            or (
                row[2] == "inactive"
                and row[3] == row[6] + 1
                and (f"end:{row[3]}" in row[7] or f"revoke:{row[3]}" in row[7])
            )
        )
        for row in rows
    ), rows


def _seed_revision17_relationship_content_candidate(
    postgres_roles: PostgresRoles,
) -> tuple[UUID, int, UUID, UUID, str, UUID, str]:
    """Create one mechanically reconstructable legacy assignment and login mapping."""

    with sf2_environment(postgres_roles) as (factory, _persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        with factory.begin() as session:
            assignment_source_key = session.execute(
                text("SELECT source_key FROM staff_assignments WHERE id=:id"),
                {"id": assignment_id},
            ).scalar_one()
            binding = session.execute(
                text(
                    "SELECT id, source_key FROM account_person_bindings "
                    "WHERE school_id=:school_id AND account_user_id=:user_id"
                ),
                {"school_id": school_id, "user_id": user_id},
            ).one()
    return (
        school_id,
        user_id,
        person_id,
        assignment_id,
        assignment_source_key,
        binding.id,
        binding.source_key,
    )


def _relationship_stable_exclusion_catalog(
    postgres_roles: PostgresRoles,
) -> dict[str, str]:
    """Read the exact rev17 stable relationship exclusions for round-trip comparison."""

    names = (
        "excl_staff_assignments_confirmed_slot_period",
        "excl_account_person_bindings_account_period",
        "excl_account_person_bindings_person_period",
    )
    with postgres_roles.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        rows = connection.execute(
            """
            SELECT constraint_catalog.conname,
                   pg_catalog.pg_get_constraintdef(constraint_catalog.oid, true)
            FROM pg_catalog.pg_constraint AS constraint_catalog
            WHERE constraint_catalog.conname = ANY(%s)
            ORDER BY constraint_catalog.conname
            """,
            (list(names),),
        ).fetchall()
    return {str(name): str(definition) for name, definition in rows}


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_revision18_backfills_unique_legacy_relationship_content_and_round_trips(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    rev17_exclusions = _relationship_stable_exclusion_catalog(isolated_revision19_schema)
    assert set(rev17_exclusions) == {
        "excl_staff_assignments_confirmed_slot_period",
        "excl_account_person_bindings_account_period",
        "excl_account_person_bindings_person_period",
    }
    (
        school_id,
        user_id,
        person_id,
        assignment_id,
        _assignment_source_key,
        binding_id,
        _binding_source_key,
    ) = _seed_revision17_relationship_content_candidate(isolated_revision19_schema)

    upgraded = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        assignment = connection.execute(
            """
            SELECT person_id, position_title, semantic_role_code, effective_from,
                   effective_to, planned_valid_to, target_version
            FROM staff_assignment_versions
            WHERE school_id=%s AND assignment_id=%s
            """,
            (school_id, assignment_id),
        ).fetchone()
        binding = connection.execute(
            """
            SELECT account_user_id, person_id, provider_code, provider_tenant_key,
                   external_subject, effective_from, effective_to, target_version
            FROM account_person_binding_versions
            WHERE school_id=%s AND binding_id=%s
            """,
            (school_id, binding_id),
        ).fetchone()
        user_identity = connection.execute(
            "SELECT provider_code, provider_tenant_key, external_subject, display_name "
            "FROM users WHERE id=%s",
            (user_id,),
        ).fetchone()
        assert user_identity is not None
        binding_columns = {
            str(row[0])
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' "
                "AND table_name='account_person_binding_versions'"
            ).fetchall()
        }
    assert assignment == (
        person_id,
        "数据管理员",
        "school_data_admin",
        NOW - timedelta(days=1),
        None,
        None,
        1,
    )
    assert binding == (
        user_id,
        person_id,
        *user_identity[:3],
        NOW - timedelta(days=1),
        None,
        1,
    )
    assert "display_name" not in binding_columns

    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            "UPDATE users SET display_name='changed login cache' WHERE id=%s",
            (user_id,),
        )

    round_trip = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert round_trip.returncode == 0, round_trip.stdout + round_trip.stderr
    _assert_revision(isolated_revision19_schema, "20260820_17")
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        assert connection.execute(
            "SELECT to_regclass('public.staff_assignment_versions'), "
            "to_regclass('public.account_person_binding_versions')"
        ).fetchone() == (None, None)
        assert connection.execute(
            "SELECT person_id FROM staff_assignments WHERE id=%s",
            (assignment_id,),
        ).fetchone() == (person_id,)
        assert connection.execute(
            "SELECT provider_code, provider_tenant_key, external_subject, display_name "
            "FROM users WHERE id=%s",
            (user_id,),
        ).fetchone() == (*user_identity[:3], "changed login cache")
    assert _relationship_stable_exclusion_catalog(isolated_revision19_schema) == rev17_exclusions


@pytest.mark.integration
@pytest.mark.destructive_migration
@pytest.mark.parametrize("fact_kind", ("assignment", "login_mapping"))
def test_revision18_upgrade_rejects_unreconstructable_multiple_relationship_upserts(
    isolated_revision19_schema: PostgresRoles,
    fact_kind: str,
) -> None:
    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    (
        school_id,
        _user_id,
        _person_id,
        assignment_id,
        assignment_source_key,
        binding_id,
        binding_source_key,
    ) = _seed_revision17_relationship_content_candidate(isolated_revision19_schema)
    _seed_revision17_lineage_sequence(
        isolated_revision19_schema,
        school_id=school_id,
        fact_kind=fact_kind,
        fact_source_key=(
            assignment_source_key if fact_kind == "assignment" else binding_source_key
        ),
        target_id=assignment_id if fact_kind == "assignment" else binding_id,
        operations=("upsert", "upsert"),
    )

    rejected = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )
    _assert_alembic_guard_55000(
        rejected,
        RELATIONSHIP_CONTENT_UPGRADE_BLOCKED_MESSAGE,
    )
    _assert_revision(isolated_revision19_schema, "20260820_17")


@pytest.mark.integration
@pytest.mark.destructive_migration
@pytest.mark.parametrize("fact_kind", ("assignment", "login_mapping"))
def test_revision18_upgrade_rejects_relationship_projection_ahead_of_its_lineage(
    isolated_revision19_schema: PostgresRoles,
    fact_kind: str,
) -> None:
    """One formal upsert cannot justify a later in-place stable-row mutation."""

    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    (
        school_id,
        _user_id,
        _person_id,
        assignment_id,
        assignment_source_key,
        binding_id,
        binding_source_key,
    ) = _seed_revision17_relationship_content_candidate(isolated_revision19_schema)
    target_id = assignment_id if fact_kind == "assignment" else binding_id
    source_key = assignment_source_key if fact_kind == "assignment" else binding_source_key
    _seed_revision17_lineage_sequence(
        isolated_revision19_schema,
        school_id=school_id,
        fact_kind=fact_kind,
        fact_source_key=source_key,
        target_id=target_id,
        operations=("upsert",),
    )
    table_name = "staff_assignments" if fact_kind == "assignment" else "account_person_bindings"
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            sql.SQL("UPDATE {} SET version=2 WHERE id=%s").format(sql.Identifier(table_name)),
            (target_id,),
        )

    rejected = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )
    _assert_alembic_guard_55000(
        rejected,
        RELATIONSHIP_CONTENT_UPGRADE_BLOCKED_MESSAGE,
    )
    _assert_revision(isolated_revision19_schema, "20260820_17")


@pytest.mark.integration
@pytest.mark.destructive_migration
@pytest.mark.parametrize(
    "valid_to",
    (None, datetime(2030, 1, 1, tzinfo=UTC)),
    ids=("open", "future-ended"),
)
def test_revision18_upgrade_rejects_effective_residence_below_inactive_ancestor(
    isolated_revision19_schema: PostgresRoles,
    valid_to: datetime | None,
) -> None:
    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    school_id, campus_id, residence_id, valid_from, expected_valid_to = (
        _seed_revision17_inactive_campus_residence(
            isolated_revision19_schema,
            valid_to=valid_to,
        )
    )

    rejected = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )
    _assert_alembic_guard_55000(
        rejected,
        INACTIVE_CAMPUS_RESIDENCE_UPGRADE_BLOCKED_MESSAGE,
    )
    _assert_revision(isolated_revision19_schema, "20260820_17")
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        preserved = connection.execute(
            """
            SELECT residence.valid_from, residence.valid_to, campus.status
            FROM student_bed_residences AS residence
            JOIN campus_locations AS campus ON campus.id = %s
            WHERE residence.school_id = %s AND residence.id = %s
            """,
            (campus_id, school_id, residence_id),
        ).fetchone()
    assert preserved == (valid_from, expected_valid_to, "inactive")


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_revision18_upgrade_allows_past_ended_residence_below_inactive_ancestor(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    terminal_at = datetime(2026, 8, 15, tzinfo=UTC)
    school_id, campus_id = _seed_formal_location_terminal_before_revision17(
        isolated_revision19_schema,
        operation="end",
        terminal_at=terminal_at,
    )
    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    _assert_revision17_structure_is_unambiguous(isolated_revision19_schema)
    past_valid_to = datetime(2026, 8, 10, tzinfo=UTC)
    valid_from = datetime(2026, 8, 1, tzinfo=UTC)
    residence_id = _seed_revision17_residence_below_existing_campus(
        isolated_revision19_schema,
        school_id=school_id,
        campus_id=campus_id,
        valid_from=valid_from,
        valid_to=past_valid_to,
    )

    upgraded = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    _assert_revision(isolated_revision19_schema, "20260821_18")
    with isolated_revision19_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        preserved = connection.execute(
            """
            SELECT valid_from, valid_to
            FROM student_bed_residences
            WHERE school_id = %s AND id = %s
            """,
            (school_id, residence_id),
        ).fetchone()
    assert preserved == (valid_from, past_valid_to)


@pytest.mark.integration
@pytest.mark.destructive_migration
@pytest.mark.parametrize(
    ("valid_from", "valid_to"),
    (
        (
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 8, 20, tzinfo=UTC),
        ),
        (
            datetime(2026, 8, 15, tzinfo=UTC),
            datetime(2026, 8, 16, tzinfo=UTC),
        ),
    ),
    ids=("crosses-end", "starts-at-end"),
)
def test_revision18_upgrade_rejects_residence_crossing_or_after_formal_location_end(
    isolated_revision19_schema: PostgresRoles,
    valid_from: datetime,
    valid_to: datetime,
) -> None:
    terminal_at = datetime(2026, 8, 15, tzinfo=UTC)
    school_id, campus_id = _seed_formal_location_terminal_before_revision17(
        isolated_revision19_schema,
        operation="end",
        terminal_at=terminal_at,
    )
    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    _assert_revision17_structure_is_unambiguous(isolated_revision19_schema)
    _seed_revision17_residence_below_existing_campus(
        isolated_revision19_schema,
        school_id=school_id,
        campus_id=campus_id,
        valid_from=valid_from,
        valid_to=valid_to,
    )

    rejected = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )
    _assert_alembic_guard_55000(
        rejected,
        INACTIVE_CAMPUS_RESIDENCE_UPGRADE_BLOCKED_MESSAGE,
    )
    _assert_revision(isolated_revision19_schema, "20260820_17")


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_revision18_upgrade_rejects_all_residence_history_below_formally_revoked_location(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    terminal_at = datetime(2026, 8, 15, tzinfo=UTC)
    school_id, campus_id = _seed_formal_location_terminal_before_revision17(
        isolated_revision19_schema,
        operation="revoke",
        terminal_at=terminal_at,
    )
    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    _assert_revision17_structure_is_unambiguous(isolated_revision19_schema)
    _seed_revision17_residence_below_existing_campus(
        isolated_revision19_schema,
        school_id=school_id,
        campus_id=campus_id,
        valid_from=datetime(2026, 8, 1, tzinfo=UTC),
        valid_to=datetime(2026, 8, 10, tzinfo=UTC),
    )

    rejected = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )
    _assert_alembic_guard_55000(
        rejected,
        INACTIVE_CAMPUS_RESIDENCE_UPGRADE_BLOCKED_MESSAGE,
    )
    _assert_revision(isolated_revision19_schema, "20260820_17")


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_revision18_upgrade_fails_closed_for_past_residence_below_unattributed_inactive_location(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    downgraded = _run_alembic(
        isolated_revision19_schema,
        "downgrade",
        "20260820_17",
    )
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    _seed_revision17_inactive_campus_residence(
        isolated_revision19_schema,
        valid_to=datetime(2026, 8, 10, tzinfo=UTC),
        attributed_terminal=False,
    )

    rejected = _run_alembic(
        isolated_revision19_schema,
        "upgrade",
        "20260821_18",
    )
    _assert_alembic_guard_55000(
        rejected,
        INACTIVE_CAMPUS_RESIDENCE_UPGRADE_BLOCKED_MESSAGE,
    )
    _assert_revision(isolated_revision19_schema, "20260820_17")


@pytest.mark.integration
def test_revision18_new_relation_facts_reject_physical_delete(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    valid_from = datetime(2026, 8, 21, tzinfo=UTC)
    organization_key = f"organization:delete-guard:{uuid4().hex}"
    campus_key = f"location:delete-guard:{uuid4().hex}"
    policy_key = f"policy:delete-guard:{uuid4().hex}"
    relationship_key = f"organization-campus:delete-guard:{uuid4().hex}"
    scope_key = f"policy-scope:delete-guard:{uuid4().hex}"
    with sf2_environment(isolated_revision19_schema) as (_factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(
            reader=persistence,
            writer=persistence,
            clock=lambda: NOW,
        )
        plan = _plan(
            label="migration-revision18-delete-guards",
            base_version=1,
            changes=(
                {
                    "operation": "upsert",
                    "fact_kind": "organization",
                    "source_key": organization_key,
                    "expected_version": 0,
                    "effective_at": valid_from,
                    "origin": {"sheet_code": "organization_units", "row_number": 2},
                    "content": {
                        "kind": "department",
                        "display_name": "delete guard organization",
                        "display_path": "/delete guard organization",
                        "status": "active",
                    },
                },
                {
                    "operation": "upsert",
                    "fact_kind": "location",
                    "source_key": campus_key,
                    "expected_version": 0,
                    "effective_at": valid_from,
                    "origin": {"sheet_code": "campus_locations", "row_number": 2},
                    "content": {
                        "kind": "campus",
                        "display_name": "delete guard campus",
                        "display_path": "/delete guard campus",
                        "status": "active",
                    },
                },
                {
                    "operation": "upsert",
                    "fact_kind": "policy",
                    "source_key": policy_key,
                    "expected_version": 0,
                    "effective_at": valid_from,
                    "origin": {"sheet_code": "policies", "row_number": 2},
                    "content": {},
                },
                {
                    "operation": "upsert",
                    "fact_kind": "organization_campus_relationship",
                    "source_key": relationship_key,
                    "expected_version": 0,
                    "effective_at": valid_from,
                    "origin": {"sheet_code": "organization_campuses", "row_number": 2},
                    "content": {
                        "organization_source_key": organization_key,
                        "campus_source_key": campus_key,
                    },
                },
                {
                    "operation": "upsert",
                    "fact_kind": "policy_scope",
                    "source_key": scope_key,
                    "expected_version": 0,
                    "effective_at": valid_from,
                    "origin": {"sheet_code": "policy_scopes", "row_number": 2},
                    "content": {
                        "policy_source_key": policy_key,
                        "campus_source_key": campus_key,
                    },
                },
            ),
        )
        _confirm(
            service=service,
            change_set=plan,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="migration-revision18-delete-guards",
        )
        with isolated_revision19_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            relationship_id = connection.execute(
                "SELECT id FROM organization_campus_relationships "
                "WHERE school_id = %s AND source_key = %s",
                (school_id, relationship_key),
            ).fetchone()
            scope_id = connection.execute(
                "SELECT id FROM school_policy_scopes WHERE school_id = %s AND source_key = %s",
                (school_id, scope_key),
            ).fetchone()
            assert relationship_id is not None and scope_id is not None
            for table_name, row_id in (
                ("organization_campus_relationships", relationship_id[0]),
                ("school_policy_scopes", scope_id[0]),
            ):
                with (
                    pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState),
                    connection.transaction(),
                ):
                    connection.execute(
                        sql.SQL("DELETE FROM {} WHERE id = %s").format(sql.Identifier(table_name)),
                        (row_id,),
                    )
            remaining = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM organization_campus_relationships WHERE id = %s),
                    (SELECT count(*) FROM school_policy_scopes WHERE id = %s)
                """,
                (relationship_id[0], scope_id[0]),
            ).fetchone()
    assert remaining == (1, 1)


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_upgrade_14_to_15_preserves_legacy_rows_and_backfills_stable_keys(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    current_master_data_schema = isolated_revision19_schema
    school_id, person_id, binding_id = uuid4(), uuid4(), uuid4()
    unit_id, unit_version_id, assignment_id = uuid4(), uuid4(), uuid4()
    location_id, location_version_id, student_id = uuid4(), uuid4(), uuid4()
    legacy_subject = f"legacy-upgrade-{uuid4()}"
    valid_from = datetime(2025, 9, 1, tzinfo=UTC)
    user_id: int | None = None
    at_revision_14 = False
    downgraded = _run_alembic(current_master_data_schema, "downgrade", "20260816_14")
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    at_revision_14 = True

    try:
        with current_master_data_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            user_row = connection.execute(
                """
                INSERT INTO users (external_subject, display_name)
                VALUES (%s, '旧登录账号')
                RETURNING id
                """,
                (legacy_subject,),
            ).fetchone()
            assert user_row is not None
            user_id = user_row[0]
            connection.execute(
                """
                INSERT INTO schools (
                    id, school_code, display_name, timezone_name, status, version
                ) VALUES (%s, 'legacy-school', '旧学校', 'Asia/Shanghai', 'active', 1)
                """,
                (school_id,),
            )
            connection.execute(
                """
                INSERT INTO school_people (id, school_id, status, version)
                VALUES (%s, %s, 'active', 1)
                """,
                (person_id, school_id),
            )
            connection.execute(
                """
                INSERT INTO account_person_bindings (
                    id, school_id, account_user_id, person_id,
                    valid_from, valid_to, status, version
                ) VALUES (%s, %s, %s, %s, %s, NULL, 'confirmed', 1)
                """,
                (binding_id, school_id, user_id, person_id, valid_from),
            )
            connection.execute(
                """
                INSERT INTO organization_units (
                    id, school_id, parent_organization_unit_id, kind, status, version
                ) VALUES (%s, %s, NULL, 'department', 'active', 1)
                """,
                (unit_id, school_id),
            )
            connection.execute(
                """
                INSERT INTO organization_unit_versions (
                    id, school_id, organization_unit_id, display_name, short_name,
                    display_path, valid_from, valid_to, source_kind, source_ref,
                    status, version
                ) VALUES (%s, %s, %s, '旧教务处', '教务处', '/旧教务处',
                          %s, NULL, 'manual', %s, 'confirmed', 1)
                """,
                (unit_version_id, school_id, unit_id, valid_from, f"legacy:{unit_version_id}"),
            )
            connection.execute(
                """
                INSERT INTO staff_assignments (
                    id, school_id, person_id, organization_unit_id, position_title,
                    semantic_role_code, appointment_source, approval_reference,
                    valid_from, valid_to, status, version
                ) VALUES (%s, %s, %s, %s, '教师', 'teacher', 'manual', %s,
                          %s, NULL, 'confirmed', 1)
                """,
                (
                    assignment_id,
                    school_id,
                    person_id,
                    unit_id,
                    f"approval:{assignment_id}",
                    valid_from,
                ),
            )
            connection.execute(
                """
                INSERT INTO campus_locations (
                    id, school_id, parent_location_id, kind, status, version
                ) VALUES (%s, %s, NULL, 'campus', 'active', 1)
                """,
                (location_id, school_id),
            )
            connection.execute(
                """
                INSERT INTO campus_location_versions (
                    id, school_id, location_id, display_name, aliases, display_path,
                    valid_from, valid_to, source_kind, source_ref, status, version
                ) VALUES (%s, %s, %s, '旧校区', ARRAY['旧校区'], '/旧校区',
                          %s, NULL, 'manual', %s, 'confirmed', 1)
                """,
                (
                    location_version_id,
                    school_id,
                    location_id,
                    valid_from,
                    f"legacy:{location_version_id}",
                ),
            )
            connection.execute(
                """
                INSERT INTO students (id, school_id, person_id, status, version)
                VALUES (%s, %s, %s, 'active', 1)
                """,
                (student_id, school_id, person_id),
            )

        upgraded = _run_alembic(current_master_data_schema, "upgrade", "20260819_15")
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
        at_revision_14 = False

        with current_master_data_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            revision = connection.execute(
                "SELECT version_num FROM public.alembic_version"
            ).fetchone()
            school = connection.execute(
                "SELECT school_code, source_key FROM schools WHERE id = %s",
                (school_id,),
            ).fetchone()
            person = connection.execute(
                "SELECT source_key FROM school_people WHERE id = %s",
                (person_id,),
            ).fetchone()
            compatibility = connection.execute(
                """
                SELECT
                    binding.source_key,
                    unit.source_key,
                    assignment.source_key,
                    assignment.assignment_kind,
                    assignment.revoked_at,
                    location.source_key,
                    student.source_key,
                    unit_version.business_code,
                    location_version.business_code,
                    account.provider_code,
                    account.provider_tenant_key,
                    binding.account_user_id,
                    binding.person_id,
                    assignment.person_id,
                    assignment.organization_unit_id,
                    student.person_id
                FROM account_person_bindings AS binding
                JOIN users AS account ON account.id = binding.account_user_id
                JOIN organization_units AS unit ON unit.id = %s
                JOIN organization_unit_versions AS unit_version
                  ON unit_version.id = %s
                JOIN staff_assignments AS assignment ON assignment.id = %s
                JOIN campus_locations AS location ON location.id = %s
                JOIN campus_location_versions AS location_version
                  ON location_version.id = %s
                JOIN students AS student ON student.id = %s
                WHERE binding.id = %s
                """,
                (
                    unit_id,
                    unit_version_id,
                    assignment_id,
                    location_id,
                    location_version_id,
                    student_id,
                    binding_id,
                ),
            ).fetchone()

        assert revision == ("20260819_15",)
        assert school is not None and school[0] == "legacy-school"
        assert school[1] == f"legacy:schools:{school_id}"
        assert person == (f"legacy:school_people:{person_id}",)
        assert compatibility == (
            f"legacy:account_person_bindings:{binding_id}",
            f"legacy:organization_units:{unit_id}",
            f"legacy:staff_assignments:{assignment_id}",
            "substantive",
            None,
            f"legacy:campus_locations:{location_id}",
            f"legacy:students:{student_id}",
            None,
            None,
            "legacy",
            "legacy",
            user_id,
            person_id,
            person_id,
            unit_id,
            person_id,
        )

        round_trip = _run_alembic(
            current_master_data_schema,
            "downgrade",
            "20260816_14",
        )
        assert round_trip.returncode == 0, round_trip.stdout + round_trip.stderr
        at_revision_14 = True
        with current_master_data_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            preserved = connection.execute(
                """
                SELECT binding.account_user_id, binding.person_id,
                       assignment.person_id, assignment.organization_unit_id,
                       student.person_id
                FROM account_person_bindings AS binding
                JOIN staff_assignments AS assignment ON assignment.id = %s
                JOIN students AS student ON student.id = %s
                WHERE binding.id = %s
                """,
                (assignment_id, student_id, binding_id),
            ).fetchone()
        assert preserved == (user_id, person_id, person_id, unit_id, person_id)
    finally:
        if not at_revision_14:
            returned = _run_alembic(
                current_master_data_schema,
                "downgrade",
                "20260816_14",
            )
            assert returned.returncode == 0, returned.stdout + returned.stderr
        if user_id is not None:
            with current_master_data_schema.migration_connection() as connection:
                connection.execute("SET ROLE campus_ai_db_owner")
                connection.execute("DELETE FROM staff_assignments WHERE id = %s", (assignment_id,))
                connection.execute(
                    "DELETE FROM organization_unit_versions WHERE id = %s",
                    (unit_version_id,),
                )
                connection.execute(
                    "DELETE FROM account_person_bindings WHERE id = %s",
                    (binding_id,),
                )
                connection.execute("DELETE FROM students WHERE id = %s", (student_id,))
                connection.execute(
                    "DELETE FROM campus_location_versions WHERE id = %s",
                    (location_version_id,),
                )
                connection.execute("DELETE FROM campus_locations WHERE id = %s", (location_id,))
                connection.execute("DELETE FROM organization_units WHERE id = %s", (unit_id,))
                connection.execute("DELETE FROM school_people WHERE id = %s", (person_id,))
                connection.execute("DELETE FROM schools WHERE id = %s", (school_id,))
                connection.execute("DELETE FROM users WHERE id = %s", (user_id,))
        restored = _run_alembic(current_master_data_schema, "upgrade", "20260824_20")
        assert restored.returncode == 0, restored.stdout + restored.stderr


@pytest.mark.integration
def test_alembic_check_reports_no_schema_drift(
    school_facts_f1_schema: PostgresRoles,
) -> None:
    checked = _run_alembic(school_facts_f1_schema, "check")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "No new upgrade operations detected" in checked.stdout


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_downgrade_is_blocked_when_sf1_authoritative_rows_exist(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    current_master_data_schema = isolated_revision19_schema
    school_id, source_id = uuid4(), uuid4()
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            """
            INSERT INTO schools (
                id, source_key, school_code, display_name, timezone_name, status, version
            ) VALUES (%s, 'downgrade-school', 'down', '降级测试',
                      'Asia/Shanghai', 'active', 1)
            """,
            (school_id,),
        )
        connection.execute(
            """
            INSERT INTO school_fact_sources (
                id, school_id, source_key, source_kind, source_ref, schema_version
            ) VALUES (%s, %s, 'downgrade-source', 'manual', 'manual:downgrade', 1)
            """,
            (source_id, school_id),
        )

    try:
        rejected = _run_alembic(current_master_data_schema, "downgrade", "20260816_14")
        combined = rejected.stdout + rejected.stderr
        assert rejected.returncode != 0
        assert DOWNGRADE_BLOCKED_MESSAGE in combined

        with current_master_data_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            revision = connection.execute(
                "SELECT version_num FROM public.alembic_version"
            ).fetchone()
            source = connection.execute(
                "SELECT source_key FROM school_fact_sources WHERE id = %s",
                (source_id,),
            ).fetchone()
        assert revision == (REVISION_19,)
        assert source == ("downgrade-source",)
    finally:
        with current_master_data_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            connection.execute("DELETE FROM school_fact_sources WHERE id = %s", (source_id,))
            connection.execute("DELETE FROM schools WHERE id = %s", (school_id,))


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_downgrade_is_blocked_when_a_new_stable_source_key_would_be_lost(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    current_master_data_schema = isolated_revision19_schema
    school_id = uuid4()
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            """
            INSERT INTO schools (
                id, source_key, school_code, display_name, timezone_name, status, version
            ) VALUES (%s, 'new-stable-school', 'new-stable', '新稳定学校',
                      'Asia/Shanghai', 'active', 1)
            """,
            (school_id,),
        )

    try:
        rejected = _run_alembic(current_master_data_schema, "downgrade", "20260816_14")
        combined = rejected.stdout + rejected.stderr
        assert rejected.returncode != 0
        assert DOWNGRADE_BLOCKED_MESSAGE in combined
    finally:
        with current_master_data_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            connection.execute("DELETE FROM schools WHERE id = %s", (school_id,))


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_downgrade_preserves_cross_namespace_identity_subjects_without_loss(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    current_master_data_schema = isolated_revision19_schema
    subject = f"cross-namespace-downgrade-{uuid4()}"
    with current_master_data_schema.migration_connection() as connection:
        connection.execute("SET ROLE campus_ai_db_owner")
        connection.execute(
            """
            INSERT INTO users (
                provider_code, provider_tenant_key, external_subject, display_name
            ) VALUES
                ('dingtalk', 'school-a', %s, '甲校用户'),
                ('dingtalk', 'school-b', %s, '乙校用户')
            """,
            (subject, subject),
        )

    try:
        rejected = _run_alembic(current_master_data_schema, "downgrade", "20260816_14")
        combined = rejected.stdout + rejected.stderr
        assert rejected.returncode != 0
        assert DOWNGRADE_BLOCKED_MESSAGE in combined

        with current_master_data_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            revision = connection.execute(
                "SELECT version_num FROM public.alembic_version"
            ).fetchone()
            rows = connection.execute(
                """
                SELECT provider_tenant_key, external_subject
                FROM users
                WHERE provider_code = 'dingtalk' AND external_subject = %s
                ORDER BY provider_tenant_key
                """,
                (subject,),
            ).fetchall()
        assert revision == (REVISION_19,)
        assert rows == [("school-a", subject), ("school-b", subject)]
    finally:
        with current_master_data_schema.migration_connection() as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            connection.execute(
                "DELETE FROM users WHERE provider_code = 'dingtalk' AND external_subject = %s",
                (subject,),
            )


def test_revision20_is_the_unique_revision19_successor() -> None:
    assert SF5B_MIGRATION_PATH.is_file(), SF5B_MIGRATION_PATH
    source = SF5B_MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "20260824_20"' in source
    assert 'down_revision = "20260823_19"' in source

    configuration = Config(str(API_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(API_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(configuration)
    assert scripts.get_revision("20260824_20").down_revision == "20260823_19"
    assert scripts.get_heads() == ["20260905_22"]
    assert scripts.get_revision("20260905_21").down_revision == "20260824_20"
    assert scripts.get_revision("20260905_22").down_revision == "20260905_21"
