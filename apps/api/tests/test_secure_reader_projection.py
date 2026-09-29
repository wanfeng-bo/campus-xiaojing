from __future__ import annotations

import importlib
import importlib.util
import inspect
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError, dataclass, fields, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.platform import contracts as platform_contracts
from app.platform.authorization.thin_contracts import NamedProjection, ScopePlan
from app.platform.contracts import AssignmentId, PersonId, SchoolId

AS_OF = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)
SCHOOL_ID = uuid4()
PERSON_ID = uuid4()
ASSIGNMENT_ID = uuid4()


def _module(name: str) -> Any:
    full_name = f"app.platform.authorization.{name}"
    assert importlib.util.find_spec(full_name) is not None, f"missing R1 module: {full_name}"
    return importlib.import_module(full_name)


def _actor() -> Any:
    return platform_contracts._issue_staff_actor_context(  # pyright: ignore[reportPrivateUsage]
        account_user_id=7,
        school_id=SchoolId(SCHOOL_ID),
        person_id=PersonId(PERSON_ID),
        assignment_id=AssignmentId(ASSIGNMENT_ID),
        assignment_version=3,
        authorization_version=5,
    )


@dataclass
class _Authorization:
    allowed: bool
    basis: object = "basis"
    scope_plan: ScopePlan = ScopePlan(
        scope_id=SCHOOL_ID,
        owner="test-only.r1.school",
        reference=f"school:{SCHOOL_ID}",
        version=1,
    )
    projection: NamedProjection = NamedProjection(name="public_location", version=1)
    basis_fingerprint: str = "basis-v1"


class _Authorizer:
    def __init__(self, revalidations: list[bool] | None = None, *, allow: bool = True) -> None:
        self.allow = allow
        self.revalidations = list(revalidations or [True, True])
        self.events: list[str] = []

    def decide(self, *, operation: Any, **_: object) -> _Authorization:
        self.events.append("decide")
        return _Authorization(
            self.allow,
            projection=NamedProjection(
                name=str(operation.projection_name),
                version=1,
            ),
        )

    def revalidate(self, authorization: _Authorization, **_: object) -> bool:
        del authorization
        self.events.append("revalidate")
        return self.revalidations.pop(0)


def _public_location_row(row_id: UUID, name: str) -> dict[str, object]:
    return {
        "row_id": row_id,
        "kind": "campus",
        "business_code": name,
        "display_name": name,
        "display_path": f"/{name}",
        "valid_from": AS_OF,
        "valid_to": None,
        "version": 1,
    }


def _fixed_projection_cases() -> tuple[tuple[str, dict[str, object], str], ...]:
    return (
        (
            "test_only.r1.self_current_assignment",
            {
                "row_id": UUID(int=21),
                "organization_unit_id": UUID(int=22),
                "position_title": "Teacher",
                "semantic_role_code": "teacher",
                "assignment_kind": "substantive",
                "effective_from": AS_OF,
                "effective_to": None,
                "planned_valid_to": None,
                "version": 1,
            },
            "SelfCurrentAssignmentRow",
        ),
        (
            "test_only.r1.public_location",
            _public_location_row(UUID(int=31), "A"),
            "PublicLocationRow",
        ),
        (
            "test_only.r1.official_office_contact",
            {
                "row_id": UUID(int=41),
                "owner_kind": "organization",
                "organization_unit_id": UUID(int=42),
                "location_id": None,
                "contact_kind": "phone",
                "contact_value": "+86-10-5555-0101",
                "service_hours": None,
                "version": 1,
            },
            "OfficialOfficeContactRow",
        ),
    )


class _Transaction:
    def __init__(self, owner: _Executor) -> None:
        self.owner = owner

    def count(self, relation: object) -> int:
        self.owner.events.append("count")
        self.owner.relations.append(relation)
        return 2

    def fetch(self, relation: object) -> list[dict[str, object]]:
        self.owner.events.append("data")
        self.owner.relations.append(relation)
        return list(self.owner.rows)


class _TransactionContext(AbstractContextManager[_Transaction]):
    def __init__(self, owner: _Executor) -> None:
        self.owner = owner

    def __enter__(self) -> _Transaction:
        self.owner.events.append("begin_repeatable_read")
        return _Transaction(self.owner)

    def __exit__(self, *args: object) -> None:
        self.owner.events.append("end_repeatable_read")


