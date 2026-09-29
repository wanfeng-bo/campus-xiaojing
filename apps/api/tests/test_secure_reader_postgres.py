from __future__ import annotations

from dataclasses import dataclass, replace
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import URL, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.composition.school_fact_reader import (
    ReaderStartupRejected,
    SchoolFactReaderSettings,
    build_school_fact_reader_engine,
    verify_school_fact_reader_database,
)
from app.platform import contracts as platform_contracts
from app.platform.authorization.secure_reader_adapters import (
    SqlAlchemyRepeatableReadExecutor,
    ThinAuthorizationReadAdapter,
)
from app.platform.authorization.secure_reader_compiler import ServerOwnedProjectionCompiler
from app.platform.authorization.secure_reader_contracts import (
    OfficialOfficeContactRow,
    PublicLocationRow,
    SecureReadAuthorizationDrift,
    SecureReadDenied,
    SecureReadRequest,
    SelfCurrentAssignmentRow,
)
from app.platform.authorization.secure_reader_registry import (
    test_operation_registry as _test_registry,
)
from app.platform.authorization.secure_reader_service import SecureCursorCodec, SecureReader
from app.platform.authorization.thin_contracts import (
    ModuleAuthorizationRequirement,
    NamedProjection,
    ScopePlan,
)
from app.platform.authorization.thin_service import ThinAuthorizationService
from app.platform.contracts import AssignmentId, PersonId, SchoolId
from tests.governance.test_reader_basis import (
    _DependencyState,  # pyright: ignore[reportPrivateUsage]
    _load_reader_api,  # pyright: ignore[reportPrivateUsage]
    _reader_harness,  # pyright: ignore[reportPrivateUsage]
)
from tests.governance.test_writer_lifecycle import (
    _actor,  # pyright: ignore[reportPrivateUsage]
    _create_duty_and_scope,  # pyright: ignore[reportPrivateUsage]
    _grant_command,  # pyright: ignore[reportPrivateUsage]
    _writer_environment,  # pyright: ignore[reportPrivateUsage]
)
from tests.master_data.conftest import PostgresRoles
from tests.school_facts.test_sf1_postgres_constraints import (
    _insert_confirmed_location_slices,  # pyright: ignore[reportPrivateUsage]
    _LocationSliceSeed,  # pyright: ignore[reportPrivateUsage]
)

pytestmark = pytest.mark.integration

AS_OF = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)


def _dto_field_names(row: Any) -> set[str]:
    return {field.name for field in dataclass_fields(row)}


@dataclass(frozen=True, slots=True)
class _Facts:
    school_id: UUID
    person_id: UUID
    assignment_id: UUID
    anchor_id: UUID


class _RevocableAuthorizer:
    def __init__(self, school_id: UUID) -> None:
        self.school_id = school_id
        self.decisions = 0
        self.revalidations = 0
        self.deny_revalidation: int | None = None

    def decide(self, *, operation: Any, **_: object) -> object:
        self.decisions += 1
        return SimpleNamespace(
            allowed=True,
            basis="test-basis",
            scope_plan=ScopePlan(
                scope_id=self.school_id,
                owner="test-only.r1.school",
                reference=f"school:{self.school_id}",
                version=1,
            ),
            projection=NamedProjection(name=operation.projection_name, version=1),
            basis_fingerprint="test-basis-v1",
        )

    def revalidate(self, authorization: object, **_: object) -> bool:
        del authorization
        self.revalidations += 1
        return self.revalidations != self.deny_revalidation


@dataclass(slots=True)
class _R1ThinModulePolicy:
    required_duty_code: str
    version: int = 1

    @property
    def module_name(self) -> str:
        return "test_only.r1.secure_reader"

    def requirement_for(self, request: object) -> ModuleAuthorizationRequirement | None:
        if getattr(request, "action_code", None) != "test_only.r1.public_location.read":
            return None
        return ModuleAuthorizationRequirement(
            duty_code=self.required_duty_code,
            projection_name="public_location",
            projection_version=1,
        )


def _reader_url(roles: PostgresRoles) -> URL:
    values = roles.reader_connection_parameters
    return URL.create(
        "postgresql+psycopg",
        username=str(values["user"]),
        password=str(values["password"]),
        host=str(values["host"]),
        port=int(values["port"]),
        database=str(values["dbname"]),
    )


