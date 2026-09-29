from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.composition.school_fact_persistence import SchoolFactPersistenceError
from app.db.rls_context import set_local_execution_context
from app.interfaces.http.school_fact_schemas import SchoolFactChangeSummaryResponse
from app.platform.contracts import (
    AssignmentId,
    PersonId,
    ResponsibilityGroupId,
    ResponsibilityTypeId,
    SchoolId,
    StaffActorContext,
)
from app.platform.directory.contracts import LoginMappingSnapshot
from app.platform.directory.repository import SqlAlchemyPersonRepository
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.execution_context import ContextNoLongerValid
from app.platform.organization.repository import SqlAlchemyOrganizationRepository
from app.platform.responsibility.repository import SqlAlchemyResponsibilityRepository
from app.platform.school_facts.application import (
    SchoolFactChangeRejected,
    SchoolFactChangeService,
)
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1, CanonicalFactChangeV1
from tests.master_data.conftest import PostgresRoles
from tests.master_data.test_execution_context import (
    ExecutionFacts,
    FakeRedis,
    _resolver,
    _runtime_session_factory_for,
)
from tests.school_facts.test_sf2_transaction import (
    NOW,
    all_fact_kinds_change_set,
    context_for,
    sf2_environment,
)


def _source_digest(label: str) -> str:
    return sha256(label.encode("utf-8")).hexdigest()


def _school_source_key(factory: object, school_id: UUID) -> str:
    with factory() as session:  # type: ignore[operator]
        return str(
            session.execute(
                text("SELECT source_key FROM schools WHERE id = :id"),
                {"id": school_id},
            ).scalar_one()
        )


def _plan(
    *,
    label: str,
    base_version: int,
    changes: tuple[dict[str, object], ...],
) -> CanonicalFactChangeSetV1:
    try:
        return CanonicalFactChangeSetV1.model_validate(
            {
                "source_sha256": _source_digest(label),
                "base_version": base_version,
                "changes": changes,
            }
        )
    except ValidationError as exc:
        pytest.fail(f"规范变更尚不能表达冻结语义 {label}: {exc}")


def _confirm(
    *,
    service: SchoolFactChangeService,
    change_set: CanonicalFactChangeSetV1,
    school_id: UUID,
    user_id: int,
    person_id: UUID,
    assignment_id: UUID,
    label: str,
):
    context = context_for(
        school_id=school_id,
        user_id=user_id,
        person_id=person_id,
        assignment_id=assignment_id,
        key=f"cp2:{label}:{uuid4().hex}",
    )
    session_binding = f"cp2-{label}-{uuid4().hex}"
    receipt = service.prepare_confirmation(
        change_set=change_set,
        context=context,
        session_binding=session_binding,
    )
    return service.confirm(
        credential=receipt.credential,
        change_set=change_set,
        context=context,
        session_binding=session_binding,
    )


def _seed_all_facts(
    *,
    service: SchoolFactChangeService,
    factory: object,
    school_id: UUID,
    user_id: int,
    person_id: UUID,
    assignment_id: UUID,
    policy_effective_to=None,
    without_residence: bool = False,
    without_login_mapping: bool = False,
):
    seed = all_fact_kinds_change_set(school_source_key=_school_source_key(factory, school_id))
    if policy_effective_to is not None or without_residence or without_login_mapping:
        payload = seed.model_dump(mode="python", exclude_none=True)
        if without_residence:
            payload["changes"] = tuple(
                change
                for change in payload["changes"]
                if change["fact_kind"] != "student_bed_residence"
            )
        if without_login_mapping:
            payload["changes"] = tuple(
                change for change in payload["changes"] if change["fact_kind"] != "login_mapping"
            )
        for change in payload["changes"]:
            if change["fact_kind"] == "policy_version" and policy_effective_to is not None:
                change["content"]["effective_to"] = policy_effective_to
        payload["source_sha256"] = _source_digest(
            "seed-policy-with-planned-expiry"
            if policy_effective_to is not None
            else "seed-no-residence"
        )
        seed = CanonicalFactChangeSetV1.model_validate(payload)
    result = _confirm(
        service=service,
        change_set=seed,
        school_id=school_id,
        user_id=user_id,
        person_id=person_id,
        assignment_id=assignment_id,
        label="seed-all",
    )
    assert result.target_version == 2
    return result


def test_global_change_version_is_independent_from_school_entity_version(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """Canonical batch sequencing lives in the ledger; school.version is entity-local."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        version_reader = persistence.for_request(
            context_for(
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                assignment_id=assignment_id,
                key=f"cp2:global-version-read:{uuid4().hex}",
            )
        )
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
        )
        non_school = _confirm(
            service=service,
            change_set=_plan(
                label="non-school-global-version-only",
                base_version=2,
                changes=(
                    _root_upsert(
                        "person",
                        "person:global-version-only",
                        0,
                        NOW + timedelta(days=1),
                        2,
                        {
                            "display_name": "仅推进全局账本版本",
                            "person_kind": "other",
                            "lifecycle_status": "active",
                        },
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="non-school-global-version-only",
        )
        with factory() as session:
            entity_version_after_non_school = int(
                session.execute(
                    text("SELECT version FROM schools WHERE id = :school_id"),
                    {"school_id": school_id},
                ).scalar_one()
            )
        assert non_school.target_version == 3
        assert version_reader.current_school_fact_version(SchoolId(school_id)) == 3
        assert entity_version_after_non_school == 2

        school_source_key = _school_source_key(factory, school_id)
        school_change = _confirm(
            service=service,
            change_set=_plan(
                label="school-entity-version-after-global-divergence",
                base_version=3,
                changes=(
                    _root_upsert(
                        "school",
                        school_source_key,
                        2,
                        NOW + timedelta(days=2),
                        2,
                        {
                            "business_code": "CP2-SCHOOL-ENTITY-V3",
                            "display_name": "学校实体独立版本",
                            "timezone_name": "Asia/Shanghai",
                            "lifecycle_status": "active",
                        },
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="school-entity-version-after-global-divergence",
        )
        with factory() as session:
            entity_version_after_school = int(
                session.execute(
                    text("SELECT version FROM schools WHERE id = :school_id"),
                    {"school_id": school_id},
                ).scalar_one()
            )
        assert school_change.target_version == 4
        assert version_reader.current_school_fact_version(SchoolId(school_id)) == 4
        assert entity_version_after_school == 3


@pytest.mark.parametrize(
    ("fact_kind", "source_key", "terminal_expected", "upsert_expected", "content"),
    (
        (
            "school",
            None,
            2,
            3,
            {
                "business_code": "CP2-REOPEN",
                "display_name": "不得被普通 upsert 重启的学校",
                "timezone_name": "Asia/Shanghai",
                "lifecycle_status": "active",
            },
        ),
        (
            "person",
            "person:parent",
            1,
            2,
            {
                "display_name": "不得重启的人员",
                "person_kind": "student_contact",
                "lifecycle_status": "active",
            },
        ),
        (
            "student",
            "student:001",
            1,
            2,
            {"person_source_key": "person:student", "status": "active"},
        ),
        (
            "organization",
            "organization:dorm",
            1,
            2,
            {
                "kind": "department",
                "display_name": "不得重启的组织",
                "display_path": "/不得重启的组织",
                "status": "active",
            },
        ),
        (
            "location",
            "location:campus",
            1,
            2,
            {
                "kind": "campus",
                "display_name": "不得重启的校区",
                "display_path": "/不得重启的校区",
                "status": "active",
            },
        ),
    ),
    ids=("school", "person", "student", "organization", "location"),
)
def test_terminal_root_facts_reject_ordinary_upsert_and_keep_stable_ids(
    current_master_data_schema: PostgresRoles,
    fact_kind: str,
    source_key: str | None,
    terminal_expected: int,
    upsert_expected: int,
    content: dict[str, object],
) -> None:
    """Frozen authority 491: ordinary upsert cannot reactivate five root identities."""

    with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            without_residence=fact_kind == "location",
        )
        actual_source_key = source_key or _school_source_key(factory, school_id)
        terminal_at = NOW + timedelta(days=1)
        terminal = _plan(
            label=f"terminal-root-{fact_kind}",
            base_version=2,
            changes=(
                _terminal_change(
                    fact_kind,
                    actual_source_key,
                    terminal_expected,
                    terminal_at,
                    2,
                ),
            ),
        )
        terminal_result = _confirm(
            service=service,
            change_set=terminal,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label=f"terminal-root-{fact_kind}",
        )
        before = _root_row(factory, school_id, fact_kind, actual_source_key)
        assert before[1] == "inactive", f"{fact_kind} 根事实终止操作未形成 inactive 终态"

        ordinary_upsert = _plan(
            label=f"ordinary-reactivate-root-{fact_kind}",
            base_version=3,
            changes=(
                _root_upsert(
                    fact_kind,
                    actual_source_key,
                    upsert_expected,
                    terminal_at + timedelta(days=1),
                    2,
                    content,
                ),
            ),
        )
        with factory() as session:  # type: ignore[operator]
            transaction = session.begin()
            try:
                source_id = UUID(
                    str(
                        session.execute(
                            text(
                                "SELECT id FROM school_fact_sources "
                                "WHERE school_id = :school_id AND source_key = :source_key"
                            ),
                            {
                                "school_id": school_id,
                                "source_key": f"change-set:{terminal_result.batch_id}",
                            },
                        ).scalar_one()
                    )
                )
                rejection: str | None = None
                try:
                    persistence._apply_change(  # pyright: ignore[reportPrivateUsage]
                        session=session,
                        school_id=school_id,
                        batch_id=terminal_result.batch_id,
                        source_id=source_id,
                        change=ordinary_upsert.changes[0],
                    )
                except SchoolFactChangeRejected as exc:
                    rejection = str(exc)
            finally:
                transaction.rollback()
        after = _root_row(factory, school_id, fact_kind, actual_source_key)

        assert rejection is not None, (
            f"{fact_kind} 根事实被结束/撤销后，真实写核仍接受普通 upsert；"
            f"terminal={before!r}, after={after!r}"
        )
        assert after == before, "拒绝普通重启时必须保留原稳定 ID、终止状态与版本"


@pytest.mark.parametrize(
    ("fact_kind", "source_key", "terminal_expected"),
    (
        ("school", None, 2),
        ("person", "person:parent", 1),
        ("student", "student:001", 1),
        ("organization", "organization:dorm", 1),
        ("location", "location:campus", 1),
    ),
    ids=("school", "person", "student", "organization", "location"),
)
@pytest.mark.parametrize("first_operation", ("end", "revoke"))
def test_inactive_root_rejects_repeated_terminal_until_explicit_reactivation(
    isolated_migration_schema: PostgresRoles,
    fact_kind: str,
    source_key: str | None,
    terminal_expected: int,
    first_operation: str,
) -> None:
    """A second terminal must not rewrite the ledger boundary of an inactive root."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            without_residence=fact_kind == "location",
        )
        actual_source_key = source_key or _school_source_key(factory, school_id)
        first_payload = _terminal_change(
            fact_kind,
            actual_source_key,
            terminal_expected,
            NOW + timedelta(days=1),
            2,
        )
        first_payload["operation"] = first_operation
        first = _confirm(
            service=service,
            change_set=_plan(
                label=f"first-{first_operation}-{fact_kind}",
                base_version=2,
                changes=(first_payload,),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label=f"first-{first_operation}-{fact_kind}",
        )
        before = _root_row(factory, school_id, fact_kind, actual_source_key)
        assert before[1] == "inactive"
        second = CanonicalFactChangeV1.model_validate(
            _terminal_change(
                fact_kind,
                actual_source_key,
                cast(int, before[2]),
                NOW + timedelta(days=2),
                3,
            )
        )
        with factory() as session:  # type: ignore[operator]
            transaction = session.begin()
            try:
                source_id = UUID(
                    str(
                        session.execute(
                            text(
                                "SELECT id FROM school_fact_sources "
                                "WHERE school_id = :school_id AND source_key = :source_key"
                            ),
                            {
                                "school_id": school_id,
                                "source_key": f"change-set:{first.batch_id}",
                            },
                        ).scalar_one()
                    )
                )
                with pytest.raises(SchoolFactChangeRejected):
                    persistence._apply_change(  # pyright: ignore[reportPrivateUsage]
                        session=session,
                        school_id=school_id,
                        batch_id=first.batch_id,
                        source_id=source_id,
                        change=second,
                    )
            finally:
                transaction.rollback()
        assert _root_row(factory, school_id, fact_kind, actual_source_key) == before


@pytest.mark.parametrize(
    ("fact_kind", "source_key", "terminal_expected", "reactivate_expected", "content"),
    (
        (
            "school",
            None,
            2,
            3,
            {
                "business_code": "CP2-REACTIVATE",
                "display_name": "显式重新启用的学校",
                "timezone_name": "Asia/Shanghai",
                "lifecycle_status": "active",
            },
        ),
        (
            "person",
            "person:parent",
            1,
            2,
            {
                "display_name": "显式重新启用的人员",
                "person_kind": "student_contact",
                "lifecycle_status": "active",
            },
        ),
        (
            "student",
            "student:001",
            1,
            2,
            {"person_source_key": "person:student", "status": "active"},
        ),
        (
            "organization",
            "organization:dorm",
            1,
            2,
            {
                "kind": "department",
                "display_name": "显式重新启用的组织",
                "display_path": "/显式重新启用的组织",
                "status": "active",
            },
        ),
        (
            "location",
            "location:campus",
            1,
            2,
            {
                "kind": "campus",
                "display_name": "显式重新启用的校区",
                "display_path": "/显式重新启用的校区",
                "status": "active",
            },
        ),
    ),
    ids=("school", "person", "student", "organization", "location"),
)
def test_terminal_root_facts_can_be_explicitly_reactivated_with_same_stable_id(
    isolated_migration_schema: PostgresRoles,
    fact_kind: str,
    source_key: str | None,
    terminal_expected: int,
    reactivate_expected: int,
    content: dict[str, object],
) -> None:
    """Explicit reactivation is one audited root change and preserves stable identity."""

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
            without_residence=fact_kind == "location",
        )
        actual_source_key = source_key or _school_source_key(factory, school_id)
        terminal_at = checked_now + timedelta(days=1)
        terminal_result = _confirm(
            service=service,
            change_set=_plan(
                label=f"terminal-before-reactivate-{fact_kind}",
                base_version=2,
                changes=(
                    _terminal_change(
                        fact_kind,
                        actual_source_key,
                        terminal_expected,
                        terminal_at,
                        2,
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label=f"terminal-before-reactivate-{fact_kind}",
        )
        before = _root_row(factory, school_id, fact_kind, actual_source_key)
        assert before[1] == "inactive"

        reactivate = _plan(
            label=f"explicit-reactivate-{fact_kind}",
            base_version=terminal_result.target_version,
            changes=(
                {
                    **_root_upsert(
                        fact_kind,
                        actual_source_key,
                        reactivate_expected,
                        terminal_at + timedelta(days=1),
                        2,
                        content,
                    ),
                    "operation": "reactivate",
                },
            ),
        )
        prepare_context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"cp2:prepare-explicit-reactivate-{fact_kind}:{uuid4().hex}",
        )
        confirm_context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"cp2:confirm-explicit-reactivate-{fact_kind}:{uuid4().hex}",
        )
        session_binding = f"cp2-explicit-reactivate-{fact_kind}-{uuid4().hex}"
        receipt = service.prepare_confirmation(
            change_set=reactivate,
            context=prepare_context,
            session_binding=session_binding,
        )
        result = service.confirm(
            credential=receipt.credential,
            change_set=reactivate,
            context=confirm_context,
            session_binding=session_binding,
        )
        after = _root_row(factory, school_id, fact_kind, actual_source_key)

        assert after == (before[0], "active", reactivate_expected + 1)
        with factory() as session:
            ledger = session.execute(
                text(
                    "SELECT lineage.operation, lineage.target_id, lineage.target_version, "
                    "unit.status, unit.target_id, unit.target_version "
                    "FROM school_fact_change_lineage AS lineage "
                    "JOIN master_data_import_units AS unit ON unit.id = lineage.unit_id "
                    "WHERE lineage.batch_id = :batch_id"
                ),
                {"batch_id": result.batch_id},
            ).one()
            audit_count = session.execute(
                text(
                    "SELECT count(*) FROM audit_logs "
                    "WHERE event_type = 'school_facts.change_set.applied' AND target_id = :id"
                ),
                {"id": str(result.batch_id)},
            ).scalar_one()
        assert ledger == (
            "reactivate",
            before[0],
            reactivate_expected + 1,
            "applied",
            before[0],
            reactivate_expected + 1,
        )
        assert audit_count == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    ("fact_kind", "source_key", "table_name"),
    (
        ("person", "person:parent", "school_people"),
        ("student", "student:001", "students"),
        ("organization", "organization:dorm", "organization_units"),
        ("location", "location:campus", "campus_locations"),
    ),
)
def test_runtime_cannot_bypass_formal_reactivation_for_non_school_roots(
    isolated_migration_schema: PostgresRoles,
    fact_kind: str,
    source_key: str,
    table_name: str,
) -> None:
    """A valid ordinary command context is not a substitute for a reactivation unit."""

    roles = isolated_migration_schema
    with sf2_environment(roles) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            without_residence=fact_kind == "location",
        )
        terminal = _plan(
            label=f"runtime-bypass-terminal-{fact_kind}",
            base_version=2,
            changes=(
                _terminal_change(
                    fact_kind,
                    source_key,
                    1,
                    NOW + timedelta(days=1),
                    2,
                ),
            ),
        )
        _confirm(
            service=service,
            change_set=terminal,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label=f"runtime-bypass-terminal-{fact_kind}",
        )
        context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"runtime-bypass-{fact_kind}:{uuid4().hex}",
        )
        envelope = persistence._envelope_factory(context)  # pyright: ignore[reportPrivateUsage]

        with psycopg.connect(
            **roles.runtime_connection_parameters,
            autocommit=False,
        ) as runtime:
            set_local_execution_context(cast(Any, runtime), envelope)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                runtime.execute(
                    sql.SQL(
                        "UPDATE {} SET status = 'active', version = version + 1 "
                        "WHERE school_id = %s AND source_key = %s"
                    ).format(sql.Identifier(table_name)),
                    (school_id, source_key),
                )
                runtime.commit()
            runtime.rollback()
        assert _root_row(factory, school_id, fact_kind, source_key)[1] == "inactive"


