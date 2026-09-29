from __future__ import annotations

import importlib
import importlib.util
import inspect
from typing import Any

from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.platform.identity import service as identity_service


def _contract_module() -> Any:
    module_name = "app.platform.identity.tuple_validator"
    assert importlib.util.find_spec(module_name) is not None, (
        "Identity must own the tuple lock and validation contract"
    )
    return importlib.import_module(module_name)


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _Dialect:
    name = "postgresql"


class _Bind:
    dialect = _Dialect()


class _RecordingSession:
    def __init__(self, user_id: object = 42) -> None:
        self.user_id = user_id
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get_bind(self) -> _Bind:
        return _Bind()

    def execute(self, statement: object, parameters: dict[str, object]) -> _Result:
        self.calls.append((str(statement), parameters))
        return _Result(self.user_id)


def test_identity_tuple_lock_material_is_stable_and_unambiguous() -> None:
    contract = _contract_module()

    first = contract.identity_tuple_advisory_lock_material(
        provider_code="dingtalk",
        provider_tenant_key="corp-001",
        external_subject="staff-001",
    )
    repeated = contract.identity_tuple_advisory_lock_material(
        provider_code="dingtalk",
        provider_tenant_key="corp-001",
        external_subject="staff-001",
    )
    delimiter_collision = contract.identity_tuple_advisory_lock_material(
        provider_code="dingtalk\x1fcorp-001",
        provider_tenant_key="staff-001",
        external_subject="",
    )

    assert first == repeated
    assert first == "v1|8:dingtalk|8:corp-001|9:staff-001"
    assert delimiter_collision != first


def test_sql_validator_locks_tuple_then_exact_user_row() -> None:
    contract = _contract_module()
    session = _RecordingSession(user_id=42)
    validator = contract.SqlAlchemyIdentityTupleValidator()

    user_id = validator.lock_existing_user_id(
        session,
        provider_code="dingtalk",
        provider_tenant_key="corp-001",
        external_subject="staff-001",
    )

    assert user_id == 42
    assert len(session.calls) == 1
    user_sql, user_parameters = session.calls[0]
    assert user_sql == (
        "SELECT app.lock_existing_identity_user_id("
        ":provider_code, :provider_tenant_key, :external_subject)"
    )
    assert user_parameters == {
        "provider_code": "dingtalk",
        "provider_tenant_key": "corp-001",
        "external_subject": "staff-001",
    }


def test_identity_service_reuses_identity_owned_lock_key() -> None:
    contract = _contract_module()
    session = _RecordingSession()

    identity_service._lock_identity_mapping(
        session,  # type: ignore[arg-type]
        provider_code="dingtalk",
        provider_tenant_key="corp-001",
        external_subject="staff-001",
    )

    assert session.calls == [
        (
            "SELECT pg_advisory_xact_lock(hashtextextended(CAST(:identity_key AS text), 0))",
            {
                "identity_key": contract.identity_tuple_advisory_lock_material(
                    provider_code="dingtalk",
                    provider_tenant_key="corp-001",
                    external_subject="staff-001",
                )
            },
        ),
    ]


def test_school_fact_login_mapping_delegates_identity_validation() -> None:
    contract = _contract_module()
    signature = inspect.signature(SqlAlchemySchoolFactPersistence.__init__)
    source = inspect.getsource(SqlAlchemySchoolFactPersistence._upsert_login_mapping)

    assert "identity_tuple_validator" in signature.parameters
    assert "_identity_tuple_validator.lock_existing_user_id" in source
    assert "SELECT id FROM users" not in source
    assert "identity_models.User" not in source
    assert hasattr(contract, "IdentityTupleValidator")