def _seed_four_projections(roles: PostgresRoles) -> _Facts:
    school_id = uuid4()
    source_id = uuid4()
    person_id = uuid4()
    organization_id = uuid4()
    assignment_id = uuid4()
    policy_id = uuid4()
    with roles.migration_connection() as connection, connection.transaction():
        connection.execute("SET LOCAL search_path = public, pg_catalog")
        connection.execute("SET LOCAL ROLE campus_ai_db_owner")
        connection.execute(
            "INSERT INTO schools "
            "(id, school_code, display_name, timezone_name, status, version) "
            "VALUES (%s, %s, 'R1 School', 'Asia/Shanghai', 'active', 1)",
            (school_id, f"r1-{school_id.hex[:12]}"),
        )
        connection.execute(
            "INSERT INTO school_fact_sources "
            "(id, school_id, source_key, source_kind, source_ref, schema_version) "
            "VALUES (%s, %s, %s, 'manual', %s, 1)",
            (source_id, school_id, f"r1:{source_id}", f"r1:{source_id}"),
        )
        connection.execute(
            "INSERT INTO school_people (id, school_id, status, version) "
            "VALUES (%s, %s, 'active', 1)",
            (person_id, school_id),
        )
        connection.execute(
            "INSERT INTO organization_units (id, school_id, kind, status, version) "
            "VALUES (%s, %s, 'department', 'active', 1)",
            (organization_id, school_id),
        )
        connection.execute(
            "INSERT INTO staff_assignments "
            "(id, school_id, person_id, organization_unit_id, position_title, "
            "semantic_role_code, appointment_source, approval_reference, valid_from, "
            "status, version) VALUES "
            "(%s, %s, %s, %s, 'Teacher', 'teacher', 'manual', 'R1', %s, "
            "'confirmed', 1)",
            (assignment_id, school_id, person_id, organization_id, AS_OF - timedelta(days=1)),
        )
        connection.execute(
            "INSERT INTO staff_assignment_versions "
            "(id, school_id, assignment_id, person_id, organization_unit_id, "
            "position_title, semantic_role_code, assignment_kind, appointment_source, "
            "approval_reference, effective_from, effective_to, planned_valid_to, "
            "revoked_at, status, target_version, source_ref, source_id, import_batch_id) "
            "VALUES (%s, %s, %s, %s, %s, 'Teacher', 'teacher', 'substantive', "
            "'manual', 'R1', %s, NULL, NULL, NULL, 'confirmed', 1, 'r1', %s, NULL)",
            (
                uuid4(),
                school_id,
                assignment_id,
                person_id,
                organization_id,
                AS_OF - timedelta(days=1),
                source_id,
            ),
        )
        location_slices = tuple(
            _LocationSliceSeed(
                location_id=uuid4(),
                source_key=f"location:r1:{school_id}:{ordinal}",
                parent_location_id=None,
                kind="campus",
                business_code=f"CAMPUS-{ordinal}",
                display_name=f"Campus {ordinal}",
                aliases=(),
                display_path=f"/Campus {ordinal}",
                effective_at=AS_OF - timedelta(days=1),
            )
            for ordinal in (1, 2)
        )
        for item in location_slices:
            connection.execute(
                "INSERT INTO campus_locations "
                "(id, school_id, source_key, parent_location_id, kind, status, version) "
                "VALUES (%s, %s, %s, NULL, 'campus', 'active', 1)",
                (item.location_id, school_id, item.source_key),
            )
        _insert_confirmed_location_slices(
            connection,
            school_id=school_id,
            slices=location_slices,
        )
        connection.execute(
            "INSERT INTO official_contact_points "
            "(id, school_id, owner_kind, organization_unit_id, location_id, "
            "contact_kind, contact_value, service_hours, lifecycle_status, "
            "visibility_scope, audience_code, valid_from, valid_to, version, "
            "source_id, import_batch_id) VALUES "
            "(%s, %s, 'school', NULL, NULL, 'phone', '010-12345678', '08:00-17:00', "
            "'active', 'public', NULL, %s, NULL, 1, %s, NULL)",
            (uuid4(), school_id, AS_OF - timedelta(days=1), source_id),
        )
        connection.execute(
            "INSERT INTO school_policies (id, school_id, source_key) VALUES (%s, %s, %s)",
            (policy_id, school_id, f"r1-policy:{policy_id}"),
        )
        connection.execute(
            "INSERT INTO school_policy_versions "
            "(id, school_id, policy_id, business_code, title, category_code, "
            "classification, publishing_organization_unit_id, published_at, "
            "effective_from, effective_to, revoked_at, content_text, version_note, "
            "supersedes_policy_version_id, version, source_id, import_batch_id) VALUES "
            "(%s, %s, %s, 'R1-POLICY', 'Restricted', 'security', 'restricted', NULL, "
            "%s, %s, NULL, NULL, 'classified body', NULL, NULL, 1, %s, NULL)",
            (uuid4(), school_id, policy_id, AS_OF, AS_OF - timedelta(days=1), source_id),
        )
        anchor = connection.execute(
            "SELECT anchor_id, school_id FROM public.school_database_anchor"
        ).fetchone()
        assert anchor is not None
        anchor_id = UUID(str(anchor[0]))
        assert UUID(str(anchor[1])) == school_id
    return _Facts(
        school_id=school_id,
        person_id=person_id,
        assignment_id=assignment_id,
        anchor_id=anchor_id,
    )


def _production_settings(
    roles: PostgresRoles,
    facts: _Facts,
    **overrides: object,
) -> SchoolFactReaderSettings:
    values = roles.reader_connection_parameters
    payload: dict[str, object] = {
        "database_host": str(values["host"]),
        "database_port": int(values["port"]),
        "database_name": str(values["dbname"]),
        "database_user": str(values["user"]),
        "database_password": str(values["password"]),
        "expected_anchor_id": facts.anchor_id,
        "expected_school_id": facts.school_id,
        "expected_system_identifier": roles.disposable_cluster_system_identifier,
        "cursor_hmac_key": "r1-reader-cursor-secret-isolated-0001",
    }
    payload.update(overrides)
    return SchoolFactReaderSettings.model_validate(payload)


