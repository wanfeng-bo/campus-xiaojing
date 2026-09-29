"""Actual Reader/Writer SQL with a deliberately test-only current-authority port.

The fixture publishes only disposable canonical facts. It does not claim to test
Governance issuance: its exact-scope double isolates the new metadata boundary.
"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import URL, create_engine, event, text

from app.composition.school_fact_own_result_authorization import F2OwnResultAuthorizer
from app.composition.school_fact_writer_executor import SqlAlchemySchoolFactWriterExecutorKernel
from app.platform.authorization.f2_reader_compiler import compile_f2
from app.platform.authorization.secure_reader_contracts import ReadAuthorization, SecureReadDenied
from app.platform.authorization.secure_reader_registry import f2_operation_registry
from app.platform.authorization.thin_contracts import NamedProjection, ScopePlan
from app.platform.school_facts.application import SchoolFactChangeRejected, SchoolFactChangeService
from app.platform.school_facts.contracts import CanonicalFactChangeSetV2
from app.platform.school_facts.f2_policy import F2_FACT_KINDS, F2FactScope
from app.platform.school_facts.ports import SchoolFactChangeProvenance
from tests.school_facts.test_f1_product_postgres import _role_settings
from tests.school_facts.test_sf2_migration import _run_alembic
from tests.school_facts.test_sf2_transaction import context_for, sf2_environment


class ExactCurrentAuthority:
    """Only the authority decision is synthetic; SQL and terminal receipts are real."""

    def __init__(self, context):
        self.context = context
        self.grants = {}
        self.revoked = False

    def require_current_many(self, actor):
        assert actor == self.context.actor
        if self.revoked:
            raise SecureReadDenied("test current authority revoked")
        result = []
        registry = f2_operation_registry()
        for kind in sorted(F2_FACT_KINDS):
            if kind not in self.grants:
                result.append((kind, ReadAuthorization(False, None, None, None, None)))
                continue
            keys = self.grants[kind]
            operation = registry.require(f"school_facts.f2.{kind}.read")
            scope = F2FactScope(actor.school_id, kind, tuple(sorted(keys)))
            result.append(
                (
                    kind,
                    ReadAuthorization(
                        True,
                        object(),
                        ScopePlan(uuid4(), "school_facts.f2", scope.reference, 1),
                        NamedProjection(operation.projection_name, 1),
                        "test-current-basis",
                    ),
                )
            )
        return tuple(result)

    def require_current(self, actor, kind, *, purpose):
        assert purpose == "read"
        for current_kind, authorization in self.require_current_many(actor):
            if current_kind == kind:
                return authorization
        raise SchoolFactChangeRejected("no exact current target authority")

    def revalidate(self, authorization, actor):
        self.revalidate_many((authorization,), actor)

    def revalidate_many(self, authorizations, actor):
        assert actor == self.context.actor
        if self.revoked:
            raise SecureReadDenied("test current authority revoked")


@pytest.fixture
def receipt_visibility(school_facts_f1_schema):
    roles = school_facts_f1_schema
    # F2's deployed NOLOGIN technical role must exist before revision23. This
    # fixture creates no usable credential, grants or memberships in this cluster.
    roles.verify_disposable_cluster()
    with roles.admin_connection() as admin:
        if (
            admin.execute(
                "SELECT 1 FROM pg_roles WHERE rolname='campus_ai_governance_writer'"
            ).fetchone()
            is None
        ):
            admin.execute(
                "CREATE ROLE campus_ai_governance_writer NOLOGIN NOSUPERUSER NOINHERIT "
                "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            )
    upgraded = _run_alembic(roles, "upgrade", "20260906_25")
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    with roles.admin_connection() as admin:
        assert admin.execute("SELECT version_num FROM public.alembic_version").fetchone() == (
            "20260906_25",
        )
    with sf2_environment(roles) as (owner, persistence, facts):
        school, user, person, assignment = facts
        now = datetime.now(UTC)
        context = context_for(
            school_id=school,
            user_id=user,
            person_id=person,
            assignment_id=assignment,
            key=f"receipt-visibility:{uuid4().hex}",
        )
        service = SchoolFactChangeService(reader=persistence, writer=persistence, clock=lambda: now)
        version = 1

        def publish(kind, key, content, *, expected=0, effective=None, operation="upsert"):
            nonlocal version
            marker = uuid4().hex
            changes = CanonicalFactChangeSetV2.model_validate(
                {
                    "schema_version": 2,
                    "base_version": version,
                    "source_sha256": sha256(marker.encode()).hexdigest(),
                    "changes": (
                        {
                            "schema_version": 2,
                            "fact_kind": kind,
                            "source_key": key,
                            "operation": operation,
                            "content": content,
                            "expected_version": expected,
                            "effective_at": effective or now - timedelta(hours=1),
                            "origin": {"sheet_code": "receipt_visibility", "row_number": 1},
                        },
                    ),
                }
            )
            locator = sha256(("sf3plan_" + marker).encode()).hexdigest()
            provenance = SchoolFactChangeProvenance(
                channel="admin",
                raw_source_sha256=changes.source_sha256,
                template_version="school_facts_v2",
                mapping_version="admin_v1",
                mapping_config_sha256="c" * 64,
                source_size_bytes=None,
                worksheet_names=(),
                result_locator_sha256=locator,
            )
            current = context_for(
                school_id=school,
                user_id=user,
                person_id=person,
                assignment_id=assignment,
                key=f"publish:{marker}",
            )
            prepared = service.prepare_confirmation(
                change_set=changes,
                context=current,
                session_binding="original-session",
                provenance=provenance,
            )
            applied = service.confirm(
                credential=prepared.credential,
                change_set=changes,
                context=current,
                session_binding="original-session",
            )
            version = applied.target_version
            return locator

        with owner() as session:
            school_key = session.scalar(
                text("SELECT source_key FROM schools WHERE id=:id"), {"id": school}
            )
            anchor = session.scalar(text("SELECT anchor_id FROM school_database_anchor"))
        publish(
            "school",
            school_key,
            {
                "business_code": "RECEIPT-VISIBILITY",
                "display_name": "Disposable receipt school",
                "timezone_name": "Asia/Shanghai",
                "lifecycle_status": "active",
            },
            expected=1,
        )
        read_values = roles.reader_connection_parameters
        write_values = roles.writer_connection_parameters

        def engine(values):
            return create_engine(
                URL.create(
                    "postgresql+psycopg",
                    username=str(values["user"]),
                    password=str(values["password"]),
                    host=str(values["host"]),
                    port=int(values["port"]),
                    database=str(values["dbname"]),
                ),
                pool_size=1,
                max_overflow=0,
                pool_timeout=0.5,
            )

        reader_engine, writer_engine = engine(read_values), engine(write_values)
        for selected, expected in (
            (reader_engine, "campus_ai_school_fact_reader"),
            (writer_engine, "campus_ai_school_fact_writer"),
        ):
            with selected.connect() as connection:
                assert connection.scalar(text("SELECT current_user")) == expected
        current_authority = ExactCurrentAuthority(context)
        current_authority.grants["school"] = (school_key,)
        authorizer = F2OwnResultAuthorizer(current_authority)
        settings = _role_settings(roles, school, anchor, writer=True).model_copy(
            update={"schema_contract": "school-facts-f2-v25"}
        )
        kernel = SqlAlchemySchoolFactWriterExecutorKernel(
            engine=writer_engine,
            settings=settings,
            final_authorization_guard=lambda *_: None,
            own_result_authorizer=authorizer,
        )
        metadata_sql, receipt_rows = [], []

        def observe_metadata(_conn, _cursor, statement, _params, _context, _many):
            if "school_fact_change_lineage" in statement:
                metadata_sql.append(statement)

        def observe_receipt(_conn, cursor, statement, _params, _context, _many):
            if "master_data_import_batches.result_payload" in statement:
                receipt_rows.append(cursor.rowcount)

        event.listen(reader_engine, "before_cursor_execute", observe_metadata)
        event.listen(writer_engine, "after_cursor_execute", observe_receipt)
        try:
            yield SimpleNamespace(
                now=now,
                context=context,
                publish=publish,
                authority=current_authority,
                authorizer=authorizer,
                kernel=kernel,
                metadata_sql=metadata_sql,
                receipt_rows=receipt_rows,
                reader_engine=reader_engine,
                writer_engine=writer_engine,
            )
        finally:
            assert reader_engine.pool.checkedout() == writer_engine.pool.checkedout() == 0
            reader_engine.dispose()
            writer_engine.dispose()


def lookup(fixture, locator):
    return fixture.kernel.check_own_result_by_locator(
        context=fixture.context,
        session_binding="new-login-session",
        locator_sha256=locator,
    )


def official(*, visibility="public"):
    return {
        "owner_kind": "school",
        "contact_kind": "phone",
        "contact_value": "000-00000001",
        "lifecycle_status": "active",
        "visibility_scope": visibility,
        **({"audience_code": "explicit-test-audience"} if visibility == "restricted" else {}),
    }


@pytest.mark.parametrize("kind", ["person_contact", "official_contact"])
def test_private_or_restricted_receipt_needs_exact_kind_target_authority_before_payload(
    receipt_visibility,
    kind,
):
    fixture = receipt_visibility
    key = "receipt:restricted-target"
    if kind == "person_contact":
        fixture.publish(
            "person",
            "receipt:other-person",
            {
                "display_name": "Disposable other person",
                "person_kind": "staff",
                "lifecycle_status": "active",
            },
        )
        content = {
            "person_source_key": "receipt:other-person",
            "contact_kind": "mobile",
            "contact_value": "00000000000",
            "is_primary": True,
            "visibility_scope": "private",
        }
    else:
        content = official(visibility="restricted")
    locator = fixture.publish(kind, key, content)
    # School-directory authority alone never supplies contact authority.
    assert lookup(fixture, locator) is None
    assert fixture.receipt_rows and fixture.receipt_rows[-1] == 0
    fixture.authority.grants[kind] = (key,)
    item = lookup(fixture, locator)
    assert item is not None and item.locator_sha256 == locator
    assert fixture.receipt_rows[-1] == 1
    assert (kind, key) in fixture.authorizer(fixture.context).allowed_targets
    assert fixture.metadata_sql == [], (
        "receipt recovery must not read historical/current fact bodies"
    )
    retained = fixture.authorizer(fixture.context)
    fixture.authority.revoked = True
    count = len(fixture.receipt_rows)
    with pytest.raises((SecureReadDenied, SchoolFactChangeRejected)):
        retained.revalidate()
    with pytest.raises((SecureReadDenied, SchoolFactChangeRejected)):
        lookup(fixture, locator)
    assert len(fixture.receipt_rows) == count


@pytest.mark.parametrize("period", ["future", "ended"])
def test_historical_receipt_remains_readable_when_current_formal_fact_is_empty(
    receipt_visibility, period
):
    fixture = receipt_visibility
    key = "receipt:temporal-target"
    locator = fixture.publish(
        "official_contact",
        key,
        official(),
        effective=fixture.now + timedelta(days=1) if period == "future" else None,
    )
    if period == "ended":
        locator = fixture.publish(
            "official_contact",
            key,
            None,
            expected=1,
            operation="end",
            effective=fixture.now - timedelta(minutes=1),
        )
    fixture.authority.grants["official_contact"] = (key,)
    operation = f2_operation_registry().require("school_facts.f2.official_contact.read")
    scope = F2FactScope(fixture.context.actor.school_id, "official_contact", (key,))
    relation = compile_f2(
        operation=operation,
        actor=fixture.context.actor,
        scope_plan=ScopePlan(uuid4(), "school_facts.f2", scope.reference, 1),
        as_of=fixture.now,
        after_id=None,
        page_size=10,
        selection=None,
    )
    with fixture.reader_engine.connect() as connection:
        assert connection.execute(relation.count_statement).scalar_one() == 0
        assert connection.execute(relation.data_statement).all() == []
    assert ("official_contact", key) in fixture.authorizer(fixture.context).allowed_targets
    recovered = lookup(fixture, locator)
    assert recovered is not None and recovered.locator_sha256 == locator
    assert fixture.receipt_rows[-1] == 1


def test_quality_receipt_metadata_needs_its_own_exact_scope_without_reading_statement_body(
    receipt_visibility,
):
    fixture = receipt_visibility
    fixture.publish("official_contact", "receipt:quality-target", official())
    key = "receipt:quality"
    locator = fixture.publish(
        "fact_quality",
        key,
        {
            "quality_kind": "source",
            "target_fact_kind": "official_contact",
            "owner_kind": "school",
            "target_source_key": "receipt:quality-target",
            "basis_reference": "disposable fixture",
            "responsibility": "test",
            "verified_at": fixture.now - timedelta(hours=1),
            "source_status": "available",
        },
    )
    assert lookup(fixture, locator) is None
    assert fixture.receipt_rows[-1] == 0
    fixture.authority.grants["fact_quality"] = (key,)
    assert ("fact_quality", key) in fixture.authorizer(fixture.context).allowed_targets
    assert lookup(fixture, locator) is not None
    assert fixture.receipt_rows[-1] == 1
    assert fixture.metadata_sql == [], "receipt recovery must not read quality statement bodies"
