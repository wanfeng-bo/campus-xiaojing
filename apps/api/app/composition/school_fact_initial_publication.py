# pyright: strict
"""One-off approved two-fact publication, using the sole canonical Writer kernel."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Self, cast
from uuid import UUID, uuid5

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.composition.school_fact_product_identity import ProductIdentity
from app.composition.school_fact_writer_executor import SchoolFactWriterExecutorKernel
from app.platform.authorization.secure_reader_adapters import ThinAuthorizationReadAdapter
from app.platform.authorization.secure_reader_contracts import ReadAuthorization
from app.platform.authorization.secure_reader_registry import ReadOperation
from app.platform.authorization.thin_contracts import (
    ModuleAuthorizationRequirement,
    ThinAuthorizationBasis,
    ThinAuthorizationRequest,
)
from app.platform.contracts import CommandExecutionContext, FrozenContractModel, StaffActorContext
from app.platform.governance.application import GovernanceWriter
from app.platform.governance.bootstrap import MigrationGovernanceBootstrapAuthorizer
from app.platform.governance.contracts import (
    CreateDutyCommand,
    CreateGrantCommand,
    CreateScopeCommand,
    EndGrantCommand,
    GovernanceActorContext,
    GovernanceWriteAuthorizationRequest,
    GovernanceWriteResult,
    ScopeResolutionRequest,
    ScopeResolutionSnapshot,
)
from app.platform.governance.ports import GovernanceDependencyPort
from app.platform.governance.repository import GovernanceRepository
from app.platform.identity.execution_context import compose_command_context
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    ConfirmationReceiptV1,
    Sha256Hex,
    canonical_plan_sha256,
)
from app.platform.school_facts.initialization_contracts import SchoolFactInitializationRequest
from app.platform.school_facts.ports import (
    SchoolFactApplyResult,
    SchoolFactChangeProvenance,
    SchoolFactResultLookup,
)
from app.platform.school_facts.read_policy import SchoolFactAuthorizationUnavailable

PUBLICATION_MODULE = "school_facts.initial_publication"
PUBLICATION_DUTY = "initial_school_fact_publisher"
PUBLICATION_ACTION = "school_facts.initial_publication.publish"
PUBLICATION_SCHEMA = "approved_manifest"
PUBLICATION_PROJECTION = "initial_publication_manifest"
MAX_APPROVAL_WINDOW = timedelta(minutes=15)


def document_sha256(document: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def publication_locator_sha256(base_locator: str, grant_id: UUID) -> str:
    return hashlib.sha256(f"{base_locator}:{grant_id}".encode()).hexdigest()


class InitialPublicationManifest(FrozenContractModel):
    school_id: UUID
    initialization_id: UUID
    assignment_id: UUID
    request_id: UUID
    approved_at: AwareDatetime
    expires_at: AwareDatetime
    approval_reference: str = Field(min_length=1, max_length=255)
    change_set: CanonicalFactChangeSetV1

    @model_validator(mode="after")
    def require_minimum_manifest(self) -> Self:
        if not timedelta(0) < self.expires_at - self.approved_at <= MAX_APPROVAL_WINDOW:
            raise ValueError("发布批准必须有不超过十五分钟的有限期限。")
        if any(
            value.int == 0
            for value in (
                self.school_id,
                self.initialization_id,
                self.assignment_id,
                self.request_id,
            )
        ):
            raise ValueError("发布必须引用已存在的学校、初始化与真实任职。")
        changes = self.change_set.changes
        if len(changes) != 2 or {c.fact_kind for c in changes} != {"location", "official_contact"}:
            raise ValueError("一次性样本仅允许一条校区和一条官方联系。")
        if any(
            c.operation != "upsert" or c.expected_version != 0 or c.effective_at != self.approved_at
            for c in changes
        ):
            raise ValueError("样本仅允许在批准时间创建两条新事实。")
        location = next(c for c in changes if c.fact_kind == "location")
        contact = next(c for c in changes if c.fact_kind == "official_contact")
        lc, cc = location.content, contact.content
        if (
            lc is None
            or cc is None
            or lc.kind != "campus"
            or lc.parent_source_key is not None
            or lc.status != "active"
            or cc.owner_kind != "location"
            or cc.location_source_key != location.source_key
            or cc.lifecycle_status != "active"
            or cc.visibility_scope not in {"public", "school_authenticated"}
            or cc.audience_code is not None
        ):
            raise ValueError("样本必须是同校独立校区及其直接所属有效官方联系。")
        return self

    @property
    def manifest_sha256(self) -> str:
        return document_sha256(self.model_dump(mode="json", exclude_none=True))

    @property
    def plan_sha256(self) -> str:
        return canonical_plan_sha256(self.change_set)

    @property
    def scope_key(self) -> str:
        return f"f1:initial-publication:{self.initialization_id}"

    @property
    def locator_sha256(self) -> str:
        return hashlib.sha256(
            f"f1-publication:{self.request_id}:{self.manifest_sha256}".encode()
        ).hexdigest()

    @property
    def scope_document(self) -> dict[str, object]:
        return PublicationScopeDocument(
            school_id=self.school_id,
            initialization_id=self.initialization_id,
            assignment_id=self.assignment_id,
            publication_manifest_sha256=self.manifest_sha256,
            canonical_plan_sha256=self.plan_sha256,
            source_sha256=self.change_set.source_sha256,
            result_locator_sha256=self.locator_sha256,
            approved_at=self.approved_at,
            expires_at=self.expires_at,
        ).model_dump(mode="json")


class PublicationScopeDocument(FrozenContractModel):
    school_id: UUID
    initialization_id: UUID
    assignment_id: UUID
    publication_manifest_sha256: Sha256Hex
    canonical_plan_sha256: Sha256Hex
    source_sha256: Sha256Hex
    result_locator_sha256: Sha256Hex
    approved_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def finite(self) -> Self:
        if not timedelta(0) < self.expires_at - self.approved_at <= MAX_APPROVAL_WINDOW:
            raise ValueError("Publication approval deadline is invalid.")
        return self


class InitialPublicationScopeResolver:
    def resolve(self, request: ScopeResolutionRequest) -> ScopeResolutionSnapshot | None:
        try:
            doc = PublicationScopeDocument.model_validate_json(
                json.dumps(request.reference_document)
            )
        except (ValueError, TypeError):
            return None
        if (
            request.schema_owner != PUBLICATION_MODULE
            or request.schema_name != PUBLICATION_SCHEMA
            or request.schema_version != 1
            or request.reference_sha256 != document_sha256(request.reference_document)
            or doc.school_id != request.school_id
            or request.as_of.utcoffset() is None
            or request.as_of < doc.approved_at
        ):
            return None
        return ScopeResolutionSnapshot(
            school_id=doc.school_id,
            owner=PUBLICATION_MODULE,
            reference=f"manifest:{doc.publication_manifest_sha256}",
            version=1,
            is_effective=True,
            is_ambiguous=False,
            valid_until=None,
        )


class InitialPublicationPolicy:
    module_name = PUBLICATION_MODULE
    version = 1

    def __init__(self, manifest: InitialPublicationManifest) -> None:
        self._manifest = manifest

    def requirement_for(
        self, request: ThinAuthorizationRequest
    ) -> ModuleAuthorizationRequirement | None:
        m = self._manifest
        if (
            type(request.actor) is not StaffActorContext
            or request.actor.school_id != m.school_id
            or request.actor.assignment_id != m.assignment_id
            or request.module_name != self.module_name
            or request.action_code != PUBLICATION_ACTION
            or request.resource_type != PUBLICATION_MODULE
            or request.resource_id != m.school_id
            or request.resource_revision != request.actor.authorization_version
            or request.as_of < m.approved_at
        ):
            return None
        return ModuleAuthorizationRequirement(PUBLICATION_DUTY, PUBLICATION_PROJECTION, 1)


class InitialPublicationFinalGuard:
    def __init__(
        self,
        *,
        manifest: InitialPublicationManifest,
        scope_id: UUID,
        grant_id: UUID,
        thin_service: object,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.manifest = manifest
        self.scope_id = scope_id
        self.grant_id = grant_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._authorizer = ThinAuthorizationReadAdapter(
            thin_service=thin_service, scope_id_resolver=lambda _op, _actor: scope_id
        )
        self._operation = ReadOperation(
            code=PUBLICATION_MODULE,
            projection_name=PUBLICATION_PROJECTION,
            action_code=PUBLICATION_ACTION,
            module_name=PUBLICATION_MODULE,
        )

    def require_current(self, actor: StaffActorContext) -> ReadAuthorization:
        m = self.manifest
        if (
            type(actor) is not StaffActorContext
            or actor.school_id != m.school_id
            or actor.assignment_id != m.assignment_id
            or self._clock() < m.approved_at
        ):
            raise SchoolFactChangeRejected("样本发布身份或批准期限不匹配。")
        authorization = self._authorizer.decide(
            operation=self._operation, actor=actor, as_of=self._clock()
        )
        scope, basis = authorization.scope_plan, authorization.basis
        if (
            not authorization.allowed
            or type(basis) is not ThinAuthorizationBasis
            or scope is None
            or scope.scope_id != self.scope_id
            or scope.owner != PUBLICATION_MODULE
            or scope.reference != f"manifest:{m.manifest_sha256}"
            or scope.version != 1
            or basis.governance.duty_code != PUBLICATION_DUTY
            or basis.governance.grant_id != self.grant_id
            or basis.governance.valid_until is None
            or not self._clock()
            < basis.governance.valid_until
            <= self._clock() + MAX_APPROVAL_WINDOW
        ):
            raise SchoolFactChangeRejected("样本发布缺少当前精确清单授权。")
        return authorization

    def __call__(
        self,
        session: Session,
        context: CommandExecutionContext,
        change_set: CanonicalFactChangeSetV1,
    ) -> None:
        m = self.manifest
        if (
            type(context) is not CommandExecutionContext
            or type(context.actor) is not StaffActorContext
            or change_set != m.change_set
            or canonical_plan_sha256(change_set) != m.plan_sha256
            or context.actor.school_id != m.school_id
            or context.actor.assignment_id != m.assignment_id
        ):
            raise SchoolFactChangeRejected("一次性发布不得扩大批准清单。")
        try:
            epoch = session.scalar(
                text("SELECT app.lock_school_fact_governance_epoch(:school_id, :assignment_id)"),
                {"school_id": m.school_id, "assignment_id": m.assignment_id},
            )
            session.execute(
                text(
                    "SELECT app.require_initial_publication_window(:school_id, "
                    ":initialization_id, :scope_id, :manifest_sha)"
                ),
                {
                    "school_id": m.school_id,
                    "initialization_id": m.initialization_id,
                    "scope_id": self.scope_id,
                    "manifest_sha": m.manifest_sha256,
                },
            )
            auth = self.require_current(context.actor)
            if (
                type(epoch) is not int
                or type(auth.basis) is not ThinAuthorizationBasis
                or auth.basis.governance.assignment_epoch != epoch
                or not self._authorizer.revalidate(auth, actor=context.actor, as_of=self._clock())
            ):
                raise SchoolFactChangeRejected("样本发布授权已变化。")
        except (SchoolFactChangeRejected, SchoolFactAuthorizationUnavailable):
            raise
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == "28000":
                raise SchoolFactChangeRejected("一次性发布窗口已关闭或不属于本批准。") from exc
            raise SchoolFactAuthorizationUnavailable("样本发布授权源暂时不可用。") from exc
        except Exception as exc:
            raise SchoolFactAuthorizationUnavailable("样本发布授权暂时不可验证。") from exc


class InitialPublicationService:
    """CLI-only adapter with confirmation ownership bound to one finite Governance Grant."""

    def __init__(
        self,
        *,
        manifest: InitialPublicationManifest,
        grant_id: UUID,
        kernel: SchoolFactWriterExecutorKernel,
        require_current: Callable[[StaffActorContext], object],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.manifest, self.grant_id = manifest, grant_id
        self._kernel, self._require_current = kernel, require_current
        self._clock = clock or (lambda: datetime.now(UTC))

    def _binding(self, identity: ProductIdentity) -> str:
        # Derive from the existing private opaque binding; never obtain the login cookie.
        return hashlib.sha256(
            f"f1-publication:{self.manifest.manifest_sha256}:{self.grant_id}:{identity.session_binding}".encode()
        ).hexdigest()

    def _context(self, identity: ProductIdentity, *, writing: bool) -> CommandExecutionContext:
        m = self.manifest
        if (
            type(identity) is not ProductIdentity
            or type(identity.actor) is not StaffActorContext
            or identity.actor.school_id != m.school_id
            or identity.actor.assignment_id != m.assignment_id
        ):
            raise SchoolFactChangeRejected("请重新验证原工程会话及履职身份。")
        if writing:
            self._require_current(identity.actor)
        return compose_command_context(
            identity.actor,
            uuid5(m.request_id, str(self.grant_id)),
            f"f1-publication:{self.grant_id}",
        )

    def prepare(self, identity: ProductIdentity) -> ConfirmationReceiptV1:
        m = self.manifest
        context = self._context(identity, writing=True)
        binding = self._binding(identity)
        report = self._kernel.preflight(
            school_id=identity.actor.school_id,
            change_set=m.change_set,
            context=context,
            session_binding=binding,
        )
        if not report.is_clean or report.plan_sha256 != m.plan_sha256:
            raise SchoolFactChangeRejected("样本预检未通过。")
        return self._kernel.prepare(
            change_set=m.change_set,
            context=context,
            session_binding=binding,
            provenance=SchoolFactChangeProvenance(
                channel="admin",
                raw_source_sha256=m.change_set.source_sha256,
                template_version="f1_initial_publication_v1",
                mapping_version="f1_initial_publication_v1",
                mapping_config_sha256=m.manifest_sha256,
                source_size_bytes=None,
                worksheet_names=(),
                result_locator_sha256=publication_locator_sha256(m.locator_sha256, self.grant_id),
            ),
        )

    def confirm(
        self, identity: ProductIdentity, receipt: ConfirmationReceiptV1
    ) -> SchoolFactApplyResult:
        if receipt.plan_sha256 != self.manifest.plan_sha256 or self._clock() >= receipt.expires_at:
            raise SchoolFactChangeRejected("确认回执与批准样本不匹配或已经过期。")
        return self._kernel.confirm(
            credential=receipt.credential,
            change_set=self.manifest.change_set,
            context=self._context(identity, writing=True),
            session_binding=self._binding(identity),
        )

    def result(self, identity: ProductIdentity) -> SchoolFactResultLookup:
        return self._kernel.result_by_locator(
            school_id=identity.actor.school_id,
            actor_user_id=identity.actor.account_user_id,
            session_binding=self._binding(identity),
            locator_sha256=publication_locator_sha256(self.manifest.locator_sha256, self.grant_id),
            context=self._context(identity, writing=False),
        )


@dataclass(frozen=True, slots=True)
class PublicationGrantReceipt:
    scope_id: UUID
    grant_id: UUID
    grant_version: int


def configure_initial_publication_grant(
    writer: GovernanceWriter, *, manifest: InitialPublicationManifest
) -> PublicationGrantReceipt:
    m = manifest

    def actor(step: str) -> GovernanceActorContext:
        return GovernanceActorContext(
            school_id=m.school_id,
            actor_kind="platform_bootstrap",
            actor_reference="deployment:campus_ai_migration",
            actor_assignment_id=None,
            actor_assignment_version=None,
            request_id=uuid5(m.request_id, step),
            reason=m.approval_reference,
        )

    duty = writer.create_duty(
        CreateDutyCommand(
            school_id=m.school_id,
            duty_code=PUBLICATION_DUTY,
            display_name="一次性初始事实发布",
            description="仅批准的校区及官方联系，发布后正式终止。",
            effective_from=m.approved_at,
            actor=actor("duty"),
            idempotency_key=f"f1-publication:{m.request_id}:duty",
        )
    )
    scope = writer.create_scope(
        CreateScopeCommand(
            school_id=m.school_id,
            scope_key=m.scope_key,
            schema_owner=PUBLICATION_MODULE,
            schema_name=PUBLICATION_SCHEMA,
            schema_version=1,
            reference_document=m.scope_document,
            reference_sha256=document_sha256(m.scope_document),
            effective_from=m.approved_at,
            actor=actor("scope"),
            idempotency_key=f"f1-publication:{m.request_id}:scope",
        )
    )
    grant = writer.create_grant(
        CreateGrantCommand(
            school_id=m.school_id,
            assignment_id=m.assignment_id,
            duty_id=duty.target_id,
            duty_version=duty.target_version,
            scope_id=scope.target_id,
            scope_version=scope.target_version,
            basis_authority="deployment.approval",
            basis_reference=m.approval_reference,
            valid_from=m.approved_at,
            effective_from=m.approved_at,
            planned_valid_to=m.expires_at,
            actor=actor("grant"),
            idempotency_key=f"f1-publication:{m.request_id}:grant",
        )
    )
    return PublicationGrantReceipt(scope.target_id, grant.target_id, grant.target_version)


def _publication_scope(
    session: Session, *, school_id: UUID, initialization_id: UUID
) -> tuple[UUID, PublicationScopeDocument]:
    row = (
        session.execute(
            text(
                "SELECT s.id, s.current_version, v.schema_owner, v.schema_name, "
                "v.schema_version, v.reference_document, v.reference_sha256 FROM "
                "public.governance_scopes s JOIN public.governance_scope_versions v ON "
                "v.school_id=s.school_id AND v.scope_id=s.id AND v.version=1 WHERE "
                "s.school_id=:school_id AND s.scope_key=:scope_key"
            ),
            {"school_id": school_id, "scope_key": f"f1:initial-publication:{initialization_id}"},
        )
        .mappings()
        .one_or_none()
    )
    if (
        row is None
        or row["current_version"] != 1
        or row["schema_owner"] != PUBLICATION_MODULE
        or row["schema_name"] != PUBLICATION_SCHEMA
        or row["schema_version"] != 1
    ):
        raise SchoolFactChangeRejected("缺少固定样本发布批准记录。")
    raw = row["reference_document"]
    if (
        not isinstance(raw, dict)
        or document_sha256(cast(dict[str, object], raw)) != row["reference_sha256"]
    ):
        raise SchoolFactChangeRejected("样本批准记录摘要不匹配。")
    try:
        doc = PublicationScopeDocument.model_validate_json(json.dumps(raw))
    except (TypeError, ValueError) as exc:
        raise SchoolFactChangeRejected("样本批准记录格式无效。") from exc
    if doc.school_id != school_id or doc.initialization_id != initialization_id:
        raise SchoolFactChangeRejected("样本批准记录不属于本次初始化。")
    return UUID(str(row["id"])), doc


@dataclass(frozen=True, slots=True)
class PublicationGrantSnapshot:
    grant_id: UUID
    current_version: int
    valid_from: datetime
    planned_valid_to: datetime
    transition_kind: str
    effective_from: datetime
    formal_ended_at: datetime | None
    revoked_at: datetime | None

    @property
    def is_terminal(self) -> bool:
        return (
            self.transition_kind in {"end", "revoke"}
            and self.effective_from <= datetime.now(UTC)
            and (self.formal_ended_at is not None or self.revoked_at is not None)
        )


@dataclass(frozen=True, slots=True)
class InitialPublicationSnapshot:
    scope_id: UUID
    duty_id: UUID
    duty_version: int
    grants: tuple[PublicationGrantSnapshot, ...]
    applied_batch_id: UUID | None


def _inspect_scope(
    session: Session, *, scope_id: UUID, doc: PublicationScopeDocument
) -> InitialPublicationSnapshot:
    duty = (
        session.execute(
            text(
                "SELECT id, current_version FROM public.governance_duties "
                "WHERE school_id=:school_id AND duty_code=:duty"
            ),
            {"school_id": doc.school_id, "duty": PUBLICATION_DUTY},
        )
        .mappings()
        .one_or_none()
    )
    if duty is None:
        raise SchoolFactChangeRejected("发布职责字典缺失。")
    duty_id = UUID(str(duty["id"]))
    rows = (
        session.execute(
            text(
                "SELECT g.id AS grant_id,g.current_version,g.assignment_id,g.duty_id,"
                "d.duty_code,v.duty_version,v.scope_version,v.valid_from,v.planned_valid_to,"
                "v.transition_kind,v.effective_from,v.formal_ended_at,v.revoked_at "
                "FROM public.governance_grants g "
                "JOIN public.governance_duties d ON d.school_id=g.school_id AND d.id=g.duty_id "
                "JOIN public.governance_grant_versions v ON v.school_id=g.school_id "
                "AND v.grant_id=g.id AND v.version=g.current_version "
                "WHERE g.school_id=:school_id AND EXISTS "
                "(SELECT 1 FROM public.governance_grant_versions original "
                "WHERE original.school_id=g.school_id AND original.grant_id=g.id "
                "AND original.scope_id=:scope_id) ORDER BY g.id"
            ),
            {"school_id": doc.school_id, "scope_id": scope_id},
        )
        .mappings()
        .all()
    )
    grants: list[PublicationGrantSnapshot] = []
    for row in rows:
        start, end = row["valid_from"], row["planned_valid_to"]
        if (
            row["assignment_id"] != doc.assignment_id
            or row["duty_id"] != duty_id
            or row["duty_code"] != PUBLICATION_DUTY
            or row["scope_version"] != 1
            or type(start) is not datetime
            or type(end) is not datetime
            or not timedelta(0) < end - start <= MAX_APPROVAL_WINDOW
            or row["transition_kind"] not in {"create", "end", "revoke"}
        ):
            raise SchoolFactChangeRejected("发布职责窗口或固定范围不匹配。")
        grants.append(
            PublicationGrantSnapshot(
                UUID(str(row["grant_id"])),
                int(row["current_version"]),
                start,
                end,
                str(row["transition_kind"]),
                row["effective_from"],
                row["formal_ended_at"],
                row["revoked_at"],
            )
        )
    if len({g.grant_id for g in grants}) != len(grants):
        raise SchoolFactChangeRejected("发布职责记录重复。")
    batches = (
        session.execute(
            text(
                "SELECT id,status,target_fact_version,result_payload,confirmed_at,"
                "confirmed_by_assignment_id,confirmed_by_user_id,confirmation_consumed_at,"
                "planned_units,succeeded_units,failed_units,pending_units,"
                "mapping_payload->>'result_locator_sha256' AS locator "
                "FROM public.master_data_import_batches WHERE school_id=:school_id "
                "AND initialization_id IS NULL AND mode='canonical_atomic_changes' "
                "AND plan_sha256=:plan AND source_sha256=:source "
                "AND mapping_payload->>'mapping_config_sha256'=:manifest"
            ),
            {
                "school_id": doc.school_id,
                "plan": doc.canonical_plan_sha256,
                "source": doc.source_sha256,
                "manifest": doc.publication_manifest_sha256,
            },
        )
        .mappings()
        .all()
    )
    windows = {publication_locator_sha256(doc.result_locator_sha256, g.grant_id): g for g in grants}
    applied: list[UUID] = []
    for batch in batches:
        window = windows.get(str(batch["locator"]))
        if window is None or batch["status"] not in {"ready_for_confirmation", "applied"}:
            raise SchoolFactChangeRejected("发布结果未知或不属于已批准职责窗口。")
        if batch["status"] == "ready_for_confirmation":
            continue
        raw_payload, confirmed = batch["result_payload"], batch["confirmed_at"]
        if not isinstance(raw_payload, dict):
            raise SchoolFactChangeRejected("正式样本发布回执内容不完整。")
        payload = cast(dict[str, object], raw_payload)
        if (
            payload.get("changed_count") != 2
            or type(payload.get("target_version")) is not int
            or payload["target_version"] != batch["target_fact_version"]
            or type(confirmed) is not datetime
            or not window.valid_from <= confirmed < window.planned_valid_to
            or batch["confirmed_by_assignment_id"] != doc.assignment_id
            or batch["confirmed_by_user_id"] is None
            or batch["confirmation_consumed_at"] is None
            or (
                batch["planned_units"],
                batch["succeeded_units"],
                batch["failed_units"],
                batch["pending_units"],
            )
            != (2, 2, 0, 0)
        ):
            raise SchoolFactChangeRejected("正式样本发布回执内容不完整。")
        batch_id = UUID(str(batch["id"]))
        facts = (
            session.execute(
                text(
                    "SELECT fact_kind,operation,target_version "
                    "FROM public.school_fact_change_lineage "
                    "WHERE school_id=:school_id AND batch_id=:batch_id"
                ),
                {"school_id": doc.school_id, "batch_id": batch_id},
            )
            .mappings()
            .all()
        )
        if (
            len(facts) != 2
            or {row["fact_kind"] for row in facts} != {"location", "official_contact"}
            or any(row["operation"] != "upsert" or row["target_version"] != 1 for row in facts)
        ):
            raise SchoolFactChangeRejected("样本正式血缘与最小范围不匹配。")
        applied.append(batch_id)
    if len(applied) > 1:
        raise SchoolFactChangeRejected("样本发布存在多个成功回执。")
    return InitialPublicationSnapshot(
        scope_id,
        duty_id,
        int(duty["current_version"]),
        tuple(grants),
        applied[0] if applied else None,
    )


def inspect_initial_publication(
    session: Session, *, manifest: InitialPublicationManifest
) -> InitialPublicationSnapshot:
    """Missing execution is explicit; inaccessible or damaged authority always raises."""
    scope_id, doc = _publication_scope(
        session, school_id=manifest.school_id, initialization_id=manifest.initialization_id
    )
    if doc.model_dump(mode="json") != manifest.scope_document:
        raise SchoolFactChangeRejected("只能恢复原已批准的精确事实计划。")
    return _inspect_scope(session, scope_id=scope_id, doc=doc)


def _publication_batch(session: Session, *, doc: PublicationScopeDocument) -> UUID:
    scope_id, _ = _publication_scope(
        session, school_id=doc.school_id, initialization_id=doc.initialization_id
    )
    result = _inspect_scope(session, scope_id=scope_id, doc=doc)
    if result.applied_batch_id is None:
        raise SchoolFactChangeRejected("尚无明确成功的样本发布回执。")
    return result.applied_batch_id


def verify_initial_publication_absent(
    session: Session, *, school_id: UUID, initialization_id: UUID
) -> None:
    """Prove no initial publication has ever started for this school; unknown fails closed.

    The caller holds the Governance bootstrap state lock during terminal mutation.
    Historical scope versions and every batch status count as publication evidence.
    """
    absent = session.scalar(
        text(
            "SELECT NOT ("
            "EXISTS (SELECT 1 FROM public.governance_scopes s WHERE "
            "s.school_id=:school_id AND (s.scope_key=:scope_key OR EXISTS "
            "(SELECT 1 FROM public.governance_scope_versions v WHERE "
            "v.school_id=s.school_id AND v.scope_id=s.id AND v.schema_owner=:owner))) "
            "OR EXISTS (SELECT 1 FROM public.governance_duties d WHERE "
            "d.school_id=:school_id AND d.duty_code=:duty) "
            "OR EXISTS (SELECT 1 FROM public.governance_grants g WHERE "
            "g.school_id=:school_id AND (EXISTS (SELECT 1 FROM "
            "public.governance_duties d WHERE d.school_id=g.school_id AND "
            "d.id=g.duty_id AND d.duty_code=:duty) OR EXISTS (SELECT 1 FROM "
            "public.governance_grant_versions gv JOIN public.governance_scopes s "
            "ON s.school_id=gv.school_id AND s.id=gv.scope_id WHERE "
            "gv.school_id=g.school_id AND gv.grant_id=g.id AND "
            "(s.scope_key=:scope_key OR EXISTS (SELECT 1 FROM "
            "public.governance_scope_versions sv WHERE sv.school_id=s.school_id "
            "AND sv.scope_id=s.id AND sv.schema_owner=:owner))))) "
            "OR EXISTS (SELECT 1 FROM public.master_data_import_batches b WHERE "
            "b.school_id=:school_id AND ("
            "b.mapping_payload->>'template_version' LIKE '%initial_publication%' OR "
            "b.mapping_payload->>'mapping_version' LIKE '%initial_publication%')))"
        ),
        {
            "school_id": school_id,
            "scope_key": f"f1:initial-publication:{initialization_id}",
            "owner": PUBLICATION_MODULE,
            "duty": PUBLICATION_DUTY,
        },
    )
    if absent is not True:
        raise SchoolFactChangeRejected("首条发布已开始或状态未知，不能延后关闭。")


def verify_initial_publication_closed(
    session: Session, *, request: SchoolFactInitializationRequest, initialization_id: UUID
) -> UUID:
    """Fixed initializer gate includes every historical grant, without caller-selected scope."""
    scope_id, doc = _publication_scope(
        session, school_id=request.school_id, initialization_id=initialization_id
    )
    result = _inspect_scope(session, scope_id=scope_id, doc=doc)
    if (
        result.applied_batch_id is None
        or not result.grants
        or any(not grant.is_terminal for grant in result.grants)
    ):
        raise SchoolFactChangeRejected("样本未正式发布或仍有未终止的临时职责。")
    return result.applied_batch_id


class InitialPublicationClosureAuthorizer(MigrationGovernanceBootstrapAuthorizer):
    """One terminal command for one published grant; never a general Governance admin."""

    def __init__(
        self, *, manifest: InitialPublicationManifest, grant: PublicationGrantReceipt
    ) -> None:
        self._manifest, self._grant = manifest, grant

    def authorize(self, request: GovernanceWriteAuthorizationRequest) -> bool:
        m, g, actor = self._manifest, self._grant, request.actor
        return (
            request.school_id == m.school_id
            and request.command_name == "governance.grant.end"
            and request.target_kind == "grant"
            and request.target_id == g.grant_id
            and actor.school_id == m.school_id
            and actor.actor_kind == "platform_bootstrap"
            and actor.actor_reference == "deployment:campus_ai_migration"
            and actor.actor_assignment_id is None
            and actor.actor_assignment_version is None
            and actor.reason == m.approval_reference
        )

    def authorize_session(
        self, repository: GovernanceRepository, request: GovernanceWriteAuthorizationRequest
    ) -> None:
        super().authorize_session(repository, request)
        if not self.authorize(request):
            raise SchoolFactChangeRejected("临时发布终止仅限固定批准的Grant。")
        # Narrow composition TCB: same UoW Session, no separate transaction or mutable callback.
        session = repository._session  # pyright: ignore[reportPrivateUsage]
        epoch = session.scalar(
            text(
                "SELECT epoch FROM public.governance_assignment_epochs WHERE "
                "school_id=:school_id AND assignment_id=:assignment_id FOR UPDATE"
            ),
            {"school_id": self._manifest.school_id, "assignment_id": self._manifest.assignment_id},
        )
        if type(epoch) is not int:
            raise SchoolFactChangeRejected("临时发布终止缺少当前授权版本。")

    def _published(self, repository: GovernanceRepository) -> None:
        m, g = self._manifest, self._grant
        session = repository._session  # pyright: ignore[reportPrivateUsage]
        scope_id, doc = _publication_scope(
            session, school_id=m.school_id, initialization_id=m.initialization_id
        )
        if scope_id != g.scope_id or doc.model_dump(mode="json") != m.scope_document:
            raise SchoolFactChangeRejected("临时发布终止批准记录不匹配。")
        _publication_batch(session, doc=doc)
        grant = repository.lock_grant(m.school_id, g.grant_id)
        version = repository.grant_version(m.school_id, g.grant_id, 1)
        if (
            grant is None
            or version is None
            or grant.assignment_id != m.assignment_id
            or version.scope_id != g.scope_id
            or version.scope_version != 1
            or version.planned_valid_to is None
            or not timedelta(0)
            < version.planned_valid_to - version.valid_from
            <= MAX_APPROVAL_WINDOW
        ):
            raise SchoolFactChangeRejected("临时发布终止Grant不属于此批准范围。")
        duty = session.scalar(
            text(
                "SELECT duty_code FROM public.governance_duties WHERE school_id=:school_id "
                "AND id=:duty_id"
            ),
            {"school_id": m.school_id, "duty_id": grant.duty_id},
        )
        if duty != PUBLICATION_DUTY:
            raise SchoolFactChangeRejected("临时发布不得终止日常职责。")

    def authorize_mutation(
        self, repository: GovernanceRepository, request: GovernanceWriteAuthorizationRequest
    ) -> None:
        super().authorize_mutation(repository, request)
        self._published(repository)

    def authorize_replay(
        self, repository: GovernanceRepository, request: GovernanceWriteAuthorizationRequest
    ) -> None:
        super().authorize_replay(repository, request)
        self._published(repository)


def end_initial_publication_grant(
    *,
    session_factory: sessionmaker[Session],
    manifest: InitialPublicationManifest,
    grant: PublicationGrantReceipt,
    dependency_port: GovernanceDependencyPort,
) -> GovernanceWriteResult:
    """Own the exact end command and its fixed Authorizer; result-unknown is not success."""
    m = manifest
    with session_factory() as session:
        scope_id, doc = _publication_scope(
            session, school_id=m.school_id, initialization_id=m.initialization_id
        )
        if scope_id != grant.scope_id or doc.model_dump(mode="json") != m.scope_document:
            raise SchoolFactChangeRejected("临时发布批准与终止回执不匹配。")
        batch_id = _publication_batch(session, doc=doc)
        ended_at = session.scalar(
            text(
                "SELECT confirmed_at FROM public.master_data_import_batches WHERE "
                "school_id=:school_id AND id=:batch_id"
            ),
            {"school_id": m.school_id, "batch_id": batch_id},
        )
        if type(ended_at) is not datetime:
            raise SchoolFactChangeRejected("发布回执缺少实际确认时间。")
    authorizer = InitialPublicationClosureAuthorizer(manifest=m, grant=grant)
    writer = GovernanceWriter(
        session_factory=session_factory,
        authorizer=authorizer,
        transaction_authorizer=authorizer,
        clock=lambda: datetime.now(UTC),
        dependency_port=dependency_port,
        scope_owner_resolvers={PUBLICATION_MODULE: InitialPublicationScopeResolver()},
    )
    return writer.end_grant(
        EndGrantCommand(
            school_id=m.school_id,
            grant_id=grant.grant_id,
            expected_version=1,
            effective_at=ended_at,
            actor=GovernanceActorContext(
                school_id=m.school_id,
                actor_kind="platform_bootstrap",
                actor_reference="deployment:campus_ai_migration",
                actor_assignment_id=None,
                actor_assignment_version=None,
                request_id=uuid5(m.request_id, "end"),
                reason=m.approval_reference,
            ),
            idempotency_key=f"f1-publication:{m.request_id}:end",
        )
    )
