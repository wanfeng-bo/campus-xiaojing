from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.composition.governance_dependencies import SqlAlchemyGovernanceDependencyAdapter
from app.platform.school_facts.application import SchoolFactChangeService
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1
from tests.master_data.conftest import PostgresRoles
from tests.school_facts.test_sf2_transaction import (
    NOW,
    all_fact_kinds_change_set,
    context_for,
    sf2_environment,
)

pytestmark = pytest.mark.integration
FUTURE = NOW + timedelta(days=7)
PLANNED_END = NOW + timedelta(days=30)
TERMINAL_AT = NOW + timedelta(days=15)


def _confirm(
    service: SchoolFactChangeService,
    change_set: CanonicalFactChangeSetV1,
    facts: tuple[UUID, int, UUID, UUID],
    *,
    label: str,
) -> None:
    school_id, user_id, person_id, assignment_id = facts
    context = context_for(
        school_id=school_id,
        user_id=user_id,
        person_id=person_id,
        assignment_id=assignment_id,
        key=f"governance-dependency:{label}:{uuid4().hex}",
    )
    session_binding = f"governance-dependency-{label}-{uuid4().hex}"
    receipt = service.prepare_confirmation(
        change_set=change_set, context=context, session_binding=session_binding
    )
    result = service.confirm(
        credential=receipt.credential,
        change_set=change_set,
        context=context,
        session_binding=session_binding,
    )
    assert result.changed_count == len(change_set.changes)


def _assignment_update() -> CanonicalFactChangeSetV1:
    return CanonicalFactChangeSetV1.model_validate(
        {
            "source_sha256": sha256(b"governance-dependency-assignment-update").hexdigest(),
            "base_version": 2,
            "changes": (
                {
                    "operation": "upsert",
                    "fact_kind": "assignment",
                    "source_key": "assignment:parent",
                    "expected_version": 1,
                    "effective_at": FUTURE,
                    "origin": {"sheet_code": "governance_dependency", "row_number": 1},
                    "content": {
                        "person_source_key": "person:parent",
                        "organization_source_key": "organization:dorm",
                        "position_title": "家校联系人（未来切片）",
                        "semantic_role_code": "family_contact",
                        "assignment_kind": "part_time",
                        "appointment_source": "manual",
                        "approval_reference": "approval:governance-dependency",
                        "valid_to": PLANNED_END,
                        "status": "confirmed",
                    },
                },
            ),
        }
    )


def _terminal_owner_facts() -> CanonicalFactChangeSetV1:
    changes = tuple(
        {
            "operation": "end",
            "fact_kind": kind,
            "source_key": key,
            "expected_version": version,
            "effective_at": TERMINAL_AT,
            "origin": {"sheet_code": "governance_dependency", "row_number": row},
            "content": None,
        }
        for row, (kind, key, version) in enumerate(
            (
                ("assignment", "assignment:parent", 2),
                ("person", "person:parent", 1),
                ("organization", "organization:dorm", 1),
            ),
            start=2,
        )
    )
    return CanonicalFactChangeSetV1.model_validate(
        {
            "source_sha256": sha256(b"governance-dependency-owner-terminal").hexdigest(),
            "base_version": 3,
            "changes": changes,
        }
    )


def test_real_school_facts_adapter_preserves_versions_boundaries_and_fails_closed(
    isolated_migration_schema: PostgresRoles,
) -> None:
    with sf2_environment(isolated_migration_schema) as (factory, persistence, facts):
        school_id, _user_id, _person_id, _assignment_id = facts
        with factory() as session:
            school_source_key = str(
                session.execute(
                    text("SELECT source_key FROM schools WHERE id = :school_id"),
                    {"school_id": school_id},
                ).scalar_one()
            )
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: NOW)
        _confirm(
            service,
            all_fact_kinds_change_set(school_source_key=school_source_key),
            facts,
            label="seed",
        )
        _confirm(service, _assignment_update(), facts, label="future-assignment")
        with factory() as session:
            target = (
                session.execute(
                    text(
                        "SELECT id, person_id, organization_unit_id FROM staff_assignments "
                        "WHERE school_id = :school_id AND source_key = 'assignment:parent'"
                    ),
                    {"school_id": school_id},
                )
                .mappings()
                .one()
            )
        assignment_id = UUID(str(target["id"]))
        person_id = UUID(str(target["person_id"]))
        organization_id = UUID(str(target["organization_unit_id"]))
        adapter = SqlAlchemyGovernanceDependencyAdapter(factory)

        before = adapter.read_assignment(
            school_id=school_id,
            assignment_id=assignment_id,
            as_of=FUTURE - timedelta(microseconds=1),
        )
        current = adapter.read_assignment(
            school_id=school_id, assignment_id=assignment_id, as_of=FUTURE
        )
        person = adapter.read_person_root(school_id=school_id, person_id=person_id, as_of=FUTURE)
        organization = adapter.read_organization_root(
            school_id=school_id, organization_id=organization_id, as_of=FUTURE
        )
        assert before is not None
        assert (before.person_id, before.organization_id) == (person_id, organization_id)
        assert (before.version, before.valid_until) == (1, FUTURE)
        assert current is not None
        assert (current.version, current.valid_until) == (2, PLANNED_END)
        assert person is not None and (person.version, person.valid_until) == (1, None)
        assert organization is not None
        assert (organization.version, organization.valid_until) == (1, None)

        _confirm(service, _terminal_owner_facts(), facts, label="terminal-owner-facts")
        assert (
            adapter.read_assignment(
                school_id=school_id, assignment_id=assignment_id, as_of=TERMINAL_AT
            )
            is None
        )
        assert (
            adapter.read_person_root(school_id=school_id, person_id=person_id, as_of=TERMINAL_AT)
            is None
        )
        assert (
            adapter.read_organization_root(
                school_id=school_id, organization_id=organization_id, as_of=TERMINAL_AT
            )
            is None
        )
