"""F3 composition reuses formal Reader connection phases and the unique Writer boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import JsonValue
from sqlalchemy.orm import Session, sessionmaker

from app.composition.school_fact_directory_services import DirectoryReadComponents
from app.composition.school_fact_f2_query_repository import F2FactQueryRepository
from app.composition.school_fact_family_authority import (
    F3OwnResultAuthorizer,
    FamilyAuthorityComponents,
    FamilyFactAuthorityDenied,
    create_family_authority_components,
)
from app.composition.school_fact_family_product_adapter import (
    ExternalFamilyReferencePort,
    FamilyStaffProductQuery,
    SchoolFactFamilyProductAdapter,
)
from app.composition.school_fact_family_settings import FamilyConfiguration
from app.composition.school_fact_family_staff_reader import (
    F3EvidenceBackend,
    FamilyStaffRepository,
    SqlFamilyStaffProjection,
)
from app.composition.school_fact_read_connection import F2ConnectionReadExecutor
from app.platform.authorization.secure_reader_contracts import SecureReadDenied
from app.platform.contracts import StaffActorContext
from app.platform.identity.execution_context import compose_command_context
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1


class DirectoryFamilyReferences(ExternalFamilyReferencePort):
    def __init__(self, directory: F2FactQueryRepository, *, evidence=None) -> None:
        self.directory = directory
        self.evidence = evidence

    def choices(self, *, actor: StaffActorContext, fact_kind: str):
        from app.composition.school_fact_directory_adapter import DirectoryProductAdapter
        from app.composition.school_fact_directory_product import FactQueryInput

        self._kind(fact_kind)
        if self.evidence is None:
            raise PermissionError("关联候选缺少当前读取依据。")
        with self.evidence.capture(actor=actor, current_time=True) as capture:
            result = DirectoryProductAdapter(self.directory, evidence=self.evidence).query(
                actor=actor,
                payload=FactQueryInput(fact_kind=fact_kind, scope="all_authorized", page_size=100),
                bounded_pages=False,
            )
            proof = capture.seal()
        if proof is None or proof.check(actor) is not True:
            raise PermissionError("关联候选读取依据已变化。")
        return result

    @staticmethod
    def _kind(kind: str) -> None:
        if kind not in {"location", "organization"}:
            raise ValueError("Unapproved external family reference kind")

    def reference_for(self, *, actor: StaffActorContext, fact_kind: str, source_key: str) -> str:
        self._kind(fact_kind)
        return self.directory.reference_for(actor=actor, fact_kind=fact_kind, source_key=source_key)

    def resolve(self, *, actor: StaffActorContext, fact_kind: str, reference: str) -> str:
        self._kind(fact_kind)
        result = self.directory.query(
            actor=actor, fact_kind=fact_kind, reference=reference, page_size=2
        )
        if result.status == "forbidden":
            raise SecureReadDenied("当前身份无权选择这个关联对象。")
        if result.status != "known" or len(result.records) != 1:
            raise ValueError("关联对象当前不可确认，请重新选择。")
        return self.directory.source_for(actor=actor, fact_kind=fact_kind, reference=reference)

    def location_name(
        self, *, actor: StaffActorContext, reference: str, as_of: datetime | None
    ) -> dict[str, JsonValue]:
        result = self.directory.query(
            actor=actor,
            fact_kind="location",
            reference=reference,
            factual_at=as_of,
            page_size=2,
        )
        if result.status != "known" or len(result.records) != 1 or result.has_more:
            return {"status": result.status if result.status != "known" else "conflict"}
        record = result.records[0]
        name = record.content.get("display_name")
        return {
            "status": "known" if isinstance(name, str) and name else "not_returned",
            "name": name if isinstance(name, str) else None,
            "reference": record.fact_ref,
            "version": record.version,
            "valid_from": record.valid_from.isoformat(),
            "valid_to": record.valid_to.isoformat() if record.valid_to else None,
            "source": {
                "label": "学校正式地点资料",
                "reference": record.publication_ref,
                "published_at": record.published_at.isoformat(),
            },
        }


@dataclass(frozen=True, slots=True)
class FamilyStaffComponents:
    authority: FamilyAuthorityComponents
    configuration: FamilyConfiguration
    repository: FamilyStaffRepository
    adapter: SchoolFactFamilyProductAdapter
    query: FamilyStaffProductQuery

    def require_maintenance(
        self, actor: StaffActorContext, change_set: CanonicalFactChangeSetV1
    ) -> None:
        try:
            self.authority.require_change_set(actor, change_set, checked_at=datetime.now(UTC))
        except FamilyFactAuthorityDenied:
            raise PermissionError("当前身份没有这项家校资料修改权限。") from None

    def require_receipt(self, actor: StaffActorContext) -> None:
        try:
            proof = F3OwnResultAuthorizer(
                thin=self.authority.thin,
                configuration=self.configuration,
                clock=lambda: datetime.now(UTC),
            )(compose_command_context(actor, uuid4(), "f3-receipt-access"))
            proof.revalidate()
        except FamilyFactAuthorityDenied:
            raise PermissionError("当前身份没有家校资料回执读取权限。") from None


def create_family_staff_components(
    *,
    sessions: sessionmaker[Session],
    configuration: FamilyConfiguration,
    reference_key: bytes,
    directory: DirectoryReadComponents,
) -> FamilyStaffComponents:
    dependency_owner = object()
    authority = create_family_authority_components(
        session_factory=sessions, configuration=configuration, evidence_owner=dependency_owner
    )
    external = DirectoryFamilyReferences(directory.query, evidence=directory.evidence)
    evidence = F3EvidenceBackend(
        sessions=sessions,
        thin=authority.thin,
        dependency_owner=dependency_owner,
        decision_scope=authority.connection_authority.read_scope,
    )
    repository = FamilyStaffRepository(
        projection=SqlFamilyStaffProjection(
            F2ConnectionReadExecutor(sessions, authority.connection_authority),
            evidence=evidence,
        ),
        configuration=configuration,
        reference_key=reference_key,
        authorize_scope=authority.require_scope,
        revalidate=authority.revalidate_permit,
        authorize_school_scope=authority.require_school_scope,
        revalidate_school_scope=authority.revalidate_school_scope,
        external_references=external,
    )
    adapter = SchoolFactFamilyProductAdapter(repository)
    query = FamilyStaffProductQuery(repository, adapter, external)
    return FamilyStaffComponents(authority, configuration, repository, adapter, query)
