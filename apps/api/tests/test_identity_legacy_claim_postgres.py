from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import URL, create_engine
from sqlalchemy.orm import sessionmaker

from app.integrations.dingtalk.client import DingTalkIdentity
from app.platform.identity.service import find_or_create_dingtalk_user
from tests.master_data.conftest import PostgresRoles

pytestmark = pytest.mark.integration


def _runtime_url(roles: PostgresRoles) -> URL:
    values = roles.runtime_connection_parameters
    return URL.create(
        "postgresql+psycopg",
        username=str(values["user"]),
        password=str(values["password"]),
        host=str(values["host"]),
        port=int(values["port"]),
        database=str(values["dbname"]),
    )


def test_revision20_legacy_claim_race_returns_one_existing_user_without_creation(
    isolated_migration_schema: PostgresRoles,
) -> None:
    roles = isolated_migration_schema
    corporation_id = f"corp-{uuid4().hex[:12]}"
    personnel_id = f"person-{uuid4().hex[:12]}"
    legacy_subject = f"dingtalk:{corporation_id}:{personnel_id}"
    with roles.admin_connection() as connection:
        seeded = connection.execute(
            "INSERT INTO public.users "
            "(provider_code, provider_tenant_key, external_subject, display_name, "
            "is_active, security_version) VALUES "
            "('legacy', 'legacy', %s, 'Legacy Name', true, 1) RETURNING id",
            (legacy_subject,),
        ).fetchone()
        assert seeded is not None
        seeded_user_id = int(seeded[0])

    engine = create_engine(_runtime_url(roles), pool_size=2, max_overflow=0)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    barrier = Barrier(2)
    identity = DingTalkIdentity(
        corporation_id=corporation_id,
        personnel_id=personnel_id,
        display_name="Claimed Name",
    )

    def claim(ordinal: int) -> int:
        with factory() as session:
            barrier.wait(timeout=10)
            user = find_or_create_dingtalk_user(
                session,
                identity,
                request_id=f"r1-identity-race-{ordinal}-{uuid4()}",
            )
            session.commit()
            return int(user.id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        user_ids = tuple(executor.map(claim, (1, 2)))

    assert user_ids == (seeded_user_id, seeded_user_id)
    with roles.admin_connection() as connection:
        rows = connection.execute(
            "SELECT id, provider_code, provider_tenant_key, external_subject, display_name "
            "FROM public.users ORDER BY id"
        ).fetchall()
    assert rows == [
        (
            seeded_user_id,
            "dingtalk",
            corporation_id,
            personnel_id,
            "Claimed Name",
        )
    ]
    engine.dispose()