def test_r1_production_reader_startup_validates_real_role_head_and_anchor(
    isolated_migration_schema: PostgresRoles,
) -> None:
    roles = isolated_migration_schema
    facts = _seed_four_projections(roles)
    settings = _production_settings(roles, facts, pool_size=1, max_overflow=0)
    engine = build_school_fact_reader_engine(settings)
    verified = verify_school_fact_reader_database(engine, settings=settings)

    assert verified.current_user == "campus_ai_school_fact_reader"
    assert verified.session_user == "campus_ai_school_fact_reader"
    assert verified.alembic_head == "20260824_20"
    assert verified.anchored_school_id == facts.school_id
    assert verified.default_transaction_read_only is True

    with engine.connect() as first_connection:
        first_pool_facts = (
            first_connection.exec_driver_sql(
                "SELECT pg_backend_pid() AS backend_pid, current_user, session_user, "
                "current_setting('default_transaction_read_only') AS default_read_only"
            )
            .mappings()
            .one()
        )
        assert first_pool_facts["current_user"] == "campus_ai_school_fact_reader"
        assert first_pool_facts["session_user"] == "campus_ai_school_fact_reader"
        assert first_pool_facts["default_read_only"] == "on"
        first_backend_pid = int(first_pool_facts["backend_pid"])
        assert (
            first_connection.exec_driver_sql(
                "SELECT set_config('app.r1_pool_probe', 'dirty', true)"
            ).scalar_one()
            == "dirty"
        )

    with engine.connect() as second_connection:
        second_pool_facts = (
            second_connection.exec_driver_sql(
                "SELECT pg_backend_pid() AS backend_pid, current_user, session_user, "
                "current_setting('default_transaction_read_only') AS default_read_only, "
                "current_setting('app.r1_pool_probe', true) AS pool_probe, "
                "txid_current_if_assigned() AS assigned_transaction_id"
            )
            .mappings()
            .one()
        )
        assert int(second_pool_facts["backend_pid"]) == first_backend_pid
        assert second_pool_facts["current_user"] == "campus_ai_school_fact_reader"
        assert second_pool_facts["session_user"] == "campus_ai_school_fact_reader"
        assert second_pool_facts["default_read_only"] == "on"
        assert second_pool_facts["pool_probe"] in (None, "")
        assert second_pool_facts["assigned_transaction_id"] is None

    with engine.connect() as poisoned_connection:
        poisoned_backend_pid = int(
            poisoned_connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
        )
        assert poisoned_backend_pid == first_backend_pid
        poisoned_connection.exec_driver_sql("SET SESSION default_transaction_read_only = off")
        poisoned_connection.commit()

    with pytest.raises(ReaderStartupRejected):
        with engine.connect():
            pass

    with engine.connect() as recovered_connection:
        recovered = recovered_connection.exec_driver_sql(
            "SELECT pg_backend_pid(), current_user, session_user, "
            "current_setting('default_transaction_read_only')"
        ).one()
        assert int(recovered[0]) != poisoned_backend_pid
        assert tuple(recovered[1:]) == (
            "campus_ai_school_fact_reader",
            "campus_ai_school_fact_reader",
            "on",
        )

    runtime_values = roles.runtime_connection_parameters
    runtime_engine = create_engine(
        URL.create(
            "postgresql+psycopg",
            username=str(runtime_values["user"]),
            password=str(runtime_values["password"]),
            host=str(runtime_values["host"]),
            port=int(runtime_values["port"]),
            database=str(runtime_values["dbname"]),
        )
    )
    with pytest.raises(ReaderStartupRejected):
        verify_school_fact_reader_database(runtime_engine, settings=settings)
    with pytest.raises(ReaderStartupRejected):
        verify_school_fact_reader_database(
            engine,
            settings=_production_settings(roles, facts, expected_anchor_id=uuid4()),
        )

    runtime_engine.dispose()
    engine.dispose()


def test_r1_reader_checkout_rejects_privileged_role_membership_drift(
    isolated_migration_schema: PostgresRoles,
) -> None:
    roles = isolated_migration_schema
    facts = _seed_four_projections(roles)
    settings = _production_settings(roles, facts, pool_size=1, max_overflow=0)
    engine: Engine | None = None
    with roles.admin_connection() as connection:
        connection.execute("GRANT campus_ai_db_owner TO campus_ai_school_fact_reader")
    try:
        engine = build_school_fact_reader_engine(settings)
        with pytest.raises(ReaderStartupRejected):
            verify_school_fact_reader_database(engine, settings=settings)
    finally:
        if engine is not None:
            engine.dispose()
        with roles.admin_connection() as connection:
            connection.execute("REVOKE campus_ai_db_owner FROM campus_ai_school_fact_reader")


