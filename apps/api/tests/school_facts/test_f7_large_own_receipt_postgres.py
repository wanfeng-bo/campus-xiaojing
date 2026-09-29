"""Owned F7 PostgreSQL: a large terminal survives a new login without all target rights."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import SecretStr
from sqlalchemy import URL, create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.composition.school_fact_writer_executor import SqlAlchemySchoolFactWriterExecutorKernel
from app.composition.school_facts import HmacSchoolFactConfirmationCredentialIssuer
from app.platform.authorization.secure_reader_contracts import SecureReadDenied
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.school_facts.application import SchoolFactChangeRejected, SchoolFactChangeService
from app.platform.school_facts.contracts import CanonicalFactChangeSetV3
from app.platform.school_facts.ports import SchoolFactChangeProvenance
from tests.school_facts.test_sf2_transaction import context_for, seed_staff
from tests.school_facts.test_sf5b_writer_executor import _writer_settings
from tests.school_facts.test_f7_large_own_receipt import (
    _reject_packages_in_own_receipt_fixture,
)
from tests.school_facts.test_f7_startup_contract import F7_PROBE_CONTRACTS, f7_probe_contract

pytestmark = pytest.mark.skipif(
    not os.environ.get("F7_TEST_DATABASE_CONFIG"), reason="owned F7 PG config required"
)


@contextmanager
def _owned_engines():
    path = Path(os.environ["F7_TEST_DATABASE_CONFIG"])
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["owner"] == "F7 isolated quality migration and reader tests"
    assert config["container"] == "campus-f7-tests-20260924"
    assert config["database"] == "f7_fixture_test"
    try:
        host = socket.gethostbyname(config["container"])
    except OSError:
        try:
            actual_id = subprocess.check_output(
                ["docker", "inspect", "--format", "{{.Id}}", config["container"]],
                text=True,
                timeout=10,
            ).strip()
            host = subprocess.check_output(
                [
                    "docker", "inspect", "--format",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                    config["container"],
                ],
                text=True,
                timeout=10,
            ).strip()
            assert actual_id.startswith(config["container_id"])
        except Exception:
            raise RuntimeError("Owned F7 test container unavailable") from None
    assert host and host.count(".") == 3

    def engine_for(role):
        def connect():
            try:
                return psycopg.connect(
                    host=host,
                    port=config["port"],
                    dbname=config["database"],
                    user=(
                        "campus_ai_migration"
                        if role == "migration"
                        else f"campus_ai_school_fact_{role}"
                    ),
                    password=config[role],
                    options="-c role=campus_ai_db_owner" if role == "migration" else "",
                    connect_timeout=3,
                )
            except Exception:
                raise RuntimeError("Owned F7 test role connection unavailable") from None

        return create_engine(
            URL.create("postgresql+psycopg", username=role),
            creator=connect,
            poolclass=NullPool,
            hide_parameters=True,
        )

    engines = {role: engine_for(role) for role in ("migration", "reader", "writer")}
    try:
        with engines["migration"].connect() as connection:
            schema_version = connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert schema_version == F7_PROBE_CONTRACTS[f7_probe_contract()]
            config["_schema_contract"] = f"school-facts-f7-v{schema_version[-2:]}"
            assert (
                str(connection.scalar(text("SELECT system_identifier FROM pg_control_system()")))
                == config["system_identifier"]
            )
            anchor = connection.execute(
                text("SELECT anchor_id,school_id FROM school_database_anchor")
            ).one_or_none()
        if anchor is None:
            seed_staff(
                sessionmaker(engines["migration"], expire_on_commit=False),
                school_source_key="school:f7-test",
            )
        with engines["migration"].connect() as connection:
            anchor = connection.execute(
                text("SELECT anchor_id,school_id FROM school_database_anchor")
            ).one()
            staff = connection.execute(
                text(
                    "SELECT a.id,a.person_id,b.account_user_id FROM staff_assignments a "
                    "JOIN account_person_bindings b ON b.school_id=a.school_id "
                    "AND b.person_id=a.person_id WHERE a.school_id=:school "
                    "ORDER BY a.created_at LIMIT 1"
                ),
                {"school": anchor.school_id},
            ).one()
        yield config, engines, anchor, staff
    finally:
        for engine in engines.values():
            engine.dispose()


def _publish_formal_receipt(config, engines, school: UUID, staff):
    marker = uuid4().hex
    key = "f7-receipt:" + marker
    locator = sha256(("f7-formal-receipt:" + marker).encode()).hexdigest()
    source_sha256 = sha256(marker.encode()).hexdigest()
    now = datetime.now(UTC)
    with engines["reader"].connect() as connection:
        base = connection.scalar(
            text("SELECT app.school_fact_current_version(:school)"), {"school": school}
        )
    change_set = CanonicalFactChangeSetV3.model_validate_json(
        json.dumps({
            "schema_version": 3,
            "base_version": base,
            "source_sha256": source_sha256,
            "changes": [
                {
                    "schema_version": 3,
                    "origin": {"sheet_code": "f7-owned-receipt", "row_number": 1},
                    "fact_kind": "person",
                    "source_key": key,
                    "operation": "upsert",
                    "expected_version": 0,
                    "effective_at": now.isoformat(),
                    "content": {
                        "display_name": "合成回执对象",
                        "person_kind": "other",
                        "lifecycle_status": "active",
                    },
                }
            ],
        })
    )
    signer = ContextProofSigner(SecretStr(config["context"]))
    persistence = SqlAlchemySchoolFactPersistence(
        sessionmaker(engines["writer"], expire_on_commit=False),
        lambda current: signer.sign_execution_context(
            current,
            session_nonce_hash=sha256(b"f7-original-login").hexdigest(),
            proof_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
        confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
            b"f7-owned-confirmation-key-32-bytes"
        ),
    )
    service = SchoolFactChangeService(reader=persistence, writer=persistence)
    execution = context_for(
        school_id=school,
        user_id=staff.account_user_id,
        person_id=staff.person_id,
        assignment_id=staff.id,
        key="f7-formal-receipt:" + marker,
    )
    provenance = SchoolFactChangeProvenance(
        channel="admin",
        raw_source_sha256=source_sha256,
        template_version="f7-owned",
        mapping_version="f7-owned",
        mapping_config_sha256=source_sha256,
        source_size_bytes=None,
        worksheet_names=(),
        result_locator_sha256=locator,
    )
    binding = sha256(b"f7-original-login").hexdigest()
    prepared = service.prepare_confirmation(
        change_set=change_set,
        context=execution,
        session_binding=binding,
        provenance=provenance,
    )
    applied = service.confirm(
        credential=prepared.credential,
        change_set=change_set,
        context=execution,
        session_binding=binding,
    )
    assert applied.changed_count == 1
    return locator, key, applied.batch_id


def test_formal_receipt_uses_real_writer_role_and_current_page_rights_across_logins():
    with _owned_engines() as (config, engines, anchor, staff):
        locator, key, batch_id = _publish_formal_receipt(
            config, engines, anchor.school_id, staff
        )
        settings = _writer_settings().model_copy(
            update={
                "database_name": config["database"],
                "database_password": SecretStr(config["writer"]),
                "expected_anchor_id": anchor.anchor_id,
                "expected_school_id": anchor.school_id,
                "expected_system_identifier": config["system_identifier"],
                "rls_context_signing_key": SecretStr(config["context"]),
                "schema_contract": config["_schema_contract"],
            }
        )
        state = {"permitted": True, "terminal": True}
        last_key = key

        class CurrentTargets:
            def for_targets(self, _context, coordinates):
                allowed = (
                    frozenset({("person", last_key)}) & coordinates
                    if state["permitted"]
                    else frozenset()
                )
                if not allowed:
                    raise SchoolFactChangeRejected("current exact target read denied")
                return SimpleNamespace(
                    allowed_targets=allowed,
                    creation_only_targets=frozenset(),
                    revalidate=lambda: None,
                )

        def current_terminal(_context):
            if not state["terminal"]:
                raise SecureReadDenied("current own terminal duty revoked")
            return SimpleNamespace(
                revalidate=lambda: (
                    None
                    if state["terminal"]
                    else (_ for _ in ()).throw(SecureReadDenied("terminal duty revoked"))
                )
            )

        kernel = SqlAlchemySchoolFactWriterExecutorKernel(
            engine=engines["writer"],
            settings=settings,
            final_authorization_guard=lambda *_: None,
            family_actor_validator=object(),
            own_result_authorizer=CurrentTargets(),
            own_terminal_authorizer=current_terminal,
            package_final_authorization_guard=_reject_packages_in_own_receipt_fixture,
        )
        execution = context_for(
            school_id=anchor.school_id,
            user_id=staff.account_user_id,
            person_id=staff.person_id,
            assignment_id=staff.id,
            key="f7-receipt-read:" + uuid4().hex,
        )
        page = kernel.read_own_target_page(
            context=execution,
            session_binding="fresh-login-not-the-original-session",
            locator_sha256=locator,
            offset=0,
            limit=20,
        )
        assert page is not None
        assert page.terminal.target_count == page.terminal.result.changed_count == 1
        assert page.next_offset == 1 and page.has_more is False
        assert [item.source_key for item in page.items] == [last_key]

        state["permitted"] = False
        terminal_only = kernel.read_own_target_page(
            context=execution,
            session_binding="another-current-login",
            locator_sha256=locator,
            offset=0,
            limit=20,
        )
        assert terminal_only is not None
        assert terminal_only.terminal.result.batch_id == page.terminal.result.batch_id
        assert terminal_only.items == ()
        state["terminal"] = False
        with pytest.raises(SecureReadDenied):
            kernel.read_own_target_page(
                context=execution,
                session_binding="another-current-login",
                locator_sha256=locator,
                offset=0,
                limit=20,
            )
        with engines["migration"].connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM master_data_import_batches "
                    "WHERE mapping_payload->>'result_locator_sha256'=:locator"
                ),
                {"locator": locator},
            ) == 1
            assert connection.scalar(
                text("SELECT count(*) FROM master_data_import_batches WHERE id=:batch"),
                {"batch": batch_id},
            ) == 1
