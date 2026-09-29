from __future__ import annotations

import importlib
import importlib.util
import inspect
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError


def initialization_module():
    name = "app.platform.school_facts.initialization_contracts"
    assert importlib.util.find_spec(name) is not None, "external initialization contract is missing"
    return importlib.import_module(name)


def manifest():
    now = datetime(2026, 9, 5, tzinfo=UTC)
    contents = [
        (
            "person",
            "person:first",
            {"display_name": "首任维护者", "person_kind": "staff", "lifecycle_status": "active"},
        ),
        (
            "organization",
            "org:office",
            {
                "business_code": "office",
                "display_name": "校务办公室",
                "kind": "department",
                "display_path": "校务办公室",
                "status": "active",
            },
        ),
        (
            "login_mapping",
            "login:first",
            {
                "person_source_key": "person:first",
                "provider_code": "local_test",
                "provider_tenant_key": "local_test",
                "external_subject": "first",
                "status": "confirmed",
            },
        ),
        (
            "assignment",
            "assignment:first",
            {
                "person_source_key": "person:first",
                "organization_source_key": "org:office",
                "position_title": "事实维护",
                "semantic_role_code": "fact_maintainer",
                "assignment_kind": "substantive",
                "appointment_source": "approved_manifest",
                "approval_reference": "approved:f1",
                "status": "confirmed",
            },
        ),
    ]
    return {
        "school_id": UUID(int=1),
        "anchor_id": UUID(int=2),
        "expected_system_identifier": "1234567890",
        "request_id": UUID(int=3),
        "approval_reference": "approved:f1:initial-maintainer",
        "operator_reference": "deployment:operator-1",
        "change_set": {
            "schema_version": 1,
            "base_version": 1,
            "source_sha256": "a" * 64,
            "changes": tuple(
                {
                    "schema_version": 1,
                    "operation": "upsert",
                    "fact_kind": kind,
                    "source_key": key,
                    "expected_version": 0,
                    "effective_at": now,
                    "origin": {"sheet_code": kind, "row_number": index + 1},
                    "content": content,
                }
                for index, (kind, key, content) in enumerate(contents)
            ),
        },
    }


def test_initialization_contract_has_external_identity_without_forged_staff():
    module = initialization_module()
    request = module.SchoolFactInitializationRequest.model_validate(manifest())
    assert request.operator_reference == "deployment:operator-1"
    assert "actor_assignment_id" not in request.model_dump()


def test_initialization_cannot_publish_official_contact_or_second_person():
    module = initialization_module()
    payload = manifest()
    payload["change_set"]["changes"] += (
        {**payload["change_set"]["changes"][0], "source_key": "person:extra"},
    )
    with pytest.raises(ValidationError):
        module.SchoolFactInitializationRequest.model_validate(payload)
    payload = manifest()
    payload["change_set"]["changes"] = payload["change_set"]["changes"][:-1]
    with pytest.raises(ValidationError):
        module.SchoolFactInitializationRequest.model_validate(payload)


def test_initialization_authority_checks_real_session_and_exact_schema():
    module = initialization_module()
    for session_user, head in [
        ("campus_ai_runtime", "20260905_22"),
        ("campus_ai_migration", "20260824_20"),
        ("campus_ai_migration", "20260905_21"),
    ]:
        with pytest.raises(module.SchoolFactInitializationRejected):
            module.require_initialization_authority(
                session_user=session_user, alembic_heads=(head,)
            )
    module.require_initialization_authority(
        session_user="campus_ai_migration", alembic_heads=("20260905_22",)
    )


def test_revision21_preserves_historical_migrations_and_distinguishes_initializer():
    path = Path(__file__).parents[2] / "alembic/versions/20260905_21_school_fact_initialization.py"
    assert path.exists(), "revision21 initialization ledger is missing"
    source = path.read_text(encoding="utf-8")
    assert 'down_revision = "20260824_20"' in source
    assert "school_fact_initializations" in source
    assert "initialization_id" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "campus_ai_runtime" in source


def test_final_guard_is_part_of_existing_writer_transaction():
    import inspect

    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence

    assert (
        "final_authorization_guard" in inspect.signature(SqlAlchemySchoolFactPersistence).parameters
    )
    source = inspect.getsource(SqlAlchemySchoolFactPersistence.confirm)
    assert source.index("self._final_authorization_guard") < source.index(
        "if batch.confirmation_consumed_at"
    )


def test_closed_initializer_does_not_reopen_or_accept_changed_manifest():
    module = initialization_module()
    with pytest.raises(module.SchoolFactInitializationRejected):
        module.require_initialization_replay(
            state="closed", original_sha256="a" * 64, request_sha256="a" * 64, action="confirm"
        )
    with pytest.raises(module.SchoolFactInitializationRejected):
        module.require_initialization_replay(
            state="prepared", original_sha256="a" * 64, request_sha256="b" * 64, action="prepare"
        )


def test_runtime_cannot_enter_external_initializer_before_any_write():
    name = "app.composition.school_fact_initialization"
    assert importlib.util.find_spec(name) is not None, "controlled initialization host is missing"
    module = importlib.import_module(name)
    request = initialization_module().SchoolFactInitializationRequest.model_validate(manifest())
    calls = []

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one(self):
            return self.value

        def scalars(self):
            return self

        def all(self):
            return [self.value]

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def begin(self):
            return self

        def execute(self, statement, *args):
            sql = str(statement)
            calls.append(sql)
            assert sql.lstrip().startswith("SELECT")
            return Result("campus_ai_runtime" if "session_user" in sql else "20260905_22")

    initializer = module.SchoolFactInitializer(session_factory=Session)
    with pytest.raises(initialization_module().SchoolFactInitializationRejected):
        initializer.prepare(request)
    assert calls


