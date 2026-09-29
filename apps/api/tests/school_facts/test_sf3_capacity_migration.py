from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.composition.school_fact_persistence import SchoolFactPersistenceError
from app.platform.contracts import SchoolId
from app.platform.school_facts.application import (
    SchoolFactChangeService,
)
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1
from tests.master_data.conftest import PostgresRoles
from tests.school_facts.test_sf2_transaction import (
    context_for,
    sf2_environment,
)

API_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = API_ROOT / "alembic/versions/20260820_17_school_fact_import_capacity.py"
MODEL_PATH = API_ROOT / "app/platform/master_data_imports/models.py"
PLAN_PATH = API_ROOT.parents[1] / "docs/superpowers/plans/2026-08-19-学校事实层SF1-SF7实施计划.md"
NOW = datetime(2026, 8, 20, 8, tzinfo=UTC)


def _run_alembic(
    roles: PostgresRoles,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    migration = roles.migration_connection_parameters
    environment = os.environ.copy()
    environment.update(
        {
            "POSTGRES_MIGRATION_HOST": str(migration["host"]),
            "POSTGRES_MIGRATION_PORT": str(migration["port"]),
            "POSTGRES_MIGRATION_DB": str(migration["dbname"]),
            "POSTGRES_MIGRATION_USER": str(migration["user"]),
            "POSTGRES_MIGRATION_PASSWORD": str(migration["password"]),
            "POSTGRES_MIGRATION_CONTEXT_SIGNING_KEY": (
                "master-data-schema-context-signing-key-32-bytes"
            ),
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=API_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )


def _policy_change_set(
    *,
    count: int,
    source_sha256: str,
    key_prefix: str = "policy:capacity",
    base_version: int = 1,
) -> CanonicalFactChangeSetV1:
    return CanonicalFactChangeSetV1.model_validate(
        {
            "source_sha256": source_sha256,
            "base_version": base_version,
            "changes": tuple(
                {
                    "operation": "upsert",
                    "fact_kind": "policy",
                    "source_key": f"{key_prefix}:{index:05d}",
                    "expected_version": 0,
                    "effective_at": NOW,
                    "origin": {
                        "sheet_code": f"capacity_{(index - 1) // 10_000 + 1}",
                        "row_number": (index - 1) % 10_000 + 2,
                    },
                    "content": {},
                }
                for index in range(1, count + 1)
            ),
        }
    )


def test_revision_17_alone_expands_sf3_outcome_capacity_and_has_safe_downgrade_guard() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert 'revision = "20260820_17"' in source
    assert 'down_revision = "20260820_16"' in source
    assert "cardinality(succeeded_units) <= 50000" in source
    assert "cardinality(failed_units) <= 50000" in source
    assert "cardinality(executed_units) <= 50000" in source
    assert "revision 17 contains outcomes larger " in source
    assert "than revision 16 can represent" in source
    assert "cardinality(succeeded_units) <= 10000" in source
    assert "cardinality(failed_units) <= 10000" in source
    assert "cardinality(executed_units) <= 10000" in source
    assert "verify_master_data_import_units_insert_terminal_consistency" in source
    assert "verify_master_data_import_units_update_terminal_consistency" in source
    assert "AFTER INSERT ON public.master_data_import_units" in source
    assert "AFTER UPDATE ON public.master_data_import_units" in source
    assert "OLD.unit_kind = 'school_fact'" in source
    assert "NEW.unit_kind = 'school_fact'" in source
    assert "OLD.status = 'ready'" in source
    assert "NEW.status = 'applied'" in source
    assert "OLD.batch_id IS NOT DISTINCT FROM NEW.batch_id" in source
    assert "OLD.sequence IS NOT DISTINCT FROM NEW.sequence" in source
    assert "NEW.version = OLD.version + 1" in source
    assert "NEW.target_id IS NULL AND NEW.target_version IS NULL" in source
    assert "NEW.target_id IS NOT NULL AND NEW.target_version >= 1" in source
    assert "WHEN (NEW.unit_kind IS DISTINCT FROM 'school_fact')" not in source
    assert "WHEN (NOT (" not in source
    assert "enforce_school_fact_unit_update_terminal_consistency" in source
    assert "v_batch_status IN ('applied', 'published')" in source
    assert "school fact unit transition requires terminal batch" in source
    assert (
        source.count(
            "CREATE CONSTRAINT TRIGGER verify_master_data_import_units_terminal_consistency"
        )
        == 1
    )


def test_runtime_contract_and_orm_match_the_50000_capacity() -> None:
    changes_field = CanonicalFactChangeSetV1.model_fields["changes"]
    metadata_text = repr(changes_field.metadata)
    model_source = MODEL_PATH.read_text(encoding="utf-8")

    assert "max_length=50000" in metadata_text
    assert "cardinality(succeeded_units) <= 50000" in model_source
    assert "cardinality(failed_units) <= 50000" in model_source
    assert "cardinality(executed_units) <= 50000" in model_source


def test_plan_locks_sf3_17_sf4_none_checkpoint3_18_sf5a_19_and_sf5b_20() -> None:
    plan = PLAN_PATH.read_text(encoding="utf-8")

    assert "20260820_17_school_fact_import_capacity.py" in plan
    assert "SF4 不产生数据库迁移、不占用 revision 编号" in plan
    assert "apps/api/alembic/versions/20260821_18_school_fact_semantics.py" in plan
    assert '`revision = "20260821_18"`' in plan
    assert '`down_revision = "20260820_17"`' in plan
    assert "Checkpoint 3 所有 pre-SF4 学校事实 Schema 语义修正的唯一 migration" in plan
    assert "不得拆分，也不得并列第二个 revision" in plan
    assert "20260823_19_governance_authority.py" in plan
    assert "`revision 19` 唯一归属 **SF5A 所需的生产持久化数据库变更**" in plan
    assert (API_ROOT / "alembic/versions/20260824_20_school_database_boundary.py").is_file()
    assert "`revision 20` 归属 **SF5B 所需的数据库层变更**" in plan
    assert "20260821_19_school_fact_importer_acl.py" not in plan
    assert "20260819_17_school_fact_importer_acl.py" not in plan
    assert "20260821_18_school_fact_importer_acl.py" not in plan
    assert "migration 18（`down_revision=20260820_17`）" not in plan


def test_canonical_change_set_rejects_50001_before_any_writer_can_receive_it() -> None:
    with pytest.raises(ValidationError):
        _policy_change_set(count=50_001, source_sha256="b" * 64)


@pytest.mark.integration
@pytest.mark.destructive_migration
def test_revision_17_supports_one_complete_50000_change_confirmation_and_guards_downgrade(
    isolated_revision19_schema: PostgresRoles,
) -> None:
    current_master_data_schema = isolated_revision19_schema
    """真实走写核证明 16 拒绝、17 完整容纳且降级不会截断终态。"""

    downgraded = _run_alembic(current_master_data_schema, "downgrade", "20260820_16")
    assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
    try:
        oversized_for_16 = _policy_change_set(count=10_001, source_sha256="1" * 64)
        with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
            school_id, user_id, person_id, assignment_id = facts
            with factory() as session:
                revision_16_constraint = str(
                    session.execute(
                        text(
                            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                            "WHERE conrelid = 'master_data_import_command_outcomes'::regclass "
                            "AND conname = 'ck_master_data_import_command_outcomes_unit_limits'"
                        )
                    ).scalar_one()
                )
            assert "10000" in revision_16_constraint
            assert "50000" not in revision_16_constraint
            with factory() as session:
                revision_16_unit_triggers = tuple(
                    session.execute(
                        text(
                            "SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger "
                            "WHERE tgrelid = 'master_data_import_units'::regclass "
                            "AND tgname LIKE "
                            "'verify_master_data_import_units%terminal_consistency' "
                            "ORDER BY tgname"
                        )
                    )
                )
                assert (
                    session.execute(
                        text(
                            "SELECT to_regprocedure("
                            "'app_private."
                            "enforce_school_fact_unit_update_terminal_consistency()'"
                            ")"
                        )
                    ).scalar_one_or_none()
                    is None
                )
            assert len(revision_16_unit_triggers) == 1
            assert revision_16_unit_triggers[0][0] == (
                "verify_master_data_import_units_terminal_consistency"
            )
            assert "AFTER INSERT OR UPDATE" in str(revision_16_unit_triggers[0][1])
            context = context_for(
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                assignment_id=assignment_id,
                key=f"sf3-capacity-16:{uuid4().hex}",
            )
            service = SchoolFactChangeService(
                reader=persistence,
                writer=persistence,
                clock=lambda: NOW,
            )
            receipt = service.prepare_confirmation(
                change_set=oversized_for_16,
                context=context,
                session_binding="sf3-capacity-revision-16",
            )
            with pytest.raises(SchoolFactPersistenceError):
                service.confirm(
                    credential=receipt.credential,
                    change_set=oversized_for_16,
                    context=context,
                    session_binding="sf3-capacity-revision-16",
                )
            with factory() as session:
                assert (
                    session.execute(
                        text("SELECT count(*) FROM school_policies WHERE school_id = :school_id"),
                        {"school_id": school_id},
                    ).scalar_one()
                    == 0
                )
                assert (
                    session.execute(
                        text(
                            "SELECT count(*) FROM school_fact_change_lineage "
                            "WHERE school_id = :school_id"
                        ),
                        {"school_id": school_id},
                    ).scalar_one()
                    == 0
                )
                assert (
                    session.execute(
                        text("SELECT version FROM schools WHERE id = :school_id"),
                        {"school_id": school_id},
                    ).scalar_one()
                    == 1
                )
            upgraded = _run_alembic(current_master_data_schema, "upgrade", "20260820_17")
            assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
            revision_17_result = service.confirm(
                credential=receipt.credential,
                change_set=oversized_for_16,
                context=context,
                session_binding="sf3-capacity-revision-16",
            )
            assert revision_17_result.changed_count == 10_001
            assert revision_17_result.target_version == 2
            with factory() as session:
                assert (
                    session.execute(
                        text(
                            "SELECT cardinality(succeeded_units) "
                            "FROM master_data_import_command_outcomes WHERE batch_id = :batch_id"
                        ),
                        {"batch_id": revision_17_result.batch_id},
                    ).scalar_one()
                    == 10_001
                )

        complete = _policy_change_set(count=50_000, source_sha256="5" * 64)
        with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
            school_id, user_id, person_id, assignment_id = facts
            context = context_for(
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                assignment_id=assignment_id,
                key=f"sf3-capacity-17:{uuid4().hex}",
            )
            service = SchoolFactChangeService(
                reader=persistence,
                writer=persistence,
                clock=lambda: NOW,
            )

            decoy = _policy_change_set(count=1, source_sha256="d" * 64)
            decoy_context = context_for(
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                assignment_id=assignment_id,
                key=f"sf3-capacity-decoy:{uuid4().hex}",
            )
            decoy_receipt = service.prepare_confirmation(
                change_set=decoy,
                context=decoy_context,
                session_binding="sf3-capacity-decoy",
            )
            with factory() as session:
                decoy_batch_id, decoy_unit_id = session.execute(
                    text(
                        "SELECT batch.id, unit.id FROM master_data_import_batches AS batch "
                        "JOIN master_data_import_units AS unit ON unit.batch_id = batch.id "
                        "WHERE batch.confirmation_token_sha256 = :credential_sha256"
                    ),
                    {
                        "credential_sha256": sha256(
                            decoy_receipt.credential.encode("ascii")
                        ).hexdigest()
                    },
                ).one()

            with factory() as session:
                with pytest.raises(DBAPIError):
                    session.execute(
                        text(
                            "UPDATE master_data_import_units SET "
                            "status = 'applied', version = version + 1 "
                            "WHERE id = :unit_id"
                        ),
                        {"unit_id": decoy_unit_id},
                    )
                    session.commit()
                session.rollback()
            with factory() as session:
                assert session.execute(
                    text(
                        "SELECT batch_id, status, version "
                        "FROM master_data_import_units WHERE id = :unit_id"
                    ),
                    {"unit_id": decoy_unit_id},
                ).one() == (decoy_batch_id, "ready", 1)

            preflight = service.preflight(
                school_id=SchoolId(school_id),
                change_set=complete,
            )
            assert preflight.is_clean is True
            receipt = service.prepare_confirmation(
                change_set=complete,
                context=context,
                session_binding="sf3-capacity-revision-17",
            )
            result = service.confirm(
                credential=receipt.credential,
                change_set=complete,
                context=context,
                session_binding="sf3-capacity-revision-17",
            )

            assert result.changed_count == 50_000
            assert result.target_version == 2
            with factory() as session:
                counts = session.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM master_data_import_units "
                        "WHERE batch_id = :batch_id), "
                        "(SELECT count(*) FROM school_fact_change_lineage "
                        "WHERE batch_id = :batch_id), "
                        "(SELECT count(*) FROM school_policies WHERE school_id = :school_id), "
                        "(SELECT count(*) FROM master_data_import_batches WHERE id = :batch_id), "
                        "(SELECT count(*) FROM audit_logs WHERE target_id = :target_id), "
                        "(SELECT version FROM schools WHERE id = :school_id), "
                        "(SELECT planned_units FROM master_data_import_batches "
                        "WHERE id = :batch_id), "
                        "(SELECT succeeded_units FROM master_data_import_batches "
                        "WHERE id = :batch_id), "
                        "(SELECT failed_units FROM master_data_import_batches "
                        "WHERE id = :batch_id), "
                        "(SELECT pending_units FROM master_data_import_batches "
                        "WHERE id = :batch_id)"
                    ),
                    {
                        "batch_id": result.batch_id,
                        "school_id": school_id,
                        "target_id": str(result.batch_id),
                    },
                ).one()
                # Canonical batch version 2 is carried by lineage/result, not the
                # stable school identity projection token, which remains 1.
                assert counts == (
                    50_000,
                    50_000,
                    50_000,
                    1,
                    1,
                    1,
                    50_000,
                    50_000,
                    0,
                    0,
                )
                outcome = session.execute(
                    text(
                        "SELECT cardinality(succeeded_units), cardinality(failed_units), "
                        "cardinality(executed_units) "
                        "FROM master_data_import_command_outcomes WHERE batch_id = :batch_id"
                    ),
                    {"batch_id": result.batch_id},
                ).one()
                assert outcome == (50_000, 0, 50_000)
                constraint_definition = str(
                    session.execute(
                        text(
                            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                            "WHERE conrelid = 'master_data_import_command_outcomes'::regclass "
                            "AND conname = 'ck_master_data_import_command_outcomes_unit_limits'"
                        )
                    ).scalar_one()
                )
                assert "50000" in constraint_definition
                trigger_definitions = dict(
                    session.execute(
                        text(
                            "SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger "
                            "WHERE tgrelid = 'master_data_import_units'::regclass "
                            "AND tgname LIKE "
                            "'verify_master_data_import_units%terminal_consistency'"
                        )
                    )
                    .tuples()
                    .all()
                )
                assert set(trigger_definitions) == {
                    "verify_master_data_import_units_insert_terminal_consistency",
                    "verify_master_data_import_units_update_terminal_consistency",
                }
                assert "AFTER INSERT" in str(
                    trigger_definitions[
                        "verify_master_data_import_units_insert_terminal_consistency"
                    ]
                )
                update_trigger = str(
                    trigger_definitions[
                        "verify_master_data_import_units_update_terminal_consistency"
                    ]
                )
                assert "AFTER UPDATE" in update_trigger
                assert "WHEN" not in update_trigger
                assert "enforce_school_fact_unit_update_terminal_consistency" in update_trigger
                update_function_security = session.execute(
                    text(
                        "SELECT function.prosecdef, owner.rolname, function.proconfig, "
                        "has_function_privilege("
                        "'campus_ai_runtime', function.oid, 'EXECUTE'), "
                        "EXISTS ("
                        "SELECT 1 FROM aclexplode("
                        "COALESCE(function.proacl, acldefault('f', function.proowner))"
                        ") AS acl WHERE acl.grantee = 0 "
                        "AND acl.privilege_type = 'EXECUTE'"
                        ") FROM pg_proc AS function "
                        "JOIN pg_roles AS owner ON owner.oid = function.proowner "
                        "WHERE function.oid = to_regprocedure("
                        "'app_private."
                        "enforce_school_fact_unit_update_terminal_consistency()'"
                        ")"
                    )
                ).one()
                assert update_function_security == (
                    True,
                    "campus_ai_db_owner",
                    ["search_path=pg_catalog"],
                    False,
                    False,
                )

            replay_context = context_for(
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                assignment_id=assignment_id,
                key=f"sf3-capacity-replay:{uuid4().hex}",
            )
            replay_receipt = service.prepare_confirmation(
                change_set=complete,
                context=replay_context,
                session_binding="sf3-capacity-replay",
            )
            replay_result = service.confirm(
                credential=replay_receipt.credential,
                change_set=complete,
                context=replay_context,
                session_binding="sf3-capacity-replay",
            )
            assert replay_result.replayed is True
            assert replay_result.changed_count == 0
            assert replay_result.target_version == 2
            with factory() as session:
                replay_bundle = session.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM master_data_import_units "
                        " WHERE batch_id = :batch_id), "
                        "(SELECT count(*) FROM master_data_import_units "
                        " WHERE batch_id = :batch_id AND status = 'applied' "
                        " AND target_id IS NULL AND target_version IS NULL), "
                        "(SELECT cardinality(succeeded_units) "
                        " FROM master_data_import_command_outcomes "
                        " WHERE batch_id = :batch_id), "
                        "(SELECT cardinality(executed_units) "
                        " FROM master_data_import_command_outcomes "
                        " WHERE batch_id = :batch_id), "
                        "(SELECT succeeded_units FROM master_data_import_batches "
                        " WHERE id = :batch_id), "
                        "(SELECT failed_units FROM master_data_import_batches "
                        " WHERE id = :batch_id), "
                        "(SELECT pending_units FROM master_data_import_batches "
                        " WHERE id = :batch_id), "
                        "(SELECT count(*) FROM school_policies WHERE school_id = :school_id), "
                        "(SELECT version FROM schools WHERE id = :school_id)"
                    ),
                    {"batch_id": replay_result.batch_id, "school_id": school_id},
                ).one()
                # Replay preserves the same lineage version without mutating the
                # stable school identity projection token.
                assert replay_bundle == (
                    50_000,
                    50_000,
                    50_000,
                    50_000,
                    50_000,
                    0,
                    0,
                    50_000,
                    1,
                )
            authoritative = service.check_result(
                school_id=SchoolId(school_id),
                change_set=complete,
            )
            assert authoritative is not None
            assert authoritative.changed_count == 50_000
            assert authoritative.target_version == 2

            with factory() as session:
                with pytest.raises(DBAPIError):
                    session.execute(
                        text(
                            "INSERT INTO master_data_import_units "
                            "(id, school_id, batch_id, sequence, row_number, unit_kind, "
                            "status, payload, payload_sha256, soft_anomalies, "
                            "initial_soft_anomalies, version) "
                            "SELECT :new_id, school_id, batch_id, 50001, row_number, "
                            "'school_fact', 'ready', payload, payload_sha256, "
                            "soft_anomalies, initial_soft_anomalies, 1 "
                            "FROM master_data_import_units "
                            "WHERE batch_id = :batch_id AND sequence = 1"
                        ),
                        {"new_id": uuid4(), "batch_id": result.batch_id},
                    )
                    session.commit()
                session.rollback()
            with factory() as session:
                assert (
                    session.execute(
                        text(
                            "SELECT count(*) FROM master_data_import_units "
                            "WHERE batch_id = :batch_id"
                        ),
                        {"batch_id": result.batch_id},
                    ).scalar_one()
                    == 50_000
                )

            with factory() as session:
                terminal_target_id = session.execute(
                    text(
                        "SELECT id FROM school_policies "
                        "WHERE school_id = :school_id ORDER BY source_key LIMIT 1"
                    ),
                    {"school_id": school_id},
                ).scalar_one()
                with pytest.raises(DBAPIError):
                    session.execute(
                        text(
                            "UPDATE master_data_import_units SET "
                            "batch_id = :terminal_batch_id, sequence = 50001, "
                            "status = 'applied', target_id = :target_id, "
                            "target_version = 1, version = version + 1 "
                            "WHERE id = :decoy_unit_id"
                        ),
                        {
                            "terminal_batch_id": result.batch_id,
                            "target_id": terminal_target_id,
                            "decoy_unit_id": decoy_unit_id,
                        },
                    )
                    session.commit()
                session.rollback()
            with factory() as session:
                assert session.execute(
                    text(
                        "SELECT batch_id, status FROM master_data_import_units WHERE id = :unit_id"
                    ),
                    {"unit_id": decoy_unit_id},
                ).one() == (decoy_batch_id, "ready")

            with factory() as session:
                with pytest.raises(DBAPIError):
                    session.execute(
                        text(
                            "UPDATE master_data_import_units "
                            "SET payload = payload || "
                            "jsonb_build_object('tampered', true) "
                            "WHERE batch_id = :batch_id AND sequence = 1"
                        ),
                        {"batch_id": result.batch_id},
                    )
                    session.commit()
                session.rollback()
            with factory() as session:
                assert (
                    session.execute(
                        text(
                            "SELECT payload ? 'tampered' FROM master_data_import_units "
                            "WHERE batch_id = :batch_id AND sequence = 1"
                        ),
                        {"batch_id": result.batch_id},
                    ).scalar_one()
                    is False
                )

            ordinary = _policy_change_set(
                count=1,
                source_sha256="e" * 64,
                key_prefix="policy:ordinary-fixture",
                base_version=2,
            )
            ordinary_context = context_for(
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                assignment_id=assignment_id,
                key=f"sf3-capacity-ordinary:{uuid4().hex}",
            )
            ordinary_receipt = service.prepare_confirmation(
                change_set=ordinary,
                context=ordinary_context,
                session_binding="sf3-capacity-ordinary",
            )
            with factory.begin() as session:
                ordinary_unit_id = session.execute(
                    text(
                        "UPDATE master_data_import_units SET unit_kind = 'person' "
                        "WHERE batch_id = ("
                        "SELECT id FROM master_data_import_batches "
                        "WHERE confirmation_token_sha256 = :credential_sha256"
                        ") RETURNING id"
                    ),
                    {
                        "credential_sha256": sha256(
                            ordinary_receipt.credential.encode("ascii")
                        ).hexdigest()
                    },
                ).scalar_one()
            ordinary_result = service.confirm(
                credential=ordinary_receipt.credential,
                change_set=ordinary,
                context=ordinary_context,
                session_binding="sf3-capacity-ordinary",
            )
            assert ordinary_result.changed_count == 1
            with factory() as session:
                with pytest.raises(DBAPIError):
                    session.execute(
                        text(
                            "UPDATE master_data_import_units "
                            "SET unit_kind = 'school_fact' WHERE id = :unit_id"
                        ),
                        {"unit_id": ordinary_unit_id},
                    )
                    session.commit()
                session.rollback()
            with factory() as session:
                assert session.execute(
                    text(
                        "SELECT unit_kind, status FROM master_data_import_units WHERE id = :unit_id"
                    ),
                    {"unit_id": ordinary_unit_id},
                ).one() == ("person", "applied")

            rejected_downgrade = _run_alembic(
                current_master_data_schema,
                "downgrade",
                "20260820_16",
            )
            assert rejected_downgrade.returncode != 0
            assert (
                "revision 17 contains outcomes larger than revision 16 can represent"
                in rejected_downgrade.stdout + rejected_downgrade.stderr
            )
            with factory() as session:
                assert (
                    session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                    == "20260820_17"
                )
                assert (
                    session.execute(
                        text(
                            "SELECT cardinality(succeeded_units) "
                            "FROM master_data_import_command_outcomes WHERE batch_id = :batch_id"
                        ),
                        {"batch_id": result.batch_id},
                    ).scalar_one()
                    == 50_000
                )
                constraint_definition = str(
                    session.execute(
                        text(
                            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                            "WHERE conrelid = 'master_data_import_command_outcomes'::regclass "
                            "AND conname = 'ck_master_data_import_command_outcomes_unit_limits'"
                        )
                    ).scalar_one()
                )
                assert "50000" in constraint_definition
    finally:
        restored = _run_alembic(
            current_master_data_schema,
            "upgrade",
            "20260823_19",
        )
        assert restored.returncode == 0, restored.stdout + restored.stderr
