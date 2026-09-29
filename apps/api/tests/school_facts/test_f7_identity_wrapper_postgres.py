"""The rebuilt F7 database gives only the fact Writer a command-bound identity lookup."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.db.rls_context import set_local_execution_context
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.execution_context import compose_command_context
from app.platform.identity.tuple_validator import SqlAlchemyIdentityTupleValidator
from tests.school_facts import test_f3_family_postgres
from tests.school_facts.test_f3_family_postgres import (
    existing_fixture_writer,
    safe_test_engine,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("F7_TEST_DATABASE_CONFIG"), reason="owned F7 PG config required"
)


def test_f7_writer_identity_lookup_is_command_bound_on_rebuilt_database() -> None:
    config = json.loads(Path(os.environ["F7_TEST_DATABASE_CONFIG"]).read_text(encoding="utf-8"))
    engines = {
        role: safe_test_engine(Path(os.environ["F7_TEST_DATABASE_CONFIG"]), role)
        for role in ("migration", "runtime", "reader", "writer")
    }
    try:
        with sessionmaker(engines["migration"])() as owner:
            assert owner.scalar(text("SELECT version_num FROM alembic_version")) >= "20260924_48"
            acl = owner.execute(
                text(
                    "SELECT has_function_privilege('campus_ai_school_fact_writer', "
                    "'app.school_fact_lock_existing_identity_user_id(text,text,text)', 'EXECUTE'), "
                    "has_function_privilege('campus_ai_school_fact_reader', "
                    "'app.school_fact_lock_existing_identity_user_id(text,text,text)', 'EXECUTE'), "
                    "has_function_privilege('campus_ai_school_fact_writer', "
                    "'app.lock_existing_identity_user_id(text,text,text)', 'EXECUTE')"
                )
            ).one()
            # Earlier isolated F3 tests aligned this disposable fixture's legacy ACL
            # with the original running instance. Rev48 leaves that ACL untouched.
            assert tuple(acl[:2]) == (True, False)

        with sessionmaker(engines["writer"])() as writer:
            with pytest.raises(DBAPIError) as denied:
                with writer.begin():
                    SqlAlchemyIdentityTupleValidator(
                        school_fact_command_wrapper=True
                    ).lock_existing_user_id(
                        writer,
                        provider_code="local_test",
                        provider_tenant_key="development",
                        external_subject="not-present-in-isolated-fixture",
                    )
            assert getattr(denied.value.orig, "sqlstate", None) == "28000"

        # The existing fixture actor is resolved through the real identity boundary.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(test_f3_family_postgres, "KEY", config["context"].encode())
            actor, identity, _, _ = existing_fixture_writer(engines)
        with sessionmaker(engines["migration"])() as owner:
            provider = owner.execute(
                text(
                    "SELECT provider_code,provider_tenant_key,external_subject "
                    "FROM users WHERE id=:user_id"
                ),
                {"user_id": actor.account_user_id},
            ).one()
        command = compose_command_context(actor, uuid4(), "f7-isolated-identity-lookup")
        envelope = ContextProofSigner(config["context"].encode()).sign_execution_context(
            command,
            session_nonce_hash=identity.session_nonce_hash,
            proof_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        with sessionmaker(engines["writer"])() as writer:
            with writer.begin():
                set_local_execution_context(writer, envelope)
                assert SqlAlchemyIdentityTupleValidator(
                    school_fact_command_wrapper=True
                ).lock_existing_user_id(
                    writer,
                    provider_code=provider.provider_code,
                    provider_tenant_key=provider.provider_tenant_key,
                    external_subject=provider.external_subject,
                ) == actor.account_user_id
    finally:
        for engine in engines.values():
            engine.dispose()
