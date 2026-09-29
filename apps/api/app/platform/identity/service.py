from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.integrations.dingtalk.client import DingTalkIdentity
from app.platform.audit.service import append_audit_log
from app.platform.identity.models import User
from app.platform.identity.tuple_validator import identity_tuple_advisory_lock_material

_LOCAL_TEST_PROVIDER = "local_test"
_LOCAL_TEST_TENANT = "development"
_DINGTALK_PROVIDER = "dingtalk"
_LEGACY_NAMESPACE = "legacy"


def _lock_identity_mapping(
    session: Session,
    *,
    provider_code: str,
    provider_tenant_key: str,
    external_subject: str,
) -> None:
    """在 PostgreSQL 事务内串行化同一外部身份的首次创建或旧键认领。"""

    if session.get_bind().dialect.name != "postgresql":
        return
    identity_key = identity_tuple_advisory_lock_material(
        provider_code=provider_code,
        provider_tenant_key=provider_tenant_key,
        external_subject=external_subject,
    )
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(CAST(:identity_key AS text), 0))"),
        {"identity_key": identity_key},
    )


def _find_namespaced_user(
    session: Session,
    *,
    provider_code: str,
    provider_tenant_key: str,
    external_subject: str,
) -> User | None:
    return session.scalar(
        select(User)
        .where(
            User.provider_code == provider_code,
            User.provider_tenant_key == provider_tenant_key,
            User.external_subject == external_subject,
        )
        .with_for_update()
    )


def _claim_legacy_user(
    session: Session,
    *,
    legacy_external_subject: str,
    provider_code: str,
    provider_tenant_key: str,
    external_subject: str,
) -> User | None:
    user_id = session.execute(
        text(
            """
            SELECT app.claim_legacy_identity_user_id(
                :legacy_external_subject,
                :provider_code,
                :provider_tenant_key,
                :external_subject
            )
            """
        ),
        {
            "legacy_external_subject": legacy_external_subject,
            "provider_code": provider_code,
            "provider_tenant_key": provider_tenant_key,
            "external_subject": external_subject,
        },
    )
    user_id = user_id.scalar_one_or_none()
    if user_id is None:
        return None
    return session.get(User, int(user_id), populate_existing=True)


def find_or_create_test_user(session: Session, external_subject: str, display_name: str) -> User:
    """按测试身份主体读取或创建本地用户映射，并在新建时提交事务。"""

    _lock_identity_mapping(
        session,
        provider_code=_LOCAL_TEST_PROVIDER,
        provider_tenant_key=_LOCAL_TEST_TENANT,
        external_subject=external_subject,
    )
    user = _find_namespaced_user(
        session,
        provider_code=_LOCAL_TEST_PROVIDER,
        provider_tenant_key=_LOCAL_TEST_TENANT,
        external_subject=external_subject,
    )
    if user is not None:
        return user

    user = _claim_legacy_user(
        session,
        legacy_external_subject=external_subject,
        provider_code=_LOCAL_TEST_PROVIDER,
        provider_tenant_key=_LOCAL_TEST_TENANT,
        external_subject=external_subject,
    )
    if user is not None:
        session.flush()
        session.commit()
        return user

    return _create_local_test_user(session, external_subject, display_name)


def find_or_create_namespaced_test_user(
    session: Session, external_subject: str, display_name: str
) -> User:
    """读取或创建明确 local_test/development 主体；永不认领或修改 legacy 用户。

    此入口只拥有账号映射，调用者负责受控工程初始化的固定账号范围。
    不创建学校人员、正式登录映射、任职或授权；既有停用账号原样返回。
    """

    if (
        type(external_subject) is not str
        or not external_subject.strip()
        or len(external_subject) > 255
        or type(display_name) is not str
        or not display_name.strip()
        or len(display_name) > 120
    ):
        raise ValueError("Explicit local test subject and display name are required.")
    _lock_identity_mapping(
        session,
        provider_code=_LOCAL_TEST_PROVIDER,
        provider_tenant_key=_LOCAL_TEST_TENANT,
        external_subject=external_subject,
    )
    user = _find_namespaced_user(
        session,
        provider_code=_LOCAL_TEST_PROVIDER,
        provider_tenant_key=_LOCAL_TEST_TENANT,
        external_subject=external_subject,
    )
    if user is not None:
        return user
    return _create_local_test_user(session, external_subject, display_name)


def _create_local_test_user(session: Session, external_subject: str, display_name: str) -> User:
    """复用本地测试账号的既有创建事务，不包含旧主体认领或学校权限。"""

    user = User(
        provider_code=_LOCAL_TEST_PROVIDER,
        provider_tenant_key=_LOCAL_TEST_TENANT,
        external_subject=external_subject,
        display_name=display_name,
        is_active=True,
        security_version=1,
    )
    session.add(user)
    session.flush()
    session.commit()
    return user


def find_or_create_dingtalk_user(
    session: Session,
    identity: DingTalkIdentity,
    request_id: str,
) -> User:
    """按钉钉稳定身份映射本地用户，并仅在首次创建时追加映射审计。"""

    external_subject = identity.personnel_id
    legacy_external_subject = (
        f"{_DINGTALK_PROVIDER}:{identity.corporation_id}:{identity.personnel_id}"
    )
    _lock_identity_mapping(
        session,
        provider_code=_DINGTALK_PROVIDER,
        provider_tenant_key=identity.corporation_id,
        external_subject=external_subject,
    )
    user = _find_namespaced_user(
        session,
        provider_code=_DINGTALK_PROVIDER,
        provider_tenant_key=identity.corporation_id,
        external_subject=external_subject,
    )
    if user is None:
        user = _claim_legacy_user(
            session,
            legacy_external_subject=legacy_external_subject,
            provider_code=_DINGTALK_PROVIDER,
            provider_tenant_key=identity.corporation_id,
            external_subject=external_subject,
        )
    if user is not None:
        user.display_name = identity.display_name
        session.flush()
        return user

    user = User(
        provider_code=_DINGTALK_PROVIDER,
        provider_tenant_key=identity.corporation_id,
        external_subject=external_subject,
        display_name=identity.display_name,
        is_active=True,
        security_version=1,
    )
    session.add(user)
    session.flush()
    append_audit_log(
        session=session,
        actor_user_id=None,
        event_type="identity.mapping.created",
        target_type="user",
        target_id=str(user.id),
        before_summary={},
        after_summary={
            "provider_code": _DINGTALK_PROVIDER,
            "provider_tenant_key": identity.corporation_id,
            "external_subject": external_subject,
            "display_name": identity.display_name,
        },
        request_id=request_id,
        reason="钉钉首次身份映射",
    )
    return user
