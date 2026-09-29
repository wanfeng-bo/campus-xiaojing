"""Real Writer capacity and bounded receipt reads in a separately cloned disposable DB.

Opt in with F7_RECEIPT_CAPACITY_DATABASE=f7_receipt_capacity_20260924 and
F7_TEST_DATABASE_CONFIG. The source fixture database is never connected here.
Provision the clone without school_database_anchor data, then use the existing
provision_school_database_anchor function to bind the clone's database identity.
Only authority decisions are controlled doubles; persistence, RLS identity,
transactions, receipt SQL and F2 target/projection compilation are real.
"""

from __future__ import annotations

import json
import os
import time
import tracemalloc
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from pydantic import SecretStr
from sqlalchemy import URL, create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.composition.school_fact_own_result_authorization import F2OwnResultAuthorizer
from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.composition.school_fact_writer_executor import SqlAlchemySchoolFactWriterExecutorKernel
from app.composition.school_facts import HmacSchoolFactConfirmationCredentialIssuer
from app.platform.authorization.secure_reader_contracts import ReadAuthorization, SecureReadDenied
from app.platform.authorization.thin_contracts import NamedProjection, ScopePlan
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.school_facts.application import SchoolFactChangeRejected, SchoolFactChangeService
from app.platform.school_facts.contracts import CanonicalFactChangeSetV3
from app.platform.school_facts.f2_policy import F2FactScope
from app.platform.school_facts.ports import SchoolFactChangeProvenance
from app.platform.school_facts.read_policy import SchoolFactAuthorizationUnavailable
from tests.school_facts.test_sf2_transaction import context_for
from tests.school_facts.test_sf5b_writer_executor import _writer_settings

DATABASE = "f7_receipt_capacity_20260924"
CURRENT_VERSION_SQL = (
    "SELECT COALESCE(MAX(target_fact_version),1) FROM master_data_import_batches "
    "WHERE school_id=:school AND mode='canonical_atomic_changes' AND status='applied'"
)
pytestmark = pytest.mark.skipif(
    os.environ.get("F7_RECEIPT_CAPACITY_DATABASE") != DATABASE
    or not os.environ.get("F7_TEST_DATABASE_CONFIG"),
    reason="explicit separate F7 disposable receipt capacity database required",
)


@contextmanager
def _capacity_engines():
    config = json.loads(Path(os.environ["F7_TEST_DATABASE_CONFIG"]).read_text(encoding="utf-8"))
    assert config["owner"] == "F7 isolated quality migration and reader tests"
    assert config["container"] == "campus-f7-tests-20260924"
    assert config["database"] == "f7_fixture_test"
    assert DATABASE != config["database"]

    def engine_for(role):
        def connect():
            try:
                return psycopg.connect(
                    host=config["container"], port=config["port"], dbname=DATABASE,
                    user="campus_ai_migration" if role == "migration"
                    else "campus_ai_school_fact_writer",
                    password=config[role],
                    options="-c role=campus_ai_db_owner" if role == "migration" else "",
                    connect_timeout=5,
                )
            except Exception:
                raise RuntimeError("Separate disposable receipt database unavailable") from None
        return create_engine(
            URL.create("postgresql+psycopg", username=role), creator=connect,
            poolclass=NullPool, hide_parameters=True,
        )

    owner, writer = engine_for("migration"), engine_for("writer")
    try:
        with owner.connect() as connection:
            assert connection.scalar(text("SELECT current_database()")) == DATABASE
            assert str(connection.scalar(text(
                "SELECT system_identifier FROM pg_control_system()"
            ))) == config["system_identifier"]
            assert connection.scalar(text("SELECT app.require_school_database_anchor()"))
            anchor = connection.execute(text(
                "SELECT anchor_id,school_id FROM school_database_anchor"
            )).one()
            staff = connection.execute(text(
                "SELECT a.id,a.person_id,b.account_user_id FROM staff_assignments a "
                "JOIN account_person_bindings b ON b.school_id=a.school_id "
                "AND b.person_id=a.person_id WHERE a.school_id=:school "
                "ORDER BY a.created_at LIMIT 1"
            ), {"school": anchor.school_id}).one()
        yield config, owner, writer, anchor, staff
    finally:
        owner.dispose()
        writer.dispose()


