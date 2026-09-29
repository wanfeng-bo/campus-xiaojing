from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import NoReturn
from uuid import uuid4

from app.platform.contracts import (
    AssignmentId,
    CommandExecutionContext,
    PersonId,
    SchoolId,
    StaffActorContext,
    _issue_staff_actor_context,  # pyright: ignore[reportPrivateUsage]
)
from app.platform.identity.execution_context import compose_command_context
from app.platform.organization.application import (
    ConfirmStaffDutyBindingHandler,
    StaffDutyBindingLegacyWriterDisabled,
)
from app.platform.organization.contracts import ConfirmStaffDutyBindingCommand


class _ConcurrentFailIfOpenedFactory:
    def __init__(self) -> None:
        self._lock = Lock()
        self.open_count = 0

    def open_command(self, context: CommandExecutionContext) -> NoReturn:
        with self._lock:
            self.open_count += 1
        raise AssertionError(f"disabled handler opened command UoW: {context.request_id}")

    def open_probe(self, context: CommandExecutionContext) -> NoReturn:
        with self._lock:
            self.open_count += 1
        raise AssertionError(f"disabled handler opened probe UoW: {context.request_id}")


class _FailIfAuthorized:
    def is_allowed(self, facts: object) -> NoReturn:
        raise AssertionError(f"disabled handler consulted old authorizer: {facts!r}")


def _command() -> ConfirmStaffDutyBindingCommand:
    return ConfirmStaffDutyBindingCommand.model_validate(
        {
            "person_id": uuid4(),
            "organization_unit_id": uuid4(),
            "position_title": "维修员",
            "semantic_role_code": "repair_worker",
            "responsibility_group_id": uuid4(),
            "interval": {
                "valid_from": datetime.now(UTC).replace(microsecond=0) + timedelta(days=1),
                "valid_to": None,
            },
            "expected_person_version": 1,
            "expected_organization_unit_version": 1,
            "expected_group_version": 1,
            "confirmation_reason": "disabled-concurrency-proof",
        }
    )


def _context() -> CommandExecutionContext:
    actor: StaffActorContext = _issue_staff_actor_context(
        account_user_id=17,
        school_id=SchoolId(uuid4()),
        person_id=PersonId(uuid4()),
        assignment_id=AssignmentId(uuid4()),
        assignment_version=1,
        authorization_version=1,
    )
    return compose_command_context(actor, uuid4(), "disabled-concurrent-call")


def test_concurrent_calls_cannot_enter_the_disabled_legacy_write_path() -> None:
    factory = _ConcurrentFailIfOpenedFactory()
    handler = ConfirmStaffDutyBindingHandler(
        uow_factory=factory,
        authorizer=_FailIfAuthorized(),
    )
    command = _command()
    context = _context()

    def invoke() -> str:
        try:
            handler.handle(command, context=context)
        except StaffDutyBindingLegacyWriterDisabled as exc:
            return str(exc)
        raise AssertionError("disabled handler unexpectedly returned success")

    with ThreadPoolExecutor(max_workers=2) as pool:
        messages = list(pool.map(lambda _: invoke(), range(2)))

    assert all("已机械禁用" in message for message in messages)
    assert factory.open_count == 0
