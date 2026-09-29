# pyright: strict
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, Protocol, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.platform.authorization.secure_reader_adapters import ThinAuthorizationReadAdapter
from app.platform.authorization.secure_reader_contracts import (
    OfficialContactCampusRow,
    OfficialContactMaintenanceTargetRow,
    PublishedOfficialContactRow,
    ReadAuthorization,
    SchoolFactReadSelection,
    SecureReadDenied,
    SecureReadPage,
    SecureReadRequest,
)
from app.platform.authorization.secure_reader_registry import ReadOperation
from app.platform.authorization.thin_contracts import ThinAuthorizationBasis
from app.platform.contracts import CommandExecutionContext, StaffActorContext
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1
from app.platform.school_facts.read_policy import (
    OFFICIAL_CONTACT_CAMPUSES,
    OFFICIAL_CONTACT_MAINTAIN,
    OFFICIAL_CONTACT_MAINTAINER_DUTY,
    OFFICIAL_CONTACT_MAINTENANCE_TARGET,
    OFFICIAL_CONTACT_MODULE,
    OFFICIAL_CONTACTS,
    SCHOOL_SCOPE_OWNER,
    OfficialContactCampus,
    OfficialContactFact,
    OfficialContactQueryResult,
    OfficialContactQuerySelection,
    SchoolFactAuthorizationUnavailable,
)

_MAX_QUERY_ROWS = 100
_DEFAULT_SOURCE_DESCRIPTION = "本实例正式学校事实维护通道的发布记录。"


class _Reader(Protocol):
    def read(self, request: SecureReadRequest) -> SecureReadPage: ...