def test_product_schema_contract_is_exact_and_legacy_default_is_unchanged():
    name = "app.db.schema_contract"
    assert importlib.util.find_spec(name) is not None, (
        "explicit product schema selection is missing"
    )
    module = importlib.import_module(name)
    assert module.schema_contract_head("identity-v20") == "20260824_20"
    assert module.schema_contract_head("school-facts-f1-v21") == "20260905_21"
    assert module.schema_contract_head("school-facts-f1-v22") == "20260905_22"
    with pytest.raises(ValueError):
        module.schema_contract_head("any-head")


def test_epoch_lock_is_a_narrow_writer_capability():
    path = Path(__file__).parents[2] / "alembic/versions/20260905_21_school_fact_initialization.py"
    source = path.read_text(encoding="utf-8")
    assert "lock_school_fact_governance_epoch" in source
    assert "FOR SHARE" in source


@pytest.mark.parametrize("contract", ["school-facts-f1-v21", "school-facts-f1-v22"])
def test_product_writer_cannot_start_without_product_final_policy(contract):
    from types import SimpleNamespace

    from app.composition.school_fact_writer_executor import SqlAlchemySchoolFactWriterExecutorKernel

    with pytest.raises(ValueError, match="final authorization"):
        SqlAlchemySchoolFactWriterExecutorKernel(
            engine=None, settings=SimpleNamespace(schema_contract=contract)
        )


def test_product_services_offer_no_writer_database_configuration():
    module = importlib.import_module("app.composition.school_fact_product_services")
    parameters = inspect.signature(module.create_school_fact_product_services).parameters
    assert "reader_settings" in parameters
    assert "executor_settings" in parameters
    assert "writer_settings" not in parameters


def test_global_version_is_reader_only_and_does_not_expose_batch_contents():
    path = Path(__file__).parents[2] / "alembic/versions/20260905_21_school_fact_initialization.py"
    source = path.read_text(encoding="utf-8")
    assert "app.school_fact_current_version" in source
    assert "MAX(target_fact_version)" in source


def test_oneoff_root_input_cannot_target_existing_deployment_database():
    import runpy

    path = Path(__file__).parents[4] / "scripts/school_facts_f1_init.py"
    assert path.is_file(), "F1 one-off CLI is missing"
    module = runpy.run_path(str(path), run_name="f1_test_module")
    with pytest.raises(ValidationError):
        module["RootManifest"].model_validate(
            {
                "db": "campus_ai_identity_test",
                "system_identifier": "1234567890123",
                "school_id": str(UUID(int=1)),
                "source_key": "school:f1",
                "school_code": "F1",
                "display_name": "合成学校",
                "timezone_name": "Asia/Shanghai",
            }
        )


def test_initializer_close_rejects_an_arbitrary_success_callback():
    from app.composition.school_fact_initialization import SchoolFactInitializer

    signature = inspect.signature(SchoolFactInitializer.close)
    assert "verify_ready" not in signature.parameters
    assert "readiness" in signature.parameters
    initializer = SchoolFactInitializer(
        session_factory=lambda: pytest.fail("must reject before DB")
    )
    request = initialization_module().SchoolFactInitializationRequest.model_validate(manifest())
    with pytest.raises(initialization_module().SchoolFactInitializationRejected):
        initializer.close(request, readiness=lambda *_args: True)


def test_fixed_closure_requires_read_separately_from_maintenance(monkeypatch):
    from types import SimpleNamespace as NS

    from pydantic import SecretStr

    from app.composition import school_fact_initialization_readiness as module
    from app.platform.authorization.thin_contracts import ThinDecisionKind

    request = initialization_module().SchoolFactInitializationRequest.model_validate(manifest())
    actor = NS(school_id=request.school_id, assignment_id=UUID(int=4), authorization_version=1)
    result = {"targets": [{"fact_kind": "assignment", "target_id": str(actor.assignment_id)}]}
    runtime_settings = NS(
        app_env="development",
        postgres_schema_contract="school-facts-f1-v22",
        postgres_user="campus_ai_runtime",
        postgres_db="campus_ai_school_facts_f1",
        postgres_expected_school_id=request.school_id,
        postgres_expected_anchor_id=request.anchor_id,
        postgres_expected_system_identifier=request.expected_system_identifier,
        redis_url="redis://127.0.0.1:6379",
        rls_context_signing_key=SecretStr("s" * 40),
    )
    reader_settings = NS(
        schema_contract="school-facts-f1-v22",
        database_name="campus_ai_school_facts_f1",
        expected_school_id=request.school_id,
        expected_anchor_id=request.anchor_id,
        expected_system_identifier=request.expected_system_identifier,
    )
    engine = NS(dispose=lambda: None)
    calls = []
    identity = NS(
        login=lambda _: "private-session",
        logout=lambda _: None,
        session_view=lambda _: NS(contexts=[NS(ref="context")]),
        select_context=lambda *_: None,
        resolve_request=lambda _: NS(actor=actor),
    )
    guard = NS(require_current=lambda actor: calls.append("maintain") or NS(allowed=True))

    def identity_factory(**kwargs):
        assert kwargs["can_maintain"](actor) is True
        return identity

    def decide(request):
        calls.append(request.action_code)
        return NS(kind=ThinDecisionKind.DENY, basis=None)

    monkeypatch.setattr(module, "build_engine", lambda _: engine)
    monkeypatch.setattr(module, "probe_database", lambda _: None)
    monkeypatch.setattr(module, "build_school_fact_reader_engine", lambda _: engine)
    monkeypatch.setattr(module, "build_session_factory", lambda _: None)
    monkeypatch.setattr(module, "sessionmaker", lambda **_: None)
    monkeypatch.setattr(module, "create_product_thin_service", lambda _: NS(decide=decide))
    monkeypatch.setattr(module, "OfficialContactFinalAuthorizationGuard", lambda **_: guard)
    monkeypatch.setattr(module, "build_school_fact_product_identity_service", identity_factory)
    monkeypatch.setattr(module.Redis, "from_url", lambda *args, **kwargs: NS(close=lambda: None))
    readiness = module.FirstMaintainerReadiness(
        runtime_settings=runtime_settings, reader_settings=reader_settings, scope_id=UUID(int=5)
    )
    with pytest.raises(initialization_module().SchoolFactInitializationRejected, match="READ duty"):
        readiness.verify_before_governance_close(request, result)
    assert "maintain" in calls
    assert "school_facts.official_contacts.read" in calls


