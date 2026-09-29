# pyright: strict

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, cast
from uuid import UUID

from app.platform.contracts import StaffActorContext

from .f2_reader_contracts import F2_ROW_SPECS, F2FactReadSelection, F2FactRow
from .secure_reader_compiler import CompiledReadRelation, ServerOwnedProjectionCompiler
from .secure_reader_contracts import (
    OfficialContactCampusRow,
    OfficialContactMaintenanceTargetRow,
    OfficialOfficeContactRow,
    PublicLocationRow,
    PublishedOfficialContactRow,
    SchoolFactReadSelection,
    SecureReadAuthorizationDrift,
    SecureReadDenied,
    SecureReadPage,
    SecureReadRequest,
    SecureReadRow,
    SelfCurrentAssignmentRow,
)
from .secure_reader_registry import ReadOperation, ReadOperationRegistry
from .thin_contracts import NamedProjection, ScopePlan

_MAX_PAGE_SIZE = 100
_MAX_CURSOR_LENGTH = 4_096


class ReadTransaction(Protocol):
    def count(self, relation: CompiledReadRelation) -> int: ...

    def fetch(self, relation: CompiledReadRelation) -> Sequence[Mapping[str, object]]: ...


class RepeatableReadExecutor(Protocol):
    def repeatable_read(self) -> AbstractContextManager[ReadTransaction]: ...