class OfficialContactQueryRepository:
    """Resolve a campus before contact reads; all SQL remains behind SecureReader."""

    def __init__(
        self,
        *,
        reader: _Reader,
        source_description: str = _DEFAULT_SOURCE_DESCRIPTION,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not source_description.strip() or len(source_description) > 500:
            raise ValueError("source description must be bounded")
        self._reader = reader
        self._source_description = source_description
        self._clock = clock or (lambda: datetime.now(UTC))

    def read_official_contacts(
        self,
        *,
        actor: StaffActorContext,
        campus_name: str | None = None,
        campus_ref: str | None = None,
        subject: Literal["official_contacts", "campuses"] = "official_contacts",
        scope: Literal["selected", "all_authorized"] = "selected",
    ) -> OfficialContactQueryResult:
        if subject not in {"official_contacts", "campuses"} or scope not in {
            "selected",
            "all_authorized",
        }:
            raise ValueError("invalid fixed query scope")
        if scope == "all_authorized" and (campus_name is not None or campus_ref is not None):
            raise ValueError("an all-authorized query cannot also select one campus")
        selection = self._selection(campus_name=campus_name, campus_ref=campus_ref)
        query = OfficialContactQuerySelection(
            subject=subject,
            scope=scope,
            campus_name=selection.campus_name,
            campus_ref=campus_ref,
        )
        now = self._clock()
        try:
            campuses = self._reader.read(
                SecureReadRequest(
                    actor=actor,
                    operation_code=OFFICIAL_CONTACT_CAMPUSES,
                    page_size=_MAX_QUERY_ROWS,
                    selection=selection,
                )
            )
            if any(type(row) is not OfficialContactCampusRow for row in campuses.rows):
                raise SecureReadDenied("invalid campus projection")
            choices = tuple(
                OfficialContactCampus(
                    campus_ref=f"campus:{row.row_id}",
                    display_name=row.display_name,
                    display_path=row.display_path,
                    version=row.version,
                )
                for row in cast(tuple[OfficialContactCampusRow, ...], campuses.rows)
            )
            if scope == "selected" and campus_name is None and campus_ref is None:
                return self._result(
                    "selection_required",
                    campuses.as_of,
                    query=query,
                    campuses=choices,
                    matched_count=campuses.total_count,
                    has_more=campuses.next_cursor is not None,
                )
            if not choices:
                return self._result(
                    "unmatched"
                    if campus_name is not None or campus_ref is not None
                    else "no_current_records",
                    campuses.as_of,
                    query=query,
                    matched_count=0 if subject == "campuses" else None,
                )
            if scope == "selected" and (len(choices) != 1 or campuses.next_cursor is not None):
                return self._result(
                    "ambiguous",
                    campuses.as_of,
                    query=query,
                    campuses=choices,
                    matched_count=campuses.total_count,
                    has_more=campuses.next_cursor is not None,
                )
            if subject == "campuses":
                return self._result(
                    "known",
                    campuses.as_of,
                    query=query,
                    campuses=choices,
                    matched_count=campuses.total_count,
                    has_more=campuses.next_cursor is not None,
                )

            facts: list[OfficialContactFact] = []
            matched_count = 0
            has_more = campuses.next_cursor is not None
            queried_at = campuses.as_of
            for campus in cast(tuple[OfficialContactCampusRow, ...], campuses.rows):
                # Each initial Reader page counts its fixed authorized relation. Once
                # the display is full, fetch only one row to obtain the remaining counts.
                remaining = _MAX_QUERY_ROWS - len(facts)
                contacts = self._reader.read(
                    SecureReadRequest(
                        actor=actor,
                        operation_code=OFFICIAL_CONTACTS,
                        page_size=max(1, remaining),
                        selection=SchoolFactReadSelection(campus_id=campus.row_id),
                    )
                )
                if any(type(row) is not PublishedOfficialContactRow for row in contacts.rows):
                    raise SecureReadDenied("invalid contact projection")
                matched_count += contacts.total_count
                queried_at = contacts.as_of
                has_more = has_more or contacts.total_count > remaining
                facts.extend(
                    OfficialContactFact(
                        fact_ref=f"official-contact:{row.lineage_id}",
                        campus_ref=f"campus:{row.campus_id}",
                        campus_name=row.campus_name,
                        contact_kind=row.contact_kind,
                        contact_value=row.contact_value,
                        service_hours=row.service_hours,
                        valid_from=row.valid_from,
                        valid_to=row.valid_to,
                        version=row.version,
                        source_kind=row.source_kind,
                        source_description=self._source_description,
                        publication_ref=f"publication:{row.batch_id}",
                        published_at=row.published_at,
                    )
                    for row in cast(
                        tuple[PublishedOfficialContactRow, ...], contacts.rows[:remaining]
                    )
                )
            return self._result(
                "known" if facts else "no_current_records",
                queried_at,
                query=query,
                campuses=choices,
                contacts=tuple(facts),
                has_more=has_more,
                matched_count=matched_count if campuses.next_cursor is None else None,
            )
        except SecureReadDenied:
            return self._result("forbidden", now, query=query)
        except (SQLAlchemyError, SchoolFactAuthorizationUnavailable):
            return self._result("source_unavailable", now, query=query)

    def resolve_maintenance_target(
        self,
        *,
        actor: StaffActorContext,
        fact_ref: str,
    ) -> OfficialContactMaintenanceTargetRow:
        """Private Writer adapter input; this result must never enter a model payload."""
        if type(fact_ref) is not str or not fact_ref.startswith("official-contact:"):
            raise ValueError("invalid official contact reference")
        lineage_id = UUID(fact_ref.removeprefix("official-contact:"))
        page = self._reader.read(
            SecureReadRequest(
                actor=actor,
                operation_code=OFFICIAL_CONTACT_MAINTENANCE_TARGET,
                page_size=1,
                selection=SchoolFactReadSelection(lineage_id=lineage_id),
            )
        )
        if (
            len(page.rows) != 1
            or page.next_cursor is not None
            or type(page.rows[0]) is not OfficialContactMaintenanceTargetRow
        ):
            raise SchoolFactChangeRejected("联系事实已变化或不可维护，请重新查询。")
        return page.rows[0]

    @staticmethod
    def _selection(*, campus_name: str | None, campus_ref: str | None) -> SchoolFactReadSelection:
        if campus_ref is not None:
            if campus_name is not None or not campus_ref.startswith("campus:"):
                raise ValueError("invalid campus selection")
            try:
                campus_id = UUID(campus_ref.removeprefix("campus:"))
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid campus selection") from exc
            return SchoolFactReadSelection(campus_id=campus_id)
        return SchoolFactReadSelection(campus_name=campus_name)

    @staticmethod
    def _result(
        status: Literal[
            "known",
            "not_recorded",
            "no_current_records",
            "selection_required",
            "unmatched",
            "ambiguous",
            "forbidden",
            "source_unavailable",
        ],
        queried_at: datetime,
        *,
        query: OfficialContactQuerySelection,
        campuses: tuple[OfficialContactCampus, ...] = (),
        contacts: tuple[OfficialContactFact, ...] = (),
        has_more: bool = False,
        matched_count: int | None = None,
    ) -> OfficialContactQueryResult:
        if query.subject == "campuses":
            query_scope = (
                "当前身份获准查询范围内、当前有效的校区记录；"
                "校区已录入不代表该校区已有联系方式，学校现实的完整覆盖未声明。"
            )
        else:
            prefix = "全部获准校区" if query.scope == "all_authorized" else "已解析校区"
            query_scope = (
                f"仅{prefix}直接所属、已正式发布且读取时有效的公开或校内可见官方渠道；"
                "未涵盖各部门及下级地点，学校现实的完整覆盖未声明。"
            )
        return OfficialContactQueryResult(
            status=status,
            queried_at=queried_at,
            coverage="unknown",
            query_scope=query_scope,
            campuses=campuses,
            contacts=contacts,
            has_more=has_more,
            query=query,
            matched_count=matched_count,
        )


class OfficialContactFinalAuthorizationGuard:
    """Recheck the owning Policy while the Writer transaction holds the grant epoch lock."""

    def __init__(
        self,
        *,
        thin_service: object,
        scope_id: UUID,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(scope_id) is not UUID:
            raise ValueError("official contact scope must be server-owned")
        self._scope_id = scope_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._authorizer = ThinAuthorizationReadAdapter(
            thin_service=thin_service,
            scope_id_resolver=lambda _operation, _actor: scope_id,
        )
        self._operation = ReadOperation(
            code=OFFICIAL_CONTACTS,
            projection_name="official_contact_maintenance",
            action_code=OFFICIAL_CONTACT_MAINTAIN,
            module_name=OFFICIAL_CONTACT_MODULE,
        )

    def require_current(self, actor: StaffActorContext) -> ReadAuthorization:
        """Use the identical Policy for prepare/UI checks; final commit still rechecks it."""
        try:
            authorization = self._authorizer.decide(
                operation=self._operation,
                actor=actor,
                as_of=self._clock(),
            )
            scope = authorization.scope_plan
            if (
                not authorization.allowed
                or type(authorization.basis) is not ThinAuthorizationBasis
                or scope is None
                or scope.scope_id != self._scope_id
                or scope.owner != SCHOOL_SCOPE_OWNER
                or scope.reference != f"school:{actor.school_id}"
                or scope.version != 1
                or authorization.basis.governance.duty_code != OFFICIAL_CONTACT_MAINTAINER_DUTY
            ):
                raise SchoolFactChangeRejected("当前没有有效的官方联系维护职责。")
            return authorization
        except (SchoolFactChangeRejected, SchoolFactAuthorizationUnavailable):
            raise
        except Exception as exc:
            raise SchoolFactAuthorizationUnavailable("当前官方联系维护授权不可验证。") from exc

    def __call__(
        self,
        session: Session,
        context: CommandExecutionContext,
        change_set: CanonicalFactChangeSetV1,
    ) -> None:
        if (
            type(context) is not CommandExecutionContext
            or type(context.actor) is not StaffActorContext
            or type(change_set) is not CanonicalFactChangeSetV1
            or not change_set.changes
            or any(change.fact_kind != "official_contact" for change in change_set.changes)
        ):
            raise SchoolFactChangeRejected("此维护能力只接受正式官方联系事实。")
        actor = context.actor
        try:
            epoch = session.scalar(
                text("SELECT app.lock_school_fact_governance_epoch(:school_id, :assignment_id)"),
                {
                    "school_id": UUID(str(actor.school_id)),
                    "assignment_id": UUID(str(actor.assignment_id)),
                },
            )
            if type(epoch) is not int or epoch < 1:
                raise SchoolFactChangeRejected("当前维护授权缺少可验证的版本。")
            authorization = self.require_current(actor)
            basis = authorization.basis
            if (
                type(basis) is not ThinAuthorizationBasis
                or basis.governance.assignment_epoch != epoch
                or not self._authorizer.revalidate(authorization, actor=actor, as_of=self._clock())
            ):
                raise SchoolFactChangeRejected("当前官方联系维护授权已经变化。")
        except (SchoolFactChangeRejected, SchoolFactAuthorizationUnavailable):
            raise
        except Exception as exc:
            raise SchoolFactAuthorizationUnavailable("当前官方联系维护授权不可验证。") from exc
