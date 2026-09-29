from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from threading import RLock
from typing import Annotated, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, StringConstraints

from app.composition.school_facts import InMemorySchoolFactPlanStore
from app.platform.contracts import FrozenContractModel, StaffActorContext
from app.platform.identity.execution_context import compose_command_context
from app.platform.master_data_imports.ports import PrivateSourceOwner
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.change_boundary import SchoolFactChangeBoundary
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    CanonicalFactChangeV1,
    CanonicalFactContentV1,
    FactOriginV1,
)
from app.platform.school_facts.ports import SchoolFactChangeProvenance
from app.platform.school_facts.read_policy import OfficialContactFact, OfficialContactQueryResult

_Text = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1000)
]
_Ref = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_:-]{8,128}$")]


class ContactChangeInput(FrozenContractModel):
    contact_ref: _Ref
    contact_value: _Text
    service_hours: _Text | None = Field(
        description=(
            "实际服务时间文本或null；用户未要求变更时，原样传入最新读取的service_hours"
            "（包括null）。未知或未录入用null，不把「保持原样」「不填」等操作说明写成事实值。"
        )
    )
    reason: _Text


class ContactSource(FrozenContractModel):
    kind: str
    label: str
    published_at: AwareDatetime | None
    reference: str | None


class ContactCard(FrozenContractModel):
    reference: str
    campus_ref: str
    campus_name: str
    contact_kind: str
    contact_value: str
    service_hours: str | None
    valid_from: AwareDatetime
    valid_to: AwareDatetime | None
    version: int
    source: ContactSource


class ContactAfter(FrozenContractModel):
    contact_value: str
    service_hours: str | None


class ContactSummary(FrozenContractModel):
    before: ContactCard
    after: ContactAfter
    reason: str


class ContactProposal(FrozenContractModel):
    proposal_ref: str
    summary: ContactSummary
    expires_at: AwareDatetime


class ContactReceipt(FrozenContractModel):
    reference: str
    status: Literal["succeeded", "rejected", "unknown"]
    message: str
    changed_count: int | None = None
    target_version: int | None = None
    request_id: str | None = None


class ContactCoverage(FrozenContractModel):
    status: Literal["unknown", "partial", "complete"]
    description: str


class ContactCandidate(FrozenContractModel):
    reference: str
    label: str
    path: str


class ContactQuerySelection(FrozenContractModel):
    subject: Literal["official_contacts", "campuses"]
    scope: Literal["selected", "all_authorized"]
    campus_name: str | None
    campus_ref: str | None


class ContactListing(FrozenContractModel):
    entity: Literal["contacts", "campuses"]
    returned_count: int = Field(ge=0, le=100)
    matched_count: int | None = Field(ge=0)
    truncated: bool


class ContactQueryView(FrozenContractModel):
    status: Literal[
        "known",
        "display_limited",
        "not_recorded",
        "no_current_records",
        "selection_required",
        "unmatched",
        "ambiguous",
        "source_unavailable",
        "denied",
    ]
    items: tuple[ContactCard, ...] = ()
    candidates: tuple[ContactCandidate, ...] = ()
    resolved_campuses: tuple[ContactCandidate, ...] = ()
    query: ContactQuerySelection
    listing: ContactListing
    coverage: ContactCoverage
    queried_at: AwareDatetime
    display_notice: str | None = None


def contact_card(fact: OfficialContactFact) -> ContactCard:
    return ContactCard(
        reference=fact.fact_ref,
        campus_ref=fact.campus_ref,
        campus_name=fact.campus_name,
        contact_kind=fact.contact_kind,
        contact_value=fact.contact_value,
        service_hours=fact.service_hours,
        valid_from=fact.valid_from,
        valid_to=fact.valid_to,
        version=fact.version,
        source=ContactSource(
            kind=fact.source_kind,
            label=fact.source_description,
            published_at=fact.published_at,
            reference=fact.publication_ref,
        ),
    )


