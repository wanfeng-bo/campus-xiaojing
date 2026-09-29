from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from app.domains.campus.models import CampusLocation, CampusLocationVersion
from app.platform import contracts as platform_contracts
from app.platform.authorization.secure_reader_compiler import CompiledReadRelation
from app.platform.authorization.secure_reader_registry import (
    test_operation_registry as _test_registry,
)
from app.platform.authorization.secure_reader_service import SecureCursorCodec, SecureReader
from app.platform.authorization.thin_contracts import NamedProjection, ScopePlan
from app.platform.contracts import AssignmentId, PersonId, SchoolId
from app.platform.directory.models import OfficialContactPoint
from app.platform.organization.models import StaffAssignmentVersion
from app.platform.policies.models import SchoolPolicyVersion

AS_OF = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)
SCHOOL_ID = uuid4()


def _actor() -> Any:
    return platform_contracts._issue_staff_actor_context(  # pyright: ignore[reportPrivateUsage]
        account_user_id=7,
        school_id=SchoolId(SCHOOL_ID),
        person_id=PersonId(uuid4()),
        assignment_id=AssignmentId(uuid4()),
        assignment_version=3,
        authorization_version=5,
    )


def test_fixed_projection_fields_are_backed_by_current_orm_columns() -> None:
    assignment = set(StaffAssignmentVersion.__table__.c.keys())
    location = set(CampusLocation.__table__.c.keys()) | set(
        CampusLocationVersion.__table__.c.keys()
    )
    official = set(OfficialContactPoint.__table__.c.keys())
    policy = set(SchoolPolicyVersion.__table__.c.keys())

    assert {
        "assignment_id",
        "organization_unit_id",
        "position_title",
        "semantic_role_code",
        "assignment_kind",
        "effective_from",
        "effective_to",
        "planned_valid_to",
        "target_version",
    } <= assignment
    assert {
        "id",
        "kind",
        "business_code",
        "display_name",
        "display_path",
        "valid_from",
        "valid_to",
        "version",
    } <= location
    assert {
        "id",
        "owner_kind",
        "organization_unit_id",
        "location_id",
        "contact_kind",
        "contact_value",
        "service_hours",
        "version",
    } <= official
    assert {
        "id",
        "policy_id",
        "business_code",
        "title",
        "classification",
        "content_text",
        "effective_from",
        "effective_to",
        "version",
    } <= policy


class _Transaction:
    def count(self, relation: CompiledReadRelation) -> int:
        return 2

    def fetch(self, relation: CompiledReadRelation) -> Sequence[Mapping[str, object]]:
        return tuple(
            {
                "row_id": row_id,
                "kind": "campus",
                "business_code": name,
                "display_name": name,
                "display_path": f"/{name}",
                "valid_from": AS_OF,
                "valid_to": None,
                "version": 1,
            }
            for row_id, name in ((UUID(int=1), "A"), (UUID(int=2), "B"))
        )


class _TransactionContext(AbstractContextManager[_Transaction]):
    def __enter__(self) -> _Transaction:
        return _Transaction()

    def __exit__(self, *args: object) -> None:
        pass


class _Executor:
    def repeatable_read(self) -> _TransactionContext:
        return _TransactionContext()


class _Authorizer:
    def __init__(self) -> None:
        self.decision_times: list[datetime] = []
        self.revalidation_times: list[datetime] = []

    def decide(self, *, as_of: datetime, operation: Any, **_: object) -> object:
        self.decision_times.append(as_of)
        return SimpleNamespace(
            allowed=True,
            basis="basis",
            scope_plan=ScopePlan(
                scope_id=SCHOOL_ID,
                owner="test-only.r1.school",
                reference=f"school:{SCHOOL_ID}",
                version=1,
            ),
            projection=NamedProjection(name=operation.projection_name, version=1),
            basis_fingerprint="basis-v1",
        )

    def revalidate(self, authorization: object, *, as_of: datetime, **_: object) -> bool:
        del authorization
        self.revalidation_times.append(as_of)
        return True


def test_cursor_freezes_data_as_of_but_next_page_authorizes_at_fresh_server_time() -> None:
    from app.platform.authorization.secure_reader_compiler import ServerOwnedProjectionCompiler
    from app.platform.authorization.secure_reader_contracts import SecureReadRequest

    actor = _actor()
    clock_values: Iterator[datetime] = iter(
        (
            AS_OF,
            AS_OF + timedelta(seconds=1),
            AS_OF + timedelta(seconds=2),
            AS_OF + timedelta(minutes=5),
            AS_OF + timedelta(minutes=5, seconds=1),
            AS_OF + timedelta(minutes=5, seconds=2),
        )
    )
    authorizer = _Authorizer()
    reader = SecureReader(
        registry=_test_registry(),
        compiler=ServerOwnedProjectionCompiler(),
        authorizer=authorizer,
        executor=_Executor(),
        cursor_codec=SecureCursorCodec(b"test-secret"),
        clock=lambda: next(clock_values),
    )
    first = reader.read(
        SecureReadRequest(
            actor=actor,
            operation_code="test_only.r1.public_location",
            page_size=1,
        )
    )
    second = reader.read(
        SecureReadRequest(
            actor=actor,
            operation_code="test_only.r1.public_location",
            cursor=first.next_cursor,
            page_size=1,
        )
    )

    assert first.as_of == second.as_of == AS_OF
    assert authorizer.decision_times == [AS_OF, AS_OF + timedelta(minutes=5)]
    assert authorizer.revalidation_times == [
        AS_OF + timedelta(seconds=1),
        AS_OF + timedelta(seconds=2),
        AS_OF + timedelta(minutes=5, seconds=1),
        AS_OF + timedelta(minutes=5, seconds=2),
    ]


def test_secure_reader_test_operations_are_not_mounted_by_production_routes() -> None:
    api_root = Path(__file__).resolve().parents[1] / "app"
    mounted_sources = [api_root / "main.py", *api_root.rglob("router.py")]
    source = "\n".join(path.read_text(encoding="utf-8") for path in mounted_sources)

    assert "secure_reader_registry" not in source
    assert "test_only.r1." not in source
