"""F2 application queries and final exact-change authorization over SecureReader."""

from __future__ import annotations

import hmac
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.platform.authorization.f2_reader_compiler import F2_DEFINITIONS, query_selector_error
from app.platform.authorization.f2_reader_contracts import F2FactReadSelection, F2FactRow
from app.platform.authorization.secure_reader_adapters import ThinAuthorizationReadAdapter
from app.platform.authorization.secure_reader_contracts import (
    ReadAuthorization,
    SecureReadDenied,
    SecureReadPage,
    SecureReadRequest,
)
from app.platform.authorization.secure_reader_registry import ReadOperation, f2_operation_registry
from app.platform.authorization.thin_contracts import ThinAuthorizationBasis
from app.platform.contracts import CommandExecutionContext, StaffActorContext
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1, fact_content_fields
from app.platform.school_facts.f2_policy import F2_FACT_KINDS, F2FactScope
from app.platform.school_facts.read_policy import SchoolFactAuthorizationUnavailable
from app.platform.school_facts.school_scope_reference import (
    decode_school_scope_reference,
    encode_school_scope_reference,
    is_school_scope_reference,
    school_scope_reference_coordinates,
)


@dataclass(frozen=True, slots=True)
class F2PublicFact:
    fact_ref: str
    fact_kind: str
    content: dict[str, object]
    version: int
    valid_from: datetime
    valid_to: datetime | None
    source_kind: str
    publication_ref: str
    published_at: datetime


@dataclass(frozen=True, slots=True)
class F2FactQueryResult:
    status: str
    records: tuple[F2PublicFact, ...]
    matched_count: int | None
    has_more: bool
    next_cursor: str | None
    queried_at: datetime
    factual_at: datetime | None = None
    read_version: int | None = None
    coverage: str = "unknown"
    query_scope: str = (
        "当前履职身份明确获权的事实对象及请求时点；不含隐含下级范围，空结果不证明现实不存在。"
    )


@dataclass(frozen=True, slots=True)
class F2MaintenanceTarget:
    source_key: str
    expected_version: int
    school_fact_version: int
    content: dict[str, object]
    safe_record: F2PublicFact


def _reference_kind(fact_kind: str, field: str, content: Mapping[str, object]) -> str:
    if field in {"target_source_key", "replacement_source_key"}:
        kind = content.get("target_fact_kind")
        if not isinstance(kind, str) or kind not in F2_FACT_KINDS:
            raise ValueError("Invalid quality target kind.")
        return kind
    for key, kind, _ in F2_DEFINITIONS[fact_kind].refs:
        if key == field:
            return kind
    raise ValueError("Unregistered reference field.")


class _Reader(Protocol):
    def read(self, request: SecureReadRequest) -> SecureReadPage: ...


def _scope(authorization: ReadAuthorization) -> F2FactScope:
    if authorization.scope_plan is None:
        raise SchoolFactChangeRejected("授权范围不可验证。")
    return F2FactScope.from_reference(authorization.scope_plan.reference)


