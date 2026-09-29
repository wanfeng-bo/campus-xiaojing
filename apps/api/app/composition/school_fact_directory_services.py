"""F2 composition: owner metadata, exact authority and fixed Reader. No write bypass."""

import json
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.composition.school_fact_authority_connection import ConnectionBoundAuthority
from app.composition.school_fact_directory_settings import DirectoryConfiguration
from app.composition.school_fact_f2_query_repository import (
    F2FactQueryRepository,
    F2FinalAuthorizationGuard,
)
from app.composition.school_fact_own_result_authorization import F2OwnResultAuthorizer
from app.composition.school_fact_product_services import (
    ProductThinAuthorizationService,
    ReadOnlyGovernanceDependencies,
)
from app.composition.school_fact_read_capture import (
    CapturingCompiler,
    CapturingExecutor,
    CapturingReader,
    CapturingThinService,
    EvidenceGovernanceDependencies,
    F2EvidenceBackend,
    owner_root_round,
)
from app.composition.school_fact_read_connection import F2ConnectionReadExecutor
from app.composition.school_fact_reader import SchoolFactReaderSettings
from app.platform.authorization.secure_reader_registry import ReadOperation, f2_operation_registry
from app.platform.authorization.secure_reader_service import SecureCursorCodec, SecureReader
from app.platform.contracts import StaffActorContext
from app.platform.governance.basis import GovernanceAuthorizationBasisService
from app.platform.governance.reader import GovernanceAuthorityReader
from app.platform.school_facts.f2_policy import (
    F2_FACT_KINDS,
    F2_SCOPE_OWNER,
    F2FactPolicy,
    F2FactScope,
    F2ScopeResolver,
)
from app.platform.school_facts.f2_read_authorization import F2ReadAuthorizationAdapter
from app.platform.school_facts.f7_own_terminal_authorization import (
    F7OwnTerminalAuthorizer,
    F7OwnTerminalPolicy,
)
from app.platform.school_facts.read_policy import (
    OFFICIAL_CONTACT_MODULE,
    SCHOOL_SCOPE_OWNER,
    OfficialContactPolicy,
    SingleSchoolScopeResolver,
)


@contextmanager
def _fresh_root_scope(
    sessions: sessionmaker[Session],
) -> Generator[ReadOnlyGovernanceDependencies]:
    """One fresh exit pass, after workers join and before the phase returns."""
    with sessions() as owner:
        connection = owner.connection()
        if connection.get_isolation_level() != "READ COMMITTED":
            raise SQLAlchemyError("Root exit checks require READ COMMITTED")
        transaction = connection.get_transaction()
        if transaction is None:
            raise SQLAlchemyError("Root exit transaction unavailable")
        bound = sessionmaker(
            bind=connection, expire_on_commit=False, join_transaction_mode="rollback_only"
        )
        # Dependencies create a new Session per read: no identity-map/result reuse.
        yield ReadOnlyGovernanceDependencies(bound)
        if not transaction.is_active or connection.get_transaction() is not transaction:
            raise SQLAlchemyError("Root exit transaction changed during read")


