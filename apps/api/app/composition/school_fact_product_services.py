# pyright: strict
"""Explicit F1 services: runtime holds Reader credentials and fixed Executor transport only."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import Engine, column, func, select, table, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.composition.governance_dependencies import SqlAlchemyGovernanceDependencyAdapter
from app.composition.school_fact_executor_client_settings import SchoolFactExecutorClientSettings
from app.composition.school_fact_query_repository import (
    OfficialContactFinalAuthorizationGuard,
    OfficialContactQueryRepository,
)
from app.composition.school_fact_reader import (
    SchoolFactReaderSettings,
    build_school_fact_reader_engine,
    verify_school_fact_reader_database,
)
from app.composition.school_fact_writer_change_boundary import SchoolFactExecutorChangeBoundary
from app.composition.school_fact_writer_client import SchoolFactWriterExecutorClient
from app.platform.authorization.secure_reader_adapters import (
    SqlAlchemyRepeatableReadExecutor,
    ThinAuthorizationReadAdapter,
)
from app.platform.authorization.secure_reader_compiler import ServerOwnedProjectionCompiler
from app.platform.authorization.secure_reader_registry import official_contact_operation_registry
from app.platform.authorization.secure_reader_service import SecureCursorCodec, SecureReader
from app.platform.authorization.thin_contracts import (
    ModulePolicy,
    StaffActorBasis,
    ThinAuthorizationBasis,
    ThinAuthorizationDecision,
    ThinAuthorizationRequest,
    ThinSelfAuthorizationBasis,
)
from app.platform.authorization.thin_service import ThinAuthorizationService
from app.platform.contracts import AssignmentId, SchoolId, StaffActorContext
from app.platform.governance.basis import GovernanceAuthorizationBasisService
from app.platform.governance.contracts import (
    AssignmentAuthoritySnapshot,
    GovernanceAuthorityQuery,
    GovernanceAuthorizationBasis,
    RootAuthoritySnapshot,
)
from app.platform.governance.ports import AuthorityRootBundle
from app.platform.governance.reader import GovernanceAuthorityReader
from app.platform.organization.models import StaffAssignmentVersion
from app.platform.organization.repository import SqlAlchemyOrganizationRepository
from app.platform.school_facts.read_policy import (
    OFFICIAL_CONTACT_MODULE,
    SCHOOL_SCOPE_OWNER,
    OfficialContactPolicy,
    SchoolFactAuthorizationUnavailable,
    SingleSchoolScopeResolver,
)

ENGINEERING_SOURCE = "用户确认的合成学校工程资料，经本实例正式事实维护通道发布。"


class ReadOnlyGovernanceDependencies(SqlAlchemyGovernanceDependencyAdapter):
    """Reuse assignment interpretation; owner root reads use no write lock on the Reader pool."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        super().__init__(
            sessions,
            assignment_reader_factory=lambda session: SqlAlchemyOrganizationRepository(
                session, use_reader_identity_wrappers=True
            ),
        )
        self._read_sessions = sessions

    def read_authority_roots(
        self, *, school_id: UUID, assignment_id: UUID, as_of: datetime
    ) -> AuthorityRootBundle | None:
        """One current statement, no retained values or changes to owner validation."""

        def root_projection(kind: str, root_id: Any):
            root = table(
                {"person": "school_people", "organization": "organization_units"}[kind],
                column("school_id"),
                column("id"),
                column("version"),
                schema="public",
            )
            return (
                select(
                    func.jsonb_build_array(
                        root.c.version,
                        func.app.school_fact_reader_identity_root_is_effective_at(
                            root.c.school_id, kind, root.c.id, as_of
                        ),
                    )
                )
                .where(root.c.school_id == school_id, root.c.id == root_id)
                .correlate(StaffAssignmentVersion)
                .scalar_subquery()
                .label(f"authority_{kind}")
            )

        with self._read_sessions.begin() as session:
            repository = SqlAlchemyOrganizationRepository(
                session, use_reader_identity_wrappers=True
            )
            statement = repository.current_assignment_statement(
                SchoolId(school_id), AssignmentId(assignment_id), as_of=as_of
            ).add_columns(
                root_projection("person", StaffAssignmentVersion.person_id),
                root_projection("organization", StaffAssignmentVersion.organization_unit_id),
            )
            row = session.execute(statement).one_or_none()
            if row is None:
                return None, None, None
            snapshot = repository.assignment_snapshot_from_row(cast(Any, row[:4]))
            if snapshot is None:
                raise ValueError("Missing assignment snapshot")
            boundaries = tuple(
                value
                for value in (snapshot.effective_interval.valid_to, snapshot.planned_valid_to)
                if value is not None
            )
            assignment = AssignmentAuthoritySnapshot(
                school_id=school_id,
                assignment_id=assignment_id,
                person_id=snapshot.person_id,
                organization_id=snapshot.organization_unit_id,
                version=snapshot.source_version,
                is_effective=True,
                valid_until=min(boundaries, default=None),
            )

            def root_snapshot(
                kind: str, root_id: UUID, payload: Any
            ) -> RootAuthoritySnapshot | None:
                if payload is None:
                    return None
                if not isinstance(payload, list):
                    raise ValueError("Invalid owner root projection")
                values = cast(list[object], payload)
                if len(values) != 2 or type(values[0]) is not int or type(values[1]) is not bool:
                    raise ValueError("Invalid owner root projection")
                if not values[1]:
                    return None
                return RootAuthoritySnapshot(
                    school_id=school_id,
                    root_kind=kind,
                    root_id=root_id,
                    version=values[0],
                    is_effective=True,
                    valid_until=None,
                )

            return (
                assignment,
                root_snapshot("person", snapshot.person_id, row[4]),
                root_snapshot("organization", snapshot.organization_unit_id, row[5]),
            )

    def _root(
        self, *, school_id: UUID, root_id: UUID, kind: str, as_of: datetime
    ) -> RootAuthoritySnapshot | None:
        table = {"person": "school_people", "organization": "organization_units"}[kind]
        with self._read_sessions.begin() as session:
            row = session.execute(
                text(
                    f"SELECT version, app.school_fact_reader_identity_root_is_effective_at("
                    f"school_id, :kind, id, :as_of) AS is_effective FROM public.{table} "
                    "WHERE school_id = :school_id AND id = :root_id"
                ),
                {"school_id": school_id, "root_id": root_id, "kind": kind, "as_of": as_of},
            ).one_or_none()
        if row is None or not row.is_effective:
            return None
        return RootAuthoritySnapshot(
            school_id=school_id,
            root_kind=kind,
            root_id=root_id,
            version=int(row.version),
            is_effective=True,
            valid_until=None,
        )

    def read_person_root(
        self, *, school_id: UUID, person_id: UUID, as_of: datetime
    ) -> RootAuthoritySnapshot | None:
        return self._root(school_id=school_id, root_id=person_id, kind="person", as_of=as_of)

    def read_organization_root(
        self, *, school_id: UUID, organization_id: UUID, as_of: datetime
    ) -> RootAuthoritySnapshot | None:
        return self._root(
            school_id=school_id, root_id=organization_id, kind="organization", as_of=as_of
        )