def test_fixed_closure_rejects_open_governance_before_closing_anything():
    from types import SimpleNamespace

    from app.composition.school_fact_initialization_readiness import FirstMaintainerReadiness

    request = initialization_module().SchoolFactInitializationRequest.model_validate(manifest())
    readiness = object.__new__(FirstMaintainerReadiness)
    session = SimpleNamespace(scalar=lambda _: SimpleNamespace(state="open"))
    with pytest.raises(
        initialization_module().SchoolFactInitializationRejected, match="not closed"
    ):
        readiness.verify(session, request, {}, initialization_id=UUID(int=9))


def test_f1_compose_has_isolated_volumes_ports_and_no_daily_migration_secret():
    import yaml

    root = Path(__file__).parents[4]
    configuration = yaml.safe_load(
        (root / "infra/school-facts-f1/compose.yml").read_text(encoding="utf-8")
    )
    assert configuration["name"] == "campus-school-facts-f1"
    services = configuration["services"]
    assert services["product"]["ports"] == ["127.0.0.1:8001:8000"]
    assert services["postgres"]["ports"] == ["127.0.0.1:55433:5432"]
    for name in ("writer", "redis", "migration", "initializer", "role-reconcile"):
        assert "ports" not in services[name]
    assert services["product"]["secrets"] == ["product_config", "deepseek_api_key"]
    assert configuration["secrets"]["deepseek_api_key"]["file"] == "./private/deepseek-api-key"
    for name, service in services.items():
        if name != "product":
            assert "deepseek_api_key" not in service.get("secrets", [])
    assert services["writer"]["secrets"] == ["writer_config"]
    for name in ("migration", "initializer", "role-reconcile"):
        assert services[name]["profiles"] == ["init"]
    assert all(
        value["name"].startswith("campus-school-facts-f1-")
        for value in configuration["volumes"].values()
    )


def test_review_manifest_is_minimum_identity_only_after_private_placeholders_are_filled():
    root = Path(__file__).parents[4]
    source = (root / "infra/school-facts-f1/identity-manifest.example.json").read_text(
        encoding="utf-8"
    )
    source = source.replace("REPLACE_WITH_OBSERVED_ANCHOR_UUID", str(UUID(int=2)))
    source = source.replace("REPLACE_WITH_OBSERVED_SYSTEM_IDENTIFIER", "1234567890123")
    source = source.replace("REPLACE_WITH_APPROVED_SOURCE_SHA256", "a" * 64)
    parsed = initialization_module().SchoolFactInitializationRequest.model_validate_json(source)
    assert {c.fact_kind for c in parsed.change_set.changes} == {
        "school",
        "person",
        "organization",
        "assignment",
        "login_mapping",
    }


def test_only_product_has_model_egress_and_durable_metadata():
    import yaml

    root = Path(__file__).parents[4]
    config = yaml.safe_load(
        (root / "infra/school-facts-f1/compose.yml").read_text(encoding="utf-8")
    )
    for name, service in config["services"].items():
        assert ("model-egress" in service.get("networks", [])) is (name == "product")
    assert config["networks"]["f1"]["internal"] is True
    assert config["networks"]["f1"]["ipam"]["config"][0]["subnet"] == "192.168.240.0/28"
    assert config["services"]["product"]["read_only"] is True
    assert config["services"]["product"]["volumes"] == [
        "f1-model-metadata:/var/lib/campus-school-facts-f1/model-metadata"
    ]
    template = __import__("json").loads(
        (root / "infra/school-facts-f1/product-config.example.json").read_text(encoding="utf-8")
    )
    assert template["model"] is None
    assert template["trusted_proxy_cidrs"] == ["192.168.240.1/32", "192.168.241.1/32"]


def test_initializer_database_failure_does_not_echo_private_sql():
    from sqlalchemy.exc import SQLAlchemyError

    from app.composition.school_fact_initialization import SchoolFactInitializer

    request = initialization_module().SchoolFactInitializationRequest.model_validate(manifest())

    def failed_session():
        raise SQLAlchemyError("private_manifest_and_credentials")

    initializer = SchoolFactInitializer(session_factory=failed_session)
    with pytest.raises(initialization_module().SchoolFactInitializationRejected) as failure:
        initializer.result(request)
    assert "private_manifest_and_credentials" not in str(failure.value)