class _Executor:
    def __init__(self, *, rows: list[dict[str, object]] | None = None) -> None:
        self.events: list[str] = []
        self.relations: list[object] = []
        self.rows = (
            rows
            if rows is not None
            else [
                _public_location_row(UUID(int=1), "A"),
                _public_location_row(UUID(int=2), "B"),
            ]
        )

    def repeatable_read(self) -> _TransactionContext:
        return _TransactionContext(self)


def _request(operation_code: str, *, cursor: str | None = None, page_size: int = 1) -> Any:
    contracts = _module("secure_reader_contracts")
    return contracts.SecureReadRequest(
        actor=_actor(),
        operation_code=operation_code,
        cursor=cursor,
        page_size=page_size,
    )


def test_client_contract_cannot_supply_scope_projection_fields_or_sql() -> None:
    contracts = _module("secure_reader_contracts")
    parameters = set(inspect.signature(contracts.SecureReadRequest).parameters)

    assert parameters == {"actor", "operation_code", "cursor", "page_size", "selection", "factual_at"}
    assert parameters.isdisjoint(
        {"scope_plan", "scope_id", "projection", "fields", "columns", "where", "sql", "as_of"}
    )


def test_production_registry_is_deny_by_default_and_unknown_denies_before_sql() -> None:
    registry_api = _module("secure_reader_registry")
    service_api = _module("secure_reader_service")
    authorizer = _Authorizer()
    executor = _Executor()
    reader = service_api.SecureReader(
        registry=registry_api.production_operation_registry(),
        compiler=SimpleNamespace(compile=lambda **_: pytest.fail("compiler must not run")),
        authorizer=authorizer,
        executor=executor,
        cursor_codec=service_api.SecureCursorCodec(b"test-secret"),
        clock=lambda: AS_OF,
    )

    with pytest.raises(service_api.SecureReadDenied):
        reader.read(_request("test_only.r1.public_location"))

    assert authorizer.events == []
    assert executor.events == []


def test_test_registry_has_three_readable_operations_and_one_fixed_pre_read_deny() -> None:
    registry_api = _module("secure_reader_registry")
    compiler_api = _module("secure_reader_compiler")
    registry = registry_api.test_operation_registry()
    compiler = compiler_api.ServerOwnedProjectionCompiler()

    assert set(registry.operation_codes) == {
        "test_only.r1.self_current_assignment",
        "test_only.r1.public_location",
        "test_only.r1.official_office_contact",
        "test_only.r1.restricted_policy_body",
    }
    for operation_code in (
        "test_only.r1.self_current_assignment",
        "test_only.r1.public_location",
        "test_only.r1.official_office_contact",
    ):
        relation = compiler.compile(
            operation=registry.require(operation_code),
            actor=_actor(),
            scope_plan=ScopePlan(
                scope_id=SCHOOL_ID,
                owner="test-only.r1.school",
                reference=f"school:{SCHOOL_ID}",
                version=1,
            ),
            as_of=AS_OF,
            after_id=None,
            page_size=50,
        )
        sql = str(relation.data_statement).lower()
        assert "select *" not in sql
        assert " limit " in sql
        assert relation.as_of == AS_OF
        assert relation.page_size == 50
        assert 0 < len(relation.projected_fields) <= 9

    with pytest.raises(ValueError, match="projection is not registered"):
        compiler.compile(
            operation=registry.require("test_only.r1.restricted_policy_body"),
            actor=_actor(),
            scope_plan=ScopePlan(
                scope_id=SCHOOL_ID,
                owner="test-only.r1.school",
                reference=f"school:{SCHOOL_ID}",
                version=1,
            ),
            as_of=AS_OF,
            after_id=None,
            page_size=50,
        )
    compiler_source = inspect.getsource(type(compiler))
    assert "school_policy_versions" not in compiler_source
    assert "content_text" not in compiler_source