class _Basis(Protocol):
    def issue(self, query: GovernanceAuthorityQuery) -> GovernanceAuthorizationBasis: ...
    def validate(self, basis: GovernanceAuthorizationBasis, *, as_of: datetime) -> bool: ...


def _source_fault(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if (
            isinstance(current, SQLAlchemyError)
            or getattr(current, "reason", None) == "dependency_error"
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


class _CallBasis:
    def __init__(self, authority: _Basis) -> None:
        self._authority = authority
        self.unavailable = False

    def issue(self, query: GovernanceAuthorityQuery) -> GovernanceAuthorizationBasis:
        try:
            return self._authority.issue(query)
        except Exception as exc:
            self.unavailable = _source_fault(exc)
            raise

    def validate(self, basis: GovernanceAuthorizationBasis, *, as_of: datetime) -> bool:
        try:
            return self._authority.validate(basis, as_of=as_of)
        except Exception as exc:
            self.unavailable = _source_fault(exc)
            raise


class _PreparedBasis:
    """Private exact call inputs; discarded before the batch returns."""

    def __init__(
        self,
        source: _Basis,
        issued: dict[GovernanceAuthorityQuery, GovernanceAuthorizationBasis | Exception]
        | None = None,
        validated: dict[tuple[GovernanceAuthorizationBasis, datetime], bool | Exception]
        | None = None,
    ) -> None:
        self.source = source
        self.issued = issued or {}
        self.validated = validated or {}

    @staticmethod
    def _value[T](value: T | Exception) -> T:
        if isinstance(value, Exception):
            raise value
        return value

    def issue(self, query: GovernanceAuthorityQuery) -> GovernanceAuthorizationBasis:
        if query in self.issued:
            return self._value(self.issued[query])
        return self.source.issue(query)

    def validate(self, basis: GovernanceAuthorizationBasis, *, as_of: datetime) -> bool:
        if (basis, as_of) in self.validated:
            return self._value(self.validated[(basis, as_of)])
        return self.source.validate(basis, as_of=as_of)


class ProductThinAuthorizationService:
    """Call-local failure reporting around the unchanged authoritative Thin/Policy decision."""

    def __init__(
        self,
        *,
        governance_basis_service: _Basis,
        module_policies: Mapping[str, ModulePolicy] | None = None,
        self_person_validator: Callable[[StaffActorContext, datetime], bool] | None = None,
    ) -> None:
        self._authority = governance_basis_service
        self._self_person_validator = self_person_validator
        self._policies = (
            dict(module_policies)
            if module_policies is not None
            else {
                OFFICIAL_CONTACT_MODULE: OfficialContactPolicy(),
            }
        )

    def _call(self, authority: _Basis | None = None) -> tuple[_CallBasis, ThinAuthorizationService]:
        recorder = _CallBasis(authority if authority is not None else self._authority)
        return recorder, ThinAuthorizationService(
            governance_basis_service=recorder,
            module_policies=self._policies,
            self_person_validator=self._self_person_validator,
        )

    @staticmethod
    def _result(
        recorder: _CallBasis, result: ThinAuthorizationDecision
    ) -> ThinAuthorizationDecision:
        if recorder.unavailable:
            raise SchoolFactAuthorizationUnavailable("学校事实授权源暂时不可用。")
        return result

    def decide(self, request: ThinAuthorizationRequest) -> ThinAuthorizationDecision:
        recorder, service = self._call()
        return self._result(recorder, service.decide(request))

    def decide_many(
        self, requests: tuple[ThinAuthorizationRequest, ...]
    ) -> tuple[ThinAuthorizationDecision, ...]:
        """Batch owner IO only; Thin and module Policy still decide each action."""
        if len(requests) > 128 or requests and any(r.actor != requests[0].actor for r in requests):
            raise ValueError("Authorization batch identity or size changed.")
        batch = getattr(self._authority, "issue_many", None)
        if batch is None:
            return tuple(self.decide(request) for request in requests)
        queries: list[GovernanceAuthorityQuery] = []
        for request in requests:
            if type(request.actor) is not StaffActorContext:
                continue
            policy = self._policies.get(request.module_name)
            if policy is None or policy.module_name != request.module_name or policy.version < 1:
                continue
            try:
                requirement = policy.requirement_for(request)
            except Exception:
                continue
            if requirement is not None and requirement.authority_kind == "governance":
                queries.append(
                    GovernanceAuthorityQuery(
                        school_id=request.actor.school_id,
                        assignment_id=request.actor.assignment_id,
                        duty_code=requirement.duty_code,
                        scope_id=request.scope_id,
                        as_of=request.as_of,
                    )
                )
        unique = tuple(dict.fromkeys(queries))
        try:
            values = batch(unique) if unique else ()
        except Exception as exc:
            if _source_fault(exc):
                raise SchoolFactAuthorizationUnavailable("学校事实授权源暂时不可用。") from exc
            raise
        if len(values) != len(unique):
            raise SchoolFactAuthorizationUnavailable("学校事实授权源暂时不可用。")
        prepared = _PreparedBasis(self._authority, issued=dict(zip(unique, values, strict=True)))
        results: list[ThinAuthorizationDecision] = []
        for request in requests:
            recorder, service = self._call(prepared)
            results.append(self._result(recorder, service.decide(request)))
        return tuple(results)

    def revalidate_many(
        self,
        bases: tuple[ThinAuthorizationBasis | ThinSelfAuthorizationBasis, ...],
        *,
        actor: StaffActorContext,
        as_of: datetime,
        resource_revision: int,
    ) -> tuple[ThinAuthorizationDecision | Exception, ...]:
        if len(bases) > 128:
            raise ValueError("Authorization batch exceeds the fixed bound.")
        batch = getattr(self._authority, "validate_many", None)
        if batch is None or type(actor) is not StaffActorContext:
            return tuple(
                self.revalidate(
                    basis, actor=actor, as_of=as_of, resource_revision=resource_revision
                )
                for basis in bases
            )
        trusted = StaffActorBasis(**asdict(actor))
        pending = tuple(
            dict.fromkeys(
                basis.governance
                for basis in bases
                if isinstance(basis, ThinAuthorizationBasis)
                and basis.actor == trusted
                and basis.resource_revision == resource_revision
            )
        )
        try:
            values = batch(pending, as_of=as_of) if pending else ()
        except Exception as exc:
            if _source_fault(exc):
                raise SchoolFactAuthorizationUnavailable("学校事实授权源暂时不可用。") from exc
            raise
        if len(values) != len(pending):
            raise SchoolFactAuthorizationUnavailable("学校事实授权源暂时不可用。")
        prepared = _PreparedBasis(
            self._authority,
            validated={(basis, as_of): value for basis, value in zip(pending, values, strict=True)},
        )
        results: list[ThinAuthorizationDecision | Exception] = []
        for basis in bases:
            recorder, service = self._call(prepared)
            try:
                results.append(
                    self._result(
                        recorder,
                        service.revalidate(
                            basis,
                            actor=actor,
                            as_of=as_of,
                            resource_revision=resource_revision,
                        ),
                    )
                )
            except SchoolFactAuthorizationUnavailable as exc:
                results.append(exc)
        return tuple(results)

    def revalidate(
        self,
        basis: ThinAuthorizationBasis | ThinSelfAuthorizationBasis,
        *,
        actor: StaffActorContext,
        as_of: datetime,
        resource_revision: int,
    ) -> ThinAuthorizationDecision:
        recorder, service = self._call()
        return self._result(
            recorder,
            service.revalidate(
                basis,
                actor=actor,
                as_of=as_of,
                resource_revision=resource_revision,
            ),
        )


def create_product_thin_service(
    reader_sessions: sessionmaker[Session],
) -> ProductThinAuthorizationService:
    authority = GovernanceAuthorityReader(
        session_factory=reader_sessions,
        dependency_port=ReadOnlyGovernanceDependencies(reader_sessions),
        scope_owner_resolvers={SCHOOL_SCOPE_OWNER: SingleSchoolScopeResolver()},
    )
    return ProductThinAuthorizationService(
        governance_basis_service=GovernanceAuthorizationBasisService(
            session_factory=reader_sessions,
            authority_reader=authority,
        ),
    )


@dataclass(frozen=True, slots=True)
class SchoolFactProductServices:
    reader_engine: Engine
    reader_sessions: sessionmaker[Session]
    secure_reader: SecureReader
    query_repository: OfficialContactQueryRepository
    writer_boundary: SchoolFactExecutorChangeBoundary
    maintenance_guard: OfficialContactFinalAuthorizationGuard
    thin_service: ProductThinAuthorizationService
    _writer_client: SchoolFactWriterExecutorClient

    def close(self) -> None:
        self._writer_client.close()
        self.reader_engine.dispose()


def create_school_fact_product_services(
    *,
    reader_settings: SchoolFactReaderSettings,
    executor_settings: SchoolFactExecutorClientSettings,
    scope_id: UUID,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SchoolFactProductServices:
    if reader_settings.schema_contract not in {
        "school-facts-f1-v22",
        "school-facts-f2-v25",
        "school-facts-f3-v26",
        "school-facts-f3-v27",
        "school-facts-f4-v29",
        "school-facts-f4-v30",
        "school-facts-f4-v31",
        "school-facts-f5-v32",
        "school-facts-f5-v33",
        "school-facts-f5-v34",
        "school-facts-f5-v35",
        "school-facts-f5-v36",
        "school-facts-f5-v37",
        "school-facts-f5-v38",
        "school-facts-f5-v39",
        "school-facts-f5-v40",
        "school-facts-f5-v41",
        "school-facts-f6-v44",
        "school-facts-f6-v45",
        "school-facts-f7-v47",
        "school-facts-f7-v48",
        "school-facts-f7-v49",
        "school-facts-f7-v50",
        "school-facts-f7-v51",
        "school-facts-f7-v52",
        "school-facts-f7-v53",
        "school-facts-f7-v54",
        "school-facts-f7-v55",
        "school-facts-f7-v56", "school-facts-live-v57",
    }:
        raise ValueError("F1 product requires exact schema22.")
    engine = build_school_fact_reader_engine(reader_settings)
    client: SchoolFactWriterExecutorClient | None = None
    try:
        verify_school_fact_reader_database(engine, settings=reader_settings)
        sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        thin = create_product_thin_service(sessions)
        reader = SecureReader(
            registry=official_contact_operation_registry(),
            compiler=ServerOwnedProjectionCompiler(),
            authorizer=ThinAuthorizationReadAdapter(
                thin_service=thin,
                scope_id_resolver=lambda _operation, _actor: scope_id,
            ),
            executor=SqlAlchemyRepeatableReadExecutor(sessions),
            cursor_codec=SecureCursorCodec(
                reader_settings.cursor_hmac_key.get_secret_value().encode(),
                key_version=reader_settings.cursor_key_version,
                ttl_seconds=reader_settings.cursor_ttl_seconds,
            ),
            clock=clock,
        )
        client = SchoolFactWriterExecutorClient(settings=executor_settings)
        return SchoolFactProductServices(
            reader_engine=engine,
            reader_sessions=sessions,
            secure_reader=reader,
            query_repository=OfficialContactQueryRepository(
                reader=reader,
                source_description=ENGINEERING_SOURCE,
                clock=clock,
            ),
            writer_boundary=SchoolFactExecutorChangeBoundary(client),
            maintenance_guard=OfficialContactFinalAuthorizationGuard(
                thin_service=thin,
                scope_id=scope_id,
                clock=clock,
            ),
            thin_service=thin,
            _writer_client=client,
        )
    except BaseException:
        if client is not None:
            client.close()
        engine.dispose()
        raise