def contact_query_view(result: OfficialContactQueryResult) -> ContactQueryView:
    campuses = tuple(
        ContactCandidate(
            reference=item.campus_ref,
            label=item.display_name,
            path=item.display_path,
        )
        for item in result.campuses
    )
    needs_selection = result.status in {"ambiguous", "selection_required"}
    is_campus_listing = result.query.subject == "campuses" or needs_selection
    return ContactQueryView(
        status="denied" if result.status == "forbidden" else result.status,
        items=tuple(contact_card(item) for item in result.contacts),
        candidates=campuses if needs_selection else (),
        resolved_campuses=() if needs_selection else campuses,
        query=ContactQuerySelection(
            subject=result.query.subject,
            scope=result.query.scope,
            campus_name=result.query.campus_name,
            campus_ref=result.query.campus_ref,
        ),
        listing=ContactListing(
            entity="campuses" if is_campus_listing else "contacts",
            returned_count=len(campuses) if is_campus_listing else len(result.contacts),
            matched_count=result.matched_count,
            truncated=result.has_more,
        ),
        coverage=ContactCoverage(status=result.coverage, description=result.query_scope),
        queried_at=result.queried_at,
    )


def model_contact_query_view(result: ContactQueryView) -> ContactQueryView:
    """Publish one bounded ordered projection for both model input and its fact card."""
    items = list(result.items[:10])
    candidates = list(result.candidates[:10])
    owners = {item.campus_ref for item in items}
    resolved = [campus for campus in result.resolved_campuses if campus.reference in owners]
    resolved.extend(campus for campus in result.resolved_campuses if campus.reference not in owners)
    resolved = resolved[:10]
    shortened = (
        len(items) < len(result.items)
        or len(candidates) < len(result.candidates)
        or len(resolved) < len(result.resolved_campuses)
    )
    while True:
        listed = (
            items
            if result.listing.entity == "contacts"
            else candidates
            if result.status in {"ambiguous", "selection_required"}
            else resolved
        )
        display_limited = shortened and not (items or candidates or resolved)
        projected = result.model_copy(
            update={
                "status": "display_limited" if display_limited else result.status,
                "items": tuple(items),
                "candidates": tuple(candidates),
                "resolved_campuses": tuple(resolved),
                "listing": result.listing.model_copy(
                    update={
                        "returned_count": len(listed),
                        "truncated": result.listing.truncated or shortened,
                    }
                ),
                "display_notice": (
                    "结果存在，但单项内容过大，当前对话无法显示；请缩小查询范围或使用查询页面核对。"
                    if display_limited
                    else "对话最多显示10项；结果已缩减，请缩小范围查看。匹配数量仍按原范围核对。"
                    if shortened
                    else result.display_notice
                ),
            }
        )
        if (
            len(
                json.dumps(
                    projected.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
            <= 12_000
        ):
            return projected
        shortened = True
        owners = {item.campus_ref for item in items}
        extra = next(
            (
                index
                for index in reversed(range(len(resolved)))
                if resolved[index].reference not in owners
            ),
            None,
        )
        if result.listing.entity == "contacts" and extra is not None:
            resolved.pop(extra)
        elif items:
            items.pop()
            owners = {item.campus_ref for item in items}
            resolved = [campus for campus in resolved if campus.reference in owners]
        elif candidates:
            candidates.pop()
        elif resolved:
            resolved.pop()
        else:
            # Fixed query metadata is small. Do not relabel a source/authorization error
            # if an invalid oversized envelope reaches this product adapter.
            raise ContactProductUnavailable("fixed query metadata exceeds the display bound")


class ContactProductUnavailable(RuntimeError):
    """Safe candidate failure. Does not contain source keys, credentials or database text."""


class MaintenanceTarget(Protocol):
    @property
    def source_key(self) -> str: ...
    @property
    def school_fact_version(self) -> int: ...
    @property
    def version(self) -> int: ...
    @property
    def content(self) -> CanonicalFactContentV1: ...
    @property
    def public_fact(self) -> OfficialContactFact: ...


class TargetResolver(Protocol):
    def __call__(self, *, actor: StaffActorContext, fact_ref: str) -> MaintenanceTarget: ...


@dataclass(slots=True)
class _Confirmation:
    owner: PrivateSourceOwner
    actor_binding: tuple[object, ...]
    credential: str
    idempotency_key: str
    expires_at: datetime
    invoked: bool = False
    rejected: bool = False


class OfficialContactProductService:
    """Narrow maintenance consumer of the existing sole school-fact Writer.

    Plans/credentials are temporary server state. Durable result lookup always goes to the
    Writer's existing locator and can survive loss of this object's in-memory state.
    """

    def __init__(
        self,
        *,
        boundary: SchoolFactChangeBoundary,
        target_resolver: TargetResolver,
        require_maintenance: Callable[[StaffActorContext], object],
        plan_store: InMemorySchoolFactPlanStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._boundary = boundary
        self._resolve = target_resolver
        self._require = require_maintenance
        self._plans = plan_store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._confirmations: dict[str, _Confirmation] = {}
        self._lock = RLock()

    @staticmethod
    def _owner(actor: StaffActorContext, binding: str) -> PrivateSourceOwner:
        if not binding or len(binding) > 2000:
            raise ContactProductUnavailable("invalid session binding")
        return PrivateSourceOwner(
            school_id=str(actor.school_id),
            actor_user_id=actor.account_user_id,
            session_sha256=sha256(binding.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _actor_binding(actor: StaffActorContext) -> tuple[object, ...]:
        return (
            actor.person_id,
            actor.assignment_id,
            actor.assignment_version,
            actor.authorization_version,
        )

    def prepare(
        self,
        *,
        actor: StaffActorContext,
        session_binding: str,
        payload: ContactChangeInput,
        request_id: UUID,
    ) -> ContactProposal:
        self._require(actor)
        owner = self._owner(actor, session_binding)
        target = self._resolve(actor=actor, fact_ref=payload.contact_ref)
        now = self._clock()
        if target.public_fact.valid_to is not None:
            # The approved narrow edit cannot silently alter an existing scheduled ending.
            raise ContactProductUnavailable("this contact requires time-aware maintenance")
        content = target.content.model_copy(
            update={"contact_value": payload.contact_value, "service_hours": payload.service_hours}
        )
        material = json.dumps(
            {
                "input": payload.model_dump(mode="json"),
                "request": str(request_id),
                "effective_at": now.isoformat(),
            },
            sort_keys=True,
            ensure_ascii=True,
        )
        digest = sha256(material.encode("utf-8")).hexdigest()
        change_set = CanonicalFactChangeSetV1(
            source_sha256=digest,
            base_version=target.school_fact_version,
            changes=(
                CanonicalFactChangeV1(
                    operation="upsert",
                    fact_kind="official_contact",
                    source_key=target.source_key,
                    expected_version=target.version,
                    effective_at=now,
                    origin=FactOriginV1(sheet_code="official_contact_maintenance", row_number=1),
                    content=content,
                ),
            ),
        )
        key = f"official-contact:{uuid4()}"
        context = compose_command_context(actor, request_id, key)
        report = self._boundary.preflight(
            school_id=actor.school_id,
            change_set=change_set,
            context=context,
            session_binding=session_binding,
        )
        if not report.is_clean:
            raise ContactProductUnavailable("the current fact cannot be changed by this proposal")
        provenance = SchoolFactChangeProvenance(
            channel="admin",
            raw_source_sha256=digest,
            template_version="official_contact_v1",
            mapping_version="official_contact_v1",
            mapping_config_sha256=sha256(b"official_contact:two_field_change:v1").hexdigest(),
            source_size_bytes=None,
            worksheet_names=(),
        )
        record = self._plans.put(
            owner=owner, change_set=change_set, provenance=provenance, preflight=report
        )
        try:
            self._require(actor)
            confirmation = self._boundary.prepare_confirmation(
                change_set=change_set,
                context=context,
                session_binding=session_binding,
                provenance=record.provenance,
            )
            with self._lock:
                self.purge_expired()
                if len(self._confirmations) >= 128:
                    raise ContactProductUnavailable("temporary proposal capacity reached")
                self._confirmations[record.plan_ref] = _Confirmation(
                    owner,
                    self._actor_binding(actor),
                    confirmation.credential,
                    key,
                    confirmation.expires_at,
                )
        except Exception:
            self._plans.delete(record.plan_ref, owner=owner)
            raise
        return ContactProposal(
            proposal_ref=record.plan_ref,
            expires_at=confirmation.expires_at,
            summary=ContactSummary(
                before=contact_card(target.public_fact),
                after=ContactAfter(
                    contact_value=payload.contact_value, service_hours=payload.service_hours
                ),
                reason=payload.reason,
            ),
        )

    def confirm(
        self,
        *,
        actor: StaffActorContext,
        session_binding: str,
        proposal_ref: str,
        request_id: UUID,
    ) -> ContactReceipt:
        self._require(actor)
        owner = self._owner(actor, session_binding)
        with self._lock:
            self.purge_expired()
            entry = self._confirmations.get(proposal_ref)
            if (
                entry is None
                or entry.owner != owner
                or entry.actor_binding != self._actor_binding(actor)
            ):
                raise ContactProductUnavailable(
                    "proposal expired or inaccessible; query its receipt"
                )
            if entry.invoked:
                return self.receipt(
                    actor=actor,
                    session_binding=session_binding,
                    proposal_ref=proposal_ref,
                    request_id=request_id,
                )
            record = self._plans.get(proposal_ref, owner=owner)
            entry.invoked = True
        try:
            result = self._boundary.confirm(
                credential=entry.credential,
                change_set=record.change_set,
                context=compose_command_context(actor, request_id, entry.idempotency_key),
                session_binding=session_binding,
            )
        except SchoolFactChangeRejected:
            with self._lock:
                entry.rejected = True
            return ContactReceipt(
                reference=proposal_ref,
                status="rejected",
                message="正式修改被拒绝，请重新读取事实并预检。",
                request_id=str(request_id),
            )
        except Exception:
            return ContactReceipt(
                reference=proposal_ref,
                status="unknown",
                message="写入结果尚未查明，请用此回执引用核对；不要重复新建修改。",
                request_id=str(request_id),
            )
        finally:
            entry.credential = ""
        self._plans.retire(proposal_ref, owner=owner)
        return ContactReceipt(
            reference=proposal_ref,
            status="succeeded",
            message="正式修改已写入并留存审计。",
            changed_count=result.changed_count,
            target_version=result.target_version,
            request_id=str(request_id),
        )

    def require_receipt_access(self, actor: StaffActorContext) -> None:
        """Recheck the receipt capability without looking up or executing a Writer result."""
        self._require(actor)

    def require_invoked(
        self, *, actor: StaffActorContext, session_binding: str, proposal_ref: str
    ) -> None:
        """A prepared locator alone does not prove a formal confirmation occurred."""
        self.require_receipt_access(actor)
        owner = self._owner(actor, session_binding)
        with self._lock:
            self.purge_expired()
            entry = self._confirmations.get(proposal_ref)
            if (
                entry is None or not entry.invoked or entry.owner != owner
                or entry.actor_binding != self._actor_binding(actor)
            ):
                raise ContactProductUnavailable("original operation was not invoked")

    def receipt(
        self,
        *,
        actor: StaffActorContext,
        session_binding: str,
        proposal_ref: str,
        request_id: UUID,
    ) -> ContactReceipt:
        self.require_receipt_access(actor)
        owner = self._owner(actor, session_binding)
        if not proposal_ref.startswith("sf3plan_") or len(proposal_ref) > 128:
            raise ContactProductUnavailable("invalid receipt reference")
        result = self._boundary.check_result_by_locator(
            school_id=actor.school_id,
            actor_user_id=actor.account_user_id,
            session_binding=session_binding,
            locator_sha256=sha256(proposal_ref.encode("ascii")).hexdigest(),
            context=compose_command_context(
                actor, request_id, f"official-contact-result:{request_id}"
            ),
        )
        if not result.found:
            raise ContactProductUnavailable("receipt inaccessible")
        if result.result is None:
            with self._lock:
                self.purge_expired()
                entry = self._confirmations.get(proposal_ref)
                rejected = (
                    entry is not None and entry.invoked and entry.rejected
                    and entry.owner == owner and entry.actor_binding == self._actor_binding(actor)
                )
            if rejected:
                return ContactReceipt(
                    reference=proposal_ref, status="rejected",
                    message="原正式修改已明确被拒绝。",
                )
            return ContactReceipt(
                reference=proposal_ref,
                status="unknown",
                message="权威回执尚未表明已完成；此查询不会再次执行修改。",
            )
        return ContactReceipt(
            reference=proposal_ref,
            status="succeeded",
            message="已从权威写入回执查明正式修改完成。",
            changed_count=result.result.changed_count,
            target_version=result.result.target_version,
        )

    def purge_expired(self, *, now: datetime | None = None) -> int:
        now = now or self._clock()
        with self._lock:
            expired = [key for key, item in self._confirmations.items() if item.expires_at <= now]
            for key in expired:
                self._confirmations.pop(key).credential = ""
            return len(expired)