@pytest.mark.parametrize(
    ("operation_code", "raw_row", "dto_name"),
    _fixed_projection_cases(),
    ids=("self-assignment", "public-location", "official-contact"),
)
def test_fixed_projections_return_frozen_operation_specific_dtos(
    operation_code: str,
    raw_row: dict[str, object],
    dto_name: str,
) -> None:
    contracts = _module("secure_reader_contracts")
    service_api = _module("secure_reader_service")
    reader = service_api.SecureReader(
        registry=_module("secure_reader_registry").test_operation_registry(),
        compiler=_module("secure_reader_compiler").ServerOwnedProjectionCompiler(),
        authorizer=_Authorizer(),
        executor=_Executor(rows=[raw_row]),
        cursor_codec=service_api.SecureCursorCodec(b"test-secret"),
        clock=lambda: AS_OF,
    )

    page = reader.read(_request(operation_code, page_size=100))

    dto_type = getattr(contracts, dto_name)
    assert type(page.rows[0]) is dto_type
    assert tuple(field.name for field in fields(dto_type)) == tuple(raw_row)
    assert page.rows[0].row_id == raw_row["row_id"]
    with pytest.raises(FrozenInstanceError):
        setattr(page.rows[0], "row_id", uuid4())  # noqa: B010


@pytest.mark.parametrize(
    ("case_index", "field_name", "invalid_value"),
    (
        (0, "version", True),
        (1, "valid_from", "2026-08-24T08:00:00Z"),
        (2, "organization_unit_id", str(UUID(int=42))),
    ),
    ids=("self-int", "location-datetime", "contact-uuid"),
)
def test_projection_dto_type_mismatch_fails_closed(
    case_index: int,
    field_name: str,
    invalid_value: object,
) -> None:
    operation_code, original_row, _dto_name = _fixed_projection_cases()[case_index]
    invalid_row = dict(original_row)
    invalid_row[field_name] = invalid_value
    service_api = _module("secure_reader_service")
    reader = service_api.SecureReader(
        registry=_module("secure_reader_registry").test_operation_registry(),
        compiler=_module("secure_reader_compiler").ServerOwnedProjectionCompiler(),
        authorizer=_Authorizer(),
        executor=_Executor(rows=[invalid_row]),
        cursor_codec=service_api.SecureCursorCodec(b"test-secret"),
        clock=lambda: AS_OF,
    )

    with pytest.raises(service_api.SecureReadDenied, match="projection row"):
        reader.read(_request(operation_code, page_size=100))


def test_unknown_projection_dto_mapping_fails_closed() -> None:
    registry = _module("secure_reader_registry").test_operation_registry()
    compiler = _module("secure_reader_compiler").ServerOwnedProjectionCompiler()
    relation = compiler.compile(
        operation=registry.require("test_only.r1.public_location"),
        actor=_actor(),
        scope_plan=ScopePlan(
            scope_id=SCHOOL_ID,
            owner="test-only.r1.school",
            reference=f"school:{SCHOOL_ID}",
            version=1,
        ),
        as_of=AS_OF,
        after_id=None,
        page_size=50,
    )
    unknown_relation = replace(relation, operation_code="test_only.r1.unknown")
    service_api = _module("secure_reader_service")

    with pytest.raises(service_api.SecureReadDenied, match="projection row"):
        service_api._require_exact_projection_rows(
            unknown_relation,
            [_public_location_row(UUID(int=31), "A")],
        )


def test_official_contact_projection_never_touches_person_student_or_guardian_data() -> None:
    registry_api = _module("secure_reader_registry")
    compiler_api = _module("secure_reader_compiler")
    relation = compiler_api.ServerOwnedProjectionCompiler().compile(
        operation=registry_api.test_operation_registry().require(
            "test_only.r1.official_office_contact"
        ),
        actor=_actor(),
        scope_plan=ScopePlan(
            scope_id=SCHOOL_ID,
            owner="test-only.r1.school",
            reference=f"school:{SCHOOL_ID}",
            version=1,
        ),
        as_of=AS_OF,
        after_id=None,
        page_size=25,
    )
    sql = str(relation.data_statement).lower()

    assert "official_contact_points" in sql
    assert all(
        forbidden not in sql
        for forbidden in (
            "person_contact_points",
            "school_people",
            "students",
            "student_contact_relationships",
            "guardian",
        )
    )