def test_r1_real_reader_role_repeatable_read_projection_and_revocation(
    isolated_migration_schema: PostgresRoles,
) -> None:
    roles = isolated_migration_schema
    facts = _seed_four_projections(roles)
    engine = create_engine(_reader_url(roles))
    with engine.connect():
        pass
    statements: list[str] = []
    isolations: list[str] = []

    @event.listens_for(engine, "begin")
    def _record_isolation(connection: Any) -> None:
        isolations.append(str(connection.get_isolation_level()))

    @event.listens_for(engine, "before_cursor_execute")
    def _record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    actor = platform_contracts._issue_staff_actor_context(  # pyright: ignore[reportPrivateUsage]
        account_user_id=7,
        school_id=SchoolId(facts.school_id),
        person_id=PersonId(facts.person_id),
        assignment_id=AssignmentId(facts.assignment_id),
        assignment_version=1,
        authorization_version=1,
    )
    authorizer = _RevocableAuthorizer(facts.school_id)
    reader = SecureReader(
        registry=_test_registry(),
        compiler=ServerOwnedProjectionCompiler(),
        authorizer=authorizer,
        executor=SqlAlchemyRepeatableReadExecutor(sessionmaker(bind=engine)),
        cursor_codec=SecureCursorCodec(b"r1-postgres-test-secret"),
        clock=lambda: AS_OF,
    )
    expected_fields = {
        "test_only.r1.self_current_assignment": {
            "row_id",
            "organization_unit_id",
            "position_title",
            "semantic_role_code",
            "assignment_kind",
            "effective_from",
            "effective_to",
            "planned_valid_to",
            "version",
        },
        "test_only.r1.public_location": {
            "row_id",
            "kind",
            "business_code",
            "display_name",
            "display_path",
            "valid_from",
            "valid_to",
            "version",
        },
        "test_only.r1.official_office_contact": {
            "row_id",
            "owner_kind",
            "organization_unit_id",
            "location_id",
            "contact_kind",
            "contact_value",
            "service_hours",
            "version",
        },
    }
    expected_dto_types: dict[str, type[object]] = {
        "test_only.r1.self_current_assignment": SelfCurrentAssignmentRow,
        "test_only.r1.public_location": PublicLocationRow,
        "test_only.r1.official_office_contact": OfficialOfficeContactRow,
    }
    for operation_code, fields in expected_fields.items():
        before = len(statements)
        page = reader.read(
            SecureReadRequest(actor=actor, operation_code=operation_code, page_size=100)
        )
        assert page.rows
        assert type(page.rows[0]) is expected_dto_types[operation_code]
        assert _dto_field_names(page.rows[0]) == fields
        assert len(statements) - before == 2

    before = len(statements)
    with pytest.raises(SecureReadDenied, match="cannot be compiled"):
        reader.read(
            SecureReadRequest(
                actor=actor,
                operation_code="test_only.r1.restricted_policy_body",
                page_size=100,
            )
        )
    assert len(statements) == before

    before = len(statements)
    first = reader.read(
        SecureReadRequest(
            actor=actor,
            operation_code="test_only.r1.public_location",
            page_size=1,
        )
    )
    assert first.next_cursor is not None
    assert len(statements) - before == 2
    authorizer.deny_revalidation = authorizer.revalidations + 2
    before = len(statements)
    with pytest.raises(SecureReadAuthorizationDrift):
        reader.read(
            SecureReadRequest(
                actor=actor,
                operation_code="test_only.r1.public_location",
                cursor=first.next_cursor,
                page_size=1,
            )
        )
    assert len(statements) - before == 2
    assert len(statements) == 10
    assert authorizer.decisions == 6
    assert len(isolations) == 5
    assert all(value.upper() == "REPEATABLE READ" for value in isolations)
    with psycopg.connect(
        **roles.reader_connection_parameters,
        autocommit=True,
    ) as connection:
        assert connection.execute("SELECT current_user").fetchone() == (
            "campus_ai_school_fact_reader",
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                "SELECT content_text FROM public.school_policy_versions LIMIT 1"
            ).fetchone()
    engine.dispose()