def _publish(config, owner, writer, anchor, staff, count):
    if os.environ.get("F7_RECEIPT_CAPACITY_REUSE") == "1":
        with owner.connect() as connection:
            row = connection.execute(text(
                "SELECT b.id,b.mapping_payload->>'result_locator_sha256' AS locator,"
                "b.result_payload->>'changed_count' AS changed_count,"
                "b.confirmed_by_user_id,b.confirmed_by_person_id,"
                "b.confirmed_by_assignment_id,b.confirmation_token_sha256,"
                "b.confirmation_consumed_at "
                "FROM master_data_import_batches b "
                "WHERE b.school_id=:school AND b.mode='canonical_atomic_changes' "
                "AND b.status='applied' AND b.succeeded_units=:count "
                "ORDER BY b.created_at DESC LIMIT 1"
            ), {"school": anchor.school_id, "count": count}).one()
            first = connection.execute(text(
                "SELECT fact_source_key FROM school_fact_change_lineage "
                "WHERE school_id=:school AND batch_id=:batch AND row_number=1"
            ), {"school": anchor.school_id, "batch": row.id}).scalar_one()
            last = connection.execute(text(
                "SELECT fact_source_key FROM school_fact_change_lineage "
                "WHERE school_id=:school AND batch_id=:batch AND row_number=:count"
            ), {"school": anchor.school_id, "batch": row.id, "count": count}).scalar_one()
            lineage_count = connection.scalar(text(
                "SELECT count(*) FROM school_fact_change_lineage "
                "WHERE school_id=:school AND batch_id=:batch"
            ), {"school": anchor.school_id, "batch": row.id})
        prefix = first.rsplit(":", 1)[0] + ":"
        assert row.locator and len(row.locator) == 64
        assert row.changed_count == str(count) and lineage_count == count
        # Canonical confirmation replays by its consumed token and result payload;
        # active_idempotency_record_id is not populated on this direct Writer path.
        assert row.confirmation_token_sha256 and row.confirmation_consumed_at
        assert first == prefix + "00000" and last == prefix + f"{count-1:05d}"
        assert (row.confirmed_by_user_id, row.confirmed_by_person_id,
                row.confirmed_by_assignment_id) == (
                    staff.account_user_id, staff.person_id, staff.id,
                )
        context = context_for(
            school_id=anchor.school_id, user_id=staff.account_user_id,
            person_id=staff.person_id, assignment_id=staff.id,
            key="f7-capacity:read-existing:" + str(count),
        )
        print(json.dumps({"targets": count, "formal_batch_reused": True,
                          "committed_lineage_rows": lineage_count,
                          "original_confirmation_consumed": True,
                          "replay_basis": "consumed confirmation token and result payload"}))
        return context, row.locator, prefix
    marker = uuid4().hex
    prefix = f"f7-capacity:{marker}:"
    locator = sha256(marker.encode()).hexdigest()
    with owner.connect() as connection:
        base = connection.scalar(text(CURRENT_VERSION_SQL), {"school": anchor.school_id})
    now = datetime.now(UTC)
    changes = CanonicalFactChangeSetV3.model_validate({
        "schema_version": 3, "base_version": base, "source_sha256": locator,
        "changes": tuple({
            "schema_version": 3, "fact_kind": "person", "operation": "upsert",
            "source_key": prefix + f"{n:05d}", "expected_version": 0,
            "effective_at": now,
            "origin": {"sheet_code": "f7-receipt-capacity", "row_number": n + 1},
            "content": {"display_name": "合成容量人员", "person_kind": "other",
                        "lifecycle_status": "active"},
        } for n in range(count)),
    })
    signer = ContextProofSigner(SecretStr(config["context"]))
    binding = sha256(b"capacity-original-login").hexdigest()
    persistence = SqlAlchemySchoolFactPersistence(
        sessionmaker(writer, expire_on_commit=False),
        lambda context: signer.sign_execution_context(
            context, session_nonce_hash=binding,
            proof_expires_at=datetime.now(UTC) + timedelta(minutes=30),
        ),
        confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
            b"f7-owned-capacity-confirmation-test-key"
        ),
    )
    service = SchoolFactChangeService(reader=persistence, writer=persistence)
    context = context_for(
        school_id=anchor.school_id, user_id=staff.account_user_id,
        person_id=staff.person_id, assignment_id=staff.id, key="f7-capacity:" + marker,
    )
    provenance = SchoolFactChangeProvenance(
        channel="admin", raw_source_sha256=locator, template_version="f7-capacity-test",
        mapping_version="f7-capacity-test", mapping_config_sha256=locator,
        source_size_bytes=None, worksheet_names=(), result_locator_sha256=locator,
    )
    started = time.monotonic()
    prepared = service.prepare_confirmation(
        change_set=changes, context=context, session_binding=binding, provenance=provenance,
    )
    applied = service.confirm(
        credential=prepared.credential, change_set=changes,
        context=context, session_binding=binding,
    )
    assert applied.changed_count == count and not applied.replayed
    replay = service.confirm(
        credential=prepared.credential, change_set=changes,
        context=context, session_binding=binding,
    )
    assert replay.replayed and replay.batch_id == applied.batch_id
    with owner.connect() as connection:
        assert connection.scalar(text(
            "SELECT count(*) FROM school_fact_change_lineage WHERE batch_id=:batch"
        ), {"batch": applied.batch_id}) == count
        assert connection.scalar(
            text(CURRENT_VERSION_SQL), {"school": anchor.school_id}
        ) == base + 1
    print(json.dumps({"targets": count, "formal_publish_seconds": time.monotonic() - started}))
    return context, locator, prefix


