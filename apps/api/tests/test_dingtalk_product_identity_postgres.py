"""Opt-in real PG16 identity verification in an explicitly owned disposable Docker cluster.

Only synthetic fixtures are seeded by the isolated Owner. This does not publish real
school facts or prove production initialization, DingTalk network exchange, or Redis.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Generator
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from psycopg import sql
from sqlalchemy import event

from app.composition.school_fact_dingtalk_identity import (
    build_school_fact_dingtalk_identity_service,
)
from app.composition.school_fact_product_identity import ProductIdentityRejected
from app.core.settings import Settings
from app.platform.identity.context import CurrentUser
from app.platform.identity.context_proof import ContextProofSigner
from app.platform.identity.session import IdentitySessionStore
from tests.master_data import conftest as shared
from tests.master_data.test_execution_context import (
    _runtime_session_factory_for,
    _seed_execution_facts,
)
from tests.school_facts.test_sf2_migration import _run_alembic
from tests.test_f1_product_identity import MemoryRedis

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DINGTALK_POSTGRES_INTEGRATION") != "1",
    reason="explicit disposable DingTalk PostgreSQL test cluster required",
)
CONTEXT_KEY = b"master-data-schema-context-signing-key-32-bytes"


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, encoding="utf-8", timeout=30
    )


@pytest.fixture(scope="module")
def identity_template():
    """Use container psql without requiring a host PostgreSQL installation."""
    __tracebackhide__ = True
    container = os.environ["DINGTALK_TEST_POSTGRES_CONTAINER"]
    assert container.startswith("school-facts-live-integration-tests-")
    inspected = _docker("inspect", "--format", "{{json .Config.Labels}}", container)
    assert inspected.returncode == 0
    assert json.loads(inspected.stdout).get("purpose") == "this-task"
    system_id = os.environ["MASTER_DATA_DISPOSABLE_CLUSTER_SYSTEM_IDENTIFIER"]
    assert system_id != "7682019857215991846"
    database = os.environ["POSTGRES_DB"]
    assert database.endswith("_test")
    # The Docker ownership proof and TCP endpoint must name the same new cluster.
    ports = _docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", container)
    assert ports.returncode == 0
    assert {"HostIp": "127.0.0.1", "HostPort": os.environ["POSTGRES_PORT"]} in json.loads(
        ports.stdout
    )["5432/tcp"]
    container_identity = _docker(
        "exec",
        container,
        "psql",
        "-U",
        os.environ["POSTGRES_BOOTSTRAP_USER"],
        "-d",
        database,
        "-Atqc",
        "SELECT system_identifier::text FROM pg_control_system()",
    )
    assert container_identity.returncode == 0 and container_identity.stdout.strip() == system_id
    host = os.environ["POSTGRES_HOST"]
    assert host == "127.0.0.1"
    common = dict(
        host=host,
        port=int(os.environ["POSTGRES_PORT"]),
        dbname=database,
        connect_timeout=5,
        options="",
    )
    password = os.environ["POSTGRES_BOOTSTRAP_PASSWORD"]
    admin = dict(common, user=os.environ["POSTGRES_BOOTSTRAP_USER"], password=password)
    version = shared._verify_disposable_postgres_16_cluster(
        admin_connection_parameters=admin, expected_system_identifier=system_id
    )
    names = {
        "runtime": "campus_ai_runtime",
        "migration": "campus_ai_migration",
        "writer": "campus_ai_school_fact_writer",
        "reader": "campus_ai_school_fact_reader",
    }
    passwords = {
        kind: shared._derive_cluster_test_password(
            bootstrap_password=password, system_identifier=system_id, role_name=name
        )
        for kind, name in names.items()
    }
    env = os.environ.copy()
    env.update(
        POSTGRES_EXPECTED_SYSTEM_IDENTIFIER=system_id,
        POSTGRES_RUNTIME_PASSWORD=passwords["runtime"],
        POSTGRES_MIGRATION_PASSWORD=passwords["migration"],
        POSTGRES_SCHOOL_FACT_WRITER_PASSWORD=passwords["writer"],
        POSTGRES_SCHOOL_FACT_READER_PASSWORD=passwords["reader"],
    )
    forwarded = (
        "POSTGRES_DB",
        "POSTGRES_BOOTSTRAP_USER",
        "POSTGRES_BOOTSTRAP_PASSWORD",
        "POSTGRES_EXPECTED_SYSTEM_IDENTIFIER",
        "POSTGRES_RUNTIME_PASSWORD",
        "POSTGRES_MIGRATION_PASSWORD",
        "POSTGRES_SCHOOL_FACT_WRITER_PASSWORD",
        "POSTGRES_SCHOOL_FACT_READER_PASSWORD",
    )
    command = (
        "docker",
        "exec",
        *(part for key in forwarded for part in ("-e", key)),
        container,
        "sh",
        "/tmp/dingtalk-test-bootstrap.sh",
    )
    copied = _docker(
        "cp", str(shared.BOOTSTRAP_SCRIPT), container + ":/tmp/dingtalk-test-bootstrap.sh"
    )
    assert copied.returncode == 0
    secrets = (password, *passwords.values())
    result = shared._run_bootstrap(command=command, environment=env, protected_secrets=secrets)
    roles = shared.PostgresRoles(
        admin_connection_parameters=admin,
        runtime_connection_parameters=dict(
            common, user=names["runtime"], password=passwords["runtime"]
        ),
        migration_connection_parameters=dict(
            common, user=names["migration"], password=passwords["migration"]
        ),
        writer_connection_parameters=dict(
            common, user=names["writer"], password=passwords["writer"]
        ),
        reader_connection_parameters=dict(
            common, user=names["reader"], password=passwords["reader"]
        ),
        bootstrap_command=command,
        bootstrap_environment=env,
        protected_test_secrets=secrets,
        bootstrap_output=result.stdout + result.stderr,
        disposable_cluster_system_identifier=system_id,
        server_version_num=version,
    )
    template = shared.current_master_data_template(roles, schema_revision="20260905_22")
    target = next(template)
    try:
        with target.admin_connection() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname='campus_ai_governance_writer'"
                ).fetchone()
                is None
            ):
                connection.execute(
                    "CREATE ROLE campus_ai_governance_writer NOLOGIN NOSUPERUSER NOINHERIT "
                    "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                )
        yield roles, target
    finally:
        cast(Generator[shared.PostgresRoles], template).close()


@pytest.fixture
def real_identity(identity_template):
    cluster, template = identity_template
    cluster.verify_disposable_cluster()
    database = "dingtalk_identity_" + uuid4().hex + "_test"
    with cluster.admin_connection() as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER campus_ai_db_owner TEMPLATE {}").format(
                sql.Identifier(database),
                sql.Identifier(template.admin_connection_parameters["dbname"]),
            )
        )
    roles = shared._roles_for_database(template, database)
    facts = _seed_execution_facts(roles)
    with roles.admin_connection() as connection:
        assert connection.execute("SELECT count(*) FROM public.user_roles").fetchone() == (0,)
        connection.execute(
            "UPDATE public.users SET provider_code='dingtalk',"
            "provider_tenant_key='synthetic-corp' WHERE id=%s",
            (facts.user_id,),
        )
        connection.execute(
            "UPDATE public.account_person_binding_versions SET provider_code='dingtalk',"
            "provider_tenant_key='synthetic-corp' WHERE account_user_id=%s",
            (facts.user_id,),
        )
    migrated = _run_alembic(roles, "upgrade", "20260924_56")
    if migrated.returncode:
        pytest.fail(
            shared._redact(migrated.stdout + migrated.stderr, roles.protected_test_secrets)[-2500:],
            pytrace=False,
        )
    with roles.admin_connection() as connection:
        assert connection.execute("SELECT version_num FROM public.alembic_version").fetchone() == (
            "20260924_56",
        )
    sessions, engine = _runtime_session_factory_for(roles)
    checked_out = set()
    statements = []
    event.listen(engine, "checkout", lambda conn, record, proxy: checked_out.add(id(record)))
    event.listen(engine, "checkin", lambda conn, record: checked_out.discard(id(record)))
    event.listen(
        engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, parameters, context, many: statements.append(statement),
    )
    settings = SimpleNamespace(
        identity_provider="dingtalk",
        dingtalk_corp_id="synthetic-corp",
        identity_session_ttl_seconds=28800,
        identity_session_cookie_name="dingtalk_test_product",
    )
    redis = MemoryRedis()
    service = build_school_fact_dingtalk_identity_service(
        settings=cast(Settings, settings),
        session_factory=sessions,
        redis=redis,
        proof_signer=ContextProofSigner(CONTEXT_KEY),
        can_maintain=lambda _: False,
        family_enabled=True,
    )
    with roles.admin_connection() as connection:
        subject_row = connection.execute(
            "SELECT external_subject FROM public.users WHERE id=%s", (facts.user_id,)
        ).fetchone()
        assert subject_row is not None
        subject = subject_row[0]
    token = IdentitySessionStore(redis=redis, ttl_seconds=28800).create(
        CurrentUser(facts.user_id, subject, "synthetic", 1)
    )
    try:
        yield SimpleNamespace(
            service=service,
            token=token,
            roles=roles,
            facts=facts,
            checked_out=checked_out,
            statements=statements,
        )
    finally:
        engine.dispose()
        cluster.verify_disposable_cluster()
        with cluster.admin_connection() as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
            )


def test_formal_pg56_context_without_legacy_roles_returns_connections_and_logout_rejects(
    real_identity,
):
    case = real_identity
    view = case.service.session_view(case.token)
    assert view.status == "signed_in"
    assert view.selected_context_kind == "staff_assignment"
    assert view.accounts == () and view.can_maintain is False
    assert case.checked_out == set()
    selected = case.service.resolve_request(case.token)
    assert selected.actor.assignment_id == case.facts.assignment_id
    assert case.checked_out == set()
    assert case.service.resolve_private_actor(selected.actor_context_ref) == selected
    assert case.checked_out == set()
    assert not any("user_roles" in statement.lower() for statement in case.statements)
    case.service.logout(case.token)
    with pytest.raises(ProductIdentityRejected):
        case.service.resolve_private_actor(selected.actor_context_ref)
    assert case.checked_out == set()


@pytest.mark.parametrize(
    "change",
    ["provider", "corp", "security_version", "disabled", "mapping_revoked", "assignment_revoked"],
)
def test_formal_pg56_current_change_rejects_retained_model_identity(real_identity, change):
    case = real_identity
    assert case.service.session_view(case.token).selected_context_kind == "staff_assignment"
    selected = case.service.resolve_request(case.token)
    with case.roles.admin_connection() as connection:
        if change == "provider":
            connection.execute(
                "UPDATE public.users SET provider_code='local_test' WHERE id=%s",
                (case.facts.user_id,),
            )
        elif change == "corp":
            connection.execute(
                "UPDATE public.users SET provider_tenant_key='another-corp' WHERE id=%s",
                (case.facts.user_id,),
            )
        elif change == "security_version":
            connection.execute(
                "UPDATE public.users SET security_version=security_version+1 WHERE id=%s",
                (case.facts.user_id,),
            )
        elif change == "disabled":
            connection.execute(
                "UPDATE public.users SET is_active=false WHERE id=%s", (case.facts.user_id,)
            )
        elif change == "mapping_revoked":
            connection.execute(
                "UPDATE public.account_person_binding_versions SET status='rejected',"
                "revoked_at=now() WHERE account_user_id=%s",
                (case.facts.user_id,),
            )
        else:
            connection.execute(
                "UPDATE public.staff_assignments SET status='rejected',revoked_at=now(),"
                "version=version+1 WHERE id=%s",
                (case.facts.assignment_id,),
            )
    with pytest.raises(ProductIdentityRejected):
        case.service.resolve_private_actor(selected.actor_context_ref)
    assert case.checked_out == set()