class F2ScopeOwnerLookup:
    """Only current school/root and stable source-coordinate existence, no business content."""

    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def scope_exists_many(
        self, scopes: tuple[F2FactScope, ...], times: tuple[datetime, ...]
    ) -> tuple[bool, ...]:
        """One exact set of owner probes; common roots/coordinates are read once."""
        if len(scopes) > 128 or len(scopes) != len(times):
            raise ValueError("Invalid bounded scope owner request.")
        if not scopes:
            return ()
        requests = [
            {
                "ordinal": index,
                "school_id": str(scope.school_id),
                "kind": scope.fact_kind,
                "keys": list(scope.object_keys),
                "as_of": at.isoformat(),
            }
            for index, (scope, at) in enumerate(zip(scopes, times, strict=True))
        ]
        with self.sessions.begin() as session:
            rows = session.execute(
                text("""
                    WITH requested AS MATERIALIZED (
                        SELECT * FROM jsonb_to_recordset(CAST(:requests AS jsonb))
                        AS r(ordinal integer, school_id uuid, kind text, keys jsonb,
                             as_of timestamptz)
                    ), root_checks AS MATERIALIZED (
                        SELECT school_id, as_of,
                            app.school_fact_f2_reader_root_is_effective_at(
                                school_id,'school',school_id,as_of) AS valid
                        FROM (SELECT DISTINCT school_id,as_of FROM requested) AS roots
                    ), coordinates AS MATERIALIZED (
                        SELECT school_id,kind,source_key,
                            (SELECT target_id FROM public.school_fact_change_lineage
                             WHERE school_id=exact.school_id AND fact_kind=exact.kind
                             AND fact_source_key=exact.source_key
                             ORDER BY created_at DESC,id DESC LIMIT 1) AS target_id
                        FROM (SELECT DISTINCT school_id,kind,jsonb_array_elements_text(keys)
                              AS source_key FROM requested) AS exact
                    )
                    SELECT r.ordinal, roots.valid AND NOT EXISTS (
                        SELECT 1 FROM jsonb_array_elements_text(r.keys) AS key(source_key)
                        LEFT JOIN coordinates AS c ON c.school_id=r.school_id AND c.kind=r.kind
                            AND c.source_key=key.source_key
                        WHERE c.target_id IS NULL
                    ) AS valid
                    FROM requested AS r JOIN root_checks AS roots
                    ON roots.school_id=r.school_id AND roots.as_of=r.as_of
                    ORDER BY r.ordinal
                """),
                {"requests": json.dumps(requests)},
            ).all()
        if len(rows) != len(scopes) or any(row[0] != index for index, row in enumerate(rows)):
            raise SQLAlchemyError("Scope owner batch result is incomplete")
        return tuple(row[1] is True for row in rows)

    def scope_exists(self, scope: F2FactScope, as_of: datetime) -> bool:
        """Same school/current-root and latest-coordinate checks as __call__, batched."""
        if scope.is_school_wide:
            return self(scope.school_id, "school", "", as_of)
        with self.sessions.begin() as session:
            return (
                session.scalar(
                    text(
                        "SELECT app.school_fact_f2_reader_root_is_effective_at("
                        ":school_id,'school',:school_id,:as_of) AND NOT EXISTS ("
                        "SELECT 1 FROM unnest(CAST(:keys AS text[])) AS required(source_key) "
                        "WHERE (SELECT target_id FROM public.school_fact_change_lineage "
                        "WHERE school_id=:school_id AND fact_kind=:kind "
                        "AND fact_source_key=required.source_key "
                        "ORDER BY created_at DESC,id DESC LIMIT 1) IS NULL)"
                    ),
                    {
                        "school_id": scope.school_id,
                        "kind": scope.fact_kind,
                        "keys": list(scope.object_keys),
                        "as_of": as_of,
                    },
                )
                is True
            )

    def __call__(self, school_id: UUID, fact_kind: str, source_key: str, as_of: datetime) -> bool:
        if fact_kind not in F2_FACT_KINDS:
            return False
        with self.sessions.begin() as session:
            school_ok = session.scalar(
                text(
                    "SELECT app.school_fact_f2_reader_root_is_effective_at("
                    ":school_id,'school',:school_id,:as_of)"
                ),
                {"school_id": school_id, "as_of": as_of},
            )
            if school_ok is not True:
                return False
            if fact_kind == "school" and not source_key:
                return True
            target = session.scalar(
                text(
                    "SELECT target_id FROM public.school_fact_change_lineage "
                    "WHERE school_id=:school_id AND fact_kind=:kind "
                    "AND fact_source_key=:source_key ORDER BY created_at DESC,id DESC LIMIT 1"
                ),
                {"school_id": school_id, "kind": fact_kind, "source_key": source_key},
            )
            if not isinstance(target, UUID):
                return False
            return True


@dataclass(frozen=True, slots=True)
class DirectoryReadComponents:
    reader: SecureReader
    query: F2FactQueryRepository
    guard: F2FinalAuthorizationGuard
    thin: ProductThinAuthorizationService
    evidence: F2EvidenceBackend | None = None
    own_result_authorizer: F2OwnResultAuthorizer | None = None
    own_terminal_authorizer: F7OwnTerminalAuthorizer | None = None


