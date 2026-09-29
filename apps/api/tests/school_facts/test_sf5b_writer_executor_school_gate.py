from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.composition.school_fact_writer_executor import (
    SqlAlchemySchoolFactWriterExecutorKernel,
)
from app.platform import contracts as platform_contracts
from app.platform.contracts import CommandExecutionContext, SchoolId
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1

EXPECTED_SCHOOL = UUID("00000000-0000-0000-0000-000000000101")
WRONG_SCHOOL = UUID("00000000-0000-0000-0000-000000000102")
NOW = datetime(2026, 8, 24, 8, tzinfo=UTC)


def _context(school_id: UUID) -> CommandExecutionContext:
    actor = platform_contracts._issue_staff_actor_context(  # pyright: ignore[reportPrivateUsage]
        account_user_id=19,
        school_id=SchoolId(school_id),
        person_id=platform_contracts.PersonId(UUID("00000000-0000-0000-0000-000000000103")),
        assignment_id=platform_contracts.AssignmentId(UUID("00000000-0000-0000-0000-000000000104")),
        assignment_version=2,
        authorization_version=3,
    )
    return platform_contracts._issue_command_execution_context(  # pyright: ignore[reportPrivateUsage]
        actor=actor,
        request_id=UUID("00000000-0000-0000-0000-000000000105"),
        idempotency_key="wrong-school-must-fail-closed",
    )


def _change_set() -> CanonicalFactChangeSetV1:
    return CanonicalFactChangeSetV1.model_validate(
        {
            "source_sha256": "a" * 64,
            "base_version": 1,
            "changes": (
                {
                    "operation": "upsert",
                    "fact_kind": "school",
                    "source_key": "school:wrong-tenant",
                    "expected_version": 1,
                    "effective_at": NOW,
                    "origin": {"sheet_code": "schools", "row_number": 2},
                    "content": {
                        "business_code": "WRONG-TENANT",
                        "display_name": "错误学校",
                        "timezone_name": "Asia/Shanghai",
                        "lifecycle_status": "active",
                    },
                },
            ),
        }
    )


def _kernel_without_database() -> SqlAlchemySchoolFactWriterExecutorKernel:
    kernel = object.__new__(SqlAlchemySchoolFactWriterExecutorKernel)
    kernel._expected_school_id = SchoolId(EXPECTED_SCHOOL)  # pyright: ignore[reportPrivateUsage]
    return kernel


def test_production_kernel_rejects_wrong_school_for_all_fixed_operations() -> None:
    kernel = _kernel_without_database()
    context = _context(WRONG_SCHOOL)
    change_set = _change_set()

    operations = (
        lambda: kernel.preflight(
            school_id=SchoolId(WRONG_SCHOOL),
            change_set=change_set,
            context=context,
            session_binding="server-session-A",
        ),
        lambda: kernel.prepare(
            change_set=change_set,
            context=context,
            session_binding="server-session-A",
            provenance=None,
        ),
        lambda: kernel.confirm(
            credential="C" * 43,
            change_set=change_set,
            context=context,
            session_binding="server-session-A",
        ),
        lambda: kernel.result_by_digest(
            school_id=SchoolId(WRONG_SCHOOL),
            source_sha256="a" * 64,
            plan_sha256="b" * 64,
            context=context,
            session_binding="server-session-A",
        ),
        lambda: kernel.result_by_locator(
            school_id=SchoolId(WRONG_SCHOOL),
            actor_user_id=19,
            session_binding="server-session-A",
            locator_sha256="c" * 64,
            context=context,
        ),
    )

    for operation in operations:
        with pytest.raises(SchoolFactChangeRejected, match="学校"):
            operation()