def test_legacy_batch_mapper_never_selects_schema21_initialization_pointer():
    from sqlalchemy import inspect as orm_inspect
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql

    from app.platform.master_data_imports.models import MasterDataImportBatch

    sql = str(select(MasterDataImportBatch).compile(dialect=postgresql.dialect()))
    assert "initialization_id" not in sql
    assert "initialization_id" not in orm_inspect(MasterDataImportBatch).column_attrs
    assert "initialization_id" in MasterDataImportBatch.__table__.columns


def test_publication_cli_requires_persisted_real_session_for_writer_steps():
    import runpy

    path = Path(__file__).parents[4] / "scripts/school_facts_f1_init.py"
    module = runpy.run_path(str(path), run_name="f1_publication_test")
    assert "publication_operation" in module
    from types import SimpleNamespace

    with pytest.raises(ValueError, match="session"):
        module["require_publication_session"](SimpleNamespace(session_file=None), "a" * 64)


def test_publication_window_uses_finite_current_grant_not_original_scope_deadline():
    migration = (
        Path(__file__).parents[2] / "alembic/versions/20260905_21_school_fact_initialization.py"
    ).read_text(encoding="utf-8")
    window = migration.split("CREATE FUNCTION app.require_initial_publication_window", 1)[1]
    assert "v_document->>'expires_at'" not in window
    assert "interval '15 minutes'" in window
    assert "governance_grant_versions" in window
    assert "v_count <> 1" in window


def test_publication_recovery_request_has_finite_window_and_fixed_manifest():
    import importlib

    module = importlib.import_module("app.composition.school_fact_initial_publication_recovery")
    base = {
        "request_id": str(UUID(int=44)),
        "manifest_sha256": "a" * 64,
        "window_opened_at": "2026-09-05T10:00:00Z",
        "window_expires_at": "2026-09-05T10:15:00Z",
    }
    assert module.InitialPublicationRecoveryRequest.model_validate_json(
        __import__("json").dumps(base)
    ).request_id == UUID(int=44)
    with pytest.raises(ValueError):
        module.InitialPublicationRecoveryRequest.model_validate_json(
            __import__("json").dumps({**base, "window_expires_at": "2026-09-05T10:16:00Z"})
        )


def test_publication_recovery_rejects_runtime_before_any_lock_or_governance_write():
    import importlib
    from types import SimpleNamespace

    module = importlib.import_module("app.composition.school_fact_initial_publication_recovery")
    calls = []

    class Session:
        def scalar(self, statement, *args):
            calls.append(str(statement))
            return "campus_ai_runtime"

    with pytest.raises(Exception, match="migration"):
        module.require_recovery_authority(Session(), SimpleNamespace())
    assert calls == ["SELECT SESSION_USER"]


@pytest.mark.parametrize("published,end_fails", [(True, False), (False, True)])
def test_recovery_only_closes_published_and_never_creates_after_failed_end(
    monkeypatch, published, end_fails
):
    from contextlib import nullcontext
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    import app.composition.school_fact_initial_publication_recovery as recovery

    now = datetime.now(UTC)
    manifest = SimpleNamespace(
        manifest_sha256="a" * 64,
        school_id=UUID(int=1),
        assignment_id=UUID(int=2),
        approval_reference="approved",
    )
    request = recovery.InitialPublicationRecoveryRequest(
        request_id=UUID(int=88),
        manifest_sha256="a" * 64,
        window_opened_at=now - timedelta(seconds=1),
        window_expires_at=now + timedelta(minutes=10),
    )
    grant = SimpleNamespace(
        is_terminal=False,
        grant_id=UUID(int=3),
        current_version=1,
        effective_from=now - timedelta(hours=1),
    )
    snapshot = SimpleNamespace(
        scope_id=UUID(int=4),
        duty_id=UUID(int=5),
        duty_version=1,
        grants=(grant,),
        applied_batch_id=UUID(int=6) if published else None,
    )
    events = []
    physical = object()
    nested = object()

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def begin(self):
            return nullcontext()

        def connection(self):
            return physical

        def scalar(self, *args):
            return None

    def nested_factory(**kwargs):
        assert kwargs["bind"] is physical
        assert kwargs["join_transaction_mode"] == "create_savepoint"
        events.append("same_connection")
        return nested

    monkeypatch.setattr(recovery, "sessionmaker", nested_factory)
    monkeypatch.setattr(
        recovery, "require_recovery_authority", lambda *args: events.append("authority_locks")
    )
    monkeypatch.setattr(recovery, "inspect_initial_publication", lambda *args, **kwargs: snapshot)
    monkeypatch.setattr(recovery, "SqlAlchemyGovernanceDependencyAdapter", lambda factory: object())

    def closure(**kwargs):
        assert kwargs["session_factory"] is nested
        events.append("close_published")

    monkeypatch.setattr(recovery, "end_initial_publication_grant", closure)

    class Writer:
        def __init__(self, **kwargs):
            assert kwargs["session_factory"] is nested

        def end_grant(self, command):
            events.append("end_old")
            raise RuntimeError("formal end failed")

        def create_grant(self, command):
            pytest.fail("must not create a Grant for already published facts or after failed end")

    monkeypatch.setattr(recovery, "GovernanceWriter", Writer)
    if end_fails:
        with pytest.raises(RuntimeError, match="formal end failed"):
            recovery.recover_initial_publication(
                session_factory=FakeSession, manifest=manifest, request=request, now=now
            )
        assert events == ["authority_locks", "same_connection", "end_old"]
    else:
        result = recovery.recover_initial_publication(
            session_factory=FakeSession, manifest=manifest, request=request, now=now
        )
        assert result.state == "published" and result.grant is None
        assert result.applied_batch_id == UUID(int=6)
        assert events == ["authority_locks", "same_connection", "close_published"]


