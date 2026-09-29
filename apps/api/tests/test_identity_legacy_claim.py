from __future__ import annotations

from dataclasses import dataclass

from app.platform.identity import service
from app.platform.identity.models import User


class _Result:
    def scalar_one_or_none(self) -> int:
        return 41


@dataclass
class _ReloadedUser:
    id: int = 41
    provider_code: str = "dingtalk"
    provider_tenant_key: str = "corp-001"
    external_subject: str = "staff-001"


class _Session:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, dict[str, object]]] = []
        self.get_calls: list[tuple[type[object], int, bool]] = []
        self.reloaded = _ReloadedUser()

    def execute(self, statement: object, parameters: dict[str, object]) -> _Result:
        self.execute_calls.append((str(statement), parameters))
        return _Result()

    def get(self, entity: type[object], identity: int, *, populate_existing: bool) -> object:
        self.get_calls.append((entity, identity, populate_existing))
        return self.reloaded


def test_legacy_claim_uses_revision20_capability_and_exactly_reloads_user() -> None:
    session = _Session()

    claimed = service._claim_legacy_user(  # pyright: ignore[reportPrivateUsage]
        session,  # type: ignore[arg-type]
        legacy_external_subject="dingtalk:corp-001:staff-001",
        provider_code="dingtalk",
        provider_tenant_key="corp-001",
        external_subject="staff-001",
    )

    assert claimed is session.reloaded
    assert len(session.execute_calls) == 1
    sql, parameters = session.execute_calls[0]
    assert " ".join(sql.split()) == (
        "SELECT app.claim_legacy_identity_user_id( "
        ":legacy_external_subject, :provider_code, "
        ":provider_tenant_key, :external_subject )"
    )
    assert parameters == {
        "legacy_external_subject": "dingtalk:corp-001:staff-001",
        "provider_code": "dingtalk",
        "provider_tenant_key": "corp-001",
        "external_subject": "staff-001",
    }
    assert session.get_calls == [(User, 41, True)]
