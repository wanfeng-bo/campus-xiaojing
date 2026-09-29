import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import SQLAlchemyError

import app.main as main_module
from app.platform.identity.context_proof import ContextSigningKeyMismatch

app = main_module.app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_health_returns_ok_when_database_probe_succeeds(monkeypatch) -> None:
    monkeypatch.setattr("app.main.check_database_connection", lambda: None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


@pytest.mark.anyio
async def test_health_returns_safe_message_when_database_probe_fails(monkeypatch) -> None:
    def raise_database_error() -> None:
        raise SQLAlchemyError("database connection details must stay private")

    monkeypatch.setattr("app.main.check_database_connection", raise_database_error)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "数据库暂时不可用，请稍后重试。"}
    assert "database connection details" not in response.text


@pytest.mark.anyio
async def test_health_hides_context_signing_key_mismatch_details(monkeypatch) -> None:
    def raise_key_mismatch() -> None:
        raise ContextSigningKeyMismatch("fingerprints and keys must stay private")

    monkeypatch.setattr("app.main.check_database_connection", raise_key_mismatch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "数据库暂时不可用，请稍后重试。"}
    assert "fingerprints and keys" not in response.text


@pytest.mark.anyio
async def test_application_lifespan_checks_database_before_serving(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        main_module,
        "check_database_connection",
        lambda: calls.append("checked"),
    )

    async with main_module.application_lifespan(app):
        assert calls == ["checked"]