def test_restricted_policy_is_fixed_denied_before_sql_even_when_authorizer_allows() -> None:
    registry_api = _module("secure_reader_registry")
    compiler_api = _module("secure_reader_compiler")
    service_api = _module("secure_reader_service")
    authorizer = _Authorizer(allow=True)
    executor = _Executor()
    reader = service_api.SecureReader(
        registry=registry_api.test_operation_registry(),
        compiler=compiler_api.ServerOwnedProjectionCompiler(),
        authorizer=authorizer,
        executor=executor,
        cursor_codec=service_api.SecureCursorCodec(b"test-secret"),
        clock=lambda: AS_OF,
    )

    with pytest.raises(service_api.SecureReadDenied, match="cannot be compiled"):
        reader.read(_request("test_only.r1.restricted_policy_body"))

    assert authorizer.events == ["decide"]
    assert executor.events == []
    assert "test_only.r1.restricted_policy_body" not in service_api._PROJECTION_ROW_ADAPTERS


def test_count_data_share_relation_and_post_read_drift_discards_rows() -> None:
    registry_api = _module("secure_reader_registry")
    compiler_api = _module("secure_reader_compiler")
    service_api = _module("secure_reader_service")
    authorizer = _Authorizer([True, False])
    executor = _Executor()
    reader = service_api.SecureReader(
        registry=registry_api.test_operation_registry(),
        compiler=compiler_api.ServerOwnedProjectionCompiler(),
        authorizer=authorizer,
        executor=executor,
        cursor_codec=service_api.SecureCursorCodec(b"test-secret"),
        clock=lambda: AS_OF,
    )

    with pytest.raises(service_api.SecureReadAuthorizationDrift):
        reader.read(_request("test_only.r1.public_location"))

    assert authorizer.events == ["decide", "revalidate", "revalidate"]
    assert executor.events == [
        "begin_repeatable_read",
        "count",
        "data",
        "end_repeatable_read",
    ]
    assert executor.relations[0] is executor.relations[1]


@pytest.mark.parametrize(
    "row",
    (
        {**_public_location_row(UUID(int=1), "A"), "personal_phone": "forbidden"},
        {
            key: value
            for key, value in _public_location_row(UUID(int=1), "A").items()
            if key != "version"
        },
        {
            ("wrong_display_field" if key == "display_name" else key): value
            for key, value in _public_location_row(UUID(int=1), "A").items()
        },
    ),
    ids=("extra-field", "missing-field", "wrong-field"),
)
def test_projection_rows_fail_closed_unless_keys_exactly_match_compiler_fields(
    row: dict[str, object],
) -> None:
    service_api = _module("secure_reader_service")
    executor = _Executor(rows=[row])
    reader = service_api.SecureReader(
        registry=_module("secure_reader_registry").test_operation_registry(),
        compiler=_module("secure_reader_compiler").ServerOwnedProjectionCompiler(),
        authorizer=_Authorizer(),
        executor=executor,
        cursor_codec=service_api.SecureCursorCodec(b"test-secret"),
        clock=lambda: AS_OF,
    )

    with pytest.raises(service_api.SecureReadDenied, match="projection row shape"):
        reader.read(_request("test_only.r1.public_location"))

    assert executor.events == [
        "begin_repeatable_read",
        "count",
        "data",
        "end_repeatable_read",
    ]


def test_cursor_is_signed_bound_and_each_page_gets_fresh_authorization() -> None:
    registry_api = _module("secure_reader_registry")
    compiler_api = _module("secure_reader_compiler")
    service_api = _module("secure_reader_service")
    authorizer = _Authorizer([True, True, True, True])
    executor = _Executor()
    reader = service_api.SecureReader(
        registry=registry_api.test_operation_registry(),
        compiler=compiler_api.ServerOwnedProjectionCompiler(),
        authorizer=authorizer,
        executor=executor,
        cursor_codec=service_api.SecureCursorCodec(b"test-secret"),
        clock=lambda: AS_OF,
    )

    first = reader.read(_request("test_only.r1.public_location"))
    assert first.next_cursor is not None
    second = reader.read(_request("test_only.r1.public_location", cursor=first.next_cursor))
    assert second.as_of == first.as_of == AS_OF
    assert authorizer.events.count("decide") == 2

    before = (list(authorizer.events), list(executor.events))
    tampered = first.next_cursor[:-1] + ("A" if first.next_cursor[-1] != "A" else "B")
    with pytest.raises(service_api.SecureReadDenied):
        reader.read(_request("test_only.r1.public_location", cursor=tampered))
    assert authorizer.events == before[0]
    assert executor.events == before[1]