def test_initialization_close_locks_epoch_before_initialization_row():
    import inspect

    from app.composition.school_fact_initialization import SchoolFactInitializer

    source = inspect.getsource(SchoolFactInitializer.close)
    assert source.index("self._lock_closure_epoch") < source.index("self._existing")


def test_publication_review_template_is_valid_after_approved_placeholders_are_filled():
    from app.composition.school_fact_initial_publication import InitialPublicationManifest

    source = (
        Path(__file__).parents[4] / "infra/school-facts-f1/publication-manifest.example.json"
    ).read_text(encoding="utf-8")
    replacements = {
        "REPLACE_WITH_REAL_INITIALIZATION_ID": str(UUID(int=12)),
        "REPLACE_WITH_REAL_FIRST_ASSIGNMENT_ID": str(UUID(int=13)),
        "REPLACE_WITH_APPROVED_PUBLICATION_TIME": "2026-09-05T10:00:00Z",
        "REPLACE_WITH_FINITE_DEADLINE_WITHIN_15_MINUTES": "2026-09-05T10:15:00Z",
        "REPLACE_WITH_APPROVED_SYNTHETIC_SOURCE_SHA256": "a" * 64,
    }
    for before, after in replacements.items():
        source = source.replace(before, after)
    manifest = InitialPublicationManifest.model_validate_json(source)
    assert {item.fact_kind for item in manifest.change_set.changes} == {
        "location",
        "official_contact",
    }


