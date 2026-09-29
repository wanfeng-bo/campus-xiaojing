from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import text

from app.platform.school_facts.application import SchoolFactChangeService
from tests.master_data.conftest import PostgresRoles
from tests.school_facts.test_checkpoint2_lifecycle_red import (
    _confirm,
    _plan,
    _root_upsert,
    _school_source_key,
    _seed_all_facts,
    _terminal_change,
)
from tests.school_facts.test_sf2_transaction import (
    change_set,
    context_for,
    sf2_environment,
)


def test_concurrent_double_confirmation_has_one_writer_and_one_stable_replay(
    current_master_data_schema: PostgresRoles,
) -> None:
    with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        with factory() as session:
            source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :id"), {"id": school_id}
                ).scalar_one()
            )
        context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"sf2:{uuid4().hex}",
        )
        changes = change_set(school_source_key=source_key)
        service = SchoolFactChangeService(reader=persistence, writer=persistence)
        receipt = service.prepare_confirmation(
            change_set=changes, context=context, session_binding="session-concurrent"
        )

        def click() -> tuple[bool, int]:
            result = service.confirm(
                credential=receipt.credential,
                change_set=changes,
                context=context,
                session_binding="session-concurrent",
            )
            return result.replayed, result.changed_count

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = sorted(executor.map(lambda _: click(), range(2)))

        assert outcomes == [(False, 1), (True, 1)]
        with factory() as session:
            assert (
                session.execute(
                    text("SELECT version FROM schools WHERE id = :id"), {"id": school_id}
                ).scalar_one()
                == 2
            )
            assert (
                session.execute(
                    text("SELECT count(*) FROM school_versions WHERE school_id = :id"),
                    {"id": school_id},
                ).scalar_one()
                == 2
            )


def test_concurrent_school_reactivation_has_one_writer_and_one_stable_replay(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """The narrow reactivation binder serializes an exact credential without duplication."""

    checked_now = datetime.now(UTC)
    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(
            reader=persistence,
            writer=persistence,
            clock=lambda: checked_now,
        )
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
        )
        source_key = _school_source_key(factory, school_id)
        terminal = _confirm(
            service=service,
            change_set=_plan(
                label="school-terminal-before-concurrent-reactivation",
                base_version=2,
                changes=(_terminal_change("school", source_key, 2, checked_now, 2),),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="school-terminal-before-concurrent-reactivation",
        )
        reactivation = _plan(
            label="concurrent-school-reactivation",
            base_version=terminal.target_version,
            changes=(
                {
                    **_root_upsert(
                        "school",
                        source_key,
                        3,
                        checked_now + timedelta(seconds=1),
                        2,
                        {
                            "business_code": "CP2-CONCURRENT-REACTIVATE",
                            "display_name": "并发显式重新启用学校",
                            "timezone_name": "Asia/Shanghai",
                            "lifecycle_status": "active",
                        },
                    ),
                    "operation": "reactivate",
                },
            ),
        )
        context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"sf2-reactivation-concurrent:{uuid4().hex}",
        )
        session_binding = f"session-reactivation-concurrent-{uuid4().hex}"
        receipt = service.prepare_confirmation(
            change_set=reactivation,
            context=context,
            session_binding=session_binding,
        )

        def click() -> tuple[bool, int]:
            result = service.confirm(
                credential=receipt.credential,
                change_set=reactivation,
                context=context,
                session_binding=session_binding,
            )
            return result.replayed, result.changed_count

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = sorted(executor.map(lambda _: click(), range(2)))

        assert outcomes == [(False, 1), (True, 1)]
        with factory() as session:
            school_state = session.execute(
                text("SELECT status, version FROM schools WHERE id = :school_id"),
                {"school_id": school_id},
            ).one()
            batch_id = session.execute(
                text(
                    "SELECT id FROM master_data_import_batches "
                    "WHERE confirmation_token_sha256 = :credential_digest"
                ),
                {"credential_digest": sha256(receipt.credential.encode("ascii")).hexdigest()},
            ).scalar_one()
            unit_count = session.execute(
                text("SELECT count(*) FROM master_data_import_units WHERE batch_id = :batch_id"),
                {"batch_id": batch_id},
            ).scalar_one()
            lineage_count = session.execute(
                text("SELECT count(*) FROM school_fact_change_lineage WHERE batch_id = :batch_id"),
                {"batch_id": batch_id},
            ).scalar_one()
            audit_count = session.execute(
                text(
                    "SELECT count(*) FROM audit_logs "
                    "WHERE event_type = 'school_facts.change_set.applied' AND target_id = :id"
                ),
                {"id": str(batch_id)},
            ).scalar_one()
        assert tuple(school_state) == ("active", 4)
        assert (unit_count, lineage_count, audit_count) == (1, 1, 1)