class _CurrentReadGuard:
    """Controlled current decisions consumed by the real F2 scope/projection compiler."""

    def __init__(self, actor, keys):
        self.actor, self.keys = actor, keys
        self.revoked = False
        self.projection = NamedProjection("f2_person_read", 1)
        self.school = actor.school_id

    def require_current(self, actor, kind, *, purpose):
        assert actor == self.actor and kind == "person" and purpose == "read"
        if not self.keys:
            raise SchoolFactChangeRejected("No current target read grant")
        scope = F2FactScope(self.school, kind, tuple(self.keys), ())
        return ReadAuthorization(
            True, object(), ScopePlan(uuid4(), "school_facts.f2", scope.reference, 1),
            self.projection, "controlled-capacity-read-basis",
        )

    def revalidate_many(self, authorizations, actor):
        assert actor == self.actor and authorizations
        if self.revoked:
            raise SecureReadDenied("Target field authority revoked before release")


@pytest.mark.parametrize("count", [4097, 50_000])
def test_real_committed_capacity_receipt_stays_bounded_and_currently_authorized(count):
    with _capacity_engines() as (config, owner, writer, anchor, staff):
        context, locator, prefix = _publish(config, owner, writer, anchor, staff, count)
        guard = _CurrentReadGuard(context.actor, (prefix + "00000", prefix + f"{count-1:05d}"))
        duty = {"current": True, "revoke_at_release": False}

        def revalidate_terminal():
            if not duty["current"] or duty["revoke_at_release"]:
                raise SecureReadDenied("Terminal duty revoked")

        def authorize_terminal(_context):
            if not duty["current"]:
                raise SecureReadDenied("Terminal duty revoked")
            return SimpleNamespace(revalidate=revalidate_terminal)

        with owner.connect() as connection:
            schema = connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert schema in {"20260924_46", "20260924_55"}
        settings = _writer_settings().model_copy(update={
            "expected_anchor_id": anchor.anchor_id, "expected_school_id": anchor.school_id,
            "expected_system_identifier": config["system_identifier"],
            "rls_context_signing_key": SecretStr(config["context"]),
            "schema_contract": "school-facts-f7-v" + schema[-2:],
        })
        kernel = SqlAlchemySchoolFactWriterExecutorKernel(
            engine=writer, settings=settings, final_authorization_guard=lambda *_: None,
            package_final_authorization_guard=lambda *_: None,
            family_actor_validator=object(), own_terminal_authorizer=authorize_terminal,
            own_result_authorizer=F2OwnResultAuthorizer(guard),
        )
        row_counts = []

        def observed(_conn, cursor, _statement, _parameters, _context, _many):
            # DB aggregation may scan the mother set; Python must never fetch it.
            if cursor.description:
                row_counts.append(cursor.rowcount)

        event.listen(writer, "after_cursor_execute", observed)

        def read(offset, *, actor_context=context):
            return kernel.read_own_target_page(
                context=actor_context, session_binding="new-login-without-original-session",
                locator_sha256=locator, offset=offset, limit=20,
            )

        tracemalloc.start()
        started = time.monotonic()
        try:
            first, last = read(0), read(count - 20)
            assert first is not None and last is not None
            assert first.terminal.result.changed_count == first.terminal.target_count == count
            assert first.next_offset == 20 and first.has_more
            assert last.next_offset == count and not last.has_more
            assert [item.source_key for item in first.items] == [prefix + "00000"]
            assert [item.source_key for item in last.items] == [prefix + f"{count-1:05d}"]
            _, peak = tracemalloc.get_traced_memory()
            assert row_counts and 0 <= max(row_counts) <= 21
            assert peak < 8 * 1024 * 1024
            print(json.dumps({"targets": count, "two_pages_seconds": time.monotonic()-started,
                              "python_peak_bytes": peak, "select_queries": len(row_counts),
                              "max_fetched_rows": max(row_counts)}))
        finally:
            tracemalloc.stop()

        guard.keys = ()
        assert read(0).items == ()
        guard.keys = (prefix + "00000",)
        guard.projection = NamedProjection("f2_person_read", 2)
        with pytest.raises(SchoolFactAuthorizationUnavailable):
            read(0)
        guard.projection = NamedProjection("f2_person_read", 1)
        guard.school = uuid4()
        with pytest.raises(SchoolFactAuthorizationUnavailable):
            read(0)
        guard.school = context.actor.school_id
        guard.revoked = True
        with pytest.raises(SecureReadDenied):
            read(0)
        guard.revoked = False
        duty["revoke_at_release"] = True
        with pytest.raises(SecureReadDenied):
            read(0)
        duty["revoke_at_release"] = False
        duty["current"] = False
        before = len(row_counts)
        with pytest.raises(SecureReadDenied):
            read(0)
        assert len(row_counts) == before
        duty["current"] = True
        other = context_for(
            school_id=uuid4(), user_id=staff.account_user_id, person_id=staff.person_id,
            assignment_id=staff.id, key="other-school",
        )
        with pytest.raises(SchoolFactChangeRejected):
            read(0, actor_context=other)
        event.remove(writer, "after_cursor_execute", observed)