def test_cursor_rejects_oversized_input_before_base64_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_api = _module("secure_reader_service")
    decoded_values: list[str] = []
    monkeypatch.setattr(
        service_api,
        "_unbase64url",
        lambda value: decoded_values.append(value) or b"{}",
    )
    oversized = f"{'A' * 4032}.{'A' * 64}"
    assert len(oversized) == 4097

    with pytest.raises(service_api.SecureReadDenied, match="invalid secure read cursor"):
        service_api.SecureCursorCodec(b"test-secret").decode(oversized, now=AS_OF)

    assert decoded_values == []


def test_cursor_denies_expiry_and_fresh_basis_or_actor_version_drift_before_sql() -> None:
    registry_api = _module("secure_reader_registry")
    compiler_api = _module("secure_reader_compiler")
    service_api = _module("secure_reader_service")
    now = AS_OF
    authorizer = _Authorizer([True, True, True, True])
    executor = _Executor()
    reader = service_api.SecureReader(
        registry=registry_api.test_operation_registry(),
        compiler=compiler_api.ServerOwnedProjectionCompiler(),
        authorizer=authorizer,
        executor=executor,
        cursor_codec=service_api.SecureCursorCodec(b"test-secret", key_version=7, ttl_seconds=60),
        clock=lambda: now,
    )
    first = reader.read(_request("test_only.r1.public_location"))
    assert first.next_cursor is not None

    authorizer.events.clear()
    executor.events.clear()
    authorizer.decide = lambda **_: _Authorization(allowed=True, basis_fingerprint="basis-v2")  # type: ignore[method-assign]
    with pytest.raises(service_api.SecureReadDenied, match="cursor"):
        reader.read(_request("test_only.r1.public_location", cursor=first.next_cursor))
    assert executor.events == []

    authorizer.decide = lambda **_: _Authorization(  # type: ignore[method-assign]
        allowed=True,
        scope_plan=ScopePlan(
            scope_id=SCHOOL_ID,
            owner="test-only.r1.school",
            reference=f"school:{SCHOOL_ID}",
            version=2,
        ),
    )
    with pytest.raises(service_api.SecureReadDenied, match="cursor"):
        reader.read(_request("test_only.r1.public_location", cursor=first.next_cursor))
    assert executor.events == []

    key_rotated_reader = service_api.SecureReader(
        registry=registry_api.test_operation_registry(),
        compiler=compiler_api.ServerOwnedProjectionCompiler(),
        authorizer=authorizer,
        executor=executor,
        cursor_codec=service_api.SecureCursorCodec(b"test-secret", key_version=8, ttl_seconds=60),
        clock=lambda: now,
    )
    with pytest.raises(service_api.SecureReadDenied, match="cursor"):
        key_rotated_reader.read(_request("test_only.r1.public_location", cursor=first.next_cursor))
    assert executor.events == []

    changed_actor = platform_contracts._issue_staff_actor_context(  # pyright: ignore[reportPrivateUsage]
        account_user_id=7,
        school_id=SchoolId(SCHOOL_ID),
        person_id=PersonId(PERSON_ID),
        assignment_id=AssignmentId(ASSIGNMENT_ID),
        assignment_version=4,
        authorization_version=5,
    )
    with pytest.raises(service_api.SecureReadDenied, match="cursor"):
        reader.read(
            _module("secure_reader_contracts").SecureReadRequest(
                actor=changed_actor,
                operation_code="test_only.r1.public_location",
                cursor=first.next_cursor,
                page_size=1,
            )
        )

    now = AS_OF.replace(minute=2)
    with pytest.raises(service_api.SecureReadDenied, match="cursor"):
        reader.read(_request("test_only.r1.public_location", cursor=first.next_cursor))