class F2FactQueryRepository:
    def __init__(
        self,
        *,
        reader: _Reader,
        quality_readers: Mapping[str, _Reader] | None = None,
        reference_key: bytes,
        source_catalog: Mapping[str, tuple[str, ...]] | None = None,
        creation_catalog: Mapping[str, Mapping[str, str]] | None = None,
        maintenance_guard: F2FinalAuthorizationGuard | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(reference_key) is not bytes or len(reference_key) < 16:
            raise ValueError("A private domain-separated reference key is required.")
        self._reader = reader
        self._quality_readers = dict(quality_readers or {})
        self._reference_key = hmac.digest(reference_key, b"school-facts-f2-references-v1", "sha256")
        self._catalog = {kind: set(keys) for kind, keys in (source_catalog or {}).items()}
        self._creation_catalog = {
            kind: dict(choices) for kind, choices in (creation_catalog or {}).items()
        }
        self._references: dict[tuple[str, str, str], str] = {}
        self._guard = maintenance_guard
        self._clock = clock or (lambda: datetime.now(UTC))

    def _school_scope(
        self, actor: StaffActorContext, fact_kind: str, *, creation: bool
    ) -> F2FactScope | None:
        if self._guard is None:
            return None
        for purpose in ("maintain",) if creation else ("read", "maintain"):
            try:
                authorization = self._guard.require_current(actor, fact_kind, purpose=purpose)
                scope = _scope(authorization)
                if scope.school_id != actor.school_id or scope.fact_kind != fact_kind:
                    raise ValueError("Scope coordinates differ from current actor.")
                if scope.is_school_wide:
                    self._guard.revalidate(authorization, actor)
                    return scope
            except SchoolFactChangeRejected:
                continue
        return None

    def reference_for(
        self, *, actor: StaffActorContext, fact_kind: str, source_key: str, creation: bool = False
    ) -> str:
        if fact_kind not in F2_FACT_KINDS or type(source_key) is not str or not source_key:
            raise ValueError("Invalid fact reference.")
        scope = self._school_scope(actor, fact_kind, creation=creation)
        if scope is not None and scope.allows_key(source_key, is_creation=creation):
            return encode_school_scope_reference(
                secret=self._reference_key,
                family="f2",
                school_id=actor.school_id,
                assignment_id=actor.assignment_id,
                fact_kind=fact_kind,
                source_key=source_key,
                creation=creation,
            )
        prefix = "f2create" if creation else "f2"
        payload = f"{prefix}:{actor.school_id}:{fact_kind}:{source_key}".encode()
        reference = (
            f"{prefix}:{fact_kind}:{hmac.new(self._reference_key, payload, sha256).hexdigest()}"
        )
        self._references[(str(actor.school_id), fact_kind, reference)] = source_key
        return reference

    def source_for(
        self, *, actor: StaffActorContext, fact_kind: str, reference: str, creation: bool = False
    ) -> str:
        if fact_kind not in F2_FACT_KINDS or type(reference) is not str:
            raise ValueError("Invalid fact reference.")
        if is_school_scope_reference(reference):
            source = decode_school_scope_reference(
                reference,
                secret=self._reference_key,
                family="f2",
                school_id=actor.school_id,
                assignment_id=actor.assignment_id,
                fact_kind=fact_kind,
                creation=creation,
            )
            scope = self._school_scope(actor, fact_kind, creation=creation)
            if scope is None or not scope.allows_key(source, is_creation=creation):
                raise ValueError("School reference requires current school coverage.")
            return source
        prefix = "f2create" if creation else "f2"
        if not reference.startswith(f"{prefix}:{fact_kind}:"):
            raise ValueError("Reference belongs to another fact kind.")
        key = (str(actor.school_id), fact_kind, reference)
        source = self._references.get(key)
        if source is None:
            candidates = (
                self._creation_catalog.get(fact_kind, {})
                if creation
                else self._catalog.get(fact_kind, set())
            )
            for candidate in candidates:
                expected = self.reference_for(
                    actor=actor, fact_kind=fact_kind, source_key=candidate, creation=creation
                )
                if hmac.compare_digest(reference, expected):
                    source = candidate
                    break
        if source is None or creation and source not in self._creation_catalog.get(fact_kind, {}):
            raise ValueError("Reference is unavailable; select an authorized record again.")
        return source

    def _public(self, actor: StaffActorContext, row: F2FactRow) -> F2PublicFact:
        content = dict(row.content)
        for field, value in tuple(content.items()):
            if field.endswith("_source_key") and value is not None:
                refkind = _reference_kind(row.fact_kind, field, content)
                content[field.removesuffix("_source_key") + "_ref"] = self.reference_for(
                    actor=actor, fact_kind=refkind, source_key=cast(str, value)
                )
                del content[field]
        return F2PublicFact(
            self.reference_for(actor=actor, fact_kind=row.fact_kind, source_key=row.source_key),
            row.fact_kind,
            content,
            row.fact_version,
            row.valid_from,
            row.valid_to,
            row.source_kind,
            f"publication:{row.publication_id}",
            row.published_at,
        )

    def query(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str,
        reference: str | None = None,
        name: str | None = None,
        factual_at: datetime | None = None,
        scope: str = "selected",
        cursor: str | None = None,
        page_size: int = 100,
        related_ref: str | None = None,
        quality_target_kind: str | None = None,
        content_fields: tuple[str, ...] | None = None,
    ) -> F2FactQueryResult:
        if fact_kind not in F2_FACT_KINDS or scope not in {"selected", "all_authorized"}:
            raise ValueError("Unknown fixed F2 query.")
        if (
            name is not None
            and reference is not None
            or related_ref is not None
            and reference is not None
            or scope == "all_authorized"
            and (reference is not None or name is not None and related_ref is None)
        ):
            raise ValueError("Query selection is ambiguous.")
        related_parts = related_ref.split(":") if related_ref is not None else None
        if related_ref is not None and is_school_scope_reference(related_ref):
            coordinates = school_scope_reference_coordinates(related_ref)
            if coordinates is None or coordinates[0] != "f2" or coordinates[2]:
                raise ValueError("Invalid related school reference.")
            related_parts = ["f2", coordinates[1], "signed"]
        if related_parts is not None and (len(related_parts) != 3 or related_parts[0] != "f2"):
            raise ValueError("Invalid related reference.")
        selector_error = query_selector_error(
            fact_kind,
            name=name,
            related_kind=related_parts[1] if related_parts is not None else None,
        )
        if selector_error is not None:
            return F2FactQueryResult(
                "unsupported_selection",
                (),
                None,
                False,
                None,
                self._clock(),
                query_scope="本次选择条件不受该事实类型支持，未执行事实查询；不能据此判断是否有记录。",
            )
        reader = self._reader
        if quality_target_kind is not None:
            if fact_kind != "fact_quality" or quality_target_kind not in self._quality_readers:
                raise SchoolFactAuthorizationUnavailable("Bound quality reader unavailable.")
            reader = self._quality_readers[quality_target_kind]
        related_kind = related_source = None
        related_authorization = None
        if related_ref is not None:
            parts = related_parts or []
            if len(parts) != 3 or parts[0] != "f2":
                raise ValueError("Invalid related reference.")
            related_kind = parts[1]
            related_source = self.source_for(
                actor=actor, fact_kind=related_kind, reference=related_ref
            )
            if self._guard is None:
                raise SecureReadDenied("Related object authorization is unavailable.")
            related_authorization = self._guard.require_related(actor, related_kind, related_source)
        selection = F2FactReadSelection(
            related_kind=related_kind,
            related_source_key=related_source,
            source_key=self.source_for(actor=actor, fact_kind=fact_kind, reference=reference)
            if reference is not None
            else None,
            name=name,
            content_fields=content_fields,
        )
        request = SecureReadRequest(
            actor=actor,
            operation_code=f"school_facts.f2.{fact_kind}.read",
            selection=selection,
            factual_at=factual_at,
            cursor=cursor,
            page_size=page_size,
        )
        self_only = False
        try:
            if related_authorization is not None and self._guard is not None:
                self._guard.revalidate(related_authorization, actor)
            try:
                page = reader.read(request)
            except SecureReadDenied:
                if fact_kind != "person_contact":
                    raise
                self_only = True
                page = reader.read(
                    replace(request, operation_code="school_facts.f2.person_contact.self")
                )
            if any(type(row) is not F2FactRow or row.fact_kind != fact_kind for row in page.rows):
                raise SecureReadDenied("Invalid F2 projection.")
            if (
                self_only
                and not page.rows
                and any(value is not None for value in (reference, related_ref, name))
            ):
                # A self-filtered empty page cannot establish access to the requested
                # target. For historical reads only, confirm the SAME selection through
                # current self authority; never return that probe's current contents.
                if factual_at is None:
                    raise SecureReadDenied(
                        "Requested target cannot be authorized within self scope."
                    )
                current_self = reader.read(
                    replace(
                        request,
                        operation_code="school_facts.f2.person_contact.self",
                        factual_at=None,
                        cursor=None,
                        page_size=1,
                    )
                )
                if not current_self.rows or any(
                    type(row) is not F2FactRow or row.fact_kind != "person_contact"
                    for row in current_self.rows
                ):
                    raise SecureReadDenied(
                        "Requested target cannot be authorized within self scope."
                    )
            if related_authorization is not None and self._guard is not None:
                self._guard.revalidate(related_authorization, actor)
            records = tuple(self._public(actor, cast(F2FactRow, row)) for row in page.rows)
            status = "known" if records else "no_current_records"
            if scope == "selected" and name is None and reference is None and related_ref is None:
                status = "selection_required"
            elif scope == "selected" and related_ref is None and len(records) > 1:
                status = "ambiguous"
            result = F2FactQueryResult(
                status,
                records,
                page.total_count,
                page.next_cursor is not None,
                page.next_cursor,
                self._clock(),
                factual_at=page.as_of,
                read_version=max(
                    (cast(F2FactRow, row).school_fact_version for row in page.rows), default=None
                ),
            )
            if self_only:
                return replace(
                    result,
                    query_scope="仅当前身份本人的联系方式获准集合，记录按请求事实时点读取；不代表其他人员的联系方式或全校集合，空结果不证明现实不存在。",
                )
            return result
        except SecureReadDenied:
            return F2FactQueryResult("forbidden", (), None, False, None, self._clock())
        except (SQLAlchemyError, SchoolFactAuthorizationUnavailable):
            return F2FactQueryResult("source_unavailable", (), None, False, None, self._clock())

    def prepare_target(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str,
        target_ref: str | None = None,
        creation_ref: str | None = None,
    ) -> F2MaintenanceTarget:
        if (target_ref is None) == (creation_ref is None):
            raise ValueError("Select one existing or explicit creation target.")
        source = self.source_for(
            actor=actor,
            fact_kind=fact_kind,
            reference=cast(str, creation_ref or target_ref),
            creation=creation_ref is not None,
        )
        selection = (
            F2FactReadSelection(creation_key=source)
            if creation_ref is not None
            else F2FactReadSelection(source_key=source)
        )
        page = self._reader.read(
            SecureReadRequest(
                actor=actor,
                operation_code=f"school_facts.f2.{fact_kind}.maintain",
                selection=selection,
                page_size=2,
            )
        )
        if (
            len(page.rows) != 1
            or page.next_cursor is not None
            or type(page.rows[0]) is not F2FactRow
            or page.rows[0].source_key != source
            or page.rows[0].fact_kind != fact_kind
        ):
            raise SchoolFactChangeRejected("目标已变化、存在歧义或当前不可维护，请重新选择。")
        row = page.rows[0]
        return F2MaintenanceTarget(
            row.source_key,
            row.expected_version,
            row.school_fact_version,
            dict(row.content),
            self._public(actor, row),
        )

    def maintenance_content_for_source(
        self, *, actor: StaffActorContext, fact_kind: str, source_key: str
    ) -> dict[str, object]:
        page = self._reader.read(
            SecureReadRequest(
                actor=actor,
                operation_code=f"school_facts.f2.{fact_kind}.maintain",
                selection=F2FactReadSelection(source_key=source_key),
                page_size=2,
            )
        )
        if (
            len(page.rows) != 1
            or type(page.rows[0]) is not F2FactRow
            or page.rows[0].source_key != source_key
            or page.rows[0].fact_kind != fact_kind
        ):
            raise SchoolFactChangeRejected("原事实引用不可验证。")
        return dict(page.rows[0].content)

    def can_maintain(self, *, actor: StaffActorContext, fact_kind: str, reference: str) -> bool:
        if self._guard is None:
            return False
        try:
            key = self.source_for(actor=actor, fact_kind=fact_kind, reference=reference)
            auth = self._guard.require_current(actor, fact_kind)
            return _scope(auth).allows_key(key)
        except (ValueError, SchoolFactChangeRejected, SchoolFactAuthorizationUnavailable):
            return False

    def access(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str | None = None,
        purpose: str = "read",
        target_ref: str | None = None,
        fields: tuple[str, ...] = (),
    ) -> dict[str, object]:
        """Explain current authority without reading records or preparing a fictitious edit."""
        if fact_kind is not None and fact_kind not in F2_FACT_KINDS:
            raise ValueError("Unknown fact kind.")
        if purpose == "maintain":
            return self._maintenance_access(
                actor=actor, fact_kind=fact_kind, target_ref=target_ref, fields=fields
            )
        if purpose != "read" or target_ref is not None or fields:
            raise ValueError("Target and fields are only supported for maintenance eligibility.")
        if self._guard is None:
            raise SchoolFactAuthorizationUnavailable("Current authority unavailable.")
        families: list[dict[str, object]] = []
        for kind in sorted(F2_FACT_KINDS if fact_kind is None else {fact_kind}):
            item: dict[str, object] = {"fact_kind": kind}
            try:
                auth = self._guard.require_current(actor, kind, purpose="read")
                scope = _scope(auth)
                if auth.projection is None:
                    raise SchoolFactAuthorizationUnavailable("Current read projection unavailable.")
                self._guard.revalidate(auth, actor)
                item.update(
                    status="authorized_scope",
                    authorized_target_count=None
                    if scope.is_school_wide
                    else len(scope.object_keys),
                    scope_coverage=scope.coverage,
                    projection=auth.projection.name,
                )
            except SchoolFactChangeRejected:
                item["status"] = "denied"
                if kind == "person_contact":
                    try:
                        if self._guard.can_read_self(actor):
                            item["status"] = "self_only"
                    except (SchoolFactAuthorizationUnavailable, SecureReadDenied):
                        item["status"] = "source_unavailable"
            except (SchoolFactAuthorizationUnavailable, SecureReadDenied):
                item["status"] = "source_unavailable"
            families.append(item)
        return {
            "kind": "fact_access",
            "requested_fact_kind": fact_kind,
            "status": "known",
            "families": families,
            "queried_at": self._clock().isoformat(),
            "notice": (
                "这是当前履职身份的学校事实读取授权摘要，不是能力目录或资料清单。授权目标数不是已录入、当前有效或可见记录数；发布范围、字"
                "段投影、隐私及事实时点仍在正式查询时核验。对象范围以当前获批范围为准；读取权不代表维护权。本人联系方式另受当前本人"
                "关系核验。"
            ),
        }

    def _maintenance_access(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str | None,
        target_ref: str | None,
        fields: tuple[str, ...],
    ) -> dict[str, object]:
        if (
            fact_kind is None
            or fact_kind not in F2_FACT_KINDS
            or len(fields) > 70
            or len(set(fields)) != len(fields)
        ):
            raise ValueError("Select one installed family and finite unique fields.")
        allowed_fields, _ = fact_content_fields(fact_kind, schema_version=2)
        canonical_fields = tuple(
            name.removesuffix("_ref") + "_source_key" if name.endswith("_ref") else name
            for name in fields
        )
        if any(name not in allowed_fields for name in canonical_fields) or any(
            name.endswith("_source_key") for name in fields
        ):
            raise ValueError("Select public fields from this family's maintenance contract.")
        item: dict[str, object] = {"fact_kind": fact_kind}
        try:
            if self._guard is None:
                raise SchoolFactAuthorizationUnavailable("Current authority unavailable.")
            auth = self._guard.require_current(actor, fact_kind, purpose="maintain")
            scope = _scope(auth)
            # F2 grants bind exact objects, not arbitrary per-field permissions.
            # Schema-valid fields never stand in for final change-set validation.
            if target_ref is not None:
                try:
                    key = self.source_for(actor=actor, fact_kind=fact_kind, reference=target_ref)
                except ValueError as exc:
                    raise SchoolFactChangeRejected("Target outside verified scope.") from exc
                if not scope.allows_key(key):
                    raise SchoolFactChangeRejected("Target outside verified scope.")
            self._guard.revalidate(auth, actor)
            if target_ref is None:
                item.update(
                    status="authorized_scope",
                    authorized_target_count=None
                    if scope.is_school_wide
                    else len(scope.object_keys),
                    scope_coverage=scope.coverage,
                    authorized_creation_count=None
                    if scope.is_school_wide
                    else len(scope.creation_keys),
                    next_step="select_target_and_prepare",
                )
            else:
                item.update(
                    status="requires_prepare",
                    target_in_scope=True,
                    field_check="schema_only",
                    next_step="prepare",
                )
        except SchoolFactChangeRejected:
            item["status"] = "denied"
        except (SchoolFactAuthorizationUnavailable, SecureReadDenied):
            item["status"] = "source_unavailable"
        return {
            "kind": "fact_access",
            "purpose": "maintain",
            "requested_fact_kind": fact_kind,
            "status": "known",
            "families": [item],
            "queried_at": self._clock().isoformat(),
            "notice": (
                "这是当前身份的维护职责核验，不是资料清单或执行批准。denied表示本次范围未获维护许可；"
                "authorized_scope只证明存在限定职责，仍需选定对象；requires_prepare只证明对象在当前职责范围，"
                "字段名仅按维护合同核对，不代表具体修改已获准或业务预检通过。正式预检、完整差异、明确确认和执行前重验仍须完成。"
            ),
        }

    def creation_choices(
        self, *, actor: StaffActorContext, fact_kind: str
    ) -> tuple[dict[str, str], ...]:
        if self._guard is None:
            return ()
        try:
            authorization = self._guard.require_current(actor, fact_kind)
        except (SchoolFactChangeRejected, SchoolFactAuthorizationUnavailable):
            return ()
        scope = _scope(authorization)
        if scope.is_school_wide:
            if fact_kind == "school":
                return ()
            self._guard.revalidate(authorization, actor)
            return (
                {
                    "creation_ref": self.reference_for(
                        actor=actor,
                        fact_kind=fact_kind,
                        source_key=f"{fact_kind}:{uuid4()}",
                        creation=True,
                    ),
                    "label": "新建资料",
                },
            )
        return tuple(
            {
                "creation_ref": self.reference_for(
                    actor=actor, fact_kind=fact_kind, source_key=key, creation=True
                ),
                "label": label,
            }
            for key, label in self._creation_catalog.get(fact_kind, {}).items()
            if key in scope.creation_keys
        )

    def resolve_field_references(
        self, *, actor: StaffActorContext, fact_kind: str, fields: Mapping[str, object]
    ) -> dict[str, object]:
        result = dict(fields)
        if fact_kind not in F2_FACT_KINDS:
            raise ValueError("Unknown fact kind.")
        if any(name.endswith("_source_key") for name in result):
            raise ValueError("Canonical source keys are not public inputs.")
        for field, value in tuple(fields.items()):
            if not field.endswith("_ref"):
                continue
            canonical = field.removesuffix("_ref") + "_source_key"
            kind = _reference_kind(fact_kind, canonical, result)
            result[canonical] = (
                None
                if value is None
                else self.source_for(actor=actor, fact_kind=kind, reference=cast(str, value))
            )
            del result[field]
        return result


class F2FinalAuthorizationGuard:
    def __init__(
        self,
        *,
        thin_service: object,
        scope_id_resolver: Callable[[ReadOperation, StaffActorContext], UUID],
        target_content_resolver: Callable[[StaffActorContext, str, str], dict[str, object]]
        | None = None,
        clock: Callable[[], datetime] | None = None,
        authorization_round: Callable[[StaffActorContext, datetime], AbstractContextManager[None]]
        | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._authorizer = ThinAuthorizationReadAdapter(
            thin_service=thin_service, scope_id_resolver=scope_id_resolver
        )
        self._registry = f2_operation_registry()
        self._target_content_resolver = target_content_resolver
        self._authorization_round: Callable[
            [StaffActorContext, datetime], AbstractContextManager[None]
        ] = authorization_round or (lambda _actor, _at: nullcontext())

    def require_current(
        self, actor: StaffActorContext, fact_kind: str, *, purpose: str = "maintain"
    ) -> ReadAuthorization:
        try:
            operation = self._registry.require(f"school_facts.f2.{fact_kind}.{purpose}")
            authorization = self._authorizer.decide(
                operation=operation, actor=actor, as_of=self._clock()
            )
            if (
                not authorization.allowed
                or type(authorization.basis) is not ThinAuthorizationBasis
                or authorization.scope_plan is None
            ):
                raise SchoolFactChangeRejected("当前没有此类事实的精确授权。")
            scope = _scope(authorization)
            basis = authorization.basis
            suffix = "maintainer" if purpose == "maintain" else "reader"
            if (
                scope.school_id != UUID(str(actor.school_id))
                or scope.fact_kind != fact_kind
                or authorization.scope_plan.owner != "school_facts.f2"
                or authorization.scope_plan.version != 1
                or basis.action_code != operation.action_code
                or basis.governance.duty_code != f"f2_{fact_kind}_{suffix}"
            ):
                raise SchoolFactChangeRejected("当前授权不能用于此类事实。")
            return authorization
        except SchoolFactChangeRejected:
            raise
        except Exception as exc:
            raise SchoolFactAuthorizationUnavailable("当前事实授权不可验证。") from exc

    def can_read_self(self, actor: StaffActorContext) -> bool:
        from app.platform.authorization.thin_contracts import ThinSelfAuthorizationBasis

        operation = self._registry.require("school_facts.f2.person_contact.self")
        auth = self._authorizer.decide(operation=operation, actor=actor, as_of=self._clock())
        if not auth.allowed:
            return False
        if (
            type(auth.basis) is not ThinSelfAuthorizationBasis
            or auth.projection is None
            or auth.projection.name != "f2_person_contact_self"
            or auth.basis.actor.person_id != actor.person_id
            or auth.basis.actor.school_id != actor.school_id
        ):
            raise SchoolFactAuthorizationUnavailable("Current self authority unavailable.")
        self.revalidate(auth, actor)
        return True

    def require_current_many(
        self, actor: StaffActorContext
    ) -> tuple[tuple[str, ReadAuthorization], ...]:
        """Current receipt metadata uses each family's independent exact read duty."""
        kinds = tuple(sorted(F2_FACT_KINDS))
        operations = tuple(self._registry.require(f"school_facts.f2.{kind}.read") for kind in kinds)
        try:
            at = self._clock()
            with self._authorization_round(actor, at):
                decisions = self._authorizer.decide_many(
                    operations=operations, actor=actor, as_of=at
                )
            return tuple(zip(kinds, decisions, strict=True))
        except Exception as exc:
            raise SchoolFactAuthorizationUnavailable("当前回执读取授权不可验证。") from exc

    def revalidate_many(
        self, authorizations: tuple[ReadAuthorization, ...], actor: StaffActorContext
    ) -> None:
        at = self._clock()
        with self._authorization_round(actor, at):
            for outcome in self._authorizer.revalidate_many(authorizations, actor=actor, as_of=at):
                if isinstance(outcome, Exception):
                    raise outcome
                if not outcome:
                    raise SecureReadDenied("Receipt authorization changed; result discarded.")

    def require_related(
        self, actor: StaffActorContext, fact_kind: str, source_key: str
    ) -> ReadAuthorization:
        for purpose in ("read", "maintain"):
            try:
                authorization = self.require_current(actor, fact_kind, purpose=purpose)
            except SchoolFactChangeRejected:
                continue
            if _scope(authorization).allows_key(source_key):
                return authorization
        raise SecureReadDenied("Related object is outside the current exact scope.")

    def revalidate(self, authorization: ReadAuthorization, actor: StaffActorContext) -> None:
        if not self._authorizer.revalidate(authorization, actor=actor, as_of=self._clock()):
            raise SecureReadDenied("Related authorization changed; result discarded.")

    def require_change_set(
        self, actor: StaffActorContext, change_set: CanonicalFactChangeSetV1
    ) -> tuple[ReadAuthorization, ...]:
        if (
            type(actor) is not StaffActorContext
            or not isinstance(cast(object, change_set), CanonicalFactChangeSetV1)
            or not change_set.changes
        ):
            raise SchoolFactChangeRejected("只接受可信身份和正式变化集。")
        authorizations: list[ReadAuthorization] = []
        by_kind: dict[str, ReadAuthorization] = {}
        for change in change_set.changes:
            if change.fact_kind not in F2_FACT_KINDS:
                raise SchoolFactChangeRejected("变化集混入未授权事实族。")
            authorization = by_kind.get(change.fact_kind)
            if authorization is None:
                authorization = self.require_current(actor, change.fact_kind)
                by_kind[change.fact_kind] = authorization
                authorizations.append(authorization)
            scope = _scope(authorization)
            if (
                not scope.allows_key(change.source_key, is_creation=change.expected_version == 0)
                or change.expected_version == 0
                and (change.operation != "upsert" or change.fact_kind == "school")
            ):
                raise SchoolFactChangeRejected("变化集含未获权的目标或创建操作。")
            if change.content is None:
                if not F2_DEFINITIONS[change.fact_kind].refs and change.fact_kind != "fact_quality":
                    continue
                if self._target_content_resolver is None:
                    raise SchoolFactChangeRejected("原事实引用必须在终止前重新核验。")
                content = self._target_content_resolver(actor, change.fact_kind, change.source_key)
            else:
                content = change.content.model_dump(exclude_none=True, exclude_unset=True)
            if change.fact_kind == "fact_quality":
                target_kind = content.get("target_fact_kind")
                if (
                    not isinstance(target_kind, str)
                    or target_kind not in F2_FACT_KINDS
                    or target_kind == "fact_quality"
                ):
                    raise SchoolFactChangeRejected("质量事实的所属事实族无效。")
                target_auth = self.require_current(actor, target_kind)
                authorizations.append(target_auth)
                if content.get("owner_kind") == "school":
                    school_auth = self.require_current(actor, "school", purpose="read")
                    if not (_scope(school_auth).is_school_wide or _scope(school_auth).object_keys):
                        raise SchoolFactChangeRejected("学校范围的质量依据不可验证。")
                    authorizations.append(school_auth)
            for field, key in content.items():
                if not field.endswith("_source_key"):
                    continue
                kind = _reference_kind(change.fact_kind, field, content)
                # Reference authority is independent.
                # A relation grant does not expose the directory.
                permitted = False
                purposes = (
                    ("maintain",)
                    if change.fact_kind == "fact_quality"
                    and field in {"target_source_key", "replacement_source_key"}
                    else ("read", "maintain")
                )
                for purpose in purposes:
                    try:
                        refauth = self.require_current(actor, kind, purpose=purpose)
                    except SchoolFactChangeRejected:
                        continue
                    refscope = _scope(refauth)
                    same_plan_create = any(
                        c.fact_kind == kind and c.source_key == key and c.expected_version == 0
                        for c in change_set.changes
                    )
                    if (
                        refscope.allows_key(cast(str, key))
                        or purpose == "maintain"
                        and same_plan_create
                        and refscope.allows_key(cast(str, key), is_creation=True)
                    ):
                        authorizations.append(refauth)
                        permitted = True
                        break
                if not permitted:
                    raise SchoolFactChangeRejected("变化集含未获权的引用目标。")
        return tuple(authorizations)

    def __call__(
        self,
        session: Session,
        context: CommandExecutionContext,
        change_set: CanonicalFactChangeSetV1,
    ) -> None:
        if (
            type(context) is not CommandExecutionContext
            or type(context.actor) is not StaffActorContext
        ):
            raise SchoolFactChangeRejected("最终执行身份不可验证。")
        actor = context.actor
        try:
            epoch = session.scalar(
                text("SELECT app.lock_school_fact_governance_epoch(:school_id, :assignment_id)"),
                {
                    "school_id": UUID(str(actor.school_id)),
                    "assignment_id": UUID(str(actor.assignment_id)),
                },
            )
            if type(epoch) is not int or epoch < 1:
                raise SchoolFactChangeRejected("维护权限版本不可验证。")
            for authorization in self.require_change_set(actor, change_set):
                if (
                    not isinstance(authorization.basis, ThinAuthorizationBasis)
                    or authorization.basis.governance.assignment_epoch != epoch
                    or not self._authorizer.revalidate(
                        authorization, actor=actor, as_of=self._clock()
                    )
                ):
                    raise SchoolFactChangeRejected("维护或引用权限已经变化。")
        except (SchoolFactChangeRejected, SchoolFactAuthorizationUnavailable):
            raise
        except Exception as exc:
            raise SchoolFactAuthorizationUnavailable("最终事实授权不可验证。") from exc