@pytest.mark.integration
def test_r1_real_sf5a_governance_thin_authorization_vertical_slice(
    isolated_migration_schema: PostgresRoles,
) -> None:
    roles = isolated_migration_schema
    reader_api = _load_reader_api()
    with _writer_environment(roles) as writer_harness:
        duty, scope = _create_duty_and_scope(writer_harness, marker="r1_reader")
        grant = writer_harness.writer.create_grant(
            _grant_command(
                writer_harness,
                duty=duty,
                scope=scope,
                marker="r1_reader",
            )
        )
        state = _DependencyState()
        governance_reader = _reader_harness(reader_api, writer_harness, state)
        policy = _R1ThinModulePolicy(required_duty_code="test_only.r1_reader.duty")
        thin_service = ThinAuthorizationService(
            governance_basis_service=governance_reader.basis_service,
            module_policies={policy.module_name: policy},
        )
        with roles.admin_connection() as connection:
            anchor = connection.execute(
                "SELECT anchor_id, school_id FROM public.school_database_anchor"
            ).fetchone()
        assert anchor is not None
        anchor_id = UUID(str(anchor[0]))
        assert UUID(str(anchor[1])) == writer_harness.facts.school_id
        facts = _Facts(
            school_id=writer_harness.facts.school_id,
            person_id=writer_harness.facts.person_id,
            assignment_id=writer_harness.facts.assignment_id,
            anchor_id=anchor_id,
        )
        settings = _production_settings(roles, facts, pool_size=1, max_overflow=0)
        engine = build_school_fact_reader_engine(settings)
        verify_school_fact_reader_database(engine, settings=settings)
        statements: list[str] = []

        @event.listens_for(engine, "before_cursor_execute")
        def _record_statement(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            statements.append(statement)

        try:
            actor = platform_contracts._issue_staff_actor_context(  # pyright: ignore[reportPrivateUsage]
                account_user_id=writer_harness.facts.user_id,
                school_id=SchoolId(writer_harness.facts.school_id),
                person_id=PersonId(writer_harness.facts.person_id),
                assignment_id=AssignmentId(writer_harness.facts.assignment_id),
                assignment_version=1,
                authorization_version=1,
            )
            authorizer = ThinAuthorizationReadAdapter(
                thin_service=thin_service,
                scope_id_resolver=lambda _operation, _actor: scope.target_id,
            )
            reader = SecureReader(
                registry=_test_registry(),
                compiler=ServerOwnedProjectionCompiler(),
                authorizer=authorizer,
                executor=SqlAlchemyRepeatableReadExecutor(sessionmaker(bind=engine)),
                cursor_codec=SecureCursorCodec(b"r1-real-sf5a-thin-reader-secret"),
                clock=lambda: AS_OF,
            )
            page = reader.read(
                SecureReadRequest(
                    actor=actor,
                    operation_code="test_only.r1.public_location",
                    page_size=1,
                )
            )
            expected_fields = {
                "row_id",
                "kind",
                "business_code",
                "display_name",
                "display_path",
                "valid_from",
                "valid_to",
                "version",
            }
            assert page.rows
            assert all(
                type(row) is PublicLocationRow and _dto_field_names(row) == expected_fields
                for row in page.rows
            )
            assert page.next_cursor is not None
            second_page = reader.read(
                SecureReadRequest(
                    actor=actor,
                    operation_code="test_only.r1.public_location",
                    page_size=1,
                    cursor=page.next_cursor,
                )
            )
            assert second_page.rows
            assert all(
                type(row) is PublicLocationRow and _dto_field_names(row) == expected_fields
                for row in second_page.rows
            )
            assert {row.row_id for row in page.rows}.isdisjoint(
                row.row_id for row in second_page.rows
            )
            assert len(statements) == 4
            for dependency in ("assignment", "person", "organization", "scope"):
                state.invalid.add(dependency)
                before_denied = len(statements)
                with pytest.raises(SecureReadDenied):
                    reader.read(
                        SecureReadRequest(
                            actor=actor,
                            operation_code="test_only.r1.public_location",
                            page_size=1,
                        )
                    )
                assert len(statements) == before_denied
                state.invalid.remove(dependency)
            revoked_at = AS_OF + timedelta(seconds=1)
            revoked = writer_harness.writer.revoke_grant(
                writer_harness.api.RevokeGrantCommand(
                    school_id=writer_harness.facts.school_id,
                    grant_id=grant.target_id,
                    expected_version=1,
                    effective_at=revoked_at,
                    actor=_actor(writer_harness, reason="revoke R1 test-only authority"),
                    idempotency_key="sf5a:r1-reader:grant-revoke",
                )
            )
            assert revoked.target_version == 2
            revoked_reader = SecureReader(
                registry=_test_registry(),
                compiler=ServerOwnedProjectionCompiler(),
                authorizer=authorizer,
                executor=SqlAlchemyRepeatableReadExecutor(sessionmaker(bind=engine)),
                cursor_codec=SecureCursorCodec(b"r1-real-sf5a-thin-reader-secret"),
                clock=lambda: revoked_at,
            )
            before_revoked = len(statements)
            with pytest.raises(SecureReadDenied):
                revoked_reader.read(
                    SecureReadRequest(
                        actor=actor,
                        operation_code="test_only.r1.public_location",
                        page_size=1,
                    )
                )
            assert len(statements) == before_revoked
            regrant_at = revoked_at + timedelta(seconds=1)
            replacement = writer_harness.writer.create_grant(
                replace(
                    _grant_command(
                        writer_harness,
                        duty=duty,
                        scope=scope,
                        marker="r1_reader_replacement",
                    ),
                    valid_from=regrant_at,
                    effective_from=regrant_at,
                )
            )
            assert replacement.target_id != grant.target_id
            assert replacement.assignment_epoch == 3
            regranted_reader = SecureReader(
                registry=_test_registry(),
                compiler=ServerOwnedProjectionCompiler(),
                authorizer=authorizer,
                executor=SqlAlchemyRepeatableReadExecutor(sessionmaker(bind=engine)),
                cursor_codec=SecureCursorCodec(b"r1-real-sf5a-thin-reader-secret"),
                clock=lambda: regrant_at,
            )
            fresh_regranted_page = regranted_reader.read(
                SecureReadRequest(
                    actor=actor,
                    operation_code="test_only.r1.public_location",
                    page_size=1,
                )
            )
            assert fresh_regranted_page.rows
            before_stale_cursor = len(statements)
            with pytest.raises(SecureReadDenied, match="cursor authorization context is stale"):
                regranted_reader.read(
                    SecureReadRequest(
                        actor=actor,
                        operation_code="test_only.r1.public_location",
                        page_size=1,
                        cursor=page.next_cursor,
                    )
                )
            assert len(statements) == before_stale_cursor
        finally:
            engine.dispose()


def _walk_explain_plan(node: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    nodes = (node,)
    for child in node.get("Plans", ()):
        if isinstance(child, dict):
            nodes += _walk_explain_plan(child)
    return nodes


def _insert_bulk_location_lineage(
    connection: psycopg.Connection[Any],
    *,
    school_id: UUID,
    total_generated: int,
    effective_at: datetime,
) -> UUID:
    actor_row = connection.execute(
        "SELECT confirmed_by_user_id, confirmed_by_person_id, "
        "confirmed_by_assignment_id FROM master_data_import_batches "
        "WHERE school_id = %s AND mode = 'canonical_atomic_changes' "
        "ORDER BY created_at DESC LIMIT 1",
        (school_id,),
    ).fetchone()
    assert actor_row is not None
    actor_user_id, actor_person_id, actor_assignment_id = actor_row
    for setting_name, setting_value in (
        ("app.context_purpose", "command"),
        ("app.context_kind", "assignment"),
        ("app.account_user_id", str(actor_user_id)),
        ("app.person_id", str(actor_person_id)),
        ("app.assignment_id", str(actor_assignment_id)),
        ("app.assignment_version", "1"),
    ):
        connection.execute("SELECT set_config(%s, %s, true)", (setting_name, setting_value))

    batch_id = uuid4()
    source_sha256 = batch_id.hex * 2
    idempotency_record_id = uuid4()
    idempotency_key_hash = uuid4().hex * 2
    plan_sha256 = uuid4().hex * 2
    confirmed_at = datetime.now(UTC).replace(microsecond=0)
    connection.execute(
        """
        INSERT INTO master_data_import_batches (
            id, school_id, mode, source_kind, source_ref, source_sha256,
            worksheet_names, status, planned_units, succeeded_units,
            failed_units, pending_units, confirmed_by_user_id,
            confirmed_by_person_id, confirmed_by_assignment_id, confirmed_at,
            request_id, idempotency_key_hash, plan_sha256, base_fact_version,
            target_fact_version, confirmation_token_sha256,
            confirmation_expires_at, confirmation_consumed_at,
            confirmation_session_sha256, staged_by_user_id,
            staged_by_person_id, staged_by_assignment_id,
            staged_assignment_version, staged_authorization_version, version
        ) VALUES (
            %s, %s, 'canonical_atomic_changes', 'manual', %s, %s,
            '[]'::jsonb, 'applying', %s, %s,
            0, 0, %s,
            %s, %s, %s,
            %s, %s, %s, 1,
            2, %s,
            %s, %s,
            %s, %s,
            %s, %s,
            1, 1, 1
        )
        """,
        (
            batch_id,
            school_id,
            f"r1-perf:{batch_id}",
            source_sha256,
            total_generated,
            total_generated,
            actor_user_id,
            actor_person_id,
            actor_assignment_id,
            confirmed_at,
            uuid4(),
            idempotency_key_hash,
            plan_sha256,
            uuid4().hex * 2,
            confirmed_at + timedelta(hours=1),
            confirmed_at,
            uuid4().hex * 2,
            actor_user_id,
            actor_person_id,
            actor_assignment_id,
        ),
    )
    connection.execute(
        "SET CONSTRAINTS verify_master_data_import_batches_terminal_consistency IMMEDIATE"
    )
    connection.execute(
        """
        WITH generated AS (
            SELECT ordinal,
                   md5(%s || ':perf-location:' || ordinal::text)::uuid AS location_id,
                   md5(%s || ':perf-unit:' || ordinal::text)::uuid AS unit_id
              FROM generate_series(1, %s) AS series(ordinal)
        )
        INSERT INTO master_data_import_units (
            id, school_id, batch_id, sequence, row_number, unit_kind, status,
            payload, payload_sha256, soft_anomalies, initial_soft_anomalies,
            target_id, target_version, version
        )
        SELECT unit_id, %s, %s, ordinal, ordinal, 'school_fact', 'applied',
               jsonb_build_object(
                   'fact_kind', 'location',
                   'source_key', 'location:r1-perf:' || %s || ':' || ordinal::text,
                   'operation', 'upsert',
                   'effective_at', CAST(%s AS text),
                   'expected_version', 0
               ),
               md5(unit_id::text) || md5(unit_id::text),
               '[]'::jsonb, '[]'::jsonb, location_id, 1, 1
          FROM generated
        """,
        (
            str(school_id),
            str(school_id),
            total_generated,
            school_id,
            batch_id,
            str(school_id),
            effective_at.isoformat(),
        ),
    )
    connection.execute(
        "SET CONSTRAINTS verify_master_data_import_units_insert_terminal_consistency IMMEDIATE"
    )
    connection.execute(
        """
        WITH generated AS (
            SELECT ordinal,
                   md5(%s || ':perf-location:' || ordinal::text)::uuid AS location_id,
                   md5(%s || ':perf-unit:' || ordinal::text)::uuid AS unit_id,
                   md5(%s || ':perf-lineage:' || ordinal::text)::uuid AS lineage_id
              FROM generate_series(1, %s) AS series(ordinal)
        )
        INSERT INTO school_fact_change_lineage (
            id, school_id, batch_id, unit_id, fact_kind, fact_source_key,
            operation, target_id, target_version, source_sha256,
            sheet_code, row_number
        )
        SELECT lineage_id, %s, %s, unit_id, 'location',
               'location:r1-perf:' || %s || ':' || ordinal::text,
               'upsert', location_id, 1, %s, 'r1_perf_fixture', ordinal
          FROM generated
        """,
        (
            str(school_id),
            str(school_id),
            str(school_id),
            total_generated,
            school_id,
            batch_id,
            str(school_id),
            source_sha256,
        ),
    )
    connection.execute(
        """
        INSERT INTO command_idempotency_records (
            id, school_id, action_code, actor_user_id, actor_person_id,
            actor_context_type, actor_context_key, actor_assignment_id,
            actor_assignment_version, resource_type, resource_id,
            idempotency_key_hash, request_sha256, status, result_type,
            result_payload
        ) VALUES (
            %s, %s, 'school_facts.confirm', %s, %s,
            'assignment', %s, %s,
            1, 'master_data.import_batch', %s,
            %s, %s, 'succeeded', 'master_data.import_outcome',
            jsonb_build_object(
                'resource_type', 'master_data.import_batch',
                'resource_id', CAST(%s AS text),
                'resource_version', 1,
                'outcome_code', 'applied'
            )
        )
        """,
        (
            idempotency_record_id,
            school_id,
            actor_user_id,
            actor_person_id,
            f"assignment:{actor_assignment_id}:1",
            actor_assignment_id,
            str(batch_id),
            idempotency_key_hash,
            plan_sha256,
            str(batch_id),
        ),
    )
    connection.execute(
        "SET CONSTRAINTS verify_command_idempotency_records_terminal_consistency IMMEDIATE"
    )
    connection.execute(
        """
        INSERT INTO master_data_import_command_outcomes (
            id, school_id, idempotency_record_id, batch_id, action_code,
            status, batch_version, succeeded_units, failed_units, executed_units
        ) VALUES (
            %s, %s, %s, %s, 'school_facts.confirm',
            'applied', 1,
            ARRAY(SELECT generate_series(1, %s)),
            ARRAY[]::integer[],
            ARRAY(SELECT generate_series(1, %s))
        )
        """,
        (
            uuid4(),
            school_id,
            idempotency_record_id,
            batch_id,
            total_generated,
            total_generated,
        ),
    )
    connection.execute(
        "SET CONSTRAINTS verify_master_data_import_command_outcomes_terminal_consistency IMMEDIATE"
    )
    connection.execute(
        "UPDATE master_data_import_batches SET status = 'applied' WHERE school_id = %s AND id = %s",
        (school_id, batch_id),
    )
    return batch_id


def test_r1_reader_50k_projection_is_bounded_and_has_real_explain_evidence(
    isolated_migration_schema: PostgresRoles,
    record_property: Any,
) -> None:
    roles = isolated_migration_schema
    facts = _seed_four_projections(roles)
    total_generated = 50_000
    with roles.admin_connection() as connection, connection.transaction():
        connection.execute("SET LOCAL search_path = public, pg_catalog")
        # The canonical batch below owns the 50k source evidence.
        connection.execute(
            """
            WITH generated AS (
                SELECT ordinal,
                       md5(%s || ':perf-location:' || ordinal::text)::uuid AS location_id
                  FROM generate_series(1, %s) AS series(ordinal)
            )
            INSERT INTO campus_locations
                (id, school_id, source_key, parent_location_id, kind, status, version)
            SELECT location_id, %s,
                   'location:r1-perf:' || %s || ':' || ordinal::text,
                   NULL, 'campus', 'active', 1
              FROM generated
            """,
            (
                str(facts.school_id),
                total_generated,
                facts.school_id,
                str(facts.school_id),
            ),
        )
        lineage_batch_id = _insert_bulk_location_lineage(
            connection,
            school_id=facts.school_id,
            total_generated=total_generated,
            effective_at=AS_OF - timedelta(days=1),
        )
        connection.execute(
            """
            WITH generated AS (
                SELECT ordinal,
                       md5(%s || ':perf-location:' || ordinal::text)::uuid AS location_id,
                       md5(%s || ':version:' || ordinal::text)::uuid AS version_id
                  FROM generate_series(1, %s) AS series(ordinal)
            )
            INSERT INTO campus_location_versions
                (id, school_id, location_id, parent_location_id, kind,
                 business_code, display_name, display_path, valid_from, valid_to,
                 source_kind, source_ref, status, version)
            SELECT version_id, %s, location_id, NULL, 'campus',
                   'PERF-' || ordinal::text,
                   'Performance Campus ' || ordinal::text,
                   '/Performance Campus ' || ordinal::text,
                   %s, NULL, 'manual', %s, 'confirmed', 1
              FROM generated
            """,
            (
                str(facts.school_id),
                str(facts.school_id),
                total_generated,
                facts.school_id,
                AS_OF - timedelta(days=1),
                f"change-set:{lineage_batch_id}",
            ),
        )
        connection.execute("ANALYZE campus_locations")
        connection.execute("ANALYZE campus_location_versions")

    engine = create_engine(_reader_url(roles))
    actor = platform_contracts._issue_staff_actor_context(  # pyright: ignore[reportPrivateUsage]
        account_user_id=7,
        school_id=SchoolId(facts.school_id),
        person_id=PersonId(facts.person_id),
        assignment_id=AssignmentId(facts.assignment_id),
        assignment_version=1,
        authorization_version=1,
    )
    operation = _test_registry().require("test_only.r1.public_location")
    scope_plan = ScopePlan(
        scope_id=facts.school_id,
        owner="test-only.r1.school",
        reference=f"school:{facts.school_id}",
        version=1,
    )
    relation = ServerOwnedProjectionCompiler().compile(
        operation=operation,
        actor=actor,
        scope_plan=scope_plan,
        as_of=AS_OF,
        after_id=None,
        page_size=100,
    )
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    reader = SecureReader(
        registry=_test_registry(),
        compiler=ServerOwnedProjectionCompiler(),
        authorizer=_RevocableAuthorizer(facts.school_id),
        executor=SqlAlchemyRepeatableReadExecutor(sessionmaker(bind=engine)),
        cursor_codec=SecureCursorCodec(b"r1-postgres-50k-reader-secret"),
        clock=lambda: AS_OF,
    )
    page = reader.read(
        SecureReadRequest(
            actor=actor,
            operation_code=operation.code,
            page_size=100,
        )
    )

    assert page.total_count == total_generated + 2
    assert len(page.rows) == 100
    assert page.next_cursor is not None
    assert len(statements) == 2
    expected_fields = {
        "row_id",
        "kind",
        "business_code",
        "display_name",
        "display_path",
        "valid_from",
        "valid_to",
        "version",
    }
    assert all(
        type(row) is PublicLocationRow and _dto_field_names(row) == expected_fields
        for row in page.rows
    )
    assert all("select *" not in statement.lower() for statement in statements)
    assert all("campus_locations.school_id" in statement.lower() for statement in statements)

    data_sql = str(
        relation.data_statement.compile(
            engine,
            compile_kwargs={"literal_binds": True},
        )
    )
    count_sql = str(
        relation.count_statement.compile(
            engine,
            compile_kwargs={"literal_binds": True},
        )
    )
    with engine.connect() as connection:
        data_explain = connection.exec_driver_sql(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + data_sql
        ).scalar_one()
        count_explain = connection.exec_driver_sql(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + count_sql
        ).scalar_one()

    assert data_explain[0]["Plan"]["Node Type"] == "Limit"
    assert data_explain[0]["Plan"]["Actual Rows"] <= 101
    assert count_explain[0]["Plan"]["Node Type"] == "Aggregate"
    assert count_explain[0]["Plan"]["Actual Rows"] == 1

    data_plan = data_explain[0]["Plan"]
    data_nodes = _walk_explain_plan(data_plan)
    location_index_nodes = tuple(
        node
        for node in data_nodes
        if (
            node.get("Relation Name") == "campus_locations"
            and node.get("Node Type") in {"Index Scan", "Index Only Scan"}
        )
    )
    assert location_index_nodes
    explain_measurements = {
        "data_planning_ms": data_explain[0]["Planning Time"],
        "data_execution_ms": data_explain[0]["Execution Time"],
        "data_shared_hit_blocks": data_plan.get("Shared Hit Blocks", 0),
        "data_shared_read_blocks": data_plan.get("Shared Read Blocks", 0),
        "count_planning_ms": count_explain[0]["Planning Time"],
        "count_execution_ms": count_explain[0]["Execution Time"],
        "location_index": location_index_nodes[0].get("Index Name"),
    }
    assert all(value >= 0 for key, value in explain_measurements.items() if key != "location_index")
    record_property("r1_50k_explain", explain_measurements)
    for explain in (data_explain, count_explain):
        assert explain[0]["Planning Time"] >= 0
        assert explain[0]["Execution Time"] >= 0
        assert explain[0]["Plan"]["Shared Hit Blocks"] >= 0

    engine.dispose()