def create_directory_read_components(
    *,
    sessions: sessionmaker[Session],
    settings: SchoolFactReaderSettings,
    configuration: DirectoryConfiguration,
    legacy_scope_id: UUID,
    reference_key: bytes,
    self_person_validator: Callable[[StaffActorContext, datetime], bool] | None = None,
) -> DirectoryReadComponents:
    if settings.schema_contract not in {
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
        "school-facts-f7-v56",
        "school-facts-live-v57",
    }:
        raise ValueError("F2 requires exact schema25")

    dependency_owner = object()

    def build_basis(bound: sessionmaker[Session]) -> GovernanceAuthorizationBasisService:
        scope_owner = F2ScopeOwnerLookup(bound)
        authority = GovernanceAuthorityReader(
            session_factory=bound,
            dependency_port=EvidenceGovernanceDependencies(bound, owner=dependency_owner),
            scope_owner_resolvers={
                SCHOOL_SCOPE_OWNER: SingleSchoolScopeResolver(),
                F2_SCOPE_OWNER: F2ScopeResolver(
                    lookup=scope_owner,
                    lookup_scope=scope_owner.scope_exists,
                    lookup_scope_many=scope_owner.scope_exists_many,
                ),
            },
        )
        return GovernanceAuthorizationBasisService(
            session_factory=bound, authority_reader=authority
        )

    connection_authority = ConnectionBoundAuthority(sessions=sessions, build_basis=build_basis)
    policies = {
        OFFICIAL_CONTACT_MODULE: OfficialContactPolicy(),
        F2_SCOPE_OWNER: F2FactPolicy(),
    }
    if settings.schema_contract in {
        "school-facts-f7-v47",
        "school-facts-f7-v48",
        "school-facts-f7-v49",
        "school-facts-f7-v50",
        "school-facts-f7-v51",
        "school-facts-f7-v52",
        "school-facts-f7-v53",
        "school-facts-f7-v54",
        "school-facts-f7-v55",
        "school-facts-f7-v56",
        "school-facts-live-v57",
    }:
        policies[F7OwnTerminalPolicy.module_name] = F7OwnTerminalPolicy()
    thin = CapturingThinService(
        governance_basis_service=connection_authority,
        module_policies=policies,
        self_person_validator=self_person_validator,
    )
    bindings = {
        (str(b.assignment_id), b.operation_code): b.scope_id for b in configuration.scope_bindings
    }

    def scope(operation: ReadOperation, actor: StaffActorContext) -> UUID:
        if operation.code == "school_facts.f2.person_contact.self":
            return UUID(str(actor.person_id))
        if operation.module_name == OFFICIAL_CONTACT_MODULE:
            return legacy_scope_id
        return bindings.get((str(actor.assignment_id), operation.code), UUID(int=0))

    def make_reader(quality_target_kind: str | None = None) -> SecureReader:
        return CapturingReader(
            registry=f2_operation_registry(),
            clock=lambda: datetime.now(UTC),
            compiler=CapturingCompiler(),
            authorizer=F2ReadAuthorizationAdapter(
                thin_service=thin,
                scope_id_resolver=scope,
                quality_target_kind=quality_target_kind,
                serial_reads=lambda: connection_authority.current_sessions() is not sessions,
                authorization_round=lambda actor, at: owner_root_round(
                    dependency_owner,
                    actor,
                    at,
                    ReadOnlyGovernanceDependencies(sessions),
                    lambda: datetime.now(UTC),
                    fresh_scope=lambda: _fresh_root_scope(connection_authority.current_sessions()),
                ),
            ),
            executor=CapturingExecutor(
                sessions, delegate=F2ConnectionReadExecutor(sessions, connection_authority)
            ),
            cursor_codec=SecureCursorCodec(
                settings.cursor_hmac_key.get_secret_value().encode(),
                key_version=settings.cursor_key_version,
                ttl_seconds=settings.cursor_ttl_seconds,
            ),
        )

    reader = make_reader()

    def target_content(actor: StaffActorContext, kind: str, key: str) -> dict[str, object]:
        return repository.maintenance_content_for_source(
            actor=actor, fact_kind=kind, source_key=key
        )

    guard = F2FinalAuthorizationGuard(
        thin_service=thin,
        scope_id_resolver=scope,
        target_content_resolver=target_content,
        authorization_round=lambda actor, at: owner_root_round(
            dependency_owner,
            actor,
            at,
            ReadOnlyGovernanceDependencies(connection_authority.current_sessions()),
            lambda: datetime.now(UTC),
            fresh_scope=lambda: _fresh_root_scope(connection_authority.current_sessions()),
        ),
    )
    repository = F2FactQueryRepository(
        reader=reader,
        quality_readers={kind: make_reader(kind) for kind in F2_FACT_KINDS - {"fact_quality"}},
        reference_key=reference_key,
        source_catalog=configuration.source_catalog,
        creation_catalog=configuration.creation_catalog,
        maintenance_guard=guard,
    )

    @contextmanager
    def own_result_round(actor: StaffActorContext) -> Generator[None]:
        engine = sessions.kw.get("bind")
        if not isinstance(engine, Engine):
            raise SQLAlchemyError("Own receipt authority requires an owned Reader engine")
        with engine.connect() as connection, connection_authority.borrow_connection(connection):
            yield

    return DirectoryReadComponents(
        reader,
        repository,
        guard,
        thin,
        F2EvidenceBackend(
            sessions=sessions,
            thin=thin,
            dependency_owner=dependency_owner,
            decision_scope=connection_authority.read_scope,
            fresh_scope=lambda: _fresh_root_scope(connection_authority.current_sessions()),
        ),
        F2OwnResultAuthorizer(guard, authorization_round=own_result_round),
        F7OwnTerminalAuthorizer(thin_service=thin, school_scope_id=legacy_scope_id)
        if settings.schema_contract
        in {
            "school-facts-f7-v47",
            "school-facts-f7-v48",
            "school-facts-f7-v49",
            "school-facts-f7-v50",
            "school-facts-f7-v51",
            "school-facts-f7-v52",
            "school-facts-f7-v53",
            "school-facts-f7-v54",
            "school-facts-f7-v55",
            "school-facts-f7-v56",
            "school-facts-live-v57",
        }
        else None,
    )