@pytest.mark.integration
def test_runtime_cannot_bypass_student_reactivation_through_merged_status(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """Every non-active -> active transition needs the formal marker; merged is no escape."""

    roles = isolated_migration_schema
    with sf2_environment(roles) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
        )
        _confirm(
            service=service,
            change_set=_plan(
                label="student-terminal-before-merged-bypass",
                base_version=2,
                changes=(
                    _terminal_change(
                        "student",
                        "student:001",
                        1,
                        NOW + timedelta(days=1),
                        2,
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="student-terminal-before-merged-bypass",
        )
        context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"runtime-merged-student:{uuid4().hex}",
        )
        envelope = persistence._envelope_factory(context)  # pyright: ignore[reportPrivateUsage]

        with psycopg.connect(**roles.runtime_connection_parameters, autocommit=False) as runtime:
            for attempted_status in ("merged", "active"):
                set_local_execution_context(cast(Any, runtime), envelope)
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    runtime.execute(
                        "UPDATE students SET status = %s, version = version + 1 "
                        "WHERE school_id = %s AND source_key = 'student:001'",
                        (attempted_status, school_id),
                    )
                    runtime.commit()
                runtime.rollback()

        with factory() as session:
            assert (
                session.execute(
                    text(
                        "SELECT status FROM students "
                        "WHERE school_id = :school_id AND source_key = 'student:001'"
                    ),
                    {"school_id": school_id},
                ).scalar_one()
                == "inactive"
            )


def test_inactive_school_reactivation_binder_rejects_a_multi_change_bypass(
    current_master_data_schema: PostgresRoles,
) -> None:
    with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        checked_now = datetime.now(UTC).replace(microsecond=0)
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
                label="school-terminal-before-binder-bypass",
                base_version=2,
                changes=(
                    _terminal_change(
                        "school",
                        source_key,
                        2,
                        checked_now + timedelta(days=1),
                        2,
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="school-terminal-before-binder-bypass",
        )
        bypass_changes = tuple(
            CanonicalFactChangeV1.model_validate(change)
            for change in (
                {
                    **_root_upsert(
                        "school",
                        source_key,
                        3,
                        checked_now + timedelta(days=2),
                        2,
                        {
                            "business_code": "CP2-REACTIVATE-BYPASS",
                            "display_name": "不得夹带其他变更的学校重启",
                            "timezone_name": "Asia/Shanghai",
                            "lifecycle_status": "active",
                        },
                    ),
                    "operation": "reactivate",
                },
                _root_upsert(
                    "person",
                    "person:binder-bypass",
                    0,
                    checked_now + timedelta(days=2),
                    3,
                    {
                        "display_name": "不得夹带的人员",
                        "person_kind": "other",
                        "lifecycle_status": "active",
                    },
                ),
            )
        )
        # The public contract rejects this combination first. Construct only the
        # outer set without its validator so the persistence binder remains a
        # defense-in-depth constraint; each individual change is still validated.
        bypass = CanonicalFactChangeSetV1.model_construct(
            schema_version=1,
            source_sha256=_source_digest("school-reactivation-binder-bypass"),
            base_version=terminal.target_version,
            changes=bypass_changes,
        )

        with pytest.raises((SchoolFactPersistenceError, DBAPIError)) as captured:
            _confirm(
                service=service,
                change_set=bypass,
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                assignment_id=assignment_id,
                label="school-reactivation-binder-bypass",
            )
        original = (
            captured.value.orig
            if isinstance(captured.value, DBAPIError)
            else getattr(captured.value.__context__, "orig", None)
        )
        if getattr(original, "sqlstate", None) != "28000":
            pytest.fail(f"过期 binder 返回了错误 SQLSTATE：{captured.value.__context__!r}")
        with factory() as session:
            school_status = session.execute(
                text("SELECT status FROM schools WHERE id = :school_id"),
                {"school_id": school_id},
            ).scalar_one()
            injected_people = session.execute(
                text(
                    "SELECT count(*) FROM school_people "
                    "WHERE school_id = :school_id AND source_key = 'person:binder-bypass'"
                ),
                {"school_id": school_id},
            ).scalar_one()
        assert (school_status, injected_people) == ("inactive", 0)


def test_expired_school_reactivation_batch_is_rejected_before_school_prewrite(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """The narrow binder must reject an expired plan without touching the root row."""

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
                label="school-terminal-before-expired-reactivation",
                base_version=2,
                changes=(_terminal_change("school", source_key, 2, checked_now, 2),),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="school-terminal-before-expired-reactivation",
        )
        reactivation = _plan(
            label="expired-school-reactivation",
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
                            "business_code": "CP2-EXPIRED-REACTIVATE",
                            "display_name": "过期确认不得预写学校",
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
            key=f"cp2:expired-school-reactivation:{uuid4().hex}",
        )
        session_binding = f"cp2-expired-school-reactivation-{uuid4().hex}"
        receipt = service.prepare_confirmation(
            change_set=reactivation,
            context=context,
            session_binding=session_binding,
        )
        credential_digest = sha256(receipt.credential.encode("ascii")).hexdigest()
        with factory.begin() as session:
            session.execute(
                text(
                    "UPDATE master_data_import_batches "
                    "SET confirmation_expires_at = statement_timestamp() - interval '1 second' "
                    "WHERE confirmation_token_sha256 = :credential_digest"
                ),
                {"credential_digest": credential_digest},
            )

        with pytest.raises(SchoolFactPersistenceError) as captured:
            service.confirm(
                credential=receipt.credential,
                change_set=reactivation,
                context=context,
                session_binding=session_binding,
            )
        original = getattr(captured.value.__context__, "orig", None)
        assert getattr(original, "sqlstate", None) == "28000"
        with factory() as session:
            school_state = session.execute(
                text("SELECT status, version FROM schools WHERE id = :school_id"),
                {"school_id": school_id},
            ).one()
            batch_state = session.execute(
                text(
                    "SELECT status, confirmation_consumed_at "
                    "FROM master_data_import_batches "
                    "WHERE confirmation_token_sha256 = :credential_digest"
                ),
                {"credential_digest": credential_digest},
            ).one()
        assert tuple(school_state) == ("inactive", 3)
        assert tuple(batch_state) == ("ready_for_confirmation", None)


def test_applied_school_reactivation_replays_after_later_school_version_change(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """An applied credential replays its ledger result, not today's school row version."""

    checked_now = datetime.now(UTC)
    timeline_start = checked_now - timedelta(days=1)
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
                label="school-terminal-before-durable-reactivation",
                base_version=2,
                changes=(_terminal_change("school", source_key, 2, timeline_start, 2),),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="school-terminal-before-durable-reactivation",
        )
        reactivation = _plan(
            label="durable-school-reactivation",
            base_version=terminal.target_version,
            changes=(
                {
                    **_root_upsert(
                        "school",
                        source_key,
                        3,
                        timeline_start + timedelta(seconds=1),
                        2,
                        {
                            "business_code": "CP2-DURABLE-REACTIVATE",
                            "display_name": "可持久重放的学校恢复",
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
            key=f"cp2:durable-school-reactivation:{uuid4().hex}",
        )
        session_binding = f"cp2-durable-school-reactivation-{uuid4().hex}"
        receipt = service.prepare_confirmation(
            change_set=reactivation,
            context=context,
            session_binding=session_binding,
        )
        first = service.confirm(
            credential=receipt.credential,
            change_set=reactivation,
            context=context,
            session_binding=session_binding,
        )
        assert (first.target_version, first.replayed) == (4, False)

        later = _confirm(
            service=service,
            change_set=_plan(
                label="school-update-after-reactivation",
                base_version=4,
                changes=(
                    _root_upsert(
                        "school",
                        source_key,
                        4,
                        timeline_start + timedelta(seconds=2),
                        2,
                        {
                            "business_code": "CP2-AFTER-REACTIVATE",
                            "display_name": "恢复后的学校更新",
                            "timezone_name": "Asia/Shanghai",
                            "lifecycle_status": "active",
                        },
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="school-update-after-reactivation",
        )
        assert later.target_version == 5

        inactive_again = _confirm(
            service=service,
            change_set=_plan(
                label="school-terminal-after-reactivation",
                base_version=5,
                changes=(
                    _terminal_change(
                        "school",
                        source_key,
                        5,
                        timeline_start + timedelta(seconds=3),
                        2,
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="school-terminal-after-reactivation",
        )
        assert inactive_again.target_version == 6

        tampered_payload = reactivation.model_dump(mode="python", exclude_none=True)
        tampered_payload["source_sha256"] = _source_digest(
            "tampered-applied-school-reactivation-replay"
        )
        tampered_payload["changes"][0]["content"]["display_name"] = "被篡改的重放内容"
        tampered = CanonicalFactChangeSetV1.model_validate(tampered_payload)
        with pytest.raises(SchoolFactPersistenceError):
            service.confirm(
                credential=receipt.credential,
                change_set=tampered,
                context=context,
                session_binding=session_binding,
            )

        replay = service.confirm(
            credential=receipt.credential,
            change_set=reactivation,
            context=context,
            session_binding=session_binding,
        )
        assert (replay.batch_id, replay.target_version, replay.changed_count, replay.replayed) == (
            first.batch_id,
            4,
            1,
            True,
        )
        with factory() as session:
            school_version = int(
                session.execute(
                    text("SELECT version FROM schools WHERE id = :school_id"),
                    {"school_id": school_id},
                ).scalar_one()
            )
            old_batch_counts = session.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM master_data_import_units WHERE batch_id = :id), "
                    "(SELECT count(*) FROM school_fact_change_lineage WHERE batch_id = :id), "
                    "(SELECT count(*) FROM audit_logs "
                    " WHERE event_type = 'school_facts.change_set.applied' "
                    " AND target_id = CAST(:id AS text))"
                ),
                {"id": first.batch_id},
            ).one()
        assert school_version == 6
        assert tuple(old_batch_counts) == (1, 1, 1)


@pytest.mark.parametrize("fact_kind", ("assignment", "login_mapping", "policy_version"))
def test_reactivate_contract_rejects_non_root_facts(fact_kind: str) -> None:
    content_by_kind: dict[str, dict[str, object]] = {
        "assignment": _assignment_upsert("assignment:new", 1, NOW, 2)["content"],  # type: ignore[dict-item]
        "login_mapping": _login_upsert("login:new", 1, NOW, 2, external_subject="new-subject")[
            "content"
        ],  # type: ignore[dict-item]
        "policy_version": {
            "policy_source_key": "policy:late-arrival",
            "business_code": "POLICY-V2",
            "display_name": "制度版本",
            "category_code": "student_affairs",
            "classification": "internal",
            "published_at": NOW,
            "effective_from": NOW,
        },
    }
    payload = {
        "operation": "reactivate",
        "fact_kind": fact_kind,
        "source_key": f"{fact_kind}:target",
        "expected_version": 1,
        "effective_at": NOW,
        "origin": {"sheet_code": fact_kind, "row_number": 2},
        "content": content_by_kind[fact_kind],
    }

    with pytest.raises(ValidationError):
        CanonicalFactChangeSetV1.model_validate(
            {
                "source_sha256": _source_digest(f"non-root-reactivate-{fact_kind}"),
                "base_version": 1,
                "changes": (payload,),
            }
        )


@pytest.mark.parametrize(
    ("fact_kind", "content"),
    (
        (
            "school",
            {
                "business_code": "SCHOOL-REACTIVATE",
                "display_name": "显式恢复学校",
                "timezone_name": "Asia/Shanghai",
                "lifecycle_status": "active",
            },
        ),
        (
            "person",
            {
                "display_name": "显式恢复人员",
                "person_kind": "staff",
                "lifecycle_status": "active",
            },
        ),
        (
            "student",
            {"person_source_key": "person:student", "status": "active"},
        ),
        (
            "organization",
            {
                "kind": "department",
                "display_name": "显式恢复组织",
                "display_path": "/显式恢复组织",
                "status": "active",
            },
        ),
        (
            "location",
            {
                "kind": "campus",
                "display_name": "显式恢复校区",
                "display_path": "/显式恢复校区",
                "status": "active",
            },
        ),
    ),
)
def test_reactivate_contract_accepts_complete_content_for_five_roots(
    fact_kind: str,
    content: dict[str, object],
) -> None:
    change = CanonicalFactChangeV1.model_validate(
        {
            "operation": "reactivate",
            "fact_kind": fact_kind,
            "source_key": f"{fact_kind}:target",
            "expected_version": 2,
            "effective_at": NOW,
            "origin": {"sheet_code": fact_kind, "row_number": 2},
            "content": content,
        }
    )

    assert change.operation == "reactivate"
    assert change.content is not None


@pytest.mark.parametrize(
    ("fact_kind", "content"),
    (
        (
            "school",
            {
                "business_code": "SCHOOL-REACTIVATE-ONLY",
                "display_name": "独立恢复学校",
                "timezone_name": "Asia/Shanghai",
                "lifecycle_status": "active",
            },
        ),
        (
            "person",
            {
                "display_name": "独立恢复人员",
                "person_kind": "staff",
                "lifecycle_status": "active",
            },
        ),
        ("student", {"person_source_key": "person:student", "status": "active"}),
        (
            "organization",
            {
                "kind": "department",
                "display_name": "独立恢复组织",
                "display_path": "/独立恢复组织",
                "status": "active",
            },
        ),
        (
            "location",
            {
                "kind": "campus",
                "display_name": "独立恢复校区",
                "display_path": "/独立恢复校区",
                "status": "active",
            },
        ),
    ),
    ids=("school", "person", "student", "organization", "location"),
)
def test_reactivate_must_be_the_only_change_in_its_change_set(
    fact_kind: str,
    content: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CanonicalFactChangeSetV1.model_validate(
            {
                "source_sha256": _source_digest(f"mixed-reactivate-{fact_kind}"),
                "base_version": 2,
                "changes": (
                    {
                        "operation": "reactivate",
                        "fact_kind": fact_kind,
                        "source_key": f"{fact_kind}:target",
                        "expected_version": 2,
                        "effective_at": NOW,
                        "origin": {"sheet_code": fact_kind, "row_number": 2},
                        "content": content,
                    },
                    _root_upsert(
                        "person",
                        "person:mixed-reactivate-companion",
                        0,
                        NOW,
                        3,
                        {
                            "display_name": "不得夹带的人员",
                            "person_kind": "other",
                            "lifecycle_status": "active",
                        },
                    ),
                ),
            }
        )


@pytest.mark.parametrize(
    ("fact_kind", "content"),
    (
        (
            "school",
            {
                "business_code": "SCHOOL-INACTIVE-UPSERT",
                "display_name": "不得暗中停用的学校",
                "timezone_name": "Asia/Shanghai",
                "lifecycle_status": "inactive",
            },
        ),
        (
            "person",
            {
                "display_name": "不得暗中停用的人员",
                "person_kind": "staff",
                "lifecycle_status": "inactive",
            },
        ),
        ("student", {"person_source_key": "person:student", "status": "inactive"}),
        (
            "organization",
            {
                "kind": "department",
                "display_name": "不得暗中停用的组织",
                "display_path": "/不得暗中停用的组织",
                "status": "inactive",
            },
        ),
        (
            "location",
            {
                "kind": "campus",
                "display_name": "不得暗中停用的校区",
                "display_path": "/不得暗中停用的校区",
                "status": "inactive",
            },
        ),
    ),
)
def test_ordinary_root_upsert_cannot_encode_an_inactive_terminal_state(
    fact_kind: str,
    content: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CanonicalFactChangeV1.model_validate(
            {
                "operation": "upsert",
                "fact_kind": fact_kind,
                "source_key": f"{fact_kind}:target",
                "expected_version": 1,
                "effective_at": NOW,
                "origin": {"sheet_code": fact_kind, "row_number": 2},
                "content": content,
            }
        )


def test_http_change_summary_can_report_explicit_reactivation() -> None:
    summary = SchoolFactChangeSummaryResponse.model_validate(
        {
            "operation": "reactivate",
            "fact_kind": "person",
            "source_key": "person:reactivated",
            "expected_version": 2,
            "sheet_code": "people",
            "row_number": 2,
            "fields": (),
        }
    )

    assert summary.operation == "reactivate"


def _terminal_change(
    fact_kind: str,
    source_key: str,
    expected_version: int,
    effective_at,
    row_number: int,
) -> dict[str, object]:
    return {
        "operation": "revoke",
        "fact_kind": fact_kind,
        "source_key": source_key,
        "expected_version": expected_version,
        "effective_at": effective_at,
        "origin": {"sheet_code": fact_kind, "row_number": row_number},
        "content": None,
    }


def _root_upsert(
    fact_kind: str,
    source_key: str,
    expected_version: int,
    effective_at,
    row_number: int,
    content: dict[str, object],
) -> dict[str, object]:
    return {
        "operation": "upsert",
        "fact_kind": fact_kind,
        "source_key": source_key,
        "expected_version": expected_version,
        "effective_at": effective_at,
        "origin": {"sheet_code": fact_kind, "row_number": row_number},
        "content": content,
    }


def _root_row(
    factory: object,
    school_id: UUID,
    fact_kind: str,
    source_key: str,
) -> tuple[object, ...]:
    tables = {
        "school": "schools",
        "person": "school_people",
        "student": "students",
        "organization": "organization_units",
        "location": "campus_locations",
    }
    table = tables[fact_kind]
    school_filter = "id = :school_id" if fact_kind == "school" else "school_id = :school_id"
    with factory() as session:  # type: ignore[operator]
        return tuple(
            session.execute(
                text(
                    f"SELECT id, status, version FROM {table} "  # noqa: S608
                    f"WHERE {school_filter} AND source_key = :source_key"
                ),
                {"school_id": school_id, "source_key": source_key},
            ).one()
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("fact_kind", "source_key", "expected_version", "table_name"),
    (
        ("school", None, 2, "schools"),
        ("person", "person:parent", 1, "school_people"),
        ("student", "student:001", 1, "students"),
        ("organization", "organization:dorm", 1, "organization_units"),
        ("location", "location:campus", 1, "campus_locations"),
    ),
    ids=("school", "person", "student", "organization", "location"),
)
def test_root_effectiveness_uses_the_formal_future_end_boundary(
    isolated_migration_schema: PostgresRoles,
    fact_kind: str,
    source_key: str | None,
    expected_version: int,
    table_name: str,
) -> None:
    """Stable inactive projection must not make a future end effective early."""

    checked_now = datetime.now(UTC).replace(microsecond=0)
    terminal_at = checked_now + timedelta(days=2)
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
            without_residence=fact_kind == "location",
        )
        actual_source_key = source_key or _school_source_key(factory, school_id)
        _confirm(
            service=service,
            change_set=_plan(
                label=f"future-root-end-{fact_kind}",
                base_version=2,
                changes=(
                    {
                        **_terminal_change(
                            fact_kind,
                            actual_source_key,
                            expected_version,
                            terminal_at,
                            2,
                        ),
                        "operation": "end",
                    },
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label=f"future-root-end-{fact_kind}",
        )
        with factory() as session:
            school_filter = "id = :school_id" if fact_kind == "school" else "school_id = :school_id"
            target_id = session.execute(
                text(
                    f"SELECT id FROM {table_name} "  # noqa: S608 - test allowlist
                    f"WHERE {school_filter} AND source_key = :source_key"
                ),
                {"school_id": school_id, "source_key": actual_source_key},
            ).scalar_one()
            before, after = session.execute(
                text(
                    "SELECT "
                    "app.school_fact_root_is_effective_at("
                    ":school_id, :fact_kind, :target_id, :before), "
                    "app.school_fact_root_is_effective_at("
                    ":school_id, :fact_kind, :target_id, :after)"
                ),
                {
                    "school_id": school_id,
                    "fact_kind": fact_kind,
                    "target_id": target_id,
                    "before": terminal_at - timedelta(seconds=1),
                    "after": terminal_at,
                },
            ).one()

        assert (before, after) == (True, False)


@pytest.mark.integration
def test_student_root_effectiveness_uses_only_the_latest_content_lineage(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """An older upsert must not keep a later-ended student root effective."""

    checked_now = datetime.now(UTC).replace(microsecond=0)
    second_content_at = checked_now + timedelta(days=1)
    terminal_at = checked_now + timedelta(days=2)
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
        second = _confirm(
            service=service,
            change_set=_plan(
                label="student-root-second-content",
                base_version=2,
                changes=(
                    _root_upsert(
                        "student",
                        "student:001",
                        1,
                        second_content_at,
                        2,
                        {"person_source_key": "person:student", "status": "active"},
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="student-root-second-content",
        )
        _confirm(
            service=service,
            change_set=_plan(
                label="student-root-terminal-after-second-content",
                base_version=second.target_version,
                changes=(
                    {
                        **_terminal_change(
                            "student",
                            "student:001",
                            2,
                            terminal_at,
                            3,
                        ),
                        "operation": "end",
                    },
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="student-root-terminal-after-second-content",
        )
        with factory() as session:
            student_id = session.execute(
                text(
                    "SELECT id FROM students WHERE school_id = :school_id "
                    "AND source_key = 'student:001'"
                ),
                {"school_id": school_id},
            ).scalar_one()
            before, after = session.execute(
                text(
                    "SELECT app.school_fact_root_is_effective_at("
                    ":school_id, 'student', :student_id, :before), "
                    "app.school_fact_root_is_effective_at("
                    ":school_id, 'student', :student_id, :after)"
                ),
                {
                    "school_id": school_id,
                    "student_id": student_id,
                    "before": terminal_at - timedelta(seconds=1),
                    "after": terminal_at,
                },
            ).one()

    assert (before, after) == (True, False)


@pytest.mark.integration
def test_student_root_cannot_reactivate_before_a_future_end_boundary(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """A future end does not make its root eligible for early reactivation."""

    checked_now = datetime.now(UTC).replace(microsecond=0)
    terminal_at = checked_now + timedelta(days=2)
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
        terminal = _confirm(
            service=service,
            change_set=_plan(
                label="student-future-end-before-early-reactivate",
                base_version=2,
                changes=(
                    {
                        **_terminal_change("student", "student:001", 1, terminal_at, 2),
                        "operation": "end",
                    },
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="student-future-end-before-early-reactivate",
        )
        early_reactivate = _plan(
            label="student-early-reactivate-before-end",
            base_version=terminal.target_version,
            changes=(
                {
                    **_root_upsert(
                        "student",
                        "student:001",
                        2,
                        terminal_at - timedelta(seconds=1),
                        3,
                        {"person_source_key": "person:student", "status": "active"},
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
            key=f"cp2:student-early-reactivate:{uuid4().hex}",
        )
        session_binding = f"cp2-student-early-reactivate-{uuid4().hex}"
        receipt = service.prepare_confirmation(
            change_set=early_reactivate,
            context=context,
            session_binding=session_binding,
        )

        with pytest.raises(SchoolFactPersistenceError):
            service.confirm(
                credential=receipt.credential,
                change_set=early_reactivate,
                context=context,
                session_binding=session_binding,
            )


@pytest.mark.integration
def test_future_ended_student_cannot_be_reopened_by_a_backdated_ordinary_upsert(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """Backdating ordinary content cannot bypass the explicit-reactivation boundary."""

    checked_now = datetime.now(UTC).replace(microsecond=0)
    terminal_at = checked_now + timedelta(days=2)
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
        terminal = _confirm(
            service=service,
            change_set=_plan(
                label="student-future-end-before-backdated-upsert",
                base_version=2,
                changes=(
                    {
                        **_terminal_change("student", "student:001", 1, terminal_at, 2),
                        "operation": "end",
                    },
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="student-future-end-before-backdated-upsert",
        )
        backdated = _plan(
            label="student-backdated-upsert-after-future-end",
            base_version=terminal.target_version,
            changes=(
                _root_upsert(
                    "student",
                    "student:001",
                    2,
                    terminal_at - timedelta(seconds=1),
                    3,
                    {"person_source_key": "person:student", "status": "active"},
                ),
            ),
        )
        context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"cp2:student-backdated-upsert:{uuid4().hex}",
        )
        session_binding = f"cp2-student-backdated-upsert-{uuid4().hex}"
        receipt = service.prepare_confirmation(
            change_set=backdated,
            context=context,
            session_binding=session_binding,
        )

        with pytest.raises(SchoolFactChangeRejected):
            service.confirm(
                credential=receipt.credential,
                change_set=backdated,
                context=context,
                session_binding=session_binding,
            )


@pytest.mark.integration
def test_student_root_ordinary_upsert_cannot_fill_a_terminal_reactivation_gap(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """Stable active projection after reactivation cannot erase an earlier inactive gap."""

    checked_now = datetime.now(UTC).replace(microsecond=0)
    terminal_at = checked_now + timedelta(days=1)
    reactivate_at = checked_now + timedelta(days=3)
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
        terminal = _confirm(
            service=service,
            change_set=_plan(
                label="student-end-before-gap",
                base_version=2,
                changes=(
                    {
                        **_terminal_change("student", "student:001", 1, terminal_at, 2),
                        "operation": "end",
                    },
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="student-end-before-gap",
        )
        reactivated = _confirm(
            service=service,
            change_set=_plan(
                label="student-reactivate-after-gap",
                base_version=terminal.target_version,
                changes=(
                    {
                        **_root_upsert(
                            "student",
                            "student:001",
                            2,
                            reactivate_at,
                            3,
                            {"person_source_key": "person:student", "status": "active"},
                        ),
                        "operation": "reactivate",
                    },
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="student-reactivate-after-gap",
        )
        gap_upsert = _plan(
            label="student-upsert-inside-inactive-gap",
            base_version=reactivated.target_version,
            changes=(
                _root_upsert(
                    "student",
                    "student:001",
                    3,
                    terminal_at + timedelta(seconds=1),
                    4,
                    {"person_source_key": "person:student", "status": "active"},
                ),
            ),
        )
        context = context_for(
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"cp2:student-gap-upsert:{uuid4().hex}",
        )
        session_binding = f"cp2-student-gap-upsert-{uuid4().hex}"
        receipt = service.prepare_confirmation(
            change_set=gap_upsert,
            context=context,
            session_binding=session_binding,
        )

        with pytest.raises(SchoolFactChangeRejected):
            service.confirm(
                credential=receipt.credential,
                change_set=gap_upsert,
                context=context,
                session_binding=session_binding,
            )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("fact_kind", "source_key", "terminal_expected", "reactivate_expected", "content"),
    (
        (
            "school",
            None,
            2,
            3,
            {
                "business_code": "CP2-EPOCH-SCHOOL",
                "display_name": "重新启用后的学校纪元",
                "timezone_name": "Asia/Shanghai",
                "lifecycle_status": "active",
            },
        ),
        (
            "student",
            "student:001",
            1,
            2,
            {"person_source_key": "person:student", "status": "active"},
        ),
        (
            "location",
            "location:campus",
            1,
            2,
            {
                "kind": "campus",
                "display_name": "重新启用后的校区纪元",
                "display_path": "/重新启用后的校区纪元",
                "status": "active",
            },
        ),
    ),
    ids=("school", "student", "location"),
)
def test_root_revoke_invalidates_its_epoch_but_later_reactivate_starts_a_new_one(
    isolated_migration_schema: PostgresRoles,
    fact_kind: str,
    source_key: str | None,
    terminal_expected: int,
    reactivate_expected: int,
    content: dict[str, object],
) -> None:
    """Revoke is retroactive only within its epoch; explicit reactivation starts another."""

    checked_now = datetime.now(UTC).replace(microsecond=0)
    revoke_at = checked_now + timedelta(days=1)
    reactivate_at = checked_now + timedelta(days=2)
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
            without_residence=fact_kind == "location",
        )
        actual_source_key = source_key or _school_source_key(factory, school_id)
        terminal = _confirm(
            service=service,
            change_set=_plan(
                label=f"root-revoke-epoch-{fact_kind}",
                base_version=2,
                changes=(
                    _terminal_change(
                        fact_kind,
                        actual_source_key,
                        terminal_expected,
                        revoke_at,
                        2,
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label=f"root-revoke-epoch-{fact_kind}",
        )
        _confirm(
            service=service,
            change_set=_plan(
                label=f"root-reactivate-new-epoch-{fact_kind}",
                base_version=terminal.target_version,
                changes=(
                    {
                        **_root_upsert(
                            fact_kind,
                            actual_source_key,
                            reactivate_expected,
                            reactivate_at,
                            3,
                            content,
                        ),
                        "operation": "reactivate",
                    },
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label=f"root-reactivate-new-epoch-{fact_kind}",
        )
        table_name = {
            "school": "schools",
            "student": "students",
            "location": "campus_locations",
        }[fact_kind]
        school_filter = "id = :school_id" if fact_kind == "school" else "school_id = :school_id"
        with factory() as session:
            target_id = session.execute(
                text(
                    f"SELECT id FROM {table_name} "  # noqa: S608 - test allowlist
                    f"WHERE {school_filter} AND source_key = :source_key"
                ),
                {"school_id": school_id, "source_key": actual_source_key},
            ).scalar_one()
            before, between, after = session.execute(
                text(
                    "SELECT "
                    "app.school_fact_root_is_effective_at("
                    ":school_id, :fact_kind, :target_id, :before), "
                    "app.school_fact_root_is_effective_at("
                    ":school_id, :fact_kind, :target_id, :between), "
                    "app.school_fact_root_is_effective_at("
                    ":school_id, :fact_kind, :target_id, :after)"
                ),
                {
                    "school_id": school_id,
                    "fact_kind": fact_kind,
                    "target_id": target_id,
                    "before": revoke_at - timedelta(seconds=1),
                    "between": reactivate_at - timedelta(seconds=1),
                    "after": reactivate_at,
                },
            ).one()

    assert (before, between, after) == (False, False, True)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("fact_kind", "source_key", "first_expected", "second_content", "reactivate_content"),
    (
        (
            "school",
            None,
            2,
            {
                "business_code": "CP2-EPOCH-SCHOOL-V2",
                "display_name": "撤销前第二版学校",
                "timezone_name": "Asia/Shanghai",
                "lifecycle_status": "active",
            },
            {
                "business_code": "CP2-EPOCH-SCHOOL-V3",
                "display_name": "撤销后重新启用学校",
                "timezone_name": "Asia/Shanghai",
                "lifecycle_status": "active",
            },
        ),
        (
            "student",
            "student:001",
            1,
            {"person_source_key": "person:student", "status": "active"},
            {"person_source_key": "person:student", "status": "active"},
        ),
        (
            "location",
            "location:campus",
            1,
            {
                "kind": "campus",
                "display_name": "撤销前第二版校区",
                "display_path": "/撤销前第二版校区",
                "status": "active",
            },
            {
                "kind": "campus",
                "display_name": "撤销后重新启用校区",
                "display_path": "/撤销后重新启用校区",
                "status": "active",
            },
        ),
    ),
    ids=("school", "student", "location"),
)
def test_root_revoke_invalidates_every_content_slice_in_the_activation_epoch(
    isolated_migration_schema: PostgresRoles,
    fact_kind: str,
    source_key: str | None,
    first_expected: int,
    second_content: dict[str, object],
    reactivate_content: dict[str, object],
) -> None:
    """A revoke suppresses all ordinary upserts in its epoch, not only the adjacent one."""

    checked_now = datetime.now(UTC).replace(microsecond=0)
    second_at = checked_now + timedelta(days=1)
    revoke_at = checked_now + timedelta(days=2)
    reactivate_at = checked_now + timedelta(days=3)
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
            without_residence=fact_kind == "location",
        )
        actual_source_key = source_key or _school_source_key(factory, school_id)
        second = _confirm(
            service=service,
            change_set=_plan(
                label=f"root-second-upsert-before-revoke-{fact_kind}",
                base_version=2,
                changes=(
                    _root_upsert(
                        fact_kind,
                        actual_source_key,
                        first_expected,
                        second_at,
                        2,
                        second_content,
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label=f"root-second-upsert-before-revoke-{fact_kind}",
        )
        revoked = _confirm(
            service=service,
            change_set=_plan(
                label=f"root-revoke-after-second-upsert-{fact_kind}",
                base_version=second.target_version,
                changes=(
                    _terminal_change(
                        fact_kind,
                        actual_source_key,
                        first_expected + 1,
                        revoke_at,
                        3,
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label=f"root-revoke-after-second-upsert-{fact_kind}",
        )
        _confirm(
            service=service,
            change_set=_plan(
                label=f"root-reactivate-after-multi-upsert-{fact_kind}",
                base_version=revoked.target_version,
                changes=(
                    {
                        **_root_upsert(
                            fact_kind,
                            actual_source_key,
                            first_expected + 2,
                            reactivate_at,
                            4,
                            reactivate_content,
                        ),
                        "operation": "reactivate",
                    },
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label=f"root-reactivate-after-multi-upsert-{fact_kind}",
        )
        table_name = {
            "school": "schools",
            "student": "students",
            "location": "campus_locations",
        }[fact_kind]
        school_filter = "id = :school_id" if fact_kind == "school" else "school_id = :school_id"
        with factory() as session:
            target_id = session.execute(
                text(
                    f"SELECT id FROM {table_name} "  # noqa: S608 - test allowlist
                    f"WHERE {school_filter} AND source_key = :source_key"
                ),
                {"school_id": school_id, "source_key": actual_source_key},
            ).scalar_one()
            first_slice, second_slice, revoked_gap, new_epoch = session.execute(
                text(
                    "SELECT "
                    "app.school_fact_root_is_effective_at("
                    ":school_id, :fact_kind, :target_id, :first_slice), "
                    "app.school_fact_root_is_effective_at("
                    ":school_id, :fact_kind, :target_id, :second_slice), "
                    "app.school_fact_root_is_effective_at("
                    ":school_id, :fact_kind, :target_id, :revoked_gap), "
                    "app.school_fact_root_is_effective_at("
                    ":school_id, :fact_kind, :target_id, :new_epoch)"
                ),
                {
                    "school_id": school_id,
                    "fact_kind": fact_kind,
                    "target_id": target_id,
                    "first_slice": checked_now,
                    "second_slice": second_at,
                    "revoked_gap": revoke_at,
                    "new_epoch": reactivate_at,
                },
            ).one()

    assert (first_slice, second_slice, revoked_gap, new_epoch) == (
        False,
        False,
        False,
        True,
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("fact_kind", "source_key"),
    (
        ("person", "person:parent"),
        ("student", "student:001"),
        ("organization", "organization:dorm"),
    ),
)
def test_reference_preflight_resolves_roots_at_the_change_effective_time(
    isolated_migration_schema: PostgresRoles,
    fact_kind: str,
    source_key: str,
) -> None:
    """A future terminal must remain referenceable before, but never after, its boundary."""

    checked_now = datetime.now(UTC).replace(microsecond=0)
    terminal_at = checked_now + timedelta(days=2)
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
        _confirm(
            service=service,
            change_set=_plan(
                label=f"future-reference-end-{fact_kind}",
                base_version=2,
                changes=(
                    {
                        **_terminal_change(
                            fact_kind,
                            source_key,
                            1,
                            terminal_at,
                            2,
                        ),
                        "operation": "end",
                    },
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label=f"future-reference-end-{fact_kind}",
        )
        with factory() as session:
            before = persistence._current_reference_source_keys(  # pyright: ignore[reportPrivateUsage]
                session,
                school_id=SchoolId(school_id),
                reference_kind=fact_kind,
                source_keys=(source_key,),
                effective_at=terminal_at - timedelta(seconds=1),
            )
            after = persistence._current_reference_source_keys(  # pyright: ignore[reportPrivateUsage]
                session,
                school_id=SchoolId(school_id),
                reference_kind=fact_kind,
                source_keys=(source_key,),
                effective_at=terminal_at,
            )
            before_target = persistence._source_root_id(  # pyright: ignore[reportPrivateUsage]
                session,
                school_id,
                fact_kind,
                source_key,
                effective_at=terminal_at - timedelta(seconds=1),
            )
            with pytest.raises(SchoolFactChangeRejected):
                persistence._source_root_id(  # pyright: ignore[reportPrivateUsage]
                    session,
                    school_id,
                    fact_kind,
                    source_key,
                    effective_at=terminal_at,
                )

        assert before == {source_key}
        assert after == set()
        assert isinstance(before_target, UUID)


@pytest.mark.integration
def test_real_preflight_rejects_same_plan_terminal_root_references(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """The production bulk reader cannot hide a same-plan end/revoke from preflight."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            without_residence=True,
        )
        preflight_service = SchoolFactChangeService(
            reader=persistence.for_request(
                context_for(
                    school_id=school_id,
                    user_id=user_id,
                    person_id=person_id,
                    assignment_id=assignment_id,
                    key=f"same-plan-terminal-preflight:{uuid4().hex}",
                )
            ),
            writer=persistence,
            clock=lambda: NOW,
        )
        organization_plan = _plan(
            label="same-plan-organization-end-assignment",
            base_version=2,
            changes=(
                {
                    **_terminal_change(
                        "organization", "organization:dorm", 1, NOW + timedelta(days=1), 2
                    ),
                    "operation": "end",
                },
                _assignment_upsert(
                    "assignment:same-plan-invalid",
                    0,
                    NOW,
                    3,
                ),
            ),
        )
        location_plan = _plan(
            label="same-plan-location-revoke-child",
            base_version=2,
            changes=(
                {
                    **_terminal_change(
                        "location", "location:campus", 1, NOW + timedelta(days=1), 2
                    ),
                    "operation": "end",
                },
                _root_upsert(
                    "location",
                    "location:same-plan-invalid-child",
                    0,
                    NOW,
                    3,
                    {
                        "parent_source_key": "location:campus",
                        "kind": "building",
                        "business_code": "SAME-PLAN-INVALID",
                        "display_name": "同批撤销下的子地点",
                        "display_path": "/同批撤销/子地点",
                        "status": "active",
                    },
                ),
            ),
        )

        organization_report = preflight_service.preflight(
            school_id=SchoolId(school_id), change_set=organization_plan
        )
        location_report = preflight_service.preflight(
            school_id=SchoolId(school_id), change_set=location_plan
        )

    assert ("organization_source_key", "fact.reference_not_found") in {
        (issue.column_code, issue.error_code) for issue in organization_report.issues
    }
    assert ("parent_source_key", "fact.reference_not_found") in {
        (issue.column_code, issue.error_code) for issue in location_report.issues
    }


@pytest.mark.integration
def test_identity_keeps_future_ended_school_current_until_its_boundary(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """Ordinary identity remains reachable before T; reactivation is not exposed early."""

    checked_now = datetime.now(UTC).replace(microsecond=0)
    terminal_at = checked_now + timedelta(days=2)
    runtime_factory, runtime_engine = _runtime_session_factory_for(isolated_migration_schema)
    try:
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
            _confirm(
                service=service,
                change_set=_plan(
                    label="identity-future-school-end",
                    base_version=2,
                    changes=(
                        {
                            **_terminal_change(
                                "school",
                                source_key,
                                2,
                                terminal_at,
                                2,
                            ),
                            "operation": "end",
                        },
                    ),
                ),
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                assignment_id=assignment_id,
                label="identity-future-school-end",
            )
            identity_facts = ExecutionFacts(
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                student_person_id=uuid4(),
                assignment_id=assignment_id,
                guardian_relationship_id=uuid4(),
                student_id=uuid4(),
                administrative_class_id=uuid4(),
                location_id=uuid4(),
                valid_from=NOW,
            )
            resolver, _identity_session, _token = _resolver(
                facts=identity_facts,
                redis=FakeRedis(),
                runtime_session_factory=runtime_factory,
            )

            options = resolver.list_options()
            candidates = resolver.list_school_reactivation_candidates()

        assert len(options) == 1
        assert options[0].kind == "staff_assignment"
        assert candidates == ()
        assert resolver.resolve(options[0].opaque_ref).school_id == SchoolId(school_id)
    finally:
        runtime_engine.dispose()


@pytest.mark.integration
def test_identity_option_label_uses_the_school_content_slice_at_database_now(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """A future school rename must not leak through the stable compatibility projection."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
        )
        with factory() as session:
            effective_at = session.execute(
                text("SELECT statement_timestamp() + interval '4 seconds'")
            ).scalar_one()
            source_key, old_display_name = session.execute(
                text(
                    "SELECT school.source_key, version.display_name "
                    "FROM schools AS school JOIN school_versions AS version "
                    "ON version.school_id=school.id "
                    "WHERE school.id=:school_id AND version.valid_from <= statement_timestamp() "
                    "AND (version.valid_to IS NULL OR version.valid_to > statement_timestamp())"
                ),
                {"school_id": school_id},
            ).one()

        _confirm(
            service=service,
            change_set=_plan(
                label="identity-future-school-label",
                base_version=2,
                changes=(
                    _root_upsert(
                        "school",
                        source_key,
                        2,
                        effective_at,
                        2,
                        {
                            "business_code": "FUTURE-LABEL",
                            "display_name": "未来生效的学校名称",
                            "timezone_name": "Asia/Shanghai",
                            "lifecycle_status": "active",
                        },
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="identity-future-school-label",
        )

        before = _execution_context_options(
            isolated_migration_schema,
            account_user_id=user_id,
        )
        assert len(before) == 1
        assert str(before[0][5]).startswith(f"{old_display_name} · "), before
        with factory() as session:
            session.execute(
                text(
                    "SELECT pg_sleep(GREATEST(EXTRACT(EPOCH FROM "
                    "(:effective_at - statement_timestamp())) + 0.25, 0))"
                ),
                {"effective_at": effective_at},
            )
        after = _execution_context_options(
            isolated_migration_schema,
            account_user_id=user_id,
        )
        assert len(after) == 1
        assert str(after[0][5]).startswith("未来生效的学校名称 · "), after


@pytest.mark.integration
@pytest.mark.parametrize(
    ("fact_kind", "table_name"),
    (("person", "school_people"), ("organization", "organization_units")),
)
def test_identity_rechecks_formally_terminated_actor_roots(
    isolated_migration_schema: PostgresRoles,
    fact_kind: str,
    table_name: str,
) -> None:
    """A current assignment cannot keep an ended person or organization authoritative."""

    checked_now = datetime.now(UTC).replace(microsecond=0)
    runtime_factory, runtime_engine = _runtime_session_factory_for(isolated_migration_schema)
    try:
        with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
            school_id, user_id, person_id, assignment_id = facts
            identity_facts = ExecutionFacts(
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                student_person_id=uuid4(),
                assignment_id=assignment_id,
                guardian_relationship_id=uuid4(),
                student_id=uuid4(),
                administrative_class_id=uuid4(),
                location_id=uuid4(),
                valid_from=NOW,
            )
            resolver, _identity_session, _token = _resolver(
                facts=identity_facts,
                redis=FakeRedis(),
                runtime_session_factory=runtime_factory,
            )
            option = resolver.list_options()[0]
            assert resolver.resolve(option.opaque_ref).school_id == SchoolId(school_id)
            with factory() as session:
                source_key = str(
                    session.execute(
                        text(
                            f"SELECT source_key FROM {table_name} "  # noqa: S608 - allowlist
                            "WHERE school_id = :school_id AND id = :target_id"
                        ),
                        {
                            "school_id": school_id,
                            "target_id": (
                                person_id
                                if fact_kind == "person"
                                else session.execute(
                                    text(
                                        "SELECT organization_unit_id FROM staff_assignments "
                                        "WHERE id = :assignment_id"
                                    ),
                                    {"assignment_id": assignment_id},
                                ).scalar_one()
                            ),
                        },
                    ).scalar_one()
                )
            service = SchoolFactChangeService(
                reader=persistence,
                writer=persistence,
                clock=lambda: checked_now,
            )
            _confirm(
                service=service,
                change_set=_plan(
                    label=f"identity-terminal-{fact_kind}",
                    base_version=1,
                    changes=(
                        {
                            **_terminal_change(
                                fact_kind,
                                source_key,
                                1,
                                checked_now,
                                1,
                            ),
                            "operation": "end",
                        },
                    ),
                ),
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                assignment_id=assignment_id,
                label=f"identity-terminal-{fact_kind}",
            )

            assert resolver.list_options() == ()
            with pytest.raises(ContextNoLongerValid):
                resolver.resolve(option.opaque_ref)
    finally:
        runtime_engine.dispose()


@pytest.mark.integration
def test_legacy_student_history_survives_a_future_reactivation_epoch(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """A future reactivate cannot erase pre-end history for a legacy student identity."""

    checked_now = datetime.now(UTC)
    terminal_at = checked_now + timedelta(days=2)
    reactivate_at = checked_now + timedelta(days=4)
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
        legacy_id = uuid4()
        legacy_source_key = f"student:legacy:{uuid4().hex}"
        with factory.begin() as session:
            legacy_person_id = session.execute(
                text(
                    "SELECT id FROM school_people WHERE school_id = :school_id "
                    "AND source_key = 'person:parent'"
                ),
                {"school_id": school_id},
            ).scalar_one()
            session.execute(
                text(
                    "INSERT INTO students "
                    "(id, school_id, source_key, person_id, status, version) "
                    "VALUES (:id, :school_id, :source_key, :person_id, 'active', 1)"
                ),
                {
                    "id": legacy_id,
                    "school_id": school_id,
                    "source_key": legacy_source_key,
                    "person_id": legacy_person_id,
                },
            )

        terminal = _confirm(
            service=service,
            change_set=_plan(
                label="legacy-student-end",
                base_version=2,
                changes=(
                    {
                        **_terminal_change("student", legacy_source_key, 1, terminal_at, 2),
                        "operation": "end",
                    },
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="legacy-student-end",
        )
        reactivated = _confirm(
            service=service,
            change_set=_plan(
                label="legacy-student-future-reactivate",
                base_version=terminal.target_version,
                changes=(
                    {
                        **_root_upsert(
                            "student",
                            legacy_source_key,
                            2,
                            reactivate_at,
                            2,
                            {
                                "person_source_key": "person:parent",
                                "status": "active",
                            },
                        ),
                        "operation": "reactivate",
                    },
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="legacy-student-future-reactivate",
        )
        assert reactivated.target_version == 4

        with factory() as session:
            actual = session.execute(
                text(
                    "SELECT "
                    "app.school_fact_root_is_effective_at(:school_id, 'student', :id, :before), "
                    "app.school_fact_root_is_effective_at(:school_id, 'student', :id, :gap), "
                    "app.school_fact_root_is_effective_at(:school_id, 'student', :id, :after)"
                ),
                {
                    "school_id": school_id,
                    "id": legacy_id,
                    "before": terminal_at - timedelta(seconds=1),
                    "gap": terminal_at,
                    "after": reactivate_at,
                },
            ).one()
        assert actual == (True, False, True)


@pytest.mark.integration
def test_legacy_student_remains_effective_before_its_first_future_upsert(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """A first formal v2 upsert updates legacy content; it is not the identity's birth."""

    checked_now = datetime.now(UTC)
    future_update_at = checked_now + timedelta(days=2)
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
        legacy_id = uuid4()
        legacy_source_key = f"student:legacy-future-upsert:{uuid4().hex}"
        with factory.begin() as session:
            legacy_person_id = session.execute(
                text(
                    "SELECT id FROM school_people WHERE school_id = :school_id "
                    "AND source_key = 'person:parent'"
                ),
                {"school_id": school_id},
            ).scalar_one()
            session.execute(
                text(
                    "INSERT INTO students "
                    "(id, school_id, source_key, person_id, status, version) "
                    "VALUES (:id, :school_id, :source_key, :person_id, 'active', 1)"
                ),
                {
                    "id": legacy_id,
                    "school_id": school_id,
                    "source_key": legacy_source_key,
                    "person_id": legacy_person_id,
                },
            )

        _confirm(
            service=service,
            change_set=_plan(
                label="legacy-student-first-future-upsert",
                base_version=2,
                changes=(
                    _root_upsert(
                        "student",
                        legacy_source_key,
                        1,
                        future_update_at,
                        2,
                        {"person_source_key": "person:parent", "status": "active"},
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="legacy-student-first-future-upsert",
        )

        with factory() as session:
            actual = session.execute(
                text(
                    "SELECT "
                    "app.school_fact_root_is_effective_at(:school_id, 'student', :id, :before), "
                    "app.school_fact_root_is_effective_at(:school_id, 'student', :id, :after)"
                ),
                {
                    "school_id": school_id,
                    "id": legacy_id,
                    "before": future_update_at - timedelta(seconds=1),
                    "after": future_update_at,
                },
            ).one()
        assert actual == (True, True)


@pytest.mark.integration
@pytest.mark.parametrize("operation", ("upsert", "reactivate"))
def test_existing_student_identity_cannot_be_rebound_to_another_person(
    isolated_migration_schema: PostgresRoles,
    operation: str,
) -> None:
    """student -> person is an immutable stable-identity bridge after creation."""

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
        base_version = 2
        expected_version = 1
        if operation == "reactivate":
            terminal = _confirm(
                service=service,
                change_set=_plan(
                    label="student-terminal-before-person-rebind",
                    base_version=base_version,
                    changes=(
                        _terminal_change(
                            "student",
                            "student:001",
                            expected_version,
                            checked_now + timedelta(days=1),
                            2,
                        ),
                    ),
                ),
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                assignment_id=assignment_id,
                label="student-terminal-before-person-rebind",
            )
            base_version = terminal.target_version
            expected_version = 2
        with factory() as session:
            before = session.execute(
                text(
                    "SELECT person_id, status, version FROM students "
                    "WHERE school_id = :school_id AND source_key = 'student:001'"
                ),
                {"school_id": school_id},
            ).one()

        change = _root_upsert(
            "student",
            "student:001",
            expected_version,
            checked_now + timedelta(days=2),
            3,
            {"person_source_key": "person:parent", "status": "active"},
        )
        change["operation"] = operation
        with pytest.raises(SchoolFactChangeRejected):
            _confirm(
                service=service,
                change_set=_plan(
                    label=f"student-{operation}-person-rebind",
                    base_version=base_version,
                    changes=(change,),
                ),
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                assignment_id=assignment_id,
                label=f"student-{operation}-person-rebind",
            )

        with factory() as session:
            after = session.execute(
                text(
                    "SELECT person_id, status, version FROM students "
                    "WHERE school_id = :school_id AND source_key = 'student:001'"
                ),
                {"school_id": school_id},
            ).one()
            lineage = session.execute(
                text(
                    "SELECT target_version, operation FROM school_fact_change_lineage "
                    "WHERE school_id = :school_id AND fact_kind = 'student' "
                    "AND fact_source_key = 'student:001' ORDER BY target_version"
                ),
                {"school_id": school_id},
            ).all()
        assert after == before
        assert lineage == (
            [(1, "upsert")] if operation == "upsert" else [(1, "upsert"), (2, "revoke")]
        )


@pytest.mark.parametrize("fact_kind", ("assignment", "login_mapping"))
def test_terminated_assignment_and_login_mapping_cannot_reopen_same_slice(
    isolated_migration_schema: PostgresRoles,
    fact_kind: str,
) -> None:
    """Frozen authority 491: relation slices cannot be reopened in place."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
        )
        ended_at = NOW + timedelta(days=1)
        source_key = "assignment:parent" if fact_kind == "assignment" else "login:parent"
        operation = "revoke" if fact_kind == "assignment" else "end"
        terminal = _plan(
            label=f"terminal-{fact_kind}-slice",
            base_version=2,
            changes=(
                {
                    **_terminal_change(fact_kind, source_key, 1, ended_at, 2),
                    "operation": operation,
                },
            ),
        )
        _confirm(
            service=service,
            change_set=terminal,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label=f"terminal-{fact_kind}-slice",
        )
        before = _relationship_row(factory, school_id, fact_kind)
        reopen_at = ended_at + timedelta(days=1)
        upsert = (
            _assignment_upsert("assignment:parent", 2, reopen_at, 2)
            if fact_kind == "assignment"
            else _login_upsert("login:parent", 2, reopen_at, 2, external_subject="parent-001")
        )
        ordinary_reopen = _plan(
            label=f"ordinary-reopen-{fact_kind}-slice",
            base_version=3,
            changes=(upsert,),
        )
        rejection: str | None = None
        try:
            _confirm(
                service=service,
                change_set=ordinary_reopen,
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                assignment_id=assignment_id,
                label=f"ordinary-reopen-{fact_kind}-slice",
            )
        except SchoolFactChangeRejected as exc:
            rejection = str(exc)
        after = _relationship_row(factory, school_id, fact_kind)

        assert rejection is not None, (
            f"已终止 {fact_kind} 仍可被同 source key 的普通 upsert 原地重开；"
            f"terminal={before!r}, after={after!r}"
        )
        assert after == before, "拒绝原地重开时不得改写旧切片"


@pytest.mark.parametrize("fact_kind", ("assignment", "login_mapping"))
def test_backdated_upsert_cannot_erase_a_later_terminal_operation(
    isolated_migration_schema: PostgresRoles,
    fact_kind: str,
) -> None:
    """A backdated upsert cannot bypass the latest terminal lineage operation."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
        )
        ended_at = NOW + timedelta(days=2)
        source_key = "assignment:parent" if fact_kind == "assignment" else "login:parent"
        terminal_change = {
            **_terminal_change(fact_kind, source_key, 1, ended_at, 2),
            "operation": "end",
        }
        _confirm(
            service=service,
            change_set=_plan(
                label=f"end-before-backdated-{fact_kind}-upsert",
                base_version=2,
                changes=(terminal_change,),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label=f"end-before-backdated-{fact_kind}-upsert",
        )
        before = _relationship_row(factory, school_id, fact_kind)
        backdated_at = ended_at - timedelta(days=1)
        upsert = (
            _assignment_upsert(source_key, 2, backdated_at, 2)
            if fact_kind == "assignment"
            else _login_upsert(
                source_key,
                2,
                backdated_at,
                2,
                external_subject="parent-001",
            )
        )

        with pytest.raises(SchoolFactChangeRejected):
            _confirm(
                service=service,
                change_set=_plan(
                    label=f"backdated-{fact_kind}-upsert-after-end",
                    base_version=3,
                    changes=(upsert,),
                ),
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                assignment_id=assignment_id,
                label=f"backdated-{fact_kind}-upsert-after-end",
            )
        assert _relationship_row(factory, school_id, fact_kind) == before


@pytest.mark.parametrize("fact_kind", ("assignment", "login_mapping"))
def test_terminal_relationship_rejects_a_second_terminal_operation(
    isolated_migration_schema: PostgresRoles,
    fact_kind: str,
) -> None:
    """revoke followed by end must not rewrite a rejected slice back to confirmed."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
        )
        source_key = "assignment:parent" if fact_kind == "assignment" else "login:parent"
        first_terminal_at = NOW + timedelta(days=1)
        first = _confirm(
            service=service,
            change_set=_plan(
                label=f"first-terminal-{fact_kind}",
                base_version=2,
                changes=(
                    {
                        **_terminal_change(
                            fact_kind,
                            source_key,
                            1,
                            first_terminal_at,
                            2,
                        ),
                        "operation": "revoke",
                    },
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label=f"first-terminal-{fact_kind}",
        )
        assert first.target_version == 3
        before = _relationship_row(factory, school_id, fact_kind)

        with pytest.raises(SchoolFactChangeRejected):
            _confirm(
                service=service,
                change_set=_plan(
                    label=f"second-terminal-{fact_kind}",
                    base_version=3,
                    changes=(
                        {
                            **_terminal_change(
                                fact_kind,
                                source_key,
                                2,
                                first_terminal_at + timedelta(days=1),
                                2,
                            ),
                            "operation": "end",
                        },
                    ),
                ),
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                assignment_id=assignment_id,
                label=f"second-terminal-{fact_kind}",
            )

        assert _relationship_row(factory, school_id, fact_kind) == before
        with factory() as session:
            lineage = session.execute(
                text(
                    "SELECT target_version, operation FROM school_fact_change_lineage "
                    "WHERE school_id = :school_id AND fact_kind = :fact_kind "
                    "AND fact_source_key = :source_key ORDER BY target_version"
                ),
                {
                    "school_id": school_id,
                    "fact_kind": fact_kind,
                    "source_key": source_key,
                },
            ).all()
        assert lineage == [(1, "upsert"), (2, "revoke")]


def test_assignment_upsert_cannot_clear_an_existing_planned_valid_to(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
        )
        planned_valid_to = NOW + timedelta(days=30)
        _confirm(
            service=service,
            change_set=_plan(
                label="assignment-add-planned-valid-to",
                base_version=2,
                changes=(
                    _assignment_upsert(
                        "assignment:parent",
                        1,
                        NOW + timedelta(days=1),
                        2,
                        valid_to=planned_valid_to,
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="assignment-add-planned-valid-to",
        )
        before = _relationship_row(factory, school_id, "assignment")

        with pytest.raises(SchoolFactChangeRejected):
            _confirm(
                service=service,
                change_set=_plan(
                    label="assignment-omit-existing-planned-valid-to",
                    base_version=3,
                    changes=(
                        _assignment_upsert(
                            "assignment:parent",
                            2,
                            NOW + timedelta(days=2),
                            2,
                        ),
                    ),
                ),
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                assignment_id=assignment_id,
                label="assignment-omit-existing-planned-valid-to",
            )
        assert _relationship_row(factory, school_id, "assignment") == before


def test_terminated_relationships_can_reestablish_as_new_explicit_facts(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """New source keys/new targets are an explicit new fact change, not an in-place reopen."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
        )
        ended_at = NOW + timedelta(days=1)
        terminal = _plan(
            label="terminal-before-new-slices",
            base_version=2,
            changes=(
                _terminal_change("assignment", "assignment:parent", 1, ended_at, 2),
                {
                    **_terminal_change("login_mapping", "login:parent", 1, ended_at, 3),
                    "operation": "end",
                },
            ),
        )
        _confirm(
            service=service,
            change_set=terminal,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="terminal-before-new-slices",
        )
        old_target_ids = {
            fact_kind: _lineage_target_id(factory, school_id, fact_kind, source_key)
            for fact_kind, source_key in (
                ("assignment", "assignment:parent"),
                ("login_mapping", "login:parent"),
            )
        }
        terminal_rows = {
            fact_kind: _relationship_row(factory, school_id, fact_kind)
            for fact_kind in ("assignment", "login_mapping")
        }
        _seed_login_cache_user(
            factory,
            external_subject="parent-002",
            display_name="第二家长账号缓存",
        )
        reestablished = _plan(
            label="new-explicit-slices",
            base_version=3,
            changes=(
                _assignment_upsert(
                    "assignment:parent:second-appointment",
                    0,
                    ended_at + timedelta(days=1),
                    2,
                ),
                _login_upsert(
                    "login:parent:second-binding",
                    0,
                    ended_at + timedelta(days=1),
                    3,
                    external_subject="parent-002",
                ),
            ),
        )
        result = _confirm(
            service=service,
            change_set=reestablished,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="new-explicit-slices",
        )

        assert result.changed_count == 2
        with factory() as session:
            rows = session.execute(
                text(
                    "SELECT fact_kind, fact_source_key, target_id FROM "
                    "school_fact_change_lineage WHERE batch_id = :batch_id "
                    "ORDER BY fact_kind"
                ),
                {"batch_id": result.batch_id},
            ).all()
        assert {(kind, key) for kind, key, _target_id in rows} == {
            ("assignment", "assignment:parent:second-appointment"),
            ("login_mapping", "login:parent:second-binding"),
        }
        assert len({target_id for _kind, _key, target_id in rows}) == 2
        for fact_kind, _source_key, target_id in rows:
            assert target_id != old_target_ids[fact_kind]
            assert _relationship_row(factory, school_id, fact_kind) == terminal_rows[fact_kind]


@pytest.mark.integration
def test_ended_assignment_can_reestablish_with_a_new_source_at_the_boundary(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """Stable compatibility projections must not keep enforcing obsolete time ranges."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, actor_assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
        )
        with factory() as session:
            ended_at = session.execute(
                text("SELECT statement_timestamp() + interval '2 seconds'")
            ).scalar_one()
            old_target_id = session.execute(
                text(
                    "SELECT id FROM staff_assignments WHERE school_id=:school_id "
                    "AND source_key='assignment:parent'"
                ),
                {"school_id": school_id},
            ).scalar_one()

        _confirm(
            service=service,
            change_set=_plan(
                label="assignment-end-before-new-source",
                base_version=2,
                changes=(
                    {
                        **_terminal_change("assignment", "assignment:parent", 1, ended_at, 2),
                        "operation": "end",
                    },
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
            label="assignment-end-before-new-source",
        )
        result = _confirm(
            service=service,
            change_set=_plan(
                label="assignment-new-source-at-end-boundary",
                base_version=3,
                changes=(
                    _assignment_upsert(
                        "assignment:parent:after-end",
                        0,
                        ended_at,
                        2,
                        position_title="结束边界后的新任职",
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
            label="assignment-new-source-at-end-boundary",
        )

        with factory() as session:
            new_target_id = session.execute(
                text(
                    "SELECT id FROM staff_assignments WHERE school_id=:school_id "
                    "AND source_key='assignment:parent:after-end'"
                ),
                {"school_id": school_id},
            ).scalar_one()
        assert result.changed_count == 1
        assert new_target_id != old_target_id


def _assignment_upsert(
    source_key: str,
    expected_version: int,
    effective_at,
    row_number: int,
    *,
    valid_to=None,
    position_title: str = "家校联系人",
    semantic_role_code: str = "family_contact",
) -> dict[str, object]:
    content: dict[str, object] = {
        "person_source_key": "person:parent",
        "organization_source_key": "organization:dorm",
        "position_title": position_title,
        "semantic_role_code": semantic_role_code,
        "assignment_kind": "part_time",
        "appointment_source": "manual",
        "approval_reference": "approval:cp2-lifecycle",
        "status": "confirmed",
    }
    if valid_to is not None:
        content["valid_to"] = valid_to
    return {
        "operation": "upsert",
        "fact_kind": "assignment",
        "source_key": source_key,
        "expected_version": expected_version,
        "effective_at": effective_at,
        "origin": {"sheet_code": "assignments", "row_number": row_number},
        "content": content,
    }


def _login_upsert(
    source_key: str,
    expected_version: int,
    effective_at,
    row_number: int,
    *,
    external_subject: str,
    status: str = "confirmed",
) -> dict[str, object]:
    return {
        "operation": "upsert",
        "fact_kind": "login_mapping",
        "source_key": source_key,
        "expected_version": expected_version,
        "effective_at": effective_at,
        "origin": {"sheet_code": "login_mappings", "row_number": row_number},
        "content": {
            "provider_code": "local-test",
            "provider_tenant_key": "sf2-all",
            "external_subject": external_subject,
            "person_source_key": "person:parent",
            "status": status,
        },
    }


def _login_upsert_without_display(
    source_key: str,
    expected_version: int,
    effective_at: datetime,
    row_number: int,
    *,
    external_subject: str,
    status: str = "confirmed",
) -> dict[str, object]:
    return {
        "operation": "upsert",
        "fact_kind": "login_mapping",
        "source_key": source_key,
        "expected_version": expected_version,
        "effective_at": effective_at,
        "origin": {"sheet_code": "login_mappings", "row_number": row_number},
        "content": {
            "provider_code": "local-test",
            "provider_tenant_key": "sf2-all",
            "external_subject": external_subject,
            "person_source_key": "person:parent",
            "status": status,
        },
    }


def _seed_login_cache_user(factory: object, *, external_subject: str, display_name: str) -> int:
    with factory.begin() as session:  # type: ignore[operator]
        return int(
            session.execute(
                text(
                    "INSERT INTO users (provider_code, provider_tenant_key, external_subject, "
                    "display_name, is_active, security_version) VALUES "
                    "('local-test', 'sf2-all', :external_subject, :display_name, true, 1) "
                    "ON CONFLICT (provider_code, provider_tenant_key, external_subject) "
                    "DO UPDATE SET display_name=EXCLUDED.display_name RETURNING id"
                ),
                {"external_subject": external_subject, "display_name": display_name},
            ).scalar_one()
        )


def test_login_mapping_contract_has_no_display_name_content_authority() -> None:
    payload = _login_upsert_without_display(
        "login:no-display-authority",
        0,
        NOW,
        2,
        external_subject="no-display-authority",
    )

    change = CanonicalFactChangeV1.model_validate(payload)

    assert change.content is not None
    assert "display_name" not in change.content.model_dump(exclude_none=True)
    assert "display_name" not in LoginMappingSnapshot.model_fields
    payload_with_display = {
        **payload,
        "content": {**cast(dict[str, object], payload["content"]), "display_name": "越界名称"},
    }
    with pytest.raises(ValidationError):
        CanonicalFactChangeV1.model_validate(payload_with_display)


@pytest.mark.integration
def test_login_mapping_requires_existing_user_and_preserves_login_cache_display(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, actor_user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=actor_user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            without_login_mapping=True,
        )
        change_set = _plan(
            label="login-mapping-no-display-authority",
            base_version=2,
            changes=(
                _login_upsert_without_display(
                    "login:no-display-authority",
                    0,
                    NOW,
                    2,
                    external_subject="no-display-authority",
                ),
            ),
        )
        context = context_for(
            school_id=school_id,
            user_id=actor_user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            key=f"cp3:login-no-display:{uuid4().hex}",
        )
        session_binding = f"cp3-login-no-display-{uuid4().hex}"
        receipt = service.prepare_confirmation(
            change_set=change_set,
            context=context,
            session_binding=session_binding,
        )

        with pytest.raises(
            SchoolFactChangeRejected,
            match="登录缓存账号尚不存在",
        ):
            service.confirm(
                credential=receipt.credential,
                change_set=change_set,
                context=context,
                session_binding=session_binding,
            )
        with factory() as session:
            assert (
                session.execute(
                    text("SELECT count(*) FROM users WHERE external_subject='no-display-authority'")
                ).scalar_one()
                == 0
            )
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM account_person_bindings "
                        "WHERE school_id=:school_id AND source_key='login:no-display-authority'"
                    ),
                    {"school_id": school_id},
                ).scalar_one()
                == 0
            )

        with factory.begin() as session:
            cached_user_id = int(
                session.execute(
                    text(
                        "INSERT INTO users (provider_code, provider_tenant_key, "
                        "external_subject, display_name, is_active, security_version) "
                        "VALUES ('local-test', 'sf2-all', 'no-display-authority', "
                        "'登录缓存名称', true, 1) RETURNING id"
                    )
                ).scalar_one()
            )

        result = service.confirm(
            credential=receipt.credential,
            change_set=change_set,
            context=context,
            session_binding=session_binding,
        )
        assert result.changed_count == 1
        with factory() as session:
            binding_id = UUID(
                str(
                    session.execute(
                        text(
                            "SELECT id FROM account_person_bindings "
                            "WHERE school_id=:school_id "
                            "AND source_key='login:no-display-authority'"
                        ),
                        {"school_id": school_id},
                    ).scalar_one()
                )
            )
            assert (
                session.execute(
                    text("SELECT display_name FROM users WHERE id=:id"),
                    {"id": cached_user_id},
                ).scalar_one()
                == "登录缓存名称"
            )
            assert (
                session.execute(
                    text(
                        "SELECT name.display_name FROM school_person_names AS name "
                        "JOIN account_person_bindings AS binding "
                        "ON binding.school_id=name.school_id AND binding.person_id=name.person_id "
                        "WHERE binding.id=:binding_id AND name.status='confirmed' "
                        "AND name.valid_from <= :as_of "
                        "AND (name.valid_to IS NULL OR name.valid_to > :as_of)"
                    ),
                    {"binding_id": binding_id, "as_of": NOW},
                ).scalar_one()
                == "学生家长"
            )
            assert session.execute(
                text(
                    "SELECT account_user_id, external_subject FROM "
                    "account_person_binding_versions WHERE school_id=:school_id "
                    "AND binding_id=:binding_id"
                ),
                {"school_id": school_id, "binding_id": binding_id},
            ).one() == (cached_user_id, "no-display-authority")
            snapshot = SqlAlchemyPersonRepository(session).resolve_login_mapping_snapshot(
                SchoolId(school_id), binding_id, as_of=NOW
            )
        assert snapshot is not None
        assert "display_name" not in snapshot.model_dump()


def _relationship_row(
    factory: object,
    school_id: UUID,
    fact_kind: str,
) -> tuple[object, ...]:
    with factory() as session:  # type: ignore[operator]
        if fact_kind == "assignment":
            row = session.execute(
                text(
                    "SELECT id, valid_from, valid_to, revoked_at, status, version "
                    "FROM staff_assignments WHERE school_id = :school_id "
                    "AND source_key = 'assignment:parent'"
                ),
                {"school_id": school_id},
            ).one()
        else:
            row = session.execute(
                text(
                    "SELECT id, valid_from, valid_to, status, version "
                    "FROM account_person_bindings WHERE school_id = :school_id "
                    "AND source_key = 'login:parent'"
                ),
                {"school_id": school_id},
            ).one()
    return tuple(row)


def _lineage_target_id(
    factory: object,
    school_id: UUID,
    fact_kind: str,
    source_key: str,
) -> UUID:
    with factory() as session:  # type: ignore[operator]
        return UUID(
            str(
                session.execute(
                    text(
                        "SELECT target_id FROM school_fact_change_lineage "
                        "WHERE school_id = :school_id AND fact_kind = :fact_kind "
                        "AND fact_source_key = :source_key "
                        "ORDER BY target_version DESC LIMIT 1"
                    ),
                    {
                        "school_id": school_id,
                        "fact_kind": fact_kind,
                        "source_key": source_key,
                    },
                ).scalar_one()
            )
        )


def _execution_context_options(
    roles: PostgresRoles,
    *,
    account_user_id: int,
) -> list[tuple[object, ...]]:
    signer = ContextProofSigner(b"master-data-schema-context-signing-key-32-bytes")
    proof = signer.sign_account_options(
        account_user_id=account_user_id,
        authorization_version=1,
        session_nonce_hash=sha256(b"cp3-relationship-content-slices").hexdigest(),
        proof_expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    parameters = proof.parameters()
    with psycopg.connect(
        **roles.runtime_connection_parameters,
        autocommit=False,
    ) as connection:
        return connection.execute(
            "SELECT * FROM app.list_execution_context_options(%s, %s, %s, %s, %s)",
            (
                parameters["account_user_id"],
                parameters["authorization_version"],
                parameters["session_nonce_hash"],
                parameters["proof_expires_at"],
                parameters["server_proof"],
            ),
        ).fetchall()


def _school_reactivation_options(
    roles: PostgresRoles,
    *,
    account_user_id: int,
) -> list[tuple[object, ...]]:
    signer = ContextProofSigner(b"master-data-schema-context-signing-key-32-bytes")
    proof = signer.sign_account_options(
        account_user_id=account_user_id,
        authorization_version=1,
        session_nonce_hash=sha256(b"cp3-relationship-reactivation-slices").hexdigest(),
        proof_expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    parameters = proof.parameters()
    with psycopg.connect(
        **roles.runtime_connection_parameters,
        autocommit=False,
    ) as connection:
        return connection.execute(
            "SELECT * FROM app.list_school_reactivation_candidates(%s, %s, %s, %s, %s)",
            (
                parameters["account_user_id"],
                parameters["authorization_version"],
                parameters["session_nonce_hash"],
                parameters["proof_expires_at"],
                parameters["server_proof"],
            ),
        ).fetchall()


@pytest.mark.integration
def test_assignment_and_login_future_upserts_use_authoritative_content_slices(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """Real DB time must not expose future relationship content before its boundary."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, old_user_id, person_id, actor_assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=old_user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
        )
        with factory() as session:
            effective_at = session.execute(
                text("SELECT statement_timestamp() + interval '4 seconds'")
            ).scalar_one()
            planned_valid_to = effective_at + timedelta(seconds=2)
            stable_before = session.execute(
                text(
                    "SELECT a.id, a.source_key, a.valid_from, b.id, b.source_key, b.valid_from "
                    "FROM staff_assignments a CROSS JOIN account_person_bindings b "
                    "WHERE a.school_id = :school_id AND a.source_key = 'assignment:parent' "
                    "AND b.school_id = :school_id AND b.source_key = 'login:parent'"
                ),
                {"school_id": school_id},
            ).one()
            mapped_old_user_id = int(
                session.execute(
                    text("SELECT id FROM users WHERE external_subject = 'parent-001'")
                ).scalar_one()
            )
        _seed_login_cache_user(
            factory,
            external_subject="parent-future",
            display_name="未来账号缓存",
        )

        assignment_result = _confirm(
            service=service,
            change_set=_plan(
                label="assignment-future-content-slice",
                base_version=2,
                changes=(
                    _assignment_upsert(
                        "assignment:parent",
                        1,
                        effective_at,
                        2,
                        position_title="未来校区协调员",
                        semantic_role_code="future_campus_coordinator",
                        valid_to=planned_valid_to,
                    ),
                ),
            ),
            school_id=school_id,
            user_id=old_user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
            label="assignment-future-content-slice",
        )
        login_result = _confirm(
            service=service,
            change_set=_plan(
                label="login-future-content-slice",
                base_version=3,
                changes=(
                    _login_upsert(
                        "login:parent",
                        1,
                        effective_at,
                        2,
                        external_subject="parent-future",
                    ),
                ),
            ),
            school_id=school_id,
            user_id=old_user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
            label="login-future-content-slice",
        )

        with factory() as session:
            assignment_id = stable_before[0]
            binding_id = stable_before[3]
            assignment_repository = SqlAlchemyOrganizationRepository(session)
            assignment_before = assignment_repository.resolve_assignment_snapshot(
                SchoolId(school_id),
                AssignmentId(assignment_id),
                as_of=effective_at - timedelta(microseconds=1),
            )
            assignment_after = assignment_repository.resolve_assignment_snapshot(
                SchoolId(school_id),
                AssignmentId(assignment_id),
                as_of=effective_at,
            )
            login_repository = SqlAlchemyPersonRepository(session)
            login_before = login_repository.resolve_login_mapping_snapshot(
                SchoolId(school_id), binding_id, as_of=effective_at - timedelta(microseconds=1)
            )
            login_after = login_repository.resolve_login_mapping_snapshot(
                SchoolId(school_id), binding_id, as_of=effective_at
            )
            assignment_slices = session.execute(
                text(
                    "SELECT target_version, position_title, semantic_role_code, "
                    "effective_from, effective_to FROM staff_assignment_versions "
                    "WHERE school_id = :school_id AND assignment_id = :target_id "
                    "ORDER BY target_version"
                ),
                {"school_id": school_id, "target_id": assignment_id},
            ).all()
            login_slices = session.execute(
                text(
                    "SELECT target_version, external_subject, "
                    "effective_from, effective_to, account_user_id "
                    "FROM account_person_binding_versions "
                    "WHERE school_id = :school_id AND binding_id = :target_id "
                    "ORDER BY target_version"
                ),
                {"school_id": school_id, "target_id": binding_id},
            ).all()
            stable_after = session.execute(
                text(
                    "SELECT a.id, a.source_key, a.valid_from, b.id, b.source_key, b.valid_from "
                    "FROM staff_assignments a CROSS JOIN account_person_bindings b "
                    "WHERE a.id = :assignment_id AND b.id = :binding_id"
                ),
                {"assignment_id": assignment_id, "binding_id": binding_id},
            ).one()
            new_user_id = int(
                session.execute(
                    text("SELECT id FROM users WHERE external_subject = 'parent-future'")
                ).scalar_one()
            )
            bookkeeping = {
                UUID(str(batch_id)): tuple(
                    session.execute(
                        text(
                            "SELECT "
                            "(SELECT count(*) FROM master_data_import_units WHERE batch_id=:id), "
                            "(SELECT count(*) FROM school_fact_change_lineage WHERE batch_id=:id), "
                            "(SELECT count(*) FROM audit_logs WHERE target_id=:target), "
                            "(SELECT max(target_version) FROM school_fact_change_lineage "
                            "WHERE batch_id=:id)"
                        ),
                        {"id": batch_id, "target": str(batch_id)},
                    ).one()
                )
                for batch_id in (assignment_result.batch_id, login_result.batch_id)
            }

        assert stable_after == stable_before
        assert assignment_before is not None
        assert assignment_before.position_title == "家校联系人"
        assert assignment_before.semantic_role_code == "family_contact"
        assert assignment_after is not None
        assert assignment_after.position_title == "未来校区协调员"
        assert assignment_after.semantic_role_code == "future_campus_coordinator"
        assert assignment_after.planned_valid_to == planned_valid_to
        assert login_before is not None
        assert login_before.external_subject == "parent-001"
        assert login_after is not None
        assert login_after.external_subject == "parent-future"
        assert "display_name" not in login_before.model_dump()
        assert "display_name" not in login_after.model_dump()
        assert [(row[0], row[1], row[2]) for row in assignment_slices] == [
            (1, "家校联系人", "family_contact"),
            (2, "未来校区协调员", "future_campus_coordinator"),
        ]
        assert assignment_slices[0].effective_to == effective_at
        assert assignment_slices[1].effective_from == effective_at
        assert [(row[0], row[1]) for row in login_slices] == [
            (1, "parent-001"),
            (2, "parent-future"),
        ]
        assert login_slices[0].effective_to == effective_at
        assert login_slices[1].effective_from == effective_at
        assert set(bookkeeping.values()) == {(1, 1, 1, 2)}

        old_before = _execution_context_options(
            isolated_migration_schema,
            account_user_id=mapped_old_user_id,
        )
        new_before = _execution_context_options(
            isolated_migration_schema,
            account_user_id=new_user_id,
        )
        assert any(row[1] == assignment_id for row in old_before), old_before
        assert new_before == []
        runtime_factory, runtime_engine = _runtime_session_factory_for(isolated_migration_schema)
        identity_values = {
            "school_id": school_id,
            "person_id": person_id,
            "student_person_id": uuid4(),
            "assignment_id": assignment_id,
            "guardian_relationship_id": uuid4(),
            "student_id": uuid4(),
            "administrative_class_id": uuid4(),
            "location_id": uuid4(),
            "valid_from": NOW,
        }
        old_resolver, _old_session, _old_token = _resolver(
            facts=ExecutionFacts(user_id=mapped_old_user_id, **identity_values),
            redis=FakeRedis(),
            runtime_session_factory=runtime_factory,
        )
        new_resolver, _new_session, _new_token = _resolver(
            facts=ExecutionFacts(user_id=new_user_id, **identity_values),
            redis=FakeRedis(),
            runtime_session_factory=runtime_factory,
        )
        try:
            old_refs = old_resolver.list_options()
            assert len(old_refs) == 1
            old_actor = old_resolver.resolve(old_refs[0].opaque_ref)
            assert isinstance(old_actor, StaffActorContext)
            assert old_actor.assignment_id == AssignmentId(assignment_id)
            assert new_resolver.list_options() == ()
            with factory() as session:
                session.execute(
                    text(
                        "SELECT pg_sleep(GREATEST(EXTRACT(EPOCH FROM "
                        "(:effective_at - statement_timestamp())) + 0.25, 0))"
                    ),
                    {"effective_at": effective_at},
                )
            old_after = _execution_context_options(
                isolated_migration_schema,
                account_user_id=mapped_old_user_id,
            )
            new_after = _execution_context_options(
                isolated_migration_schema,
                account_user_id=new_user_id,
            )
            assert old_after == []
            assert any(row[1] == assignment_id for row in new_after)
            with pytest.raises(ContextNoLongerValid):
                old_resolver.resolve(old_refs[0].opaque_ref)
            new_refs = new_resolver.list_options()
            assert len(new_refs) == 1
            new_actor = new_resolver.resolve(new_refs[0].opaque_ref)
            assert isinstance(new_actor, StaffActorContext)
            assert new_actor.assignment_id == AssignmentId(assignment_id)
            with factory() as session:
                session.execute(
                    text(
                        "SELECT pg_sleep(GREATEST(EXTRACT(EPOCH FROM "
                        "(:planned_valid_to - statement_timestamp())) + 0.25, 0))"
                    ),
                    {"planned_valid_to": planned_valid_to},
                )
                assert (
                    SqlAlchemyOrganizationRepository(session).get_current_assignment(
                        SchoolId(school_id),
                        AssignmentId(assignment_id),
                        as_of=planned_valid_to,
                    )
                    is None
                )
            assert (
                _execution_context_options(
                    isolated_migration_schema,
                    account_user_id=new_user_id,
                )
                == []
            )
            with pytest.raises(ContextNoLongerValid):
                new_resolver.resolve(new_refs[0].opaque_ref)
            with pytest.raises(SchoolFactChangeRejected):
                _confirm(
                    service=service,
                    change_set=_plan(
                        label="assignment-cannot-reopen-after-planned-expiry",
                        base_version=4,
                        changes=(
                            _assignment_upsert(
                                "assignment:parent",
                                2,
                                planned_valid_to + timedelta(seconds=1),
                                2,
                                position_title="不得原地重开",
                                valid_to=planned_valid_to + timedelta(days=1),
                            ),
                        ),
                    ),
                    school_id=school_id,
                    user_id=old_user_id,
                    person_id=person_id,
                    assignment_id=actor_assignment_id,
                    label="assignment-cannot-reopen-after-planned-expiry",
                )
        finally:
            runtime_engine.dispose()


@pytest.mark.integration
def test_assignment_planned_expiry_allows_a_new_source_at_the_exact_boundary(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """A planned endpoint is the logical upper bound for cross-assignment exclusion."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, actor_assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
        )
        with factory() as session:
            checked_now, planned_valid_to = session.execute(
                text("SELECT statement_timestamp(), statement_timestamp() + interval '4 seconds'")
            ).one()

        _confirm(
            service=service,
            change_set=_plan(
                label="assignment-planned-expiry-old-source",
                base_version=2,
                changes=(
                    _assignment_upsert(
                        "assignment:parent",
                        1,
                        checked_now,
                        2,
                        valid_to=planned_valid_to,
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
            label="assignment-planned-expiry-old-source",
        )
        _confirm(
            service=service,
            change_set=_plan(
                label="assignment-planned-expiry-new-source",
                base_version=3,
                changes=(
                    _assignment_upsert(
                        "assignment:parent:replacement",
                        0,
                        planned_valid_to,
                        2,
                        position_title="到期后的新任职",
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
            label="assignment-planned-expiry-new-source",
        )

        with factory() as session:
            rows = session.execute(
                text(
                    "SELECT id, source_key FROM staff_assignments "
                    "WHERE school_id=:school_id AND source_key IN "
                    "('assignment:parent','assignment:parent:replacement')"
                ),
                {"school_id": school_id},
            ).all()
            assignment_ids = {row.source_key: row.id for row in rows}
            repository = SqlAlchemyOrganizationRepository(session)
            old_before = repository.get_current_assignment(
                SchoolId(school_id),
                AssignmentId(assignment_ids["assignment:parent"]),
                as_of=planned_valid_to - timedelta(microseconds=1),
            )
            new_before = repository.get_current_assignment(
                SchoolId(school_id),
                AssignmentId(assignment_ids["assignment:parent:replacement"]),
                as_of=planned_valid_to - timedelta(microseconds=1),
            )
            old_after = repository.get_current_assignment(
                SchoolId(school_id),
                AssignmentId(assignment_ids["assignment:parent"]),
                as_of=planned_valid_to,
            )
            new_after = repository.get_current_assignment(
                SchoolId(school_id),
                AssignmentId(assignment_ids["assignment:parent:replacement"]),
                as_of=planned_valid_to,
            )

        assert old_before is not None
        assert new_before is None
        assert old_after is None
        assert new_after is not None
        assert new_after.position_title == "到期后的新任职"


@pytest.mark.integration
def test_assignment_middle_correction_uses_the_planned_end_of_its_effective_slice(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """A later confirmed projection must not donate its planned end to an earlier slice."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, actor_assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
        )
        with factory() as session:
            checked_now = session.execute(text("SELECT statement_timestamp()"), {}).scalar_one()
        middle_at = checked_now + timedelta(seconds=1)
        future_at = checked_now + timedelta(seconds=3)
        future_planned_valid_to = checked_now + timedelta(seconds=6)

        _confirm(
            service=service,
            change_set=_plan(
                label="assignment-future-planned-before-middle-correction",
                base_version=2,
                changes=(
                    _assignment_upsert(
                        "assignment:parent",
                        1,
                        future_at,
                        2,
                        valid_to=future_planned_valid_to,
                        position_title="未来有计划终点的任职",
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
            label="assignment-future-planned-before-middle-correction",
        )
        _confirm(
            service=service,
            change_set=_plan(
                label="assignment-middle-correction-without-planned-end",
                base_version=3,
                changes=(
                    _assignment_upsert(
                        "assignment:parent",
                        2,
                        middle_at,
                        2,
                        position_title="中段纠错任职",
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
            label="assignment-middle-correction-without-planned-end",
        )

        with factory() as session:
            assignment_id = session.execute(
                text(
                    "SELECT id FROM staff_assignments WHERE school_id=:school_id "
                    "AND source_key='assignment:parent'"
                ),
                {"school_id": school_id},
            ).scalar_one()
            repository = SqlAlchemyOrganizationRepository(session)
            middle = repository.resolve_assignment_snapshot(
                SchoolId(school_id), AssignmentId(assignment_id), as_of=middle_at
            )
            future = repository.resolve_assignment_snapshot(
                SchoolId(school_id), AssignmentId(assignment_id), as_of=future_at
            )

        assert middle is not None
        assert middle.position_title == "中段纠错任职"
        assert middle.planned_valid_to is None
        assert future is not None
        assert future.position_title == "未来有计划终点的任职"
        assert future.planned_valid_to == future_planned_valid_to


@pytest.mark.integration
def test_future_login_mapping_status_and_display_do_not_mutate_the_global_user_early(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """A school mapping slice must not pre-apply its future content to the global account."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, actor_assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
        )
        with factory() as session:
            effective_at = session.execute(
                text("SELECT statement_timestamp() + interval '4 seconds'")
            ).scalar_one()
            account_before = session.execute(
                text(
                    "SELECT id, display_name, is_active FROM users "
                    "WHERE external_subject='parent-001'"
                )
            ).one()
            assignment_id = session.execute(
                text(
                    "SELECT id FROM staff_assignments WHERE school_id=:school_id "
                    "AND source_key='assignment:parent'"
                ),
                {"school_id": school_id},
            ).scalar_one()

        _confirm(
            service=service,
            change_set=_plan(
                label="login-future-rejected-content-slice",
                base_version=2,
                changes=(
                    _login_upsert(
                        "login:parent",
                        1,
                        effective_at,
                        2,
                        external_subject="parent-001",
                        status="rejected",
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
            label="login-future-rejected-content-slice",
        )

        with factory() as session:
            account_immediately_after_confirm = session.execute(
                text("SELECT id, display_name, is_active FROM users WHERE id=:id"),
                {"id": account_before.id},
            ).one()
            rejected_slice = session.execute(
                text(
                    "SELECT status, effective_from "
                    "FROM account_person_binding_versions "
                    "WHERE school_id=:school_id AND target_version=2"
                ),
                {"school_id": school_id},
            ).one()

        assert account_immediately_after_confirm == account_before
        assert rejected_slice == ("rejected", effective_at)
        before_options = _execution_context_options(
            isolated_migration_schema,
            account_user_id=int(account_before.id),
        )
        assert any(row[1] == assignment_id for row in before_options), before_options

        with factory() as session:
            session.execute(
                text(
                    "SELECT pg_sleep(GREATEST(EXTRACT(EPOCH FROM "
                    "(:effective_at - statement_timestamp())) + 0.25, 0))"
                ),
                {"effective_at": effective_at},
            )
            account_after_boundary = session.execute(
                text("SELECT id, display_name, is_active FROM users WHERE id=:id"),
                {"id": account_before.id},
            ).one()

        assert account_after_boundary == account_before
        assert (
            _execution_context_options(
                isolated_migration_schema,
                account_user_id=int(account_before.id),
            )
            == []
        )


@pytest.mark.integration
def test_relationship_content_slice_ddl_is_nonoverlapping_and_history_delete_rejected(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
        )
        with factory() as session:
            constraints = {
                str(name)
                for (name,) in session.execute(
                    text(
                        "SELECT catalog_constraint.conname "
                        "FROM pg_constraint AS catalog_constraint "
                        "JOIN pg_class AS relation "
                        "ON relation.oid=catalog_constraint.conrelid "
                        "JOIN pg_namespace AS namespace ON namespace.oid=relation.relnamespace "
                        "WHERE namespace.nspname='public' AND relation.relname IN "
                        "('staff_assignment_versions','account_person_binding_versions')"
                    )
                ).all()
            }
            stable_constraints = {
                str(name)
                for (name,) in session.execute(
                    text(
                        "SELECT catalog_constraint.conname "
                        "FROM pg_constraint AS catalog_constraint "
                        "JOIN pg_class AS relation "
                        "ON relation.oid=catalog_constraint.conrelid "
                        "JOIN pg_namespace AS namespace ON namespace.oid=relation.relnamespace "
                        "WHERE namespace.nspname='public' AND relation.relname IN "
                        "('staff_assignments','account_person_bindings')"
                    )
                ).all()
            }
        assert {
            "excl_staff_assignment_versions_confirmed_period",
            "excl_staff_assignment_versions_confirmed_slot_period",
            "excl_account_person_binding_versions_confirmed_period",
            "excl_account_person_binding_versions_account_period",
            "excl_account_person_binding_versions_person_period",
        } <= constraints
        assert {
            "excl_staff_assignments_confirmed_slot_period",
            "excl_account_person_bindings_account_period",
            "excl_account_person_bindings_person_period",
        }.isdisjoint(stable_constraints)

        for table_name in (
            "staff_assignment_versions",
            "account_person_binding_versions",
        ):
            with pytest.raises(DBAPIError) as exc_info:
                with factory.begin() as session:
                    session.execute(
                        text(f"DELETE FROM {table_name} WHERE school_id=:school_id"),  # noqa: S608
                        {"school_id": school_id},
                    )
            assert getattr(exc_info.value.orig, "sqlstate", None) == "55000"
            with factory() as session:
                assert (
                    int(
                        session.execute(
                            text(
                                f"SELECT count(*) FROM {table_name} "  # noqa: S608
                                "WHERE school_id=:school_id"
                            ),
                            {"school_id": school_id},
                        ).scalar_one()
                    )
                    > 0
                )


@pytest.mark.integration
def test_school_reactivation_candidates_follow_relationship_slices_at_db_time(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """The inactive-school candidate must not expose future login/assignment content early."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, actor_user_id, person_id, actor_assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=actor_user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
        )
        with factory() as session:
            effective_at = session.execute(
                text("SELECT statement_timestamp() + interval '3 seconds'")
            ).scalar_one()
            assignment_id = session.execute(
                text(
                    "SELECT id FROM staff_assignments "
                    "WHERE school_id=:school_id AND source_key='assignment:parent'"
                ),
                {"school_id": school_id},
            ).scalar_one()
            old_user_id = int(
                session.execute(
                    text("SELECT id FROM users WHERE external_subject='parent-001'")
                ).scalar_one()
            )
        _confirm(
            service=service,
            change_set=_plan(
                label="assignment-future-reactivation-candidate",
                base_version=2,
                changes=(
                    _assignment_upsert(
                        "assignment:parent",
                        1,
                        effective_at,
                        2,
                        position_title="未来停用校区管理员",
                    ),
                ),
            ),
            school_id=school_id,
            user_id=actor_user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
            label="assignment-future-reactivation-candidate",
        )
        _seed_login_cache_user(
            factory,
            external_subject="parent-reactivation-future",
            display_name="未来重启账号缓存",
        )
        _confirm(
            service=service,
            change_set=_plan(
                label="login-future-reactivation-candidate",
                base_version=3,
                changes=(
                    _login_upsert(
                        "login:parent",
                        1,
                        effective_at,
                        2,
                        external_subject="parent-reactivation-future",
                    ),
                ),
            ),
            school_id=school_id,
            user_id=actor_user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
            label="login-future-reactivation-candidate",
        )
        with factory() as session:
            new_user_id = int(
                session.execute(
                    text("SELECT id FROM users WHERE external_subject='parent-reactivation-future'")
                ).scalar_one()
            )
            inactive_at = session.execute(text("SELECT statement_timestamp() ")).scalar_one()
        _confirm(
            service=service,
            change_set=_plan(
                label="school-end-for-future-reactivation-candidate",
                base_version=4,
                changes=(
                    _terminal_change(
                        "school",
                        _school_source_key(factory, school_id),
                        2,
                        inactive_at,
                        2,
                    ),
                ),
            ),
            school_id=school_id,
            user_id=actor_user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
            label="school-end-for-future-reactivation-candidate",
        )

        runtime_factory, runtime_engine = _runtime_session_factory_for(isolated_migration_schema)
        identity_values = {
            "school_id": school_id,
            "person_id": person_id,
            "student_person_id": uuid4(),
            "assignment_id": assignment_id,
            "guardian_relationship_id": uuid4(),
            "student_id": uuid4(),
            "administrative_class_id": uuid4(),
            "location_id": uuid4(),
            "valid_from": NOW,
        }
        old_resolver, _old_session, _old_token = _resolver(
            facts=ExecutionFacts(user_id=old_user_id, **identity_values),
            redis=FakeRedis(),
            runtime_session_factory=runtime_factory,
        )
        new_resolver, _new_session, _new_token = _resolver(
            facts=ExecutionFacts(user_id=new_user_id, **identity_values),
            redis=FakeRedis(),
            runtime_session_factory=runtime_factory,
        )

        try:
            old_before = _school_reactivation_options(
                isolated_migration_schema,
                account_user_id=old_user_id,
            )
            new_before = _school_reactivation_options(
                isolated_migration_schema,
                account_user_id=new_user_id,
            )
            assert any(row[1] == assignment_id for row in old_before), old_before
            assert new_before == []
            old_refs = old_resolver.list_school_reactivation_candidates()
            assert len(old_refs) == 1
            assert old_resolver.resolve_school_reactivation_candidate(
                old_refs[0].opaque_ref
            ).assignment_id == AssignmentId(assignment_id)
            assert new_resolver.list_school_reactivation_candidates() == ()
            with factory() as session:
                session.execute(
                    text(
                        "SELECT pg_sleep(GREATEST(EXTRACT(EPOCH FROM "
                        "(:effective_at - statement_timestamp())) + 0.25, 0))"
                    ),
                    {"effective_at": effective_at},
                )
            old_after = _school_reactivation_options(
                isolated_migration_schema,
                account_user_id=old_user_id,
            )
            new_after = _school_reactivation_options(
                isolated_migration_schema,
                account_user_id=new_user_id,
            )
            assert old_after == []
            assert any(row[1] == assignment_id for row in new_after), new_after
            with pytest.raises(ContextNoLongerValid):
                old_resolver.resolve_school_reactivation_candidate(old_refs[0].opaque_ref)
            new_refs = new_resolver.list_school_reactivation_candidates()
            assert len(new_refs) == 1
            assert new_resolver.resolve_school_reactivation_candidate(
                new_refs[0].opaque_ref
            ).assignment_id == AssignmentId(assignment_id)
        finally:
            runtime_engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize("fact_kind", ("assignment", "login_mapping"))
def test_future_relationship_slice_does_not_reappear_after_earlier_stream_end(
    isolated_migration_schema: PostgresRoles,
    fact_kind: str,
) -> None:
    """An end bounds the logical stream even when a later content slice was predeclared."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, actor_user_id, person_id, actor_assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=actor_user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
        )
        with factory() as session:
            database_now = session.execute(text("SELECT statement_timestamp() ")).scalar_one()
            mapped_user_id = int(
                session.execute(
                    text("SELECT id FROM users WHERE external_subject='parent-001'")
                ).scalar_one()
            )
            target_id = session.execute(
                text(
                    "SELECT id FROM staff_assignments "
                    "WHERE school_id=:school_id AND source_key='assignment:parent'"
                    if fact_kind == "assignment"
                    else "SELECT id FROM account_person_bindings "
                    "WHERE school_id=:school_id AND source_key='login:parent'"
                ),
                {"school_id": school_id},
            ).scalar_one()
        later_slice_at = database_now - timedelta(seconds=1)
        ended_at = database_now - timedelta(seconds=2)
        upsert = (
            _assignment_upsert(
                "assignment:parent",
                1,
                later_slice_at,
                2,
                position_title="不得在终止后复现的未来切片",
            )
            if fact_kind == "assignment"
            else _login_upsert(
                "login:parent",
                1,
                later_slice_at,
                2,
                external_subject="parent-001",
            )
        )
        _confirm(
            service=service,
            change_set=_plan(
                label=f"{fact_kind}-predeclared-later-slice",
                base_version=2,
                changes=(upsert,),
            ),
            school_id=school_id,
            user_id=actor_user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
            label=f"{fact_kind}-predeclared-later-slice",
        )
        _confirm(
            service=service,
            change_set=_plan(
                label=f"{fact_kind}-end-before-predeclared-slice",
                base_version=3,
                changes=(
                    {
                        **_terminal_change(
                            fact_kind,
                            ("assignment:parent" if fact_kind == "assignment" else "login:parent"),
                            2,
                            ended_at,
                            2,
                        ),
                        "operation": "end",
                    },
                ),
            ),
            school_id=school_id,
            user_id=actor_user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
            label=f"{fact_kind}-end-before-predeclared-slice",
        )
        with factory() as session:
            snapshot = (
                SqlAlchemyOrganizationRepository(session).resolve_assignment_snapshot(
                    SchoolId(school_id),
                    AssignmentId(target_id),
                    as_of=database_now,
                )
                if fact_kind == "assignment"
                else SqlAlchemyPersonRepository(session).resolve_login_mapping_snapshot(
                    SchoolId(school_id),
                    target_id,
                    as_of=database_now,
                )
            )
            lineage = session.execute(
                text(
                    "SELECT target_version, operation FROM school_fact_change_lineage "
                    "WHERE school_id=:school_id AND fact_kind=:fact_kind "
                    "AND target_id=:target_id ORDER BY target_version"
                ),
                {
                    "school_id": school_id,
                    "fact_kind": fact_kind,
                    "target_id": target_id,
                },
            ).all()

        assert snapshot is None
        assert lineage == [(1, "upsert"), (2, "upsert"), (3, "end")]
        assert (
            _execution_context_options(
                isolated_migration_schema,
                account_user_id=mapped_user_id,
            )
            == []
        )


@pytest.mark.integration
def test_assignment_revoke_preserves_planned_end_on_every_content_slice(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
        )
        planned_valid_to = NOW + timedelta(days=30)
        changed_at = NOW + timedelta(days=1)
        _confirm(
            service=service,
            change_set=_plan(
                label="assignment-sliced-planned-expiry",
                base_version=2,
                changes=(
                    _assignment_upsert(
                        "assignment:parent",
                        1,
                        changed_at,
                        2,
                        valid_to=planned_valid_to,
                        position_title="有计划到期的任职",
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="assignment-sliced-planned-expiry",
        )
        revoked_at = NOW + timedelta(days=10)
        _confirm(
            service=service,
            change_set=_plan(
                label="assignment-sliced-revoke",
                base_version=3,
                changes=(
                    {
                        **_terminal_change("assignment", "assignment:parent", 2, revoked_at, 2),
                        "operation": "revoke",
                    },
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="assignment-sliced-revoke",
        )
        with factory() as session:
            rows = session.execute(
                text(
                    "SELECT version.target_version, version.planned_valid_to, "
                    "version.revoked_at, version.effective_from, version.effective_to "
                    "FROM staff_assignment_versions AS version "
                    "JOIN staff_assignments AS stable "
                    "ON stable.school_id=version.school_id "
                    "AND stable.id=version.assignment_id "
                    "WHERE version.school_id=:school_id "
                    "AND stable.source_key='assignment:parent' "
                    "ORDER BY version.target_version"
                ),
                {"school_id": school_id},
            ).all()

        assert [(row.target_version, row.planned_valid_to) for row in rows] == [
            (1, None),
            (2, planned_valid_to),
        ]
        assert all(row.revoked_at == revoked_at for row in rows)
        assert rows[0].effective_to == changed_at
        assert rows[1].effective_to is None


@pytest.mark.integration
def test_assignment_end_truncates_technical_slice_and_preserves_planned_end(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, actor_assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
        )
        content_changed_at = NOW + timedelta(days=1)
        ended_at = NOW + timedelta(days=10)
        planned_valid_to = NOW + timedelta(days=30)
        _confirm(
            service=service,
            change_set=_plan(
                label="assignment-planned-before-explicit-end",
                base_version=2,
                changes=(
                    _assignment_upsert(
                        "assignment:parent",
                        1,
                        content_changed_at,
                        2,
                        valid_to=planned_valid_to,
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
            label="assignment-planned-before-explicit-end",
        )
        _confirm(
            service=service,
            change_set=_plan(
                label="assignment-explicit-early-end",
                base_version=3,
                changes=(
                    {
                        **_terminal_change("assignment", "assignment:parent", 2, ended_at, 2),
                        "operation": "end",
                    },
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
            label="assignment-explicit-early-end",
        )
        with factory() as session:
            assignment_id = session.execute(
                text(
                    "SELECT id FROM staff_assignments "
                    "WHERE school_id=:school_id AND source_key='assignment:parent'"
                ),
                {"school_id": school_id},
            ).scalar_one()
            stable_planned = session.execute(
                text("SELECT valid_to FROM staff_assignments WHERE id=:id"),
                {"id": assignment_id},
            ).scalar_one()
            slices = session.execute(
                text(
                    "SELECT target_version, effective_to, planned_valid_to "
                    "FROM staff_assignment_versions WHERE assignment_id=:id "
                    "ORDER BY target_version"
                ),
                {"id": assignment_id},
            ).all()
            repository = SqlAlchemyOrganizationRepository(session)
            before = repository.resolve_assignment_snapshot(
                SchoolId(school_id),
                AssignmentId(assignment_id),
                as_of=ended_at - timedelta(microseconds=1),
            )
            after = repository.resolve_assignment_snapshot(
                SchoolId(school_id),
                AssignmentId(assignment_id),
                as_of=ended_at,
            )

        assert stable_planned == planned_valid_to
        assert slices == [
            (1, content_changed_at, None),
            (2, ended_at, planned_valid_to),
        ]
        assert before is not None
        assert after is None


@pytest.mark.integration
def test_assignment_end_cannot_extend_past_its_planned_expiry(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, actor_assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
        )
        planned_valid_to = NOW + timedelta(days=30)
        _confirm(
            service=service,
            change_set=_plan(
                label="assignment-bounded-before-late-end",
                base_version=2,
                changes=(
                    _assignment_upsert(
                        "assignment:parent",
                        1,
                        NOW + timedelta(days=1),
                        2,
                        valid_to=planned_valid_to,
                    ),
                ),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=actor_assignment_id,
            label="assignment-bounded-before-late-end",
        )
        before = _relationship_row(factory, school_id, "assignment")

        with pytest.raises(SchoolFactChangeRejected):
            _confirm(
                service=service,
                change_set=_plan(
                    label="assignment-end-after-planned-expiry",
                    base_version=3,
                    changes=(
                        {
                            **_terminal_change(
                                "assignment",
                                "assignment:parent",
                                2,
                                planned_valid_to + timedelta(days=1),
                                2,
                            ),
                            "operation": "end",
                        },
                    ),
                ),
                school_id=school_id,
                user_id=user_id,
                person_id=person_id,
                assignment_id=actor_assignment_id,
                label="assignment-end-after-planned-expiry",
            )
        assert _relationship_row(factory, school_id, "assignment") == before


def test_assignment_revoke_preserves_planned_valid_to(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """Frozen authority 493: revoked assignment keeps its original planned expiry."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
        )
        planned_valid_to = NOW + timedelta(days=30)
        schedule = _plan(
            label="assignment-planned-expiry",
            base_version=2,
            changes=(
                _assignment_upsert(
                    "assignment:parent",
                    1,
                    NOW + timedelta(days=1),
                    2,
                    valid_to=planned_valid_to,
                ),
            ),
        )
        _confirm(
            service=service,
            change_set=schedule,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="assignment-planned-expiry",
        )
        revoked_at = NOW + timedelta(days=10)
        revoke = _plan(
            label="assignment-revoke-before-expiry",
            base_version=3,
            changes=(_terminal_change("assignment", "assignment:parent", 2, revoked_at, 2),),
        )
        _confirm(
            service=service,
            change_set=revoke,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="assignment-revoke-before-expiry",
        )

        with factory() as session:
            actual = session.execute(
                text(
                    "SELECT valid_to, revoked_at FROM staff_assignments "
                    "WHERE school_id = :school_id AND source_key = 'assignment:parent'"
                ),
                {"school_id": school_id},
            ).one()
        assert actual == (planned_valid_to, revoked_at), (
            "任职撤销必须保留计划到期日，并以独立 revoked_at 表达提前撤销"
        )


def test_policy_version_revoke_preserves_effective_to_and_records_revoked_at(
    current_master_data_schema: PostgresRoles,
) -> None:
    """Frozen authority 493: policy expiry and revocation are independent facts."""

    with sf2_environment(current_master_data_schema) as (factory, persistence, facts):
        school_id, user_id, person_id, assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        planned_effective_to = NOW + timedelta(days=30)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            policy_effective_to=planned_effective_to,
        )
        with factory() as session:
            columns = set(
                session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' "
                        "AND table_name = 'school_policy_versions'"
                    )
                ).scalars()
            )
        assert "revoked_at" in columns, (
            "school_policy_versions 缺少独立 revoked_at，无法同时表达提前撤销与原失效日"
        )

        revoked_at = NOW + timedelta(days=10)
        revoke = _plan(
            label="policy-version-revoke-before-expiry",
            base_version=2,
            changes=(
                _terminal_change(
                    "policy_version",
                    "policy-version:dorm-v1",
                    1,
                    revoked_at,
                    2,
                ),
            ),
        )
        _confirm(
            service=service,
            change_set=revoke,
            school_id=school_id,
            user_id=user_id,
            person_id=person_id,
            assignment_id=assignment_id,
            label="policy-version-revoke-before-expiry",
        )
        with factory() as session:
            actual = session.execute(
                text(
                    "SELECT version.effective_to, version.revoked_at "
                    "FROM school_policy_versions AS version "
                    "JOIN school_fact_change_lineage AS lineage "
                    "ON lineage.target_id = version.id "
                    "WHERE lineage.school_id = :school_id "
                    "AND lineage.fact_source_key = 'policy-version:dorm-v1' "
                    "ORDER BY lineage.target_version DESC LIMIT 1"
                ),
                {"school_id": school_id},
            ).one()
        assert actual == (planned_effective_to, revoked_at)


# Retired legacy Repairs runtime-contract coverage.  The production route and
# compatibility repository are no longer supported SF5B capabilities; these
# helpers remain only as readable historical evidence and are not collected.
def _seed_repair_responsibility_for_assignment(
    *,
    factory: object,
    school_id: UUID,
    assignment_id: UUID,
    assignment_version: int,
) -> tuple[UUID, UUID]:
    """Create the four responsibility projections around one formal assignment."""

    responsibility_type_id = uuid4()
    group_id = uuid4()
    marker = uuid4().hex
    with factory.begin() as session:  # type: ignore[union-attr]
        session.execute(
            text(
                "INSERT INTO responsibility_types "
                "(id, school_id, code, display_name, status, version) "
                "VALUES (:id, :school_id, :code, '切片时态报修职责', 'active', 1)"
            ),
            {
                "id": responsibility_type_id,
                "school_id": school_id,
                "code": f"cp3-responsibility-{marker}",
            },
        )
        session.execute(
            text(
                "INSERT INTO responsibility_groups "
                "(id, school_id, code, manager_assignment_id, manager_assignment_version, "
                "status, version) VALUES "
                "(:id, :school_id, :code, :assignment_id, :assignment_version, 'active', 1)"
            ),
            {
                "id": group_id,
                "school_id": school_id,
                "code": f"cp3-responsibility-group-{marker}",
                "assignment_id": assignment_id,
                "assignment_version": assignment_version,
            },
        )
        session.execute(
            text(
                "INSERT INTO responsibility_group_versions "
                "(id, school_id, responsibility_group_id, display_name, valid_from, "
                "status, version) VALUES "
                "(:id, :school_id, :group_id, '切片时态报修组', :valid_from, 'confirmed', 1)"
            ),
            {
                "id": uuid4(),
                "school_id": school_id,
                "group_id": group_id,
                "valid_from": NOW,
            },
        )
        session.execute(
            text(
                "INSERT INTO responsibility_group_memberships "
                "(id, school_id, responsibility_group_id, assignment_id, valid_from, "
                "status, version) VALUES "
                "(:id, :school_id, :group_id, :assignment_id, :valid_from, 'confirmed', 1)"
            ),
            {
                "id": uuid4(),
                "school_id": school_id,
                "group_id": group_id,
                "assignment_id": assignment_id,
                "valid_from": NOW,
            },
        )
        session.execute(
            text(
                "INSERT INTO repair_responsibility_grants "
                "(id, school_id, responsibility_type_id, responsibility_group_id, "
                "route_mode, valid_from, status, version) VALUES "
                "(:id, :school_id, :type_id, :group_id, 'manager_dispatch', "
                ":valid_from, 'confirmed', 1)"
            ),
            {
                "id": uuid4(),
                "school_id": school_id,
                "type_id": responsibility_type_id,
                "group_id": group_id,
                "valid_from": NOW,
            },
        )
    return responsibility_type_id, group_id


def _assert_responsibility_assignment_visible(
    *,
    repository: SqlAlchemyResponsibilityRepository,
    school_id: UUID,
    person_id: UUID,
    assignment_id: UUID,
    assignment_version: int,
    responsibility_type_id: UUID,
    group_id: UUID,
    as_of: datetime,
) -> None:
    assignment = repository.get_current_repair_assignment(
        SchoolId(school_id),
        PersonId(person_id),
        AssignmentId(assignment_id),
        assignment_version,
        as_of=as_of,
    )
    relationships = repository.list_current_repair_relationships_for_assignment(
        SchoolId(school_id),
        PersonId(person_id),
        AssignmentId(assignment_id),
        assignment_version,
        as_of=as_of,
    )
    route_members = repository.list_current_repair_route_members(
        SchoolId(school_id),
        ResponsibilityGroupId(group_id),
        as_of=as_of,
    )
    grant = repository.resolve_repair_grant(
        SchoolId(school_id),
        ResponsibilityTypeId(responsibility_type_id),
        location_id=None,
        repair_subtype=None,
        severity=None,
        as_of=as_of,
    )

    assert assignment is not None
    assert assignment.person_id == PersonId(person_id)
    assert assignment.assignment_version == assignment_version
    assert len(relationships) == 1
    assert relationships[0].person_id == PersonId(person_id)
    assert relationships[0].assignment_version == assignment_version
    assert len(route_members) == 1
    assert route_members[0].person_id == PersonId(person_id)
    assert route_members[0].assignment_version == assignment_version
    assert grant is not None
    assert grant.manager_assignment_id == AssignmentId(assignment_id)
    assert grant.manager_assignment_version == assignment_version


@pytest.mark.integration
def _retired_legacy_repairs_current_queries_use_future_assignment_content_slice(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """Every repair responsibility projection selects assignment content at ``as_of``."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, actor_person_id, actor_assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=actor_person_id,
            assignment_id=actor_assignment_id,
        )
        with factory() as session:
            checked_now = session.execute(text("SELECT statement_timestamp()")).scalar_one()
            assignment_id, old_person_id, new_person_id = session.execute(
                text(
                    "SELECT assignment.id, old_person.id, new_person.id "
                    "FROM staff_assignments AS assignment "
                    "JOIN school_people AS old_person "
                    "ON old_person.school_id=assignment.school_id "
                    "AND old_person.source_key='person:parent' "
                    "JOIN school_people AS new_person "
                    "ON new_person.school_id=assignment.school_id "
                    "AND new_person.source_key='person:student' "
                    "WHERE assignment.school_id=:school_id "
                    "AND assignment.source_key='assignment:parent'"
                ),
                {"school_id": school_id},
            ).one()
        effective_at = checked_now + timedelta(days=1)
        future_change = _assignment_upsert(
            "assignment:parent",
            1,
            effective_at,
            2,
            position_title="未来报修负责人",
            semantic_role_code="future_repair_manager",
        )
        future_content = cast(dict[str, object], future_change["content"])
        future_content["person_source_key"] = "person:student"
        _confirm(
            service=service,
            change_set=_plan(
                label="responsibility-future-assignment-content",
                base_version=2,
                changes=(future_change,),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=actor_person_id,
            assignment_id=actor_assignment_id,
            label="responsibility-future-assignment-content",
        )
        with factory() as session:
            assignment_version = int(
                session.execute(
                    text("SELECT version FROM staff_assignments WHERE id=:id"),
                    {"id": assignment_id},
                ).scalar_one()
            )
        responsibility_type_id, group_id = _seed_repair_responsibility_for_assignment(
            factory=factory,
            school_id=school_id,
            assignment_id=assignment_id,
            assignment_version=assignment_version,
        )

        with factory() as session:
            repository = SqlAlchemyResponsibilityRepository(session)
            _assert_responsibility_assignment_visible(
                repository=repository,
                school_id=school_id,
                person_id=old_person_id,
                assignment_id=assignment_id,
                assignment_version=assignment_version,
                responsibility_type_id=responsibility_type_id,
                group_id=group_id,
                as_of=effective_at - timedelta(microseconds=1),
            )
            assert (
                repository.get_current_repair_assignment(
                    SchoolId(school_id),
                    PersonId(old_person_id),
                    AssignmentId(assignment_id),
                    assignment_version,
                    as_of=effective_at,
                )
                is None
            )
            assert (
                repository.list_current_repair_relationships_for_assignment(
                    SchoolId(school_id),
                    PersonId(old_person_id),
                    AssignmentId(assignment_id),
                    assignment_version,
                    as_of=effective_at,
                )
                == ()
            )
            _assert_responsibility_assignment_visible(
                repository=repository,
                school_id=school_id,
                person_id=new_person_id,
                assignment_id=assignment_id,
                assignment_version=assignment_version,
                responsibility_type_id=responsibility_type_id,
                group_id=group_id,
                as_of=effective_at,
            )


@pytest.mark.integration
def _retired_legacy_repairs_current_queries_honor_formal_assignment_end_boundary(
    isolated_migration_schema: PostgresRoles,
) -> None:
    """A formal end keeps responsibility routing before its boundary and removes it after."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, actor_person_id, actor_assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=actor_person_id,
            assignment_id=actor_assignment_id,
        )
        with factory() as session:
            checked_now = session.execute(text("SELECT statement_timestamp()")).scalar_one()
            assignment_id, person_id = session.execute(
                text(
                    "SELECT assignment.id, person.id FROM staff_assignments AS assignment "
                    "JOIN school_people AS person ON person.school_id=assignment.school_id "
                    "AND person.source_key='person:parent' "
                    "WHERE assignment.school_id=:school_id "
                    "AND assignment.source_key='assignment:parent'"
                ),
                {"school_id": school_id},
            ).one()
        ended_at = checked_now + timedelta(days=1)
        terminal_change = _terminal_change("assignment", "assignment:parent", 1, ended_at, 2)
        terminal_change["operation"] = "end"
        _confirm(
            service=service,
            change_set=_plan(
                label="responsibility-formal-assignment-end",
                base_version=2,
                changes=(terminal_change,),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=actor_person_id,
            assignment_id=actor_assignment_id,
            label="responsibility-formal-assignment-end",
        )
        with factory() as session:
            assignment_version = int(
                session.execute(
                    text("SELECT version FROM staff_assignments WHERE id=:id"),
                    {"id": assignment_id},
                ).scalar_one()
            )
        responsibility_type_id, group_id = _seed_repair_responsibility_for_assignment(
            factory=factory,
            school_id=school_id,
            assignment_id=assignment_id,
            assignment_version=assignment_version,
        )

        with factory() as session:
            repository = SqlAlchemyResponsibilityRepository(session)
            _assert_responsibility_assignment_visible(
                repository=repository,
                school_id=school_id,
                person_id=person_id,
                assignment_id=assignment_id,
                assignment_version=assignment_version,
                responsibility_type_id=responsibility_type_id,
                group_id=group_id,
                as_of=ended_at - timedelta(microseconds=1),
            )
            assert (
                repository.get_current_repair_assignment(
                    SchoolId(school_id),
                    PersonId(person_id),
                    AssignmentId(assignment_id),
                    assignment_version,
                    as_of=ended_at,
                )
                is None
            )
            assert (
                repository.list_current_repair_relationships_for_assignment(
                    SchoolId(school_id),
                    PersonId(person_id),
                    AssignmentId(assignment_id),
                    assignment_version,
                    as_of=ended_at,
                )
                == ()
            )
            assert (
                repository.list_current_repair_route_members(
                    SchoolId(school_id),
                    ResponsibilityGroupId(group_id),
                    as_of=ended_at,
                )
                == ()
            )
            assert (
                repository.resolve_repair_grant(
                    SchoolId(school_id),
                    ResponsibilityTypeId(responsibility_type_id),
                    location_id=None,
                    repair_subtype=None,
                    severity=None,
                    as_of=ended_at,
                )
                is None
            )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("fact_kind", "source_key"),
    (
        pytest.param("person", "person:parent", id="ended-person"),
        pytest.param("organization", "organization:dorm", id="ended-organization"),
    ),
)
def _retired_legacy_repairs_current_queries_require_effective_assignment_roots(
    isolated_migration_schema: PostgresRoles,
    fact_kind: str,
    source_key: str,
) -> None:
    """An assignment cannot route repairs after its person or organization root ends."""

    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, user_id, actor_person_id, actor_assignment_id = facts
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _seed_all_facts(
            service=service,
            factory=factory,
            school_id=school_id,
            user_id=user_id,
            person_id=actor_person_id,
            assignment_id=actor_assignment_id,
        )
        with factory() as session:
            checked_now = session.execute(text("SELECT statement_timestamp()")).scalar_one()
            assignment_id, person_id, assignment_version = session.execute(
                text(
                    "SELECT assignment.id, person.id, assignment.version "
                    "FROM staff_assignments AS assignment "
                    "JOIN school_people AS person ON person.school_id=assignment.school_id "
                    "AND person.source_key='person:parent' "
                    "WHERE assignment.school_id=:school_id "
                    "AND assignment.source_key='assignment:parent'"
                ),
                {"school_id": school_id},
            ).one()
        ended_at = checked_now + timedelta(days=1)
        terminal_change = _terminal_change(fact_kind, source_key, 1, ended_at, 2)
        terminal_change["operation"] = "end"
        _confirm(
            service=service,
            change_set=_plan(
                label=f"responsibility-formal-{fact_kind}-end",
                base_version=2,
                changes=(terminal_change,),
            ),
            school_id=school_id,
            user_id=user_id,
            person_id=actor_person_id,
            assignment_id=actor_assignment_id,
            label=f"responsibility-formal-{fact_kind}-end",
        )
        responsibility_type_id, group_id = _seed_repair_responsibility_for_assignment(
            factory=factory,
            school_id=school_id,
            assignment_id=assignment_id,
            assignment_version=assignment_version,
        )

        with factory() as session:
            repository = SqlAlchemyResponsibilityRepository(session)
            _assert_responsibility_assignment_visible(
                repository=repository,
                school_id=school_id,
                person_id=person_id,
                assignment_id=assignment_id,
                assignment_version=assignment_version,
                responsibility_type_id=responsibility_type_id,
                group_id=group_id,
                as_of=ended_at - timedelta(microseconds=1),
            )
            assert (
                repository.get_current_repair_assignment(
                    SchoolId(school_id),
                    PersonId(person_id),
                    AssignmentId(assignment_id),
                    assignment_version,
                    as_of=ended_at,
                )
                is None
            )
            assert (
                repository.list_current_repair_relationships_for_assignment(
                    SchoolId(school_id),
                    PersonId(person_id),
                    AssignmentId(assignment_id),
                    assignment_version,
                    as_of=ended_at,
                )
                == ()
            )
            assert (
                repository.list_current_repair_route_members(
                    SchoolId(school_id),
                    ResponsibilityGroupId(group_id),
                    as_of=ended_at,
                )
                == ()
            )
            assert (
                repository.resolve_repair_grant(
                    SchoolId(school_id),
                    ResponsibilityTypeId(responsibility_type_id),
                    location_id=None,
                    repair_subtype=None,
                    severity=None,
                    as_of=ended_at,
                )
                is None
            )
