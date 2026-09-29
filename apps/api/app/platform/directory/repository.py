from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Text, cast, exists, func, literal, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import TableValuedAlias

from app.platform.contracts import EffectiveInterval, PersonId, SchoolId
from app.platform.directory.contracts import (
    LockedPersonFact,
    LoginMappingSnapshot,
    PersonSnapshot,
)
from app.platform.directory.models import (
    AccountPersonBinding,
    AccountPersonBindingVersion,
    SchoolPerson,
    SchoolPersonName,
)


class SqlAlchemyPersonRepository:
    """在调用方事务内维护自然人与历史姓名版本。"""

    def __init__(self, session: Session) -> None:
        """绑定调用方 Session，不创建或关闭数据库会话。"""

        self._session = session

    def add_person(self, person: SchoolPerson) -> None:
        """把自然人稳定主体加入现有事务并 flush。"""

        self._session.add(person)
        self._session.flush()

    def add_name(self, name: SchoolPersonName) -> None:
        """加入姓名版本并 flush，使区间冲突在提交前暴露。"""

        self._session.add(name)
        self._session.flush()

    def add_account_binding(self, binding: AccountPersonBinding) -> None:
        """加入账号绑定并 flush，使跨校或重叠错误立即暴露。"""

        self._session.add(binding)
        self._session.flush()

    def get_person_for_update(
        self,
        school_id: SchoolId,
        person_id: PersonId,
    ) -> SchoolPerson | None:
        """在学校范围内锁定指定自然人稳定主体。"""

        statement = (
            select(SchoolPerson)
            .where(
                SchoolPerson.school_id == school_id,
                SchoolPerson.id == person_id,
            )
            .with_for_update()
        )
        return self._session.scalar(statement)

    def save_person(self, person: SchoolPerson) -> None:
        """flush 自然人变更，不执行 commit 或 rollback。"""

        self._session.add(person)
        self._session.flush()

    def lock_person(
        self,
        school_id: SchoolId,
        person_id: PersonId,
        *,
        as_of: datetime,
    ) -> LockedPersonFact | None:
        """锁定稳定人员行，以账本时态投影当前状态和稳定版本。"""

        statement = (
            select(
                SchoolPerson,
                func.app.school_fact_root_is_effective_at(
                    SchoolPerson.school_id,
                    cast(literal("person"), Text),
                    SchoolPerson.id,
                    as_of,
                ).label("is_effective"),
            )
            .where(
                SchoolPerson.school_id == school_id,
                SchoolPerson.id == person_id,
            )
            .with_for_update(of=SchoolPerson)
        )
        row = self._session.execute(statement).one_or_none()
        if row is None or not row.is_effective:
            return None
        person = row.SchoolPerson
        return LockedPersonFact(
            school_id=SchoolId(person.school_id),
            person_id=PersonId(person.id),
            status="active",
            version=person.version,
        )

    def resolve_person_snapshot(
        self,
        school_id: SchoolId,
        person_id: PersonId,
        *,
        as_of: datetime,
    ) -> PersonSnapshot | None:
        """按历史时点解析姓名版本，并以正式账本裁定结束与撤销。"""

        terminal = _content_version_terminal(
            fact_kind="person",
            school_id=SchoolPersonName.school_id,
            target_id=SchoolPersonName.person_id,
            content_version=SchoolPersonName.version,
        )
        statement = select(
            SchoolPersonName.school_id,
            SchoolPersonName.person_id,
            SchoolPersonName.display_name,
            SchoolPersonName.version,
            SchoolPersonName.valid_from,
            SchoolPersonName.valid_to,
            func.clock_timestamp().label("captured_at"),
        ).where(
            SchoolPersonName.school_id == school_id,
            SchoolPersonName.person_id == person_id,
            SchoolPersonName.status == "confirmed",
            SchoolPersonName.valid_from <= as_of,
            or_(
                SchoolPersonName.valid_to.is_(None),
                SchoolPersonName.valid_to > as_of,
            ),
            ~_terminal_invalid_at(
                terminal,
                as_of=as_of,
                correlate_from=SchoolPersonName,
            ),
        )
        name = self._session.execute(statement).mappings().one_or_none()
        if name is None:
            return None
        return PersonSnapshot(
            school_id=SchoolId(name["school_id"]),
            person_id=PersonId(name["person_id"]),
            display_name=name["display_name"],
            source_version=name["version"],
            captured_at=name["captured_at"],
            effective_interval=EffectiveInterval(
                valid_from=name["valid_from"],
                valid_to=name["valid_to"],
            ),
            snapshot_basis="directory.person_name_as_of",
        )

    def resolve_person_snapshots(
        self,
        school_id: SchoolId,
        person_ids: tuple[PersonId, ...],
        *,
        as_of: datetime,
    ) -> dict[PersonId, PersonSnapshot]:
        """一次解析同校姓名版本，并以正式账本裁定结束与撤销。"""

        if not person_ids:
            return {}
        terminal = _content_version_terminal(
            fact_kind="person",
            school_id=SchoolPersonName.school_id,
            target_id=SchoolPersonName.person_id,
            content_version=SchoolPersonName.version,
        )
        statement = select(
            SchoolPersonName.school_id,
            SchoolPersonName.person_id,
            SchoolPersonName.display_name,
            SchoolPersonName.version,
            SchoolPersonName.valid_from,
            SchoolPersonName.valid_to,
            func.clock_timestamp().label("captured_at"),
        ).where(
            SchoolPersonName.school_id == school_id,
            SchoolPersonName.person_id.in_(person_ids),
            SchoolPersonName.status == "confirmed",
            SchoolPersonName.valid_from <= as_of,
            or_(
                SchoolPersonName.valid_to.is_(None),
                SchoolPersonName.valid_to > as_of,
            ),
            ~_terminal_invalid_at(
                terminal,
                as_of=as_of,
                correlate_from=SchoolPersonName,
            ),
        )
        names = self._session.execute(statement).mappings().all()
        return {
            PersonId(name["person_id"]): PersonSnapshot(
                school_id=SchoolId(name["school_id"]),
                person_id=PersonId(name["person_id"]),
                display_name=name["display_name"],
                source_version=name["version"],
                captured_at=name["captured_at"],
                effective_interval=EffectiveInterval(
                    valid_from=name["valid_from"],
                    valid_to=name["valid_to"],
                ),
                snapshot_basis="directory.person_name_as_of",
            )
            for name in names
        }

    def resolve_login_mapping_snapshot(
        self,
        school_id: SchoolId,
        binding_id: UUID,
        *,
        as_of: datetime,
    ) -> LoginMappingSnapshot | None:
        """按技术内容切片还原指定时点的完整登录映射。"""

        terminal = _content_version_terminal(
            fact_kind="login_mapping",
            school_id=AccountPersonBindingVersion.school_id,
            target_id=AccountPersonBindingVersion.binding_id,
            content_version=AccountPersonBindingVersion.target_version,
        )
        statement = select(
            AccountPersonBindingVersion,
            func.clock_timestamp().label("captured_at"),
        ).where(
            AccountPersonBindingVersion.school_id == school_id,
            AccountPersonBindingVersion.binding_id == binding_id,
            AccountPersonBindingVersion.status == "confirmed",
            AccountPersonBindingVersion.revoked_at.is_(None),
            AccountPersonBindingVersion.effective_from <= as_of,
            or_(
                AccountPersonBindingVersion.effective_to.is_(None),
                AccountPersonBindingVersion.effective_to > as_of,
            ),
            ~_terminal_invalid_at(
                terminal,
                as_of=as_of,
                correlate_from=AccountPersonBindingVersion,
            ),
        )
        row = self._session.execute(statement).one_or_none()
        if row is None:
            return None
        version, captured_at = row
        return LoginMappingSnapshot(
            school_id=SchoolId(version.school_id),
            binding_id=version.binding_id,
            account_user_id=version.account_user_id,
            person_id=PersonId(version.person_id),
            provider_code=version.provider_code,
            provider_tenant_key=version.provider_tenant_key,
            external_subject=version.external_subject,
            status=version.status,
            source_version=version.target_version,
            captured_at=captured_at,
            effective_interval=EffectiveInterval(
                valid_from=version.effective_from,
                valid_to=version.effective_to,
            ),
            snapshot_basis="directory.login_mapping_content_slice_as_of",
        )


def _content_version_terminal(
    *,
    fact_kind: str,
    school_id: Any,
    target_id: Any,
    content_version: Any,
) -> TableValuedAlias:
    """Build the narrow ledger lookup without granting runtime table access."""

    return func.app.school_fact_content_version_terminal(
        school_id,
        cast(literal(fact_kind), Text),
        target_id,
        content_version,
    ).table_valued("operation", "effective_at", joins_implicitly=True)


def _terminal_invalid_at(
    terminal: TableValuedAlias,
    *,
    as_of: datetime,
    correlate_from: Any,
):
    """A revoke erases the slice; an end bounds it at its recorded effective time."""

    return exists(
        select(1)
        .select_from(terminal)
        .where(
            or_(
                terminal.c.operation == "revoke",
                terminal.c.effective_at.is_(None),
                terminal.c.effective_at <= as_of,
            )
        )
    ).correlate(correlate_from)