def test_f1_build_context_excludes_private_configs_and_exchange_without_hiding_product():
    import yaml

    root = Path(__file__).parents[4]
    compose_path = root / "infra/school-facts-f1/compose.yml"
    configuration = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    private_inputs = [
        (compose_path.parent / secret["file"]).resolve()
        for secret in configuration["secrets"].values()
    ]
    exchange = configuration["services"]["initializer"]["volumes"][0].split(":", 1)[0]
    private_inputs.append((compose_path.parent / exchange / "publication-session.json").resolve())
    for service in configuration["services"].values():
        if "build" not in service:
            continue
        build = service["build"]
        context = (compose_path.parent / build["context"]).resolve()
        dockerfile = context / build["dockerfile"]
        override = dockerfile.with_name(dockerfile.name + ".dockerignore")
        ignore = override if override.exists() else context / ".dockerignore"
        rules = [
            line.strip().rstrip("/")
            for line in ignore.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        # This shipping context uses explicit directory exclusions, with no re-inclusion.
        assert not any(rule.startswith("!") for rule in rules)
        excluded_directories = [
            (context / rule).resolve()
            for rule in rules
            if not any(character in rule for character in "*?[")
        ]
        for private_input in private_inputs:
            assert any(private_input.is_relative_to(path) for path in excluded_directories), (
                "F1 build context includes private input: "
                + str(private_input.relative_to(context))
            )
        for product_input in (
            root / "apps/api/app/composition/school_fact_product.py",
            root / "scripts/school_facts_f1_init.py",
            compose_path.parent / "product-config.example.json",
        ):
            assert not any(
                product_input.resolve().is_relative_to(path) for path in excluded_directories
            )


@pytest.mark.parametrize(
    "relative_path",
    [
        "infra/school-facts-f1/redis-start.sh",
        "infra/school-facts-f1/postgres-role-bootstrap.sh",
        "infra/postgres/001-campus-ai-roles.sh",
    ],
)
def test_f1_shell_entrypoints_keep_lf_on_windows_checkout(relative_path):
    import subprocess

    root = Path(__file__).parents[4]
    raw = (root / relative_path).read_bytes()
    assert raw.startswith(b"#!/usr/bin/env sh\n"), "Linux entrypoint shebang must end with LF"
    assert b"\r" not in raw, "Windows CRLF breaks shell shebangs and set -eu"
    attributes = subprocess.check_output(
        ["git", "check-attr", "text", "eol", "--", relative_path],
        cwd=root,
        text=True,
        encoding="utf-8",
    ).splitlines()
    assert attributes == [f"{relative_path}: text: set", f"{relative_path}: eol: lf"]


@pytest.mark.parametrize("entry", ["initializer", "recovery", "root", "session_factory"])
@pytest.mark.parametrize(
    "session_user,head",
    [
        ("campus_ai_migration", "20260905_22"),
        ("campus_ai_migration", "20260824_20"),
        ("campus_ai_migration", "20260905_21"),
        ("campus_ai_runtime", "20260905_22"),
    ],
)
def test_noinherit_initialization_entries_switch_role_only_after_real_identity(
    monkeypatch, entry, session_user, head
):
    import runpy
    from types import SimpleNamespace

    from app.composition.school_fact_initial_publication_recovery import require_recovery_authority
    from app.composition.school_fact_initialization import SchoolFactInitializer
    from app.platform.school_facts.application import SchoolFactChangeRejected

    calls = []

    class ReachedAfterSchema(Exception):
        pass

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one(self):
            return self.value

        def scalars(self):
            return self

        def all(self):
            return [self.value]

        def __iter__(self):
            return iter([self.value])

    class NoInheritSession:
        owner = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def begin(self):
            return self

        def execute(self, statement, *args):
            sql = str(statement).upper()
            calls.append(sql)
            if sql == "SELECT SESSION_USER":
                return Result(session_user)
            if sql == "SET LOCAL ROLE CAMPUS_AI_DB_OWNER":
                assert session_user == "campus_ai_migration", "must reject runtime before SET ROLE"
                self.owner = True
                return Result(None)
            if "ALEMBIC_VERSION" in sql:
                if not self.owner:
                    raise PermissionError("NOINHERIT cannot read alembic_version before SET ROLE")
                return Result(head)
            raise ReachedAfterSchema

        def scalar(self, statement, *args):
            return self.execute(statement, *args).scalar_one()

        def scalars(self, statement, *args):
            return self.execute(statement, *args)

    session = NoInheritSession()

    def invoke():
        if entry == "initializer":
            request = initialization_module().SchoolFactInitializationRequest.model_validate(
                manifest()
            )
            with SchoolFactInitializer(session_factory=lambda: session)._transaction(request):
                pytest.fail("fake must stop before any actual data")
        elif entry == "recovery":
            require_recovery_authority(session, SimpleNamespace())
        else:
            cli = runpy.run_path(str(Path(__file__).parents[4] / "scripts/school_facts_f1_init.py"))
            if entry == "root":
                cli["root"](SimpleNamespace(begin=lambda: session), SimpleNamespace())
            else:
                listeners = []

                def listen(_target, event_name):
                    assert event_name == "after_begin"

                    def decorate(callback):
                        listeners.append(callback)
                        return callback

                    return decorate

                monkeypatch.setattr(cli["event"], "listens_for", listen)
                cli["migration_session_factory"](None)
                listeners[0](None, None, session)

    if session_user == "campus_ai_migration" and head == "20260905_22":
        with pytest.raises(ReachedAfterSchema):
            invoke()
    else:
        with pytest.raises(
            (
                initialization_module().SchoolFactInitializationRejected,
                SchoolFactChangeRejected,
                ValueError,
            )
        ):
            invoke()
    if session_user != "campus_ai_migration":
        assert calls == ["SELECT SESSION_USER"]
    else:
        assert calls[:2] == ["SELECT SESSION_USER", "SET LOCAL ROLE CAMPUS_AI_DB_OWNER"]
        assert "ALEMBIC_VERSION" in calls[2]


def test_initializer_manual_batch_obeys_existing_source_metadata_contract(monkeypatch):
    from contextlib import nullcontext
    from datetime import UTC, datetime, timedelta
    from hashlib import sha256
    from types import SimpleNamespace

    from app.composition.school_fact_initialization import SchoolFactInitializer

    request = initialization_module().SchoolFactInitializationRequest.model_validate(manifest())
    token = "unit-only-initializer-confirmation"
    record = SimpleNamespace(
        id=UUID(int=99),
        state="prepared",
        manifest_sha256=request.manifest_sha256(),
        confirmation_token_sha256=sha256(token.encode()).hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )

    class CheckedBeforeDatabaseWrite(Exception):
        pass

    class Session:
        def execute(self, statement):
            parameters = statement.compile().params
            assert parameters["source_kind"] == "manual"
            assert parameters.get("filename_summary") is None
            assert parameters["source_size_bytes"] is None
            raise CheckedBeforeDatabaseWrite

    initializer = SchoolFactInitializer(session_factory=lambda: None)
    monkeypatch.setattr(initializer, "_transaction", lambda _: nullcontext(Session()))
    monkeypatch.setattr(initializer, "_existing", lambda *_: record)
    monkeypatch.setattr(initializer, "_preflight", lambda *_: None)
    with pytest.raises(CheckedBeforeDatabaseWrite):
        initializer.confirm(request, credential=token)


@pytest.mark.parametrize(
    "function_name,delegation",
    [
        (
            "school_fact_reader_assignment_terminal",
            "p_school_id, 'assignment', p_target_id, p_content_version",
        ),
        (
            "school_fact_reader_identity_root_is_effective_at",
            "p_school_id, p_fact_kind, p_target_id, p_as_of",
        ),
    ],
)
def test_f1_reader_authority_migration_keeps_tenant_and_role_gate(function_name, delegation):
    import runpy

    path = Path(__file__).parents[2] / (
        "alembic/versions/20260905_22_school_fact_reader_authority.py"
    )
    assert path.is_file(), "Reader authority must follow immutable revision 21"
    migration = runpy.run_path(str(path))
    assert migration["revision"] == "20260905_22"
    assert migration["down_revision"] == "20260905_21"
    statements = []

    class Operations:
        def execute(self, statement):
            statements.append(str(statement))

    migration["upgrade"].__globals__["op"] = Operations()
    migration["upgrade"]()
    sql = "\n".join(statements)
    body = sql.split(f"CREATE FUNCTION app.{function_name}(", 1)[1].split("$function$;", 1)[0]
    assert "SECURITY DEFINER" in body and "SET search_path = pg_catalog" in body
    assert "SESSION_USER <> 'campus_ai_school_fact_reader'" in body
    assert "app.require_school_database_anchor() IS DISTINCT FROM p_school_id" in body
    assert "ERRCODE = '28000'" in body and "ERRCODE = '22023'" in body
    assert delegation in body
    assert "EXCEPTION WHEN" not in body
    assert "FROM public." not in body
    assert sql.index("SET LOCAL ROLE campus_ai_context_definer") < sql.index("CREATE FUNCTION")
    assert "REVOKE CREATE ON SCHEMA app FROM campus_ai_context_definer" in sql
    for statement in statements:
        if statement.startswith("GRANT EXECUTE ON FUNCTION"):
            assert "app.school_fact_reader_" in statement
            assert statement.endswith("TO campus_ai_school_fact_reader")
        assert "GRANT SELECT" not in statement
        assert "GRANT INSERT" not in statement
    if function_name.endswith("identity_root_is_effective_at"):
        assert "p_fact_kind NOT IN ('person', 'organization')" in body
    else:
        assert "p_content_version < 1" in body


def test_f1_reader_authority_role_reconcile_restores_only_narrow_functions():
    path = Path(__file__).parents[4] / "infra/postgres/001-campus-ai-roles.sh"
    script = path.read_text(encoding="utf-8")
    assert "DO $f1_reader_authority$" in script
    block = script.split("DO $f1_reader_authority$", 1)[1].split("$f1_reader_authority$;", 1)[0]
    assert "app.school_fact_reader_assignment_terminal(uuid,uuid,bigint)" in block
    assert (
        "app.school_fact_reader_identity_root_is_effective_at(uuid,text,uuid,timestamptz)" in block
    )
    assert "pg_catalog.to_regprocedure(v_signature) IS NOT NULL" in block
    assert "REVOKE ALL PRIVILEGES ON FUNCTION" in block
    assert "FROM PUBLIC, campus_ai_runtime, campus_ai_school_fact_writer" in block
    assert "GRANT EXECUTE ON FUNCTION %s TO campus_ai_school_fact_reader" in block
    assert "school_fact_content_version_terminal(" not in block
    assert "app.school_fact_root_is_effective_at(" not in block
    assert "GRANT SELECT" not in block and "CREATE FUNCTION" not in block


@pytest.mark.parametrize("artifact", ["migration", "role_reconcile"])
def test_f1_reader_lineage_does_not_enter_legacy_signed_context_policy(artifact):
    import runpy

    expected = (
        "ALTER POLICY tenant_select ON public.school_fact_change_lineage "
        "TO campus_ai_runtime"
    )
    if artifact == "migration":
        path = Path(__file__).parents[2] / (
            "alembic/versions/20260905_22_school_fact_reader_authority.py"
        )
        migration = runpy.run_path(str(path))
        statements = []

        class Operations:
            def execute(self, statement):
                statements.append(str(statement))

        migration["upgrade"].__globals__["op"] = Operations()
        migration["upgrade"]()
        sql = "\n".join(statements)
        assert expected in statements
    else:
        root = Path(__file__).parents[4]
        sql = (root / "infra/postgres/001-campus-ai-roles.sh").read_text(encoding="utf-8")
        sql = sql.split("DO $f1_reader_authority$", 1)[1].split("$f1_reader_authority$;", 1)[0]
        assert expected in " ".join(sql.split())
        assert sql.index("pg_catalog.to_regprocedure(") < sql.index("ALTER POLICY tenant_select")
    assert "DROP POLICY" not in sql
    assert "GRANT EXECUTE ON FUNCTION app.require_school_context()" not in sql
    assert "GRANT SELECT" not in sql


def _apply_batch_with_postcondition_session(monkeypatch, changes, *, education_rows=None,
                                           invalid_residences=0):
    """Keep the real apply orchestration/postconditions; isolate only unit DML."""
    import json
    from hashlib import sha256
    from types import SimpleNamespace

    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from app.platform.school_facts.contracts import CanonicalFactChangeSetV1, CanonicalFactChangeV1

    schema_version = max(change.schema_version for change in changes)
    changes = tuple(CanonicalFactChangeV1.model_validate({
        **change.model_dump(exclude_unset=True), "schema_version": schema_version,
    }) for change in changes)
    school_id = UUID(int=1)
    batch = SimpleNamespace(id=UUID(int=8), mapping_payload={})
    units = tuple(
        SimpleNamespace(
            id=UUID(int=100 + index), version=1,
            payload_sha256=sha256(json.dumps(
                change.model_dump(mode="json", exclude_none=True),
                sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            ).encode()).hexdigest(),
        )
        for index, change in enumerate(changes)
    )

    class Session:
        def execute(self, statement, parameters=None):
            sql = str(statement)
            if sql.startswith("SELECT id FROM schools "):
                return SimpleNamespace(scalar_one_or_none=lambda: school_id)
            if "FROM master_data_import_units" in sql:
                return SimpleNamespace(scalars=lambda: iter(units))
            if "FROM school_education_facts" in sql:
                assert education_rows is not None, "schema22 has no education tables"
                return SimpleNamespace(all=lambda: education_rows)
            if "FROM student_bed_residences AS residence" in sql:
                return SimpleNamespace(scalar_one=lambda: invalid_residences)
            raise AssertionError("unexpected postcondition query")

        def add(self, _row):
            pass

        def flush(self):
            pass

    writer = SqlAlchemySchoolFactPersistence(None, None)
    monkeypatch.setattr(writer, "_current_global_fact_version", lambda *_: 1)
    monkeypatch.setattr(writer, "_ensure_batch_source", lambda *_, **__: UUID(int=9))
    monkeypatch.setattr(
        writer, "_apply_change",
        lambda **kwargs: (UUID(int=10), kwargs["change"].expected_version + 1),
    )
    plan = CanonicalFactChangeSetV1(
        schema_version=schema_version,
        base_version=1, source_sha256="a" * 64, changes=changes,
    )
    return writer.apply_canonical_units_in_transaction(
        Session(), batch=batch, change_set=plan, school_id=school_id,
    )


def test_f1_first_identity_apply_does_not_require_later_education_schema(monkeypatch):
    request = initialization_module().SchoolFactInitializationRequest.model_validate(manifest())
    units = _apply_batch_with_postcondition_session(monkeypatch, request.change_set.changes)
    assert len(units) == 4
    assert all(unit.status == "applied" and unit.target_version == 1 for unit in units)


@pytest.mark.parametrize("kind", ["organization", "assignment"])
@pytest.mark.parametrize("operation", ["upsert", "end", "revoke"])
def test_existing_identity_changes_still_reject_invalid_education_state(
    monkeypatch, kind, operation,
):
    from types import SimpleNamespace

    from app.platform.school_facts.application import SchoolFactChangeRejected
    from app.platform.school_facts.contracts import CanonicalFactChangeV1

    payload = next(c for c in manifest()["change_set"]["changes"] if c["fact_kind"] == kind)
    payload = {**payload, "expected_version": 1, "operation": operation}
    if operation != "upsert":
        payload["content"] = None
    change = CanonicalFactChangeV1.model_validate(payload)
    orphan = ("class_position_definition", "position:orphan", SimpleNamespace(
        valid_from=change.effective_at, valid_to=None,
        payload={"class_source_key": "class:missing", "display_name": "班长",
                 "allow_multiple": False, "allow_concurrent": False, "max_holders": 1},
    ))
    with pytest.raises(SchoolFactChangeRejected):
        _apply_batch_with_postcondition_session(monkeypatch, (change,), education_rows=(orphan,))


@pytest.mark.parametrize(
    "kind", ["education_stage", "teaching_course", "student_school_relationship"]
)
def test_identity_batch_with_education_or_enrollment_still_checks_education(monkeypatch, kind):
    from types import SimpleNamespace

    from app.platform.school_facts.application import SchoolFactChangeRejected
    from app.platform.school_facts.contracts import CanonicalFactChangeV1

    request = initialization_module().SchoolFactInitializationRequest.model_validate(manifest())
    content = (
        {"student_source_key": "student:one", "student_number": "S001",
         "enrollment_status": "enrolled", "residence_mode": "day",
         "primary_campus_source_key": "location:campus",
         "administrative_campus_source_key": "location:campus"}
        if kind == "student_school_relationship" else {"display_name": "合成教育事实"}
    )
    change = CanonicalFactChangeV1.model_validate(dict(
        schema_version=3, operation="upsert", fact_kind=kind, source_key="education:new",
        expected_version=0, effective_at=request.change_set.changes[0].effective_at,
        origin={"sheet_code": "education", "row_number": 5}, content=content,
    ))
    orphan = ("offering_member_source", "source:orphan", SimpleNamespace(
        valid_from=change.effective_at, valid_to=None,
        payload={"offering_source_key": "offering:missing", "source_kind": "administrative_class",
                 "class_source_key": "class:missing"},
    ))
    with pytest.raises(SchoolFactChangeRejected):
        _apply_batch_with_postcondition_session(
            monkeypatch, (*request.change_set.changes, change), education_rows=(orphan,),
        )


@pytest.mark.parametrize("kind,table", [("organization", "organization_units"),
                                       ("assignment", "school_fact_change_lineage")])
def test_new_identity_version_cannot_overwrite_or_reopen_existing_source_key(kind, table):
    from types import SimpleNamespace

    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from app.platform.school_facts.application import SchoolFactChangeRejected
    from app.platform.school_facts.contracts import CanonicalFactChangeV1

    change = CanonicalFactChangeV1.model_validate(next(
        c for c in manifest()["change_set"]["changes"] if c["fact_kind"] == kind
    ))

    class ExistingIdentitySession:
        def execute(self, statement, parameters):
            sql = str(statement)
            assert f"FROM {table}" in sql
            if kind == "organization":
                assert "FOR UPDATE" in sql
            else:
                assert "ORDER BY target_version DESC" in sql
            assert "status" not in sql and "revoked_at" not in sql
            assert parameters["source_key"] == change.source_key
            return SimpleNamespace(one_or_none=lambda: (UUID(int=11), 2))

    with pytest.raises(SchoolFactChangeRejected, match="版本已变化"):
        SqlAlchemySchoolFactPersistence(None, None)._apply_change(
            session=ExistingIdentitySession(), school_id=UUID(int=1), batch_id=UUID(int=8),
            source_id=UUID(int=9), change=change,
        )


@pytest.mark.parametrize("kind", ["assignment", "organization"])
def test_education_cannot_reference_an_identity_that_does_not_exist(kind):
    from types import SimpleNamespace

    from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
    from app.platform.school_facts.application import SchoolFactChangeRejected

    class MissingIdentitySession:
        def execute(self, statement, parameters):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def add(self, _row):
            pytest.fail("a dangling education reference must never be persisted")

    version = SimpleNamespace(
        payload={kind + "_source_key": "future:identity"},
        valid_from=datetime(2026, 9, 5, tzinfo=UTC), valid_to=None,
    )
    with pytest.raises(SchoolFactChangeRejected):
        SqlAlchemySchoolFactPersistence._write_education_references(
            MissingIdentitySession(), UUID(int=1), version,
        )


@pytest.mark.parametrize("operation", ["end", "revoke"])
def test_identity_compatibility_keeps_location_residence_postcondition(monkeypatch, operation):
    from app.platform.school_facts.application import SchoolFactChangeRejected
    from app.platform.school_facts.contracts import CanonicalFactChangeV1

    change = CanonicalFactChangeV1.model_validate(dict(
        operation=operation, fact_kind="location", source_key="location:dorm",
        expected_version=1, effective_at=datetime(2026, 9, 5, tzinfo=UTC),
        origin={"sheet_code": "location", "row_number": 1}, content=None,
    ))
    with pytest.raises(SchoolFactChangeRejected, match="入住历史"):
        _apply_batch_with_postcondition_session(monkeypatch, (change,), invalid_residences=1)