class SecureReadAuthorizer(Protocol):
    def decide(
        self,
        *,
        operation: ReadOperation,
        actor: StaffActorContext,
        as_of: datetime,
    ) -> object: ...

    def revalidate(
        self,
        authorization: object,
        *,
        actor: StaffActorContext,
        as_of: datetime,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class _CursorClaims:
    account_user_id: int
    person_id: UUID
    assignment_id: UUID
    assignment_version: int
    authorization_version: int
    scope_id: UUID
    scope_owner: str
    scope_reference: str
    scope_version: int
    projection_name: str
    projection_version: int
    basis_fingerprint: str
    operation_code: str
    school_id: UUID
    as_of: datetime
    after_id: UUID
    page_size: int

    key_version: int
    issued_at: datetime
    expires_at: datetime
    selection_key: str = ""


class SecureCursorCodec:
    """Sign cursor scope so a client cannot widen or rewrite a later page."""

    def __init__(
        self,
        secret: bytes,
        *,
        key_version: int = 1,
        ttl_seconds: int = 600,
    ) -> None:
        if not secret:
            raise ValueError("cursor secret must not be empty")
        if key_version < 1:
            raise ValueError("cursor key version must be positive")
        if not 1 <= ttl_seconds <= 3_600:
            raise ValueError("cursor TTL is outside the fixed reader bound")
        self._secret = bytes(secret)
        self._key_version = key_version
        self._ttl_seconds = ttl_seconds

    @property
    def key_version(self) -> int:
        return self._key_version

    def expires_at(self, issued_at: datetime) -> datetime:
        return issued_at + timedelta(seconds=self._ttl_seconds)

    def encode(self, claims: _CursorClaims) -> str:
        payload = json.dumps(
            {
                "operation_code": claims.operation_code,
                "selection_key": claims.selection_key,
                "account_user_id": claims.account_user_id,
                "school_id": str(claims.school_id),
                "person_id": str(claims.person_id),
                "assignment_id": str(claims.assignment_id),
                "assignment_version": claims.assignment_version,
                "authorization_version": claims.authorization_version,
                "scope_id": str(claims.scope_id),
                "scope_owner": claims.scope_owner,
                "scope_reference": claims.scope_reference,
                "scope_version": claims.scope_version,
                "projection_name": claims.projection_name,
                "projection_version": claims.projection_version,
                "basis_fingerprint": claims.basis_fingerprint,
                "as_of": claims.as_of.isoformat(),
                "after_id": str(claims.after_id),
                "page_size": claims.page_size,
                "key_version": claims.key_version,
                "issued_at": claims.issued_at.isoformat(),
                "expires_at": claims.expires_at.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return f"{_base64url(payload)}.{_base64url(signature)}"

    def decode(self, cursor: str, *, now: datetime) -> _CursorClaims:
        if type(cursor) is not str or not 1 <= len(cursor) <= _MAX_CURSOR_LENGTH:
            raise SecureReadDenied("invalid secure read cursor")
        try:
            payload_text, signature_text = cursor.split(".", maxsplit=1)
            payload = _unbase64url(payload_text)
            expected = _base64url(hmac.new(self._secret, payload, hashlib.sha256).digest())
            if not hmac.compare_digest(signature_text, expected):
                raise ValueError("invalid cursor signature")
            raw = cast(dict[str, object], json.loads(payload.decode("utf-8")))
            claims = _CursorClaims(
                operation_code=str(raw["operation_code"]),
                selection_key=str(raw.get("selection_key", "")),
                account_user_id=int(cast(int | str, raw["account_user_id"])),
                school_id=UUID(str(raw["school_id"])),
                person_id=UUID(str(raw["person_id"])),
                assignment_id=UUID(str(raw["assignment_id"])),
                assignment_version=int(cast(int | str, raw["assignment_version"])),
                authorization_version=int(cast(int | str, raw["authorization_version"])),
                scope_id=UUID(str(raw["scope_id"])),
                scope_owner=str(raw["scope_owner"]),
                scope_reference=str(raw["scope_reference"]),
                scope_version=int(cast(int | str, raw["scope_version"])),
                projection_name=str(raw["projection_name"]),
                projection_version=int(cast(int | str, raw["projection_version"])),
                basis_fingerprint=str(raw["basis_fingerprint"]),
                as_of=datetime.fromisoformat(str(raw["as_of"])),
                after_id=UUID(str(raw["after_id"])),
                page_size=int(cast(int | str, raw["page_size"])),
                key_version=int(cast(int | str, raw["key_version"])),
                issued_at=datetime.fromisoformat(str(raw["issued_at"])),
                expires_at=datetime.fromisoformat(str(raw["expires_at"])),
            )
            if (
                now.utcoffset() is None
                or claims.issued_at.utcoffset() is None
                or claims.expires_at.utcoffset() is None
                or claims.key_version != self._key_version
                or claims.issued_at > now
                or now >= claims.expires_at
                or claims.expires_at - claims.issued_at != timedelta(seconds=self._ttl_seconds)
            ):
                raise ValueError("cursor freshness or key version is invalid")
            return claims
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise SecureReadDenied("invalid secure read cursor") from exc


class SecureReader:
    def __init__(
        self,
        *,
        registry: ReadOperationRegistry,
        compiler: ServerOwnedProjectionCompiler,
        authorizer: SecureReadAuthorizer,
        executor: RepeatableReadExecutor,
        cursor_codec: SecureCursorCodec,
        clock: Callable[[], datetime],
    ) -> None:
        self._registry = registry
        self._compiler = compiler
        self._authorizer = authorizer
        self._executor = executor
        self._cursor_codec = cursor_codec
        self._clock = clock

    def read(self, request: SecureReadRequest) -> SecureReadPage:
        try:
            operation = self._registry.require(request.operation_code)
        except KeyError as exc:
            raise SecureReadDenied("read operation is not registered") from exc
        if not 1 <= request.page_size <= _MAX_PAGE_SIZE:
            raise SecureReadDenied("page size is outside the fixed reader bound")
        if type(request.actor) is not StaffActorContext:
            raise SecureReadDenied("actor context is not trusted")

        if request.selection is not None and type(request.selection) not in {
            SchoolFactReadSelection,
            F2FactReadSelection,
        }:
            raise SecureReadDenied("invalid fixed read selection")
        authorization_as_of = self._clock()
        cursor = self._decode_cursor(request, now=authorization_as_of)
        if request.factual_at is not None and request.factual_at.utcoffset() is None:
            raise SecureReadDenied("factual time must carry a timezone")
        if (
            cursor is not None
            and request.factual_at is not None
            and request.factual_at != cursor.as_of
        ):
            raise SecureReadDenied("cursor factual time cannot change")
        query_as_of = (
            cursor.as_of if cursor is not None else (request.factual_at or authorization_as_of)
        )
        after_id = cursor.after_id if cursor is not None else None

        authorization = self._authorizer.decide(
            operation=operation,
            actor=request.actor,
            as_of=authorization_as_of,
        )
        scope_plan, projection, basis_fingerprint = _require_authorization_context(
            authorization,
            operation=operation,
        )
        if cursor is not None and not _cursor_matches_authorization(
            cursor,
            request=request,
            scope_plan=scope_plan,
            projection=projection,
            basis_fingerprint=basis_fingerprint,
        ):
            raise SecureReadDenied("cursor authorization context is stale")
        try:
            relation = self._compiler.compile(
                operation=operation,
                actor=request.actor,
                scope_plan=scope_plan,
                as_of=query_as_of,
                after_id=after_id,
                page_size=request.page_size,
                selection=request.selection,
            )
        except (TypeError, ValueError) as exc:
            raise SecureReadDenied("thin authorization scope cannot be compiled") from exc

        def before_read() -> None:
            if not self._authorizer.revalidate(
                authorization,
                actor=request.actor,
                as_of=self._clock(),
            ):
                raise SecureReadDenied("thin authorization changed before the read")

        def after_read() -> None:
            if not self._authorizer.revalidate(
                authorization,
                actor=request.actor,
                as_of=self._clock(),
            ):
                raise SecureReadAuthorizationDrift(
                    "thin authorization changed after the read; result discarded"
                )

        @contextmanager
        def legacy_read():
            with self._executor.repeatable_read() as transaction:
                before_read()
                yield transaction
                after_read()

        validated_read = getattr(self._executor, "validated_read", None)
        scope = (
            cast(AbstractContextManager[ReadTransaction], validated_read(before_read, after_read))
            if callable(validated_read)
            else legacy_read()
        )
        with scope as transaction:
            total_count = transaction.count(relation)
            raw_fetched = tuple(transaction.fetch(relation))
            fetched = _require_exact_projection_rows(relation, raw_fetched)

        visible_rows = fetched[: request.page_size]
        next_cursor = self._next_cursor(
            request=request,
            operation=operation,
            as_of=query_as_of,
            scope_plan=scope_plan,
            projection=projection,
            basis_fingerprint=basis_fingerprint,
            issued_at=authorization_as_of,
            previous_cursor=cursor,
            fetched=fetched,
            visible_rows=visible_rows,
        )
        return SecureReadPage(
            rows=visible_rows,
            total_count=total_count,
            as_of=query_as_of,
            next_cursor=next_cursor,
        )

    def _decode_cursor(self, request: SecureReadRequest, *, now: datetime) -> _CursorClaims | None:
        if request.cursor is None:
            return None
        claims = self._cursor_codec.decode(request.cursor, now=now)
        if (
            claims.operation_code != request.operation_code
            or claims.school_id != UUID(str(request.actor.school_id))
            or claims.person_id != UUID(str(request.actor.person_id))
            or claims.assignment_id != UUID(str(request.actor.assignment_id))
            or claims.account_user_id != request.actor.account_user_id
            or claims.assignment_version != request.actor.assignment_version
            or claims.authorization_version != request.actor.authorization_version
            or claims.page_size != request.page_size
            or claims.selection_key != (request.selection.binding_key if request.selection else "")
        ):
            raise SecureReadDenied("cursor is not bound to this read")
        return claims

    def _next_cursor(
        self,
        *,
        request: SecureReadRequest,
        operation: ReadOperation,
        as_of: datetime,
        scope_plan: ScopePlan,
        projection: NamedProjection,
        basis_fingerprint: str,
        issued_at: datetime,
        previous_cursor: _CursorClaims | None,
        fetched: tuple[SecureReadRow, ...],
        visible_rows: tuple[SecureReadRow, ...],
    ) -> str | None:
        if len(fetched) <= request.page_size or not visible_rows:
            return None
        after_id = visible_rows[-1].row_id
        if type(after_id) is not UUID:
            raise SecureReadDenied("projection did not return its fixed cursor column")
        cursor_issued_at = previous_cursor.issued_at if previous_cursor is not None else issued_at
        cursor_expires_at = (
            previous_cursor.expires_at
            if previous_cursor is not None
            else self._cursor_codec.expires_at(issued_at)
        )
        return self._cursor_codec.encode(
            _CursorClaims(
                operation_code=operation.code,
                selection_key=request.selection.binding_key if request.selection else "",
                account_user_id=request.actor.account_user_id,
                school_id=UUID(str(request.actor.school_id)),
                person_id=UUID(str(request.actor.person_id)),
                assignment_id=UUID(str(request.actor.assignment_id)),
                assignment_version=request.actor.assignment_version,
                authorization_version=request.actor.authorization_version,
                scope_id=scope_plan.scope_id,
                scope_owner=scope_plan.owner,
                scope_reference=_cursor_scope_reference(scope_plan),
                scope_version=scope_plan.version,
                projection_name=projection.name,
                projection_version=projection.version,
                basis_fingerprint=basis_fingerprint,
                as_of=as_of,
                after_id=after_id,
                page_size=request.page_size,
                key_version=self._cursor_codec.key_version,
                issued_at=cursor_issued_at,
                expires_at=cursor_expires_at,
            )
        )


_FieldSpec = tuple[str, tuple[type[object], ...]]
_PROJECTION_ROW_ADAPTERS: dict[
    str,
    tuple[type[object], tuple[_FieldSpec, ...]],
] = {
    "school_facts.official_contact_campuses": (
        OfficialContactCampusRow,
        (
            ("row_id", (UUID,)),
            ("display_name", (str,)),
            ("display_path", (str,)),
            ("valid_from", (datetime,)),
            ("valid_to", (datetime, type(None))),
            ("version", (int,)),
        ),
    ),
    "school_facts.official_contact_maintenance_target": (
        OfficialContactMaintenanceTargetRow,
        (
            ("row_id", (UUID,)),
            ("source_key", (str,)),
            ("expected_version", (int,)),
            ("campus_id", (UUID,)),
            ("campus_source_key", (str,)),
            ("school_fact_version", (int,)),
            ("owner_kind", (str,)),
            ("contact_kind", (str,)),
            ("contact_value", (str,)),
            ("service_hours", (str, type(None))),
            ("lifecycle_status", (str,)),
            ("visibility_scope", (str,)),
            ("audience_code", (str, type(None))),
            ("valid_from", (datetime,)),
            ("valid_to", (datetime, type(None))),
        ),
    ),
    "school_facts.official_contacts": (
        PublishedOfficialContactRow,
        (
            ("row_id", (UUID,)),
            ("campus_id", (UUID,)),
            ("campus_name", (str,)),
            ("contact_kind", (str,)),
            ("contact_value", (str,)),
            ("service_hours", (str, type(None))),
            ("valid_from", (datetime,)),
            ("valid_to", (datetime, type(None))),
            ("version", (int,)),
            ("source_kind", (str,)),
            ("lineage_id", (UUID,)),
            ("batch_id", (UUID,)),
            ("published_at", (datetime,)),
        ),
    ),
    "test_only.r1.self_current_assignment": (
        SelfCurrentAssignmentRow,
        (
            ("row_id", (UUID,)),
            ("organization_unit_id", (UUID,)),
            ("position_title", (str,)),
            ("semantic_role_code", (str,)),
            ("assignment_kind", (str,)),
            ("effective_from", (datetime,)),
            ("effective_to", (datetime, type(None))),
            ("planned_valid_to", (datetime, type(None))),
            ("version", (int,)),
        ),
    ),
    "test_only.r1.public_location": (
        PublicLocationRow,
        (
            ("row_id", (UUID,)),
            ("kind", (str,)),
            ("business_code", (str, type(None))),
            ("display_name", (str,)),
            ("display_path", (str,)),
            ("valid_from", (datetime,)),
            ("valid_to", (datetime, type(None))),
            ("version", (int,)),
        ),
    ),
    "test_only.r1.official_office_contact": (
        OfficialOfficeContactRow,
        (
            ("row_id", (UUID,)),
            ("owner_kind", (str,)),
            ("organization_unit_id", (UUID, type(None))),
            ("location_id", (UUID, type(None))),
            ("contact_kind", (str,)),
            ("contact_value", (str,)),
            ("service_hours", (str, type(None))),
            ("version", (int,)),
        ),
    ),
}


def _projection_row_denied() -> SecureReadDenied:
    return SecureReadDenied("projection row shape or type does not match server-owned DTO")


def _projection_value_matches(value: object, allowed_types: tuple[type[object], ...]) -> bool:
    if type(value) not in allowed_types:
        return False
    return type(value) is not datetime or value.utcoffset() is not None


def _require_exact_projection_rows(
    relation: CompiledReadRelation,
    rows: Sequence[object],
) -> tuple[SecureReadRow, ...]:
    adapter = _PROJECTION_ROW_ADAPTERS.get(relation.operation_code)
    if relation.operation_code.startswith("school_facts.f2."):
        from .secure_reader_registry import f2_operation_registry

        try:
            f2_operation_registry().require(relation.operation_code)
        except KeyError as exc:
            raise _projection_row_denied() from exc
        adapter = (F2FactRow, F2_ROW_SPECS)
    if adapter is None:
        raise _projection_row_denied()
    dto_type, field_specs = adapter
    expected_fields = tuple(field for field, _allowed_types in field_specs)
    if relation.projected_fields != expected_fields:
        raise _projection_row_denied()

    dto_rows: list[SecureReadRow] = []
    for raw_row in rows:
        if not isinstance(raw_row, Mapping):
            raise _projection_row_denied()
        row = cast(Mapping[str, object], raw_row)
        try:
            actual_fields = tuple(row.keys())
        except Exception as exc:
            raise _projection_row_denied() from exc
        if actual_fields != expected_fields:
            raise _projection_row_denied()

        values: dict[str, object] = {}
        for field, allowed_types in field_specs:
            try:
                value = row[field]
                valid = _projection_value_matches(value, allowed_types)
            except Exception as exc:
                raise _projection_row_denied() from exc
            if not valid:
                raise _projection_row_denied()
            values[field] = value
        try:
            constructor = cast(Callable[..., SecureReadRow], dto_type)
            dto_row = constructor(**values)
        except Exception as exc:
            raise _projection_row_denied() from exc
        if type(dto_row) is not dto_type:
            raise _projection_row_denied()
        dto_rows.append(dto_row)
    return tuple(dto_rows)


def _require_authorization_context(
    authorization: object,
    *,
    operation: ReadOperation,
) -> tuple[ScopePlan, NamedProjection, str]:
    if not bool(getattr(authorization, "allowed", False)):
        raise SecureReadDenied("thin authorization denied the read")
    scope_plan = getattr(authorization, "scope_plan", None)
    projection = getattr(authorization, "projection", None)
    basis_fingerprint = getattr(authorization, "basis_fingerprint", None)
    if (
        type(scope_plan) is not ScopePlan
        or type(projection) is not NamedProjection
        or projection.name != operation.projection_name
        or projection.version < 1
        or not isinstance(basis_fingerprint, str)
        or not basis_fingerprint
    ):
        raise SecureReadDenied("thin authorization context is incomplete")
    return scope_plan, projection, basis_fingerprint


def _cursor_scope_reference(scope_plan: ScopePlan) -> str:
    if scope_plan.owner in {"school_facts.f2", "school_facts.f2.self"}:
        return "sha256:" + hashlib.sha256(scope_plan.reference.encode("utf-8")).hexdigest()
    return scope_plan.reference


def _cursor_matches_authorization(
    cursor: _CursorClaims,
    *,
    request: SecureReadRequest,
    scope_plan: ScopePlan,
    projection: NamedProjection,
    basis_fingerprint: str,
) -> bool:
    return (
        cursor.operation_code == request.operation_code
        and cursor.scope_id == scope_plan.scope_id
        and cursor.scope_owner == scope_plan.owner
        and cursor.scope_reference == _cursor_scope_reference(scope_plan)
        and cursor.scope_version == scope_plan.version
        and cursor.projection_name == projection.name
        and cursor.projection_version == projection.version
        and cursor.basis_fingerprint == basis_fingerprint
    )


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unbase64url(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


__all__ = [
    "ReadTransaction",
    "RepeatableReadExecutor",
    "SecureCursorCodec",
    "SecureReadAuthorizationDrift",
    "SecureReadDenied",
    "SecureReader",
]
