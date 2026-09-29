from __future__ import annotations

import heapq
import json
import secrets
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from copy import copy
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal, Never, Self, cast
from uuid import UUID, uuid4

from sqlalchemy import (
    Select,
    String,
    Table,
    and_,
    case,
    delete,
    func,
    insert,
    or_,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, SessionTransaction, sessionmaker

from app.composition.school_facts import HmacSchoolFactConfirmationCredentialIssuer
from app.db.rls_context import (
    DatabaseExecutionContextEnvelope,
    purge_expired_school_fact_preparations,
    require_root_reactivation_context,
    require_school_reactivation_context,
    school_fact_school_reactivation_preflight,
    school_fact_school_reactivation_prepare,
    school_reactivation_replay_result,
    set_local_execution_context,
    set_local_root_reactivation_context,
    set_local_school_reactivation_context,
)
from app.domains.students.education_contracts import (
    EDUCATION_KINDS,
    EDUCATION_REFERENCES,
    education_extra_references,
)
from app.domains.students.education_models import (
    EducationFact,
    EducationReference,
    EducationVersion,
)
from app.domains.students.education_versions import EducationSlice, revise_timeline
from app.domains.students.family_rules import (
    CurrentFamilyFactActorValidator,
    FamilyRelationshipState,
    validate_family_transition,
    validate_new_family_request,
)
from app.platform.contracts import CommandExecutionContext, SchoolId, StaffActorContext
from app.platform.directory import models as directory_models
from app.platform.idempotency import models as idempotency_models
from app.platform.identity import models as identity_models
from app.platform.identity.tuple_validator import (
    IdentityTupleValidator,
    SqlAlchemyIdentityTupleValidator,
)
from app.platform.master_data_imports.models import (
    MasterDataImportBatch,
    MasterDataImportUnit,
    SchoolFactChangeLineage,
)
from app.platform.organization import models as organization_models
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import (
    F2_QUALITY_TARGET_KINDS,
    F6_FACILITY_KINDS,
    F7_FAMILY_QUALITY_TARGET_KINDS,
    CoverageBasisV2,
    CanonicalFactChangeSetV1,
    CanonicalFactChangeV1,
    canonical_plan_sha256,
    safe_content_payload,
)
from app.platform.school_facts.f7_coverage_basis import mother_set_digest
from app.platform.school_facts.models import F2_MODELS
from app.platform.school_facts.package_integrity import (
    require_full_scope_provenance_integrity,
)
from app.platform.school_facts.ports import (
    ConfirmationCredentialBinding,
    FactVersionProjection,
    PreparedConfirmation,
    PreparedConfirmationResult,
    ResidenceFactProjection,
    SchoolFactApplyResult,
    SchoolFactChangeProvenance,
    SchoolFactConfirmationCredentialIssuer,
    SchoolFactOwnOriginalResult,
    SchoolFactOwnOriginalSegment,
    SchoolFactOwnResult,
    SchoolFactOwnResultPage,
    SchoolFactOwnResultScope,
    SchoolFactOwnResultTarget,
    SchoolFactOwnTargetPage,
    SchoolFactOwnTerminal,
    SchoolFactResultLookup,
    SchoolFactSchoolReactivationProjection,
    validate_own_original_selection,
)
from app.platform.tenancy import models as tenancy_models

_METADATA_DEPENDENCIES = (
    directory_models,
    idempotency_models,
    identity_models,
    organization_models,
    tenancy_models,
)


class SchoolFactPersistenceError(RuntimeError):
    """固定隐藏 SQL、连接参数和原始 DBAPI 文本的持久化错误。"""


class SchoolFactCommitOutcomeUnknown(SchoolFactPersistenceError):
    """提交应答丢失且新连接无法证明终态。"""


_ROOT_FACTS: dict[str, tuple[str, str]] = {
    "school": ("schools", "source_key"),
    "person": ("school_people", "source_key"),
    "student": ("students", "source_key"),
    "organization": ("organization_units", "source_key"),
    "location": ("campus_locations", "source_key"),
}

_SOURCE_ROOT_TABLES = {
    "school": "schools",
    "person": "school_people",
    "student": "students",
    "login_mapping": "account_person_bindings",
    "organization": "organization_units",
    "assignment": "staff_assignments",
    "location": "campus_locations",
    "dorm_bed": "dorm_beds",
    "policy": "school_policies",
}

_REFERENCE_KINDS = {
    "assignment_source_key": "assignment",
    "official_contact_source_key": "official_contact",
    "person_source_key": "person",
    "organization_source_key": "organization",
    "campus_source_key": "campus",
    "primary_campus_source_key": "campus",
    "administrative_campus_source_key": "campus",
    "location_source_key": "location",
    "room_source_key": "location",
    "student_source_key": "student",
    "bed_source_key": "dorm_bed",
    "contact_person_source_key": "person",
    "guardian_person_source_key": "person",
    "review_of_relationship_source_key": "guardian_relationship",
    "publishing_organization_source_key": "organization",
    "policy_source_key": "policy",
    "supersedes_policy_version_source_key": "policy_version",
}

_REFERENCE_KINDS.update(EDUCATION_REFERENCES)


class SqlAlchemySchoolFactPersistence:
    """以一个 Session/事务写入整批事实、血缘和固定审计。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        envelope_factory: Callable[[CommandExecutionContext], DatabaseExecutionContextEnvelope],
        confirmation_credential_issuer: SchoolFactConfirmationCredentialIssuer | None = None,
        identity_tuple_validator: IdentityTupleValidator | None = None,
        request_context: CommandExecutionContext | None = None,
        final_authorization_guard: Callable[
            [Session, CommandExecutionContext, CanonicalFactChangeSetV1], None
        ]
        | None = None,
        family_actor_validator: CurrentFamilyFactActorValidator | None = None,
        package_final_authorization_guard: Callable[
            [Session, CommandExecutionContext, CanonicalFactChangeSetV1], None
        ] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._envelope_factory = envelope_factory
        self._confirmation_credential_issuer = (
            confirmation_credential_issuer
            or HmacSchoolFactConfirmationCredentialIssuer(secrets.token_bytes(32))
        )
        self._identity_tuple_validator = (
            identity_tuple_validator or SqlAlchemyIdentityTupleValidator()
        )
        self._request_context = request_context
        self._final_authorization_guard = final_authorization_guard
        self._package_final_authorization_guard = package_final_authorization_guard
        self._family_actor_validator = family_actor_validator

    def for_request(
        self,
        context: CommandExecutionContext,
    ) -> Self:
        """Create an immutable request-scoped view without sharing mutable context."""

        bound = copy(self)
        bound._request_context = context
        return bound

    def _authorize_confirmed_batch(self, session, context, change_set, batch) -> None:
        """Source is from the locked confirmation row after full digest/owner validation.

        Input schema and user-supplied row origins are never permission selectors.
        Old confirmations keep their original owner dispatch and cannot opt into F7.
        """
        if batch.source_kind == "package":
            guard = self._package_final_authorization_guard
            if guard is None:
                raise SchoolFactChangeRejected("全范围导入的最终授权尚未接通。")
        else:
            guard = self._final_authorization_guard
        if guard is not None:
            guard(session, context, change_set)

    @contextmanager
    def _request_bound_session(self) -> Generator[Session]:
        with self._session_factory() as session:
            with session.begin():
                self._set_local_request_context(session)
                yield session

    def _set_local_request_context(self, session: Session) -> None:
        if self._request_context is not None:
            set_local_execution_context(
                session,
                self._envelope_factory(self._request_context),
            )

    def preflight_school_reactivation(
        self,
        school_id: SchoolId,
        change_set: CanonicalFactChangeSetV1,
        plan_sha256: str,
    ) -> SchoolFactSchoolReactivationProjection:
        if not self._is_single_school_reactivation(change_set):
            raise SchoolFactChangeRejected("窄预检只接受单一学校重新启用计划。")
        if self._request_context is None:
            raise SchoolFactChangeRejected("学校重新启用预检缺少可信命令上下文。")
        change = change_set.changes[0]
        session = self._session_factory()
        try:
            with session.begin():
                projection = school_fact_school_reactivation_preflight(
                    session,
                    self._envelope_factory(self._request_context),
                    fact_source_key=change.source_key,
                    expected_version=change.expected_version,
                    effective_at=change.effective_at,
                    base_fact_version=change_set.base_version,
                    source_sha256=change_set.source_sha256,
                    plan_sha256=plan_sha256,
                )
            replay_result = (
                SchoolFactApplyResult(
                    batch_id=projection.replay_batch_id,
                    target_version=cast(int, projection.replay_target_version),
                    changed_count=cast(int, projection.replay_changed_count),
                    replayed=True,
                )
                if projection.replay_batch_id is not None
                else None
            )
            return SchoolFactSchoolReactivationProjection(
                replay_result=replay_result,
                current_fact_version=projection.current_fact_version,
                current_target_version=projection.current_target_version,
            )
        except DBAPIError as exc:
            self._raise_db_error(exc)
        finally:
            session.close()

    def current_school_fact_version(self, school_id: SchoolId) -> int:
        with self._request_bound_session() as session:
            school_exists = session.execute(
                text("SELECT 1 FROM schools WHERE id = :school_id"),
                {"school_id": school_id},
            ).scalar_one_or_none()
            if school_exists is None:
                raise SchoolFactChangeRejected("学校事实环境不存在。")
            value = self._current_global_fact_version(session, UUID(str(school_id)))
        if value < 1:
            raise SchoolFactChangeRejected("学校事实环境不存在。")
        return value

    @staticmethod
    def _current_global_fact_version(session: Session, school_id: UUID) -> int:
        value = session.execute(
            text(
                "SELECT COALESCE(MAX(target_fact_version), 1) "
                "FROM master_data_import_batches "
                "WHERE school_id = :school_id "
                "AND mode = 'canonical_atomic_changes' AND status = 'applied'"
            ),
            {"school_id": school_id},
        ).scalar_one()
        return int(value)

    def find_fact_version(
        self,
        school_id: SchoolId,
        fact_kind: str,
        source_key: str,
    ) -> FactVersionProjection | None:
        with self._request_bound_session() as session:
            root = _ROOT_FACTS.get(fact_kind)
            if root is not None:
                table_name, key_column = root
                school_filter = (
                    "id = :school_id" if fact_kind == "school" else "school_id = :school_id"
                )
                value = session.execute(
                    text(
                        f"SELECT version FROM {table_name} "  # noqa: S608 - fixed allowlist
                        f"WHERE {school_filter} AND {key_column} = :source_key"
                    ),
                    {"school_id": school_id, "source_key": source_key},
                ).scalar_one_or_none()
            else:
                value = session.execute(
                    text(
                        "SELECT max(target_version) FROM school_fact_change_lineage "
                        "WHERE school_id = :school_id AND fact_kind = :fact_kind "
                        "AND fact_source_key = :source_key"
                    ),
                    {
                        "school_id": school_id,
                        "fact_kind": fact_kind,
                        "source_key": source_key,
                    },
                ).scalar_one()
        return None if value is None else FactVersionProjection(version=int(value))

    def find_fact_versions_bulk(
        self,
        school_id: SchoolId,
        targets: tuple[tuple[str, str], ...],
    ) -> dict[tuple[str, str], FactVersionProjection]:
        """在一个 Session 内按事实类别批量读取版本，避免大批量逐项建连接。"""

        keys_by_kind: dict[str, set[str]] = {}
        for fact_kind, source_key in targets:
            keys_by_kind.setdefault(fact_kind, set()).add(source_key)
        projections: dict[tuple[str, str], FactVersionProjection] = {}
        if not keys_by_kind:
            return projections
        with self._request_bound_session() as session:
            for fact_kind, source_keys in keys_by_kind.items():
                ordered_keys = sorted(source_keys)
                root = _ROOT_FACTS.get(fact_kind)
                if root is not None:
                    table_name, key_column = root
                    school_filter = (
                        "id = :school_id" if fact_kind == "school" else "school_id = :school_id"
                    )
                    rows = session.execute(
                        text(
                            f"SELECT {key_column}, version FROM {table_name} "  # noqa: S608
                            f"WHERE {school_filter} AND {key_column} = ANY(:source_keys)"
                        ),
                        {"school_id": school_id, "source_keys": ordered_keys},
                    )
                    for source_key, version in rows:
                        projections[(fact_kind, str(source_key))] = FactVersionProjection(
                            version=int(version)
                        )
                    continue
                rows = session.execute(
                    text(
                        "SELECT fact_source_key, max(target_version) "
                        "FROM school_fact_change_lineage "
                        "WHERE school_id = :school_id AND fact_kind = :fact_kind "
                        "AND fact_source_key = ANY(:source_keys) GROUP BY fact_source_key"
                    ),
                    {
                        "school_id": school_id,
                        "fact_kind": fact_kind,
                        "source_keys": ordered_keys,
                    },
                )
                for source_key, version in rows:
                    projections[(fact_kind, str(source_key))] = FactVersionProjection(
                        version=int(version)
                    )
        return projections

    def find_residence_student_source_keys_bulk(
        self,
        school_id: SchoolId,
        residence_source_keys: tuple[str, ...],
    ) -> dict[str, str]:
        """按稳定入住键批量读取学生稳定键，只供调宿配对预检。"""

        ordered_keys = sorted(set(residence_source_keys))
        if not ordered_keys:
            return {}
        with self._request_bound_session() as session:
            return self._residence_student_source_keys_in_session(
                session,
                school_id=school_id,
                residence_source_keys=ordered_keys,
            )

    @staticmethod
    def _residence_student_source_keys_in_session(
        session: Session,
        *,
        school_id: SchoolId | UUID,
        residence_source_keys: list[str] | tuple[str, ...],
    ) -> dict[str, str]:
        rows = session.execute(
            text(
                "SELECT latest.fact_source_key, student.source_key "
                "FROM ("
                "SELECT DISTINCT ON (lineage.fact_source_key) "
                "lineage.fact_source_key, lineage.target_id "
                "FROM school_fact_change_lineage AS lineage "
                "WHERE lineage.school_id = :school_id "
                "AND lineage.fact_kind = 'student_bed_residence' "
                "AND lineage.fact_source_key = ANY(:source_keys) "
                "ORDER BY lineage.fact_source_key, lineage.target_version DESC"
                ") AS latest "
                "JOIN student_bed_residences AS residence "
                "ON residence.school_id = :school_id "
                "AND residence.id = latest.target_id "
                "JOIN students AS student ON student.school_id = :school_id "
                "AND student.id = residence.student_id"
            ),
            {"school_id": school_id, "source_keys": list(residence_source_keys)},
        )
        return {str(residence_key): str(student_key) for residence_key, student_key in rows}

    def find_residence_facts_bulk(
        self,
        school_id: SchoolId,
        residence_source_keys: tuple[str, ...],
    ) -> dict[str, ResidenceFactProjection]:
        """按入住稳定键读取学生与床位，供无 edge 的转宿语义配对。"""

        ordered_keys = sorted(set(residence_source_keys))
        if not ordered_keys:
            return {}
        with self._request_bound_session() as session:
            rows = session.execute(
                text(
                    "SELECT latest.fact_source_key, student.source_key, bed.source_key "
                    "FROM (SELECT DISTINCT ON (lineage.fact_source_key) "
                    "lineage.fact_source_key, lineage.target_id "
                    "FROM school_fact_change_lineage AS lineage "
                    "WHERE lineage.school_id = :school_id "
                    "AND lineage.fact_kind = 'student_bed_residence' "
                    "AND lineage.fact_source_key = ANY(:source_keys) "
                    "ORDER BY lineage.fact_source_key, lineage.target_version DESC) AS latest "
                    "JOIN student_bed_residences AS residence "
                    "ON residence.school_id = :school_id AND residence.id = latest.target_id "
                    "JOIN students AS student ON student.school_id = residence.school_id "
                    "AND student.id = residence.student_id "
                    "JOIN dorm_beds AS bed ON bed.school_id = residence.school_id "
                    "AND bed.id = residence.bed_id"
                ),
                {"school_id": school_id, "source_keys": ordered_keys},
            )
            return {
                str(residence_key): ResidenceFactProjection(
                    student_source_key=str(student_key),
                    bed_source_key=str(bed_source_key),
                )
                for residence_key, student_key, bed_source_key in rows
            }

    def find_organization_campus_ids(
        self,
        school_id: SchoolId,
        organization_source_key: str,
        effective_at: datetime,
    ) -> tuple[UUID, ...]:
        """返回显式有效关系；不存在关系时绝不扩展成全校校区。"""

        with self._request_bound_session() as session:
            rows = session.execute(
                text(
                    "SELECT campus.id FROM organization_campus_relationships AS relation "
                    "JOIN organization_units AS organization "
                    "ON organization.school_id = relation.school_id "
                    "AND organization.id = relation.organization_unit_id "
                    "JOIN organization_unit_versions AS organization_version "
                    "ON organization_version.school_id = organization.school_id "
                    "AND organization_version.organization_unit_id = organization.id "
                    "AND organization_version.status = 'confirmed' "
                    "AND organization_version.valid_from <= :effective_at "
                    "AND (organization_version.valid_to IS NULL "
                    "OR organization_version.valid_to > :effective_at) "
                    "JOIN campus_locations AS campus ON campus.school_id = relation.school_id "
                    "AND campus.id = relation.campus_location_id "
                    "JOIN campus_location_versions AS campus_version "
                    "ON campus_version.school_id = campus.school_id "
                    "AND campus_version.location_id = campus.id "
                    "AND campus_version.status = 'confirmed' "
                    "AND campus_version.valid_from <= :effective_at "
                    "AND (campus_version.valid_to IS NULL "
                    "OR campus_version.valid_to > :effective_at) "
                    "WHERE relation.school_id = :school_id "
                    "AND organization.source_key = :organization_source_key "
                    "AND relation.valid_from <= :effective_at "
                    "AND (relation.valid_to IS NULL OR relation.valid_to > :effective_at) "
                    "AND NOT EXISTS (SELECT 1 FROM "
                    "app.school_fact_content_version_terminal("
                    "relation.school_id, 'organization_campus_relationship', "
                    "relation.id, relation.version) AS terminal "
                    "WHERE terminal.operation = 'revoke' "
                    "OR terminal.effective_at IS NULL "
                    "OR terminal.effective_at <= :effective_at) "
                    "AND NOT EXISTS (SELECT 1 FROM "
                    "app.school_fact_content_version_terminal("
                    "organization.school_id, 'organization', organization.id, "
                    "organization_version.version) AS terminal "
                    "WHERE terminal.operation = 'revoke' "
                    "OR terminal.effective_at IS NULL "
                    "OR terminal.effective_at <= :effective_at) "
                    "AND NOT EXISTS (SELECT 1 FROM "
                    "app.school_fact_content_version_terminal("
                    "campus.school_id, 'location', campus.id, campus_version.version) AS terminal "
                    "WHERE terminal.operation = 'revoke' "
                    "OR terminal.effective_at IS NULL "
                    "OR terminal.effective_at <= :effective_at) "
                    "AND campus_version.kind = 'campus' "
                    "ORDER BY campus.id"
                ),
                {
                    "school_id": school_id,
                    "organization_source_key": organization_source_key,
                    "effective_at": effective_at,
                },
            ).scalars()
            return tuple(UUID(str(campus_id)) for campus_id in rows)

    def unresolved_reference_codes(
        self,
        school_id: SchoolId,
        change: CanonicalFactChangeV1,
    ) -> tuple[str, ...]:
        if change.schema_version in {3, 4}:
            return self.unresolved_reference_codes_bulk(school_id, (change,)).get(
                (change.fact_kind, change.source_key), ()
            )
        content = safe_content_payload(change.content) or {}
        if not any(isinstance(content.get(field_code), str) for field_code in _REFERENCE_KINDS):
            parent_source_key = content.get("parent_source_key")
            if not isinstance(parent_source_key, str):
                return ()
        reference_kinds = dict(_REFERENCE_KINDS)
        reference_kinds["parent_source_key"] = (
            "organization" if change.fact_kind == "organization" else "location"
        )
        missing: list[str] = []
        with self._request_bound_session() as session:
            for field_code, reference_kind in reference_kinds.items():
                source_key = content.get(field_code)
                if not isinstance(source_key, str):
                    continue
                existing = self._current_reference_source_keys(
                    session,
                    school_id=school_id,
                    reference_kind=reference_kind,
                    source_keys=(source_key,),
                    effective_at=change.effective_at,
                )
                if source_key not in existing:
                    if self._bounded_historical_residence_bed_exists(
                        session,
                        school_id=school_id,
                        change=change,
                        field_code=field_code,
                        reference_kind=reference_kind,
                        source_key=source_key,
                    ):
                        continue
                    missing.append(field_code)
        return tuple(sorted(missing))

    @staticmethod
    def _bounded_historical_residence_bed_exists(
        session: Session,
        *,
        school_id: SchoolId,
        change: CanonicalFactChangeV1,
        field_code: str,
        reference_kind: str,
        source_key: str,
    ) -> bool:
        """已结束的历史入住只要求稳定床位存在，不套用今天的祖先 current 状态。"""

        if (
            change.fact_kind != "student_bed_residence"
            or change.operation != "upsert"
            or field_code != "bed_source_key"
            or reference_kind != "dorm_bed"
            or change.content is None
            or change.content.valid_to is None
        ):
            return False
        return bool(
            session.execute(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM dorm_beds AS bed "
                    "WHERE bed.school_id = :school_id "
                    "AND bed.source_key = :source_key "
                    "AND app.school_fact_location_history_interval_is_resolvable("
                    "bed.school_id, bed.room_location_id, :valid_from, :valid_to)) "
                    "AND :valid_to <= statement_timestamp()"
                ),
                {
                    "school_id": school_id,
                    "source_key": source_key,
                    "valid_from": change.effective_at,
                    "valid_to": change.content.valid_to,
                },
            ).scalar_one()
        )

    @staticmethod
    def _current_reference_source_keys(
        session: Session,
        *,
        school_id: SchoolId,
        reference_kind: str,
        source_keys: tuple[str, ...] | list[str],
        effective_at: datetime,
    ) -> set[str]:
        """只返回当前可用引用；地点可用性包含整条祖先链。"""

        ordered_keys = sorted(source_keys)
        if reference_kind in EDUCATION_KINDS:
            return set(
                session.execute(
                    select(EducationFact.source_key)
                    .join(
                        EducationVersion,
                        and_(
                            EducationVersion.school_id == EducationFact.school_id,
                            EducationVersion.fact_id == EducationFact.id,
                        ),
                    )
                    .where(
                        EducationFact.school_id == school_id,
                        EducationFact.kind == reference_kind,
                        EducationFact.source_key.in_(ordered_keys),
                        EducationVersion.superseded_at.is_(None),
                        EducationVersion.valid_from <= effective_at,
                        (
                            EducationVersion.valid_to.is_(None)
                            | (EducationVersion.valid_to > effective_at)
                        ),
                    )
                ).scalars()
            )
        if reference_kind in {"location", "campus"}:
            kind_filter = " AND version.kind = 'campus'" if reference_kind == "campus" else ""
            rows = session.execute(
                text(
                    "SELECT stable.source_key FROM campus_locations AS stable "
                    "JOIN campus_location_versions AS version "
                    "ON version.school_id = stable.school_id "
                    "AND version.location_id = stable.id "
                    "WHERE stable.school_id = :school_id "
                    "AND stable.source_key = ANY(:source_keys) "
                    "AND version.status = 'confirmed' "
                    "AND version.valid_from <= :effective_at "
                    "AND (version.valid_to IS NULL OR version.valid_to > :effective_at) "
                    f"{kind_filter} "  # noqa: S608 - fixed fragment
                    "AND app.school_fact_location_history_is_resolvable("
                    "stable.school_id, stable.id, :effective_at)"
                ),
                {
                    "school_id": school_id,
                    "source_keys": ordered_keys,
                    "effective_at": effective_at,
                },
            ).scalars()
            return {str(source_key) for source_key in rows}
        if reference_kind == "dorm_bed":
            rows = session.execute(
                text(
                    "SELECT bed.source_key FROM dorm_beds AS bed "
                    "JOIN campus_location_versions AS room "
                    "ON room.school_id = bed.school_id "
                    "AND room.location_id = bed.room_location_id "
                    "WHERE bed.school_id = :school_id "
                    "AND bed.source_key = ANY(:source_keys) "
                    "AND room.status = 'confirmed' "
                    "AND room.valid_from <= :effective_at "
                    "AND (room.valid_to IS NULL OR room.valid_to > :effective_at) "
                    "AND app.school_fact_location_history_is_resolvable("
                    "bed.school_id, bed.room_location_id, :effective_at)"
                ),
                {
                    "school_id": school_id,
                    "source_keys": ordered_keys,
                    "effective_at": effective_at,
                },
            ).scalars()
            return {str(source_key) for source_key in rows}
        table_name = _SOURCE_ROOT_TABLES.get(reference_kind)
        if table_name is not None:
            school_filter = (
                "id = :school_id" if reference_kind == "school" else "school_id = :school_id"
            )
            temporal_filter = (
                " AND app.school_fact_root_is_effective_at("
                ":school_id, :reference_kind, id, :effective_at)"
                if reference_kind in _ROOT_FACTS
                else ""
            )
            rows = session.execute(
                text(
                    f"SELECT source_key FROM {table_name} "  # noqa: S608 - fixed allowlist
                    f"WHERE {school_filter} AND source_key = ANY(:source_keys)"  # noqa: S608
                    f"{temporal_filter}"  # noqa: S608 - fixed fragments
                ),
                {
                    "school_id": school_id,
                    "source_keys": ordered_keys,
                    "reference_kind": reference_kind,
                    "effective_at": effective_at,
                },
            ).scalars()
        else:
            rows = session.execute(
                text(
                    "SELECT DISTINCT fact_source_key FROM school_fact_change_lineage "
                    "WHERE school_id = :school_id AND fact_kind = :fact_kind "
                    "AND fact_source_key = ANY(:source_keys)"
                ),
                {
                    "school_id": school_id,
                    "fact_kind": reference_kind,
                    "source_keys": ordered_keys,
                },
            ).scalars()
        return {str(source_key) for source_key in rows}

    def unresolved_reference_codes_bulk(
        self,
        school_id: SchoolId,
        changes: tuple[CanonicalFactChangeV1, ...],
    ) -> dict[tuple[str, str], tuple[str, ...]]:
        """批量读取所有外部引用；计划内引用仍由应用层按同一规则豁免。"""

        if any(change.replacement_segments is not None for change in changes):
            expanded = []
            identities = {}
            for change in changes:
                if change.replacement_segments is None:
                    expanded.append(change)
                    identities[change.fact_kind, change.source_key] = (
                        change.fact_kind,
                        change.source_key,
                        "",
                    )
                    continue
                for index, segment in enumerate(change.replacement_segments):
                    key = "f4-preflight-segment:" + str(len(expanded))
                    expanded.append(
                        change.model_copy(
                            update={
                                "source_key": key,
                                "replacement_segments": None,
                                "content": segment.content,
                                "effective_at": segment.effective_at,
                            }
                        )
                    )
                    identities[change.fact_kind, key] = (
                        change.fact_kind,
                        change.source_key,
                        f"replacement_segments.{index}.",
                    )
            found = self.unresolved_reference_codes_bulk(school_id, tuple(expanded))
            result = {}
            for target, codes in found.items():
                kind, key, prefix = identities[target]
                result[kind, key] = result.get((kind, key), ()) + tuple(
                    prefix + code for code in codes
                )
            return result

        references_by_target: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
        effective_at_by_target: dict[tuple[str, str], datetime] = {}
        source_keys_by_time_and_kind: dict[tuple[datetime, str], set[str]] = {}
        for change in changes:
            content = safe_content_payload(change.content) or {}
            references: list[tuple[str, str, str]] = []
            for field_code, reference_kind in _REFERENCE_KINDS.items():
                value = content.get(field_code)
                if isinstance(value, str):
                    references.append((field_code, reference_kind, value))
                    source_keys_by_time_and_kind.setdefault(
                        (change.effective_at, reference_kind), set()
                    ).add(value)
            for field_code, reference_kind, value in education_extra_references(
                change.fact_kind, change.content
            ):
                references.append((field_code, reference_kind, value))
                source_keys_by_time_and_kind.setdefault(
                    (change.effective_at, reference_kind), set()
                ).add(value)
            parent_source_key = content.get("parent_source_key")
            if isinstance(parent_source_key, str):
                parent_kind = "organization" if change.fact_kind == "organization" else "location"
                references.append(("parent_source_key", parent_kind, parent_source_key))
                source_keys_by_time_and_kind.setdefault(
                    (change.effective_at, parent_kind), set()
                ).add(parent_source_key)
            if references:
                target = (change.fact_kind, change.source_key)
                references_by_target[target] = references
                effective_at_by_target[target] = change.effective_at
        if not references_by_target:
            return {}

        existing: set[tuple[datetime, str, str]] = set()
        bounded_historical_beds: set[tuple[str, str, str]] = set()
        with self._request_bound_session() as session:
            for (effective_at, reference_kind), source_keys in source_keys_by_time_and_kind.items():
                current_keys = self._current_reference_source_keys(
                    session,
                    school_id=school_id,
                    reference_kind=reference_kind,
                    source_keys=sorted(source_keys),
                    effective_at=effective_at,
                )
                existing.update(
                    (effective_at, reference_kind, source_key) for source_key in current_keys
                )
            for change in changes:
                content = safe_content_payload(change.content) or {}
                bed_source_key = content.get("bed_source_key")
                if not isinstance(bed_source_key, str):
                    continue
                if self._bounded_historical_residence_bed_exists(
                    session,
                    school_id=school_id,
                    change=change,
                    field_code="bed_source_key",
                    reference_kind="dorm_bed",
                    source_key=bed_source_key,
                ):
                    bounded_historical_beds.add(
                        (change.fact_kind, change.source_key, bed_source_key)
                    )

        return {
            target: tuple(
                sorted(
                    field_code
                    for field_code, reference_kind, source_key in references
                    if (
                        effective_at_by_target[target],
                        reference_kind,
                        source_key,
                    )
                    not in existing
                    and (target[0], target[1], source_key) not in bounded_historical_beds
                )
            )
            for target, references in references_by_target.items()
        }

    def find_applied_plan(
        self,
        school_id: SchoolId,
        source_sha256: str,
        plan_sha256: str,
    ) -> SchoolFactApplyResult | None:
        with self._request_bound_session() as session:
            batch = session.execute(
                self._applied_plan_query(school_id, source_sha256, plan_sha256)
            ).scalar_one_or_none()
            return self._result_from_batch(batch, replayed=True) if batch is not None else None

    def find_own_result_targets(
        self,
        school_id: SchoolId,
        actor_user_id: int,
        actor_person_id: UUID,
        locator_sha256: str,
    ) -> SchoolFactOwnResultScope | None:
        context = self._request_context
        if (
            type(context) is not CommandExecutionContext
            or type(context.actor) is not StaffActorContext
            or (context.actor.school_id, context.actor.account_user_id, context.actor.person_id)
            != (school_id, actor_user_id, actor_person_id)
        ):
            raise SchoolFactChangeRejected("本人回执必须使用当前可信操作者上下文。")
        if (
            type(locator_sha256) is not str
            or len(locator_sha256) != 64
            or any(c not in "0123456789abcdef" for c in locator_sha256)
        ):
            raise SchoolFactChangeRejected("本人回执定位摘要无效。")
        batch, lineage = MasterDataImportBatch, SchoolFactChangeLineage
        target_predicate = and_(lineage.school_id == batch.school_id, lineage.batch_id == batch.id)
        target_count = (
            select(func.count(lineage.id))
            .where(target_predicate)
            .correlate(batch)
            .scalar_subquery()
        )
        # Locate only the authenticated owner's one completed atomic receipt.
        # Result JSON participates in integrity predicates, never the SELECT projection.
        statement = (
            select(batch.id, lineage.fact_kind, lineage.fact_source_key, batch.succeeded_units)
            .join(lineage, target_predicate)
            .where(
                batch.school_id == school_id,
                batch.mode == "canonical_atomic_changes",
                batch.staged_by_user_id == actor_user_id,
                batch.staged_by_person_id == actor_person_id,
                batch.confirmed_by_user_id == actor_user_id,
                batch.confirmed_by_person_id == actor_person_id,
                batch.status == "applied",
                batch.confirmed_at.is_not(None),
                batch.mapping_payload["result_locator_sha256"].astext == locator_sha256,
                batch.result_payload.is_not(None),
                batch.target_fact_version > 0,
                sql_cast(batch.result_payload["target_version"], String)
                == sql_cast(batch.target_fact_version, String),
                sql_cast(batch.result_payload["changed_count"], String)
                == sql_cast(batch.succeeded_units, String),
                batch.succeeded_units == batch.planned_units,
                batch.succeeded_units > 0,
                batch.succeeded_units <= 4096,
                batch.failed_units == 0,
                batch.pending_units == 0,
                target_count == batch.succeeded_units,
            )
            .limit(4097)
        )
        with self._request_bound_session() as session:
            rows = session.execute(statement).all()
        if not rows:
            return None
        coordinates = frozenset((row.fact_kind, row.fact_source_key) for row in rows)
        if (
            len({row.id for row in rows}) != 1
            or len(rows) != rows[0].succeeded_units
            or len(coordinates) != len(rows)
            or any(
                type(v) is not str or not v or len(v) > 255 for pair in coordinates for v in pair
            )
        ):
            raise SchoolFactPersistenceError("本人回执的正式对象坐标不完整。")
        return coordinates

    def find_own_terminal_by_locator(
        self,
        school_id: SchoolId,
        actor_user_id: int,
        actor_person_id: UUID,
        locator_sha256: str,
    ) -> SchoolFactOwnTerminal | None:
        """Check a complete committed batch without selecting or disclosing target keys."""

        context = self._request_context
        if (
            type(context) is not CommandExecutionContext
            or type(context.actor) is not StaffActorContext
            or (context.actor.school_id, context.actor.account_user_id, context.actor.person_id)
            != (school_id, actor_user_id, actor_person_id)
        ):
            raise SchoolFactChangeRejected("本人回执必须使用当前可信操作者上下文。")
        if (
            type(locator_sha256) is not str
            or len(locator_sha256) != 64
            or any(c not in "0123456789abcdef" for c in locator_sha256)
        ):
            raise SchoolFactChangeRejected("本人回执定位摘要无效。")
        batch, lineage = MasterDataImportBatch, SchoolFactChangeLineage
        target_count = (
            select(func.count(lineage.id))
            .where(lineage.school_id == batch.school_id, lineage.batch_id == batch.id)
            .correlate(batch)
            .scalar_subquery()
        )
        statement = (
            select(
                batch.id,
                batch.confirmed_at,
                batch.result_payload,
                batch.target_fact_version,
                batch.succeeded_units,
                target_count.label("target_count"),
            )
            .where(
                batch.school_id == school_id,
                batch.mode == "canonical_atomic_changes",
                batch.staged_by_user_id == actor_user_id,
                batch.staged_by_person_id == actor_person_id,
                batch.confirmed_by_user_id == actor_user_id,
                batch.confirmed_by_person_id == actor_person_id,
                batch.status == "applied",
                batch.confirmed_at.is_not(None),
                batch.mapping_payload["result_locator_sha256"].astext == locator_sha256,
                batch.result_payload.is_not(None),
                batch.target_fact_version > 0,
                batch.succeeded_units == batch.planned_units,
                batch.succeeded_units.between(1, 50_000),
                batch.failed_units == 0,
                batch.pending_units == 0,
                target_count == batch.succeeded_units,
            )
            .limit(2)
        )
        with self._request_bound_session() as session:
            rows = session.execute(statement).mappings().all()
            if not rows:
                return None
            if len(rows) != 1:
                raise SchoolFactPersistenceError("本人回执定位不唯一。")
            row = rows[0]
            duplicate = session.execute(
                select(lineage.fact_kind)
                .where(lineage.school_id == school_id, lineage.batch_id == row["id"])
                .group_by(lineage.fact_kind, lineage.fact_source_key)
                .having(func.count(lineage.id) > 1)
                .limit(1)
            ).first()
            if duplicate is not None:
                raise SchoolFactPersistenceError("本人回执对象坐标重复。")
        payload = row["result_payload"]
        if payload is None:
            return None
        if (
            type(payload) is not dict
            or type(payload.get("target_version")) is not int
            or type(payload.get("changed_count")) is not int
            or payload["target_version"] != row["target_fact_version"]
            or payload["changed_count"] != row["succeeded_units"]
            or row["target_count"] != row["succeeded_units"]
        ):
            raise SchoolFactPersistenceError("本人回执的正式结果不完整。")
        confirmed = row["confirmed_at"]
        if confirmed.tzinfo is None:
            confirmed = confirmed.replace(tzinfo=UTC)
        return SchoolFactOwnTerminal(
            locator_sha256=locator_sha256,
            confirmed_at=confirmed,
            result=SchoolFactApplyResult(
                row["id"], row["target_fact_version"], row["succeeded_units"], True
            ),
            target_count=row["target_count"],
        )

    def read_own_target_page(
        self,
        school_id: SchoolId,
        actor_user_id: int,
        actor_person_id: UUID,
        locator_sha256: str,
        offset: int,
        limit: int,
    ) -> SchoolFactOwnTargetPage | None:
        """Return at most one bounded page; Writer must authorize before release."""

        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise SchoolFactChangeRejected("本人回执明细分页边界无效。")
        terminal = self.find_own_terminal_by_locator(
            school_id, actor_user_id, actor_person_id, locator_sha256
        )
        if terminal is None:
            return None
        if offset > terminal.target_count:
            raise SchoolFactChangeRejected("本人回执明细分页越过原操作范围。")
        lineage = SchoolFactChangeLineage
        with self._request_bound_session() as session:
            rows = session.execute(
                select(
                    lineage.fact_kind,
                    lineage.fact_source_key,
                    lineage.operation,
                    lineage.target_version,
                )
                .where(lineage.school_id == school_id, lineage.batch_id == terminal.result.batch_id)
                .order_by(lineage.fact_kind, lineage.fact_source_key)
                .offset(offset)
                .limit(limit + 1)
            ).all()
        page_rows = rows[:limit]
        if len(page_rows) != min(limit, max(terminal.target_count - offset, 0)):
            raise SchoolFactPersistenceError("本人回执明细分页不完整。")
        if any(
            type(row.fact_kind) is not str
            or not row.fact_kind
            or len(row.fact_kind) > 64
            or type(row.fact_source_key) is not str
            or not row.fact_source_key
            or len(row.fact_source_key) > 255
            or row.operation not in {"upsert", "end", "revoke", "reactivate", "correct", "cancel"}
            or type(row.target_version) is not int
            or row.target_version < 1
            for row in page_rows
        ):
            raise SchoolFactPersistenceError("本人回执明细不完整。")
        return SchoolFactOwnTargetPage(
            terminal=terminal,
            items=tuple(
                SchoolFactOwnResultTarget(
                    row.fact_kind, row.fact_source_key, row.operation, row.target_version
                )
                for row in page_rows
            ),
            offset=offset,
            next_offset=offset + len(page_rows),
            has_more=offset + len(page_rows) < terminal.target_count,
        )

    def list_own_results(
        self,
        school_id: SchoolId,
        actor_user_id: int,
        actor_person_id: UUID,
        allowed_targets: SchoolFactOwnResultScope,
        limit: int,
        *,
        creation_only_targets: SchoolFactOwnResultScope = frozenset(),
    ) -> SchoolFactOwnResultPage:
        items = self._read_own_results(
            school_id,
            actor_user_id,
            actor_person_id,
            allowed_targets,
            limit + 1,
            creation_only_targets=creation_only_targets,
        )
        return SchoolFactOwnResultPage(items=items[:limit], has_more=len(items) > limit)

    def find_own_result_by_locator(
        self,
        school_id: SchoolId,
        actor_user_id: int,
        actor_person_id: UUID,
        locator_sha256: str,
        allowed_targets: SchoolFactOwnResultScope,
        *,
        creation_only_targets: SchoolFactOwnResultScope = frozenset(),
    ) -> SchoolFactOwnResult | None:
        items = self._read_own_results(
            school_id,
            actor_user_id,
            actor_person_id,
            allowed_targets,
            1,
            locator_sha256,
            creation_only_targets=creation_only_targets,
        )
        return items[0] if items else None

    def _read_own_results(
        self,
        school_id: SchoolId,
        actor_user_id: int,
        actor_person_id: UUID,
        allowed_targets: SchoolFactOwnResultScope,
        limit: int,
        locator_sha256: str | None = None,
        *,
        creation_only_targets: SchoolFactOwnResultScope = frozenset(),
    ) -> tuple[SchoolFactOwnResult, ...]:
        context = self._request_context
        if (
            type(context) is not CommandExecutionContext
            or type(context.actor) is not StaffActorContext
            or (context.actor.school_id, context.actor.account_user_id, context.actor.person_id)
            != (school_id, actor_user_id, actor_person_id)
        ):
            raise SchoolFactChangeRejected("本人回执必须使用当前可信操作者上下文。")
        if not allowed_targets:
            return ()
        batch, lineage = MasterDataImportBatch, SchoolFactChangeLineage
        target_predicate = and_(lineage.school_id == batch.school_id, lineage.batch_id == batch.id)
        targets = select(lineage.id).where(target_predicate)
        target_count = select(func.count(lineage.id)).where(target_predicate).scalar_subquery()
        locator = batch.mapping_payload["result_locator_sha256"].astext
        statement = select(
            batch.id,
            locator.label("locator"),
            batch.confirmed_at,
            batch.result_payload,
            batch.target_fact_version,
            batch.succeeded_units,
        ).where(
            batch.school_id == school_id,
            batch.mode == "canonical_atomic_changes",
            batch.staged_by_user_id == actor_user_id,
            batch.staged_by_person_id == actor_person_id,
            batch.confirmed_by_user_id == actor_user_id,
            batch.confirmed_by_person_id == actor_person_id,
            batch.status == "applied",
            batch.confirmed_at.is_not(None),
            batch.result_payload.is_not(None),
            # Non-product provenance may legitimately have no recovery locator.
            # Non-null malformed locators still fail the integrity checks below.
            locator.is_not(None),
            batch.result_payload["target_version"].astext.is_not(None),
            batch.result_payload["changed_count"].astext.is_not(None),
            batch.succeeded_units == batch.planned_units,
            batch.failed_units == 0,
            batch.pending_units == 0,
            targets.exists(),
            target_count == batch.succeeded_units,
            ~targets.where(
                tuple_(lineage.fact_kind, lineage.fact_source_key).not_in(sorted(allowed_targets))
            ).exists(),
        )
        if creation_only_targets:
            if not creation_only_targets <= allowed_targets:
                raise SchoolFactChangeRejected("Creation receipt scope mismatch.")
            unit = MasterDataImportUnit
            # Only persisted successful creation commands qualify. No current fact body is read.
            creation_count = (
                select(func.count(unit.id))
                .where(
                    unit.school_id == lineage.school_id,
                    unit.batch_id == lineage.batch_id,
                    unit.status == "applied",
                    unit.payload["fact_kind"].astext == lineage.fact_kind,
                    unit.payload["source_key"].astext == lineage.fact_source_key,
                    unit.payload["operation"].astext == "upsert",
                    sql_cast(unit.payload["expected_version"], String) == "0",
                    func.jsonb_typeof(func.jsonb_extract_path(unit.payload, "expected_version"))
                    == "number",
                )
                .correlate(lineage)
                .scalar_subquery()
            )
            invalid_creation = targets.where(
                tuple_(lineage.fact_kind, lineage.fact_source_key).in_(
                    sorted(creation_only_targets)
                ),
                or_(
                    lineage.operation != "upsert", lineage.target_version != 1, creation_count != 1
                ),
            )
            statement = statement.where(~invalid_creation.exists())
        if locator_sha256 is not None:
            statement = statement.where(locator == locator_sha256)
        # Ownership, complete target scope and completeness all precede LIMIT and
        # result payload projection; an inaccessible newer row cannot hide a permitted row.
        statement = statement.order_by(batch.confirmed_at.desc(), batch.id.desc()).limit(limit)
        with self._request_bound_session() as session:
            rows = session.execute(statement).mappings().all()
            if not rows:
                return ()
            target_rows = session.execute(
                select(
                    lineage.batch_id,
                    lineage.fact_kind,
                    lineage.fact_source_key,
                    lineage.operation,
                    lineage.target_version,
                )
                .where(
                    lineage.school_id == school_id,
                    lineage.batch_id.in_([row["id"] for row in rows]),
                )
                .order_by(lineage.batch_id, lineage.fact_kind, lineage.fact_source_key)
                .limit(limit * len(allowed_targets) + 1)
            ).all()
        by_batch: dict[UUID, list[SchoolFactOwnResultTarget]] = {}
        for target in target_rows:
            if (target.fact_kind, target.fact_source_key) not in allowed_targets:
                raise SchoolFactPersistenceError("本人回执对象范围发生变化。")
            by_batch.setdefault(target.batch_id, []).append(
                SchoolFactOwnResultTarget(
                    target.fact_kind,
                    target.fact_source_key,
                    target.operation,
                    target.target_version,
                    (target.fact_kind, target.fact_source_key) in creation_only_targets,
                )
            )
        results: list[SchoolFactOwnResult] = []
        for row in rows:
            raw_payload = cast(object, row["result_payload"])
            if not isinstance(raw_payload, dict):
                raise SchoolFactPersistenceError("本人回执缺少确定结果。")
            payload = cast(dict[str, object], raw_payload)
            target_version = payload.get("target_version")
            changed_count = payload.get("changed_count")
            digest = row["locator"]
            result_targets = tuple(by_batch.get(row["id"], ()))
            if (
                type(target_version) is not int
                or type(changed_count) is not int
                or target_version != row["target_fact_version"]
                or target_version < 1
                or changed_count != row["succeeded_units"]
                or changed_count != len(result_targets)
                or type(digest) is not str
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise SchoolFactPersistenceError("本人回执的正式结果或对象依据不完整。")
            confirmed = row["confirmed_at"]
            if confirmed.tzinfo is None:
                confirmed = confirmed.replace(tzinfo=UTC)
            results.append(
                SchoolFactOwnResult(
                    locator_sha256=digest,
                    confirmed_at=confirmed,
                    result=SchoolFactApplyResult(row["id"], target_version, changed_count, True),
                    targets=result_targets,
                )
            )
        return tuple(results)

    @staticmethod
    def _original_version_segments(
        session: Session,
        school_id: UUID,
        target_id: UUID,
        batch_id: UUID,
        revision: int,
        fields: tuple[str, ...],
    ) -> tuple[SchoolFactOwnOriginalSegment, ...]:
        validate_own_original_selection({"teaching_assignment": fields}, 0, 1)
        version = EducationVersion
        projected = func.jsonb_build_object(
            *(item for field in fields for item in (field, version.payload[field]))
        )
        rows = session.execute(
            select(version.valid_from, version.valid_to, projected)
            .where(
                version.school_id == school_id,
                version.fact_id == target_id,
                version.fact_kind == "teaching_assignment",
                version.revision == revision,
                version.import_batch_id == batch_id,
            )
            .order_by(version.valid_from)
            .limit(129)
        ).all()
        if len(rows) > 128:
            raise SchoolFactPersistenceError("Original revision exceeds bounded projection.")
        segments: list[SchoolFactOwnOriginalSegment] = []
        for start, end, content in rows:
            if not isinstance(content, dict):
                raise SchoolFactPersistenceError("Original revision projection is incomplete.")
            content = cast(dict[str, object], content)
            if (
                set(content) != set(fields)
                or any(type(v) is not str or not v for v in content.values())
                or start.utcoffset() is None
                or (end is not None and (end.utcoffset() is None or end <= start))
                or (segments and (segments[-1].valid_to is None or segments[-1].valid_to > start))
            ):
                raise SchoolFactPersistenceError("Original revision projection is incomplete.")
            segments.append(
                SchoolFactOwnOriginalSegment(start, end, cast(dict[str, str], dict(content)))
            )
        return tuple(segments)

    @staticmethod
    def _original_segment_difference(
        before: tuple[SchoolFactOwnOriginalSegment, ...],
        after: tuple[SchoolFactOwnOriginalSegment, ...],
    ) -> tuple[tuple[SchoolFactOwnOriginalSegment, ...], tuple[SchoolFactOwnOriginalSegment, ...]]:
        boundaries = sorted(
            {
                point
                for row in (*before, *after)
                for point in (row.effective_at, row.valid_to)
                if point is not None
            }
        )
        old: list[SchoolFactOwnOriginalSegment] = []
        new: list[SchoolFactOwnOriginalSegment] = []
        for index, start in enumerate(boundaries):
            end = boundaries[index + 1] if index + 1 < len(boundaries) else None
            values = [
                next(
                    (
                        s.content
                        for s in rows
                        if s.effective_at <= start and (s.valid_to is None or start < s.valid_to)
                    ),
                    None,
                )
                for rows in (before, after)
            ]
            if values[0] == values[1]:
                continue
            for value, output in zip(values, (old, new), strict=True):
                if value is not None:
                    if output and output[-1].valid_to == start and output[-1].content == value:
                        previous = output.pop()
                        output.append(
                            SchoolFactOwnOriginalSegment(previous.effective_at, end, dict(value))
                        )
                    else:
                        output.append(SchoolFactOwnOriginalSegment(start, end, dict(value)))
        if len(old) > 128 or len(new) > 128:
            raise SchoolFactPersistenceError(
                "Original changed intervals exceed bounded projection."
            )
        return tuple(old), tuple(new)

    def read_own_original_target(
        self,
        actor: object,
        result: SchoolFactOwnResult,
        target: SchoolFactOwnResultTarget,
        fields: tuple[str, ...],
    ) -> SchoolFactOwnOriginalResult:
        validate_own_original_selection({target.fact_kind: fields}, 0, 1)
        context = self._request_context
        if (
            type(actor) is not StaffActorContext
            or type(context) is not CommandExecutionContext
            or context.actor != actor
            or target not in result.targets
            or target.creation_receipt
        ):
            raise SchoolFactChangeRejected("Original receipt identity or target is unavailable.")
        batch, lineage, root = MasterDataImportBatch, SchoolFactChangeLineage, EducationFact
        coordinates = (
            lineage.school_id == actor.school_id,
            lineage.fact_kind == target.fact_kind,
            lineage.fact_source_key == target.source_key,
            batch.status == "applied",
            root.school_id == actor.school_id,
            root.kind == target.fact_kind,
            root.source_key == target.source_key,
            root.version >= target.target_version,
        )
        base = (
            select(lineage.target_id, lineage.target_version, lineage.batch_id, lineage.operation)
            .join(
                batch,
                and_(batch.school_id == lineage.school_id, batch.id == lineage.batch_id),
            )
            .join(root, and_(root.school_id == lineage.school_id, root.id == lineage.target_id))
        )
        exact = base.where(
            *coordinates,
            lineage.batch_id == result.result.batch_id,
            lineage.target_version == target.target_version,
            lineage.operation == target.operation,
            batch.mode == "canonical_atomic_changes",
            batch.staged_by_user_id == actor.account_user_id,
            batch.staged_by_person_id == actor.person_id,
            batch.confirmed_by_user_id == actor.account_user_id,
            batch.confirmed_by_person_id == actor.person_id,
            batch.mapping_payload["result_locator_sha256"].astext == result.locator_sha256,
        ).limit(2)
        with self._request_bound_session() as session:
            original = session.execute(exact).all()
            if len(original) != 1:
                raise SchoolFactPersistenceError("Original receipt revision is unavailable.")
            target_id = original[0][0]
            previous = session.execute(
                base.where(
                    *coordinates,
                    lineage.target_id == target_id,
                    lineage.target_version < target.target_version,
                )
                .order_by(lineage.target_version.desc())
                .limit(1)
            ).all()
            if target.target_version > 1 and not previous:
                raise SchoolFactPersistenceError("Original preceding revision is unavailable.")
            after = self._original_version_segments(
                session,
                actor.school_id,
                target_id,
                result.result.batch_id,
                target.target_version,
                fields,
            )
            before = (
                self._original_version_segments(
                    session,
                    actor.school_id,
                    target_id,
                    previous[0][2],
                    previous[0][1],
                    fields,
                )
                if previous
                else ()
            )
            if (not after and target.operation not in {"end", "cancel", "revoke"}) or (
                previous and not before and previous[0][3] not in {"end", "cancel", "revoke"}
            ):
                raise SchoolFactPersistenceError("Original applied fields are unavailable.")
        before, after = self._original_segment_difference(before, after)
        return SchoolFactOwnOriginalResult(
            "available",
            target.target_version,
            target.operation,
            fields,
            after,
            before,
            bool(before or after),
        )

    def find_result_by_locator(
        self,
        school_id: SchoolId,
        actor_user_id: int,
        session_sha256: str,
        locator_sha256: str,
    ) -> SchoolFactResultLookup:
        with self._request_bound_session() as session:
            batch = session.execute(
                select(MasterDataImportBatch)
                .where(
                    MasterDataImportBatch.school_id == school_id,
                    MasterDataImportBatch.staged_by_user_id == actor_user_id,
                    MasterDataImportBatch.confirmation_session_sha256 == session_sha256,
                    MasterDataImportBatch.mapping_payload["result_locator_sha256"].astext
                    == locator_sha256,
                )
                .order_by(
                    case(
                        (
                            and_(
                                MasterDataImportBatch.status == "applied",
                                MasterDataImportBatch.result_payload.is_not(None),
                            ),
                            0,
                        ),
                        else_=1,
                    ),
                    MasterDataImportBatch.confirmed_at.asc().nulls_last(),
                    MasterDataImportBatch.created_at.desc(),
                    MasterDataImportBatch.id.asc(),
                )
                .limit(1)
            ).scalar_one_or_none()
        if batch is None:
            return SchoolFactResultLookup(found=False, result=None)
        return SchoolFactResultLookup(
            found=True,
            result=self._result_from_batch(batch, replayed=True),
        )

    def prepare(self, prepared: PreparedConfirmation) -> PreparedConfirmationResult:
        batch_id = uuid4()
        provenance = prepared.provenance
        if provenance is None:
            raise SchoolFactChangeRejected("学校事实确认缺少可核验来源。")
        if provenance.channel == "package":
            try:
                require_full_scope_provenance_integrity(provenance, prepared.change_set)
            except ValueError as exc:
                raise SchoolFactChangeRejected(str(exc)) from None
        if self._is_single_school_reactivation(prepared.change_set):
            return self._prepare_school_reactivation(
                prepared=prepared,
                provenance=provenance,
                proposed_batch_id=batch_id,
            )
        session = self._session_factory()
        try:
            with session.begin():
                self._set_local_request_context(session)
                active_preparation = self._reuse_active_preparation(
                    session,
                    prepared=prepared,
                    provenance=provenance,
                )
                if active_preparation is not None:
                    return active_preparation
                else:
                    credential = self._confirmation_credential_issuer.issue(
                        self._credential_binding(
                            prepared,
                            provenance=provenance,
                            batch_id=batch_id,
                            expires_at=prepared.expires_at,
                        )
                    )
                    batch = MasterDataImportBatch(
                        id=batch_id,
                        school_id=prepared.school_id,
                        mode="canonical_atomic_changes",
                        source_kind=(
                            "manual" if provenance.channel == "admin" else provenance.channel
                        ),
                        source_ref=f"sf2:{batch_id}",
                        source_sha256=prepared.source_sha256,
                        filename_summary={
                            "admin": None,
                            "excel": "school-facts.xlsx",
                            "package": "school-facts-package",
                        }[provenance.channel],
                        source_size_bytes=provenance.source_size_bytes,
                        worksheet_names=(
                            list(provenance.worksheet_names)
                            if provenance.channel == "excel"
                            else (
                                [item.logical_source_code for item in provenance.source_manifest]
                                if provenance.channel == "package"
                                else sorted(
                                {change.origin.sheet_code for change in prepared.change_set.changes}
                                )
                            )
                        ),
                        mapping_version=1,
                        mapping_payload=self._mapping_payload(prepared, provenance),
                        status="ready_for_confirmation",
                        planned_units=len(prepared.change_set.changes),
                        succeeded_units=0,
                        failed_units=0,
                        pending_units=len(prepared.change_set.changes),
                        confirmation_card_sha256=prepared.plan_sha256,
                        request_id=prepared.request_id,
                        idempotency_key_hash=prepared.idempotency_key_sha256,
                        plan_sha256=prepared.plan_sha256,
                        base_fact_version=prepared.base_version,
                        target_fact_version=prepared.base_version + 1,
                        confirmation_token_sha256=sha256(credential.encode("ascii")).hexdigest(),
                        confirmation_expires_at=prepared.expires_at,
                        confirmation_session_sha256=prepared.session_sha256,
                        staged_by_user_id=prepared.actor_user_id,
                        staged_by_person_id=prepared.actor_person_id,
                        staged_by_assignment_id=prepared.actor_assignment_id,
                        staged_assignment_version=prepared.actor_assignment_version,
                        staged_authorization_version=prepared.authorization_version,
                    )
                    session.add(batch)
                    session.flush()
                    for sequence, change in enumerate(prepared.change_set.changes, start=1):
                        private_payload = change.model_dump(mode="json", exclude_none=True)
                        encoded = json.dumps(
                            private_payload,
                            sort_keys=True,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                        payload = {
                            "schema_version": change.schema_version,
                            "operation": change.operation,
                            "fact_kind": change.fact_kind,
                            "source_key": change.source_key,
                            "expected_version": change.expected_version,
                            "effective_at": private_payload["effective_at"],
                            "origin": change.origin.model_dump(mode="json", exclude_none=True),
                        }
                        # Keep the canonical causal coordinates independently of any
                        # resulting version rows; a valid change may leave no rows.
                        if change.cause_source_key is not None:
                            payload.update(
                                cause_source_key=change.cause_source_key,
                                cause_version=change.cause_version,
                            )
                        session.add(
                            MasterDataImportUnit(
                                school_id=prepared.school_id,
                                batch_id=batch_id,
                                sequence=sequence,
                                row_number=change.origin.row_number,
                                unit_kind="school_fact",
                                status="ready",
                                payload=payload,
                                payload_sha256=sha256(encoded).hexdigest(),
                                soft_anomalies=[],
                                initial_soft_anomalies=[],
                            )
                        )
                    session.flush()
            return PreparedConfirmationResult(
                batch_id=batch_id,
                credential=credential,
                expires_at=prepared.expires_at,
            )
        except IntegrityError:
            raise SchoolFactChangeRejected("确认计划已存在或不再有效。") from None
        except DBAPIError as exc:
            self._raise_db_error(exc)
        finally:
            session.close()

    def _prepare_school_reactivation(
        self,
        *,
        prepared: PreparedConfirmation,
        provenance: SchoolFactChangeProvenance,
        proposed_batch_id: UUID,
    ) -> PreparedConfirmationResult:
        if provenance.channel != "admin" or provenance.raw_source_sha256 != prepared.source_sha256:
            raise SchoolFactChangeRejected("学校重新启用准备只接受管理台或默认管理来源。")
        if self._request_context is None:
            raise SchoolFactChangeRejected("学校重新启用准备缺少可信命令上下文。")

        proposed_unit_id = uuid4()
        proposed_credential = self._confirmation_credential_issuer.issue(
            self._credential_binding(
                prepared,
                provenance=provenance,
                batch_id=proposed_batch_id,
                expires_at=prepared.expires_at,
            )
        )
        change = prepared.change_set.changes[0]
        private_payload = change.model_dump(mode="json", exclude_none=True)
        unit_payload_sha256 = sha256(
            json.dumps(
                private_payload,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        session = self._session_factory()
        try:
            with session.begin():
                bound = school_fact_school_reactivation_prepare(
                    session,
                    self._envelope_factory(self._request_context),
                    fact_source_key=change.source_key,
                    expected_version=change.expected_version,
                    effective_at=change.effective_at,
                    base_fact_version=prepared.base_version,
                    source_sha256=prepared.source_sha256,
                    plan_sha256=prepared.plan_sha256,
                    proposed_batch_id=proposed_batch_id,
                    proposed_unit_id=proposed_unit_id,
                    confirmation_token_sha256=sha256(
                        proposed_credential.encode("ascii")
                    ).hexdigest(),
                    confirmation_session_sha256=prepared.session_sha256,
                    confirmation_expires_at=prepared.expires_at,
                    unit_payload_sha256=unit_payload_sha256,
                    result_locator_sha256=provenance.result_locator_sha256,
                    template_version=provenance.template_version,
                    mapping_version=provenance.mapping_version,
                    mapping_config_sha256=provenance.mapping_config_sha256,
                    origin_sheet_code=change.origin.sheet_code,
                    origin_row_number=change.origin.row_number,
                )
            credential = self._confirmation_credential_issuer.issue(
                self._credential_binding(
                    prepared,
                    provenance=provenance,
                    batch_id=bound.batch_id,
                    expires_at=bound.confirmation_expires_at,
                )
            )
            if sha256(credential.encode("ascii")).hexdigest() != bound.confirmation_token_sha256:
                raise SchoolFactChangeRejected("学校重新启用待确认签名环境已改变。")
            return PreparedConfirmationResult(
                batch_id=bound.batch_id,
                credential=credential,
                expires_at=bound.confirmation_expires_at,
            )
        except IntegrityError:
            raise SchoolFactChangeRejected("确认计划已存在或不再有效。") from None
        except DBAPIError as exc:
            self._raise_db_error(exc)
        finally:
            session.close()

    def purge_expired(self, *, now: datetime | None = None) -> int:
        """物理删除未点击且已过期的准备态；任何终态均不在清理范围。"""

        checked_at = now or datetime.now().astimezone()
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            raise RuntimeError("学校事实准备态清理时钟必须携带时区。")
        session = self._session_factory()
        try:
            with session.begin():
                self._set_local_request_context(session)
                if session.scalar(
                    text(
                        "SELECT to_regprocedure("
                        "'app.purge_expired_school_fact_preparations()') IS NOT NULL"
                    )
                ):
                    return purge_expired_school_fact_preparations(session)
                batches = tuple(
                    session.execute(
                        select(MasterDataImportBatch)
                        .where(
                            MasterDataImportBatch.mode == "canonical_atomic_changes",
                            MasterDataImportBatch.status == "ready_for_confirmation",
                            MasterDataImportBatch.confirmation_consumed_at.is_(None),
                            MasterDataImportBatch.confirmation_expires_at <= checked_at,
                        )
                        .order_by(
                            MasterDataImportBatch.confirmation_expires_at.asc(),
                            MasterDataImportBatch.id.asc(),
                        )
                        .with_for_update(skip_locked=True)
                    ).scalars()
                )
                batch_ids = tuple(batch.id for batch in batches)
                if not batch_ids:
                    return 0
                session.execute(
                    delete(MasterDataImportUnit).where(MasterDataImportUnit.batch_id.in_(batch_ids))
                )
                session.execute(
                    delete(MasterDataImportBatch).where(
                        MasterDataImportBatch.id.in_(batch_ids),
                        MasterDataImportBatch.status == "ready_for_confirmation",
                        MasterDataImportBatch.confirmation_consumed_at.is_(None),
                    )
                )
            return len(batch_ids)
        except DBAPIError as exc:
            self._raise_db_error(exc)
        finally:
            session.close()

    @staticmethod
    def _mapping_payload(
        prepared: PreparedConfirmation,
        provenance: SchoolFactChangeProvenance,
    ) -> dict[str, object]:
        return {
            **({"replacement_targets": [t.model_dump(mode="json")
                                       for t in provenance.replacement_targets]}
               if provenance.replacement_targets else {}),
            "schema_version": prepared.change_set.schema_version,
            "raw_source_sha256": provenance.raw_source_sha256,
            "template_version": provenance.template_version,
            "mapping_version": provenance.mapping_version,
            "mapping_config_sha256": provenance.mapping_config_sha256,
            "result_locator_sha256": provenance.result_locator_sha256,
            "source_manifest": [
                {
                    "logical_source_code": item.logical_source_code,
                    "source_sha256": item.source_sha256,
                    "size_bytes": item.size_bytes,
                }
                for item in provenance.source_manifest
            ],
            "policy_attachments": [
                {
                    "source_slot": item.source_slot,
                    "source_sha256": item.source_sha256,
                    "fact_source_key": item.fact_source_key,
                    "field_code": item.field_code,
                }
                for item in provenance.policy_attachments
            ],
            "row_lineage": [
                {
                    "fact_kind": item.fact_kind,
                    "fact_source_key": item.fact_source_key,
                    "raw_source_sha256": item.raw_source_sha256,
                    "source_slot": item.source_slot,
                    "source_sheet_code": item.source_sheet_code,
                    "source_row_number": item.source_row_number,
                    "field_lineage": [
                        {
                            "target_field_code": target_field_code,
                            "source_field_code": source_field_code,
                        }
                        for target_field_code, source_field_code in zip(
                            item.target_field_codes,
                            item.source_field_codes,
                            strict=True,
                        )
                    ],
                }
                for item in provenance.row_lineage
            ],
        }

    def _reuse_active_preparation(
        self,
        session: Session,
        *,
        prepared: PreparedConfirmation,
        provenance: SchoolFactChangeProvenance,
    ) -> PreparedConfirmationResult | None:
        locator_sha256 = provenance.result_locator_sha256
        if locator_sha256 is None:
            return None
        lock_material = (
            f"{prepared.school_id}:{prepared.actor_user_id}:"
            f"{prepared.session_sha256}:{locator_sha256}"
        ).encode("ascii")
        advisory_key = int.from_bytes(
            sha256(lock_material).digest()[:8],
            byteorder="big",
            signed=True,
        )
        session.execute(
            text("SELECT pg_advisory_xact_lock(:advisory_key)"),
            {"advisory_key": advisory_key},
        )
        candidates = tuple(
            session.execute(
                select(MasterDataImportBatch)
                .where(
                    MasterDataImportBatch.school_id == prepared.school_id,
                    MasterDataImportBatch.staged_by_user_id == prepared.actor_user_id,
                    MasterDataImportBatch.confirmation_session_sha256 == prepared.session_sha256,
                    MasterDataImportBatch.mapping_payload["result_locator_sha256"].astext
                    == locator_sha256,
                )
                .order_by(
                    case((MasterDataImportBatch.status == "applied", 0), else_=1),
                    MasterDataImportBatch.created_at.asc(),
                    MasterDataImportBatch.id.asc(),
                )
                .with_for_update()
            ).scalars()
        )
        if not candidates:
            return None
        batch = candidates[0]
        if batch.status == "applied":
            raise SchoolFactChangeRejected("学校事实计划已经应用，请查询权威结果。")
        if batch.status != "ready_for_confirmation":
            raise SchoolFactChangeRejected("学校事实计划正在处理，请查询权威结果。")
        expected_payload = self._mapping_payload(prepared, provenance)
        if not all(
            (
                batch.plan_sha256 == prepared.plan_sha256,
                batch.source_sha256 == prepared.source_sha256,
                batch.base_fact_version == prepared.base_version,
                batch.planned_units == len(prepared.change_set.changes),
                batch.mapping_payload == expected_payload,
                batch.staged_by_person_id == prepared.actor_person_id,
                batch.staged_by_assignment_id == prepared.actor_assignment_id,
                batch.staged_assignment_version == prepared.actor_assignment_version,
                batch.staged_authorization_version == prepared.authorization_version,
            )
        ):
            raise SchoolFactChangeRejected("学校事实计划定位与当前规范变更不一致。")
        if batch.idempotency_key_hash != prepared.idempotency_key_sha256:
            raise SchoolFactChangeRejected("学校事实计划已有待确认请求。")
        if batch.confirmation_expires_at is None or batch.confirmation_token_sha256 is None:
            raise SchoolFactChangeRejected("学校事实待确认状态不完整。")
        credential = self._confirmation_credential_issuer.issue(
            self._credential_binding(
                prepared,
                provenance=provenance,
                batch_id=batch.id,
                expires_at=batch.confirmation_expires_at,
            )
        )
        if sha256(credential.encode("ascii")).hexdigest() != batch.confirmation_token_sha256:
            raise SchoolFactChangeRejected("学校事实确认签名环境已改变。")
        return PreparedConfirmationResult(
            batch_id=batch.id,
            credential=credential,
            expires_at=batch.confirmation_expires_at,
        )

    @staticmethod
    def _credential_binding(
        prepared: PreparedConfirmation,
        *,
        provenance: SchoolFactChangeProvenance,
        batch_id: UUID,
        expires_at: datetime,
    ) -> ConfirmationCredentialBinding:
        return ConfirmationCredentialBinding(
            batch_id=batch_id,
            school_id=prepared.school_id,
            actor_user_id=prepared.actor_user_id,
            session_sha256=prepared.session_sha256,
            idempotency_key_sha256=prepared.idempotency_key_sha256,
            plan_sha256=prepared.plan_sha256,
            source_sha256=prepared.source_sha256,
            base_version=prepared.base_version,
            result_locator_sha256=provenance.result_locator_sha256,
            expires_at=expires_at,
        )

    def confirm(
        self,
        *,
        credential_sha256: str,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_sha256: str,
        now: datetime,
    ) -> SchoolFactApplyResult:
        actor = context.actor
        if type(actor) is not StaffActorContext:
            raise SchoolFactChangeRejected("确认操作者无效。")
        session = self._session_factory()
        try:
            transaction = session.begin()
            try:
                envelope = self._envelope_factory(context)
                if self._is_single_school_reactivation(change_set):
                    change = change_set.changes[0]
                    private_change_payload = change.model_dump(
                        mode="json",
                        exclude_none=True,
                    )
                    set_local_school_reactivation_context(
                        session,
                        envelope,
                        confirmation_token_sha256=credential_sha256,
                        confirmation_session_sha256=session_sha256,
                        fact_source_key=change.source_key,
                        expected_version=change.expected_version,
                        plan_sha256=canonical_plan_sha256(change_set),
                        source_sha256=change_set.source_sha256,
                        unit_payload_sha256=sha256(
                            json.dumps(
                                private_change_payload,
                                sort_keys=True,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest(),
                        base_version=change_set.base_version,
                    )
                    if self._final_authorization_guard is not None:
                        self._final_authorization_guard(session, context, change_set)
                    narrow_replay = school_reactivation_replay_result(session)
                    if narrow_replay is not None:
                        transaction.rollback()
                        return SchoolFactApplyResult(
                            batch_id=narrow_replay.batch_id,
                            target_version=narrow_replay.target_version,
                            changed_count=narrow_replay.changed_count,
                            replayed=True,
                        )
                elif self._is_single_non_school_root_reactivation(change_set):
                    change = change_set.changes[0]
                    set_local_execution_context(session, envelope)
                    set_local_root_reactivation_context(
                        session,
                        confirmation_token_sha256=credential_sha256,
                        confirmation_session_sha256=session_sha256,
                        fact_kind=cast(
                            Literal["person", "student", "organization", "location"],
                            change.fact_kind,
                        ),
                        fact_source_key=change.source_key,
                        expected_version=change.expected_version,
                    )
                else:
                    set_local_execution_context(session, envelope)
                if self._package_final_authorization_guard is None:
                    # Preserve the old lock/authorization order for retained channels.
                    if self._final_authorization_guard is not None:
                        self._final_authorization_guard(session, context, change_set)
                else:
                    # Select the package guard only from the validated stored batch,
                    # while preserving governance-before-confirmation lock ordering.
                    epoch = session.scalar(
                        text("SELECT app.lock_school_fact_governance_epoch(:school,:assignment)"),
                        {"school": actor.school_id, "assignment": actor.assignment_id},
                    )
                    if type(epoch) is not int or epoch < 1:
                        raise SchoolFactChangeRejected("当前维护授权版本不可验证。")
                batch = session.execute(
                    select(MasterDataImportBatch)
                    .where(MasterDataImportBatch.confirmation_token_sha256 == credential_sha256)
                    .with_for_update()
                ).scalar_one_or_none()
                if batch is None:
                    raise SchoolFactChangeRejected("确认凭据无效。")
                self._validate_confirmation(
                    batch=batch,
                    change_set=change_set,
                    actor=actor,
                    session_sha256=session_sha256,
                    now=now,
                )
                if (
                    self._package_final_authorization_guard is not None
                    or batch.source_kind == "package"
                ):
                    self._authorize_confirmed_batch(session, context, change_set, batch)
                if batch.confirmation_consumed_at is not None:
                    result = self._result_from_batch(batch, replayed=True)
                    if result is None:
                        raise SchoolFactChangeRejected("确认凭据已消费且结果不可用。")
                    transaction.rollback()
                    return result

                duplicate = session.execute(
                    self._applied_plan_query(
                        actor.school_id,
                        change_set.source_sha256,
                        canonical_plan_sha256(change_set),
                        exclude_batch_id=batch.id,
                    )
                ).scalar_one_or_none()
                if duplicate is not None:
                    units = tuple(
                        session.execute(
                            select(MasterDataImportUnit)
                            .where(MasterDataImportUnit.batch_id == batch.id)
                            .order_by(MasterDataImportUnit.sequence)
                            .with_for_update()
                        ).scalars()
                    )
                    if len(units) != len(change_set.changes):
                        raise SchoolFactChangeRejected("确认计划不完整。")
                    for unit in units:
                        unit.status = "applied"
                        unit.version += 1
                    batch.confirmation_consumed_at = now
                    batch.confirmed_by_user_id = actor.account_user_id
                    batch.confirmed_by_person_id = actor.person_id
                    batch.confirmed_by_assignment_id = actor.assignment_id
                    batch.confirmed_at = now
                    batch.status = "applied"
                    batch.target_fact_version = duplicate.target_fact_version
                    batch.succeeded_units = len(units)
                    batch.pending_units = 0
                    batch.result_payload = {
                        "target_version": int(duplicate.target_fact_version or 1),
                        "changed_count": 0,
                    }
                    self._append_audit(session, batch, actor, context, changed_count=0)
                    session.flush()
                    self._append_terminal_outcome(
                        session=session,
                        batch=batch,
                        actor=actor,
                        context=context,
                        sequences=[unit.sequence for unit in units],
                    )
                    session.flush()
                    self._commit_transaction(transaction)
                    return self._result_from_batch(batch, replayed=True)  # type: ignore[return-value]

                units = self.apply_canonical_units_in_transaction(
                    session, batch=batch, change_set=change_set, school_id=actor.school_id,
                    context=context,
                )
                self._append_audit(
                    session,
                    batch,
                    actor,
                    context,
                    changed_count=len(units),
                )
                target_version = change_set.base_version + 1
                batch.confirmation_consumed_at = now
                batch.confirmed_by_user_id = actor.account_user_id
                batch.confirmed_by_person_id = actor.person_id
                batch.confirmed_by_assignment_id = actor.assignment_id
                batch.confirmed_at = now
                batch.status = "applied"
                batch.succeeded_units = len(units)
                batch.pending_units = 0
                batch.result_payload = {
                    "target_version": target_version,
                    "changed_count": len(units),
                }
                session.flush()
                self._append_terminal_outcome(
                    session=session,
                    batch=batch,
                    actor=actor,
                    context=context,
                    sequences=[unit.sequence for unit in units],
                )
                session.flush()
                self._commit_transaction(transaction)
                return SchoolFactApplyResult(
                    batch_id=batch.id,
                    target_version=target_version,
                    changed_count=len(units),
                    replayed=False,
                )
            except BaseException:
                if transaction.is_active:
                    transaction.rollback()
                raise
        except SchoolFactChangeRejected:
            raise
        except DBAPIError as exc:
            if self._is_commit_unknown(exc):
                recovered = self._probe_result(credential_sha256)
                if recovered is not None:
                    return recovered
                raise SchoolFactCommitOutcomeUnknown("学校事实提交结果未知。") from None
            self._raise_db_error(exc)
        finally:
            session.close()

    def _revalidate_package_replacement(self, session, batch, school_id, context):
        """Called only under the school lock, before any canonical unit is applied."""
        from app.platform.school_facts.replacement_contracts import PackageReplacementTarget

        payload = batch.mapping_payload or {}
        raw = payload.get("replacement_targets")
        if raw is None:
            return
        guard = self._package_final_authorization_guard
        if (batch.source_kind != "package" or context is None or guard is None
                or not callable(getattr(guard, "require_replacement", None))
                or type(raw) is not list or not 1 <= len(raw) <= 50_000):
            raise SchoolFactChangeRejected("完整替换选定集证明不可用。")
        try:
            targets = tuple(
                PackageReplacementTarget.model_validate_json(json.dumps(t)) for t in raw
            )
        except ValueError:
            raise SchoolFactChangeRejected("完整替换选定集证明无效。") from None
        if len({(t.fact_kind, t.source_key) for t in targets}) != len(targets):
            raise SchoolFactChangeRejected("完整替换选定集含重复对象。")
        guard.require_replacement(context, targets)
        grouped: dict[str, list] = {}
        for target in targets:
            grouped.setdefault(target.fact_kind, []).append(target)
        for kind, selected in grouped.items():
            keys = [t.source_key for t in selected]
            root = _ROOT_FACTS.get(kind)
            if root is not None:
                table_name, key_column = root
                school_filter = "id" if kind == "school" else "school_id"
                statement = text(
                    f"SELECT {key_column}, version FROM {table_name} "  # noqa: S608 - fixed allowlist
                    f"WHERE {school_filter}=:school AND {key_column}=ANY(:keys)"
                )
                params = {"school": school_id, "keys": keys}
            else:
                statement = text(
                    "SELECT fact_source_key, MAX(target_version) FROM school_fact_change_lineage "
                    "WHERE school_id=:school AND fact_kind=:kind AND fact_source_key=ANY(:keys) "
                    "GROUP BY fact_source_key"
                )
                params = {"school": school_id, "kind": kind, "keys": keys}
            versions = dict(session.execute(statement, params).all())
            if any(versions.get(t.source_key, 0) != t.expected_version for t in selected):
                raise SchoolFactChangeRejected("完整替换选定对象已新增、移除或改变，请重新预览。")

    def apply_canonical_units_in_transaction(
        self,
        session: Session,
        *,
        batch: MasterDataImportBatch,
        change_set: CanonicalFactChangeSetV1,
        school_id: UUID,
        context: CommandExecutionContext | None = None,
    ) -> tuple[MasterDataImportUnit, ...]:
        """Shared apply kernel; caller owns authenticated transaction and confirmation."""
        school_lock = session.execute(
            text("SELECT id FROM schools WHERE id = :school_id FOR UPDATE"),
            {"school_id": school_id},
        ).scalar_one_or_none()
        if school_lock is None:
            raise SchoolFactChangeRejected("学校事实环境不存在。")
        current_version = self._current_global_fact_version(session, school_id)
        if current_version != change_set.base_version:
            raise SchoolFactChangeRejected("学校事实基础版本已变化。")
        self._revalidate_package_replacement(session, batch, school_id, context)
        units = tuple(
            session.execute(
                select(MasterDataImportUnit)
                .where(MasterDataImportUnit.batch_id == batch.id)
                .order_by(MasterDataImportUnit.sequence)
                .with_for_update()
            ).scalars()
        )
        if len(units) != len(change_set.changes):
            raise SchoolFactChangeRejected("确认计划不完整。")
        source_id = self._ensure_batch_source(
            session, batch, schema_version=change_set.schema_version
        )
        for unit, change in zip(units, change_set.changes, strict=True):
            encoded = json.dumps(
                change.model_dump(mode="json", exclude_none=True),
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if unit.payload_sha256 != sha256(encoded).hexdigest():
                raise SchoolFactChangeRejected("确认内容与预检计划不一致。")
        residence_terminal_source_keys = tuple(
            change.source_key
            for change in change_set.changes
            if change.fact_kind == "student_bed_residence" and change.operation in {"end", "revoke"}
        )
        residence_students_by_terminal = (
            self._residence_student_source_keys_in_session(
                session,
                school_id=school_id,
                residence_source_keys=residence_terminal_source_keys,
            )
            if residence_terminal_source_keys
            else {}
        )
        ordered_pairs = self._ordered_change_pairs(
            units,
            change_set,
            residence_students_by_terminal,
        )
        if all(
            change.fact_kind == "policy"
            and change.operation == "upsert"
            and change.expected_version == 0
            for _unit, change in ordered_pairs
        ):
            self._bulk_apply_new_policies(
                session=session,
                school_id=school_id,
                batch_id=batch.id,
                source_sha256=change_set.source_sha256,
                pairs=ordered_pairs,
            )
        else:
            for unit, change in ordered_pairs:
                target_id, target_version = self._apply_change(
                    session=session,
                    school_id=school_id,
                    batch_id=batch.id,
                    source_id=source_id,
                    change=change,
                )
                unit.status = "applied"
                unit.target_id = target_id
                unit.target_version = target_version
                unit.version += 1
                session.add(
                    SchoolFactChangeLineage(
                        school_id=school_id,
                        batch_id=batch.id,
                        unit_id=unit.id,
                        fact_kind=change.fact_kind,
                        fact_source_key=change.source_key,
                        operation=change.operation,
                        target_id=target_id,
                        target_version=target_version,
                        source_sha256=change_set.source_sha256,
                        sheet_code=change.origin.sheet_code,
                        row_number=change.origin.row_number,
                    )
                )
                session.flush()
        # A batch may alter this same mother set after its quality row was
        # applied. Recompute once more against the final transactional state;
        # the school lock and rollback make a mismatch atomic.
        for change in change_set.changes:
            if (
                change.fact_kind == "fact_quality"
                and change.content is not None
                and isinstance(getattr(change.content, "coverage_basis", None), CoverageBasisV2)
            ):
                self._verify_complete_coverage_basis(
                    session, school_id, change, {},
                )
        if any(
            change.fact_kind in EDUCATION_KINDS
            or change.fact_kind == "student_school_relationship"
            # CAS above proves these identities are new at version zero. Existing
            # education references cannot point to them; only changes to existing
            # identities can invalidate those references. This also keeps the
            # schema22 first-identity path independent of later education tables.
            or (
                change.fact_kind in {"assignment", "organization"}
                and change.expected_version > 0
            )
            for change in change_set.changes
        ):
            self._validate_education_state(session, school_id)
        self._validate_location_terminal_residences(
            session=session,
            school_id=school_id,
            change_set=change_set,
        )
        return units

    @staticmethod
    def _is_single_school_reactivation(change_set: CanonicalFactChangeSetV1) -> bool:
        return (
            len(change_set.changes) == 1
            and change_set.changes[0].fact_kind == "school"
            and change_set.changes[0].operation == "reactivate"
        )

    @staticmethod
    def _is_single_non_school_root_reactivation(
        change_set: CanonicalFactChangeSetV1,
    ) -> bool:
        return (
            len(change_set.changes) == 1
            and change_set.changes[0].fact_kind in {"person", "student", "organization", "location"}
            and change_set.changes[0].operation == "reactivate"
        )

    @staticmethod
    def _validate_confirmation(
        *,
        batch: MasterDataImportBatch,
        change_set: CanonicalFactChangeSetV1,
        actor: StaffActorContext,
        session_sha256: str,
        now: datetime,
    ) -> None:
        bindings = (
            batch.school_id == actor.school_id,
            batch.staged_by_user_id == actor.account_user_id,
            batch.staged_by_person_id == actor.person_id,
            batch.staged_by_assignment_id == actor.assignment_id,
            batch.staged_assignment_version == actor.assignment_version,
            batch.staged_authorization_version == actor.authorization_version,
            batch.confirmation_session_sha256 == session_sha256,
            batch.plan_sha256 == canonical_plan_sha256(change_set),
            batch.source_sha256 == change_set.source_sha256,
            batch.base_fact_version == change_set.base_version,
        )
        if not all(bindings):
            raise SchoolFactChangeRejected("确认凭据与当前计划或可信上下文不匹配。")
        if batch.confirmation_expires_at is None or now >= batch.confirmation_expires_at:
            raise SchoolFactChangeRejected("确认凭据已过期。")

    @staticmethod
    def _ordered_change_pairs(
        units: tuple[MasterDataImportUnit, ...],
        change_set: CanonicalFactChangeSetV1,
        residence_students_by_terminal: Mapping[str, str],
    ) -> tuple[tuple[MasterDataImportUnit, CanonicalFactChangeV1], ...]:
        pairs = tuple(zip(units, change_set.changes, strict=True))
        index_by_target = {
            (change.fact_kind, change.source_key): index
            for index, (_unit, change) in enumerate(pairs)
        }
        dependents: list[list[int]] = [[] for _pair in pairs]
        indegrees = [0 for _pair in pairs]
        for index, (_unit, change) in enumerate(pairs):
            content = safe_content_payload(change.content) or {}
            dependencies: set[tuple[str, str]] = set()
            for segment in change.replacement_segments or ():
                for field_name, reference_kind in _REFERENCE_KINDS.items():
                    source_key = getattr(segment.content, field_name, None)
                    if isinstance(source_key, str):
                        dependencies.add((reference_kind, source_key))
                dependencies.update(
                    (kind, key)
                    for _field, kind, key in education_extra_references(
                        change.fact_kind, segment.content
                    )
                )
            for field_name, reference_kind in _REFERENCE_KINDS.items():
                source_key = content.get(field_name)
                if isinstance(source_key, str):
                    dependency_kind = "location" if reference_kind == "campus" else reference_kind
                    dependencies.add((dependency_kind, source_key))
            if change.content is not None:
                dependencies.update(
                    (kind, key)
                    for _field, kind, key in education_extra_references(
                        change.fact_kind, change.content
                    )
                )
            if content.get("education_target_source_key") is not None:
                dependencies.add(
                    (
                        str(content["education_target_kind"]),
                        str(content["education_target_source_key"]),
                    )
                )
            parent_source_key = content.get("parent_source_key")
            if isinstance(parent_source_key, str):
                parent_kind = "organization" if change.fact_kind == "organization" else "location"
                dependencies.add((parent_kind, parent_source_key))
            if change.fact_kind == "fact_quality":
                for field in ("target_source_key", "replacement_source_key"):
                    if isinstance(content.get(field), str):
                        dependencies.add((str(content["target_fact_kind"]), str(content[field])))
            for dependency in dependencies:
                dependency_index = index_by_target.get(dependency)
                if dependency_index is None:
                    continue
                indegrees[index] += 1
                dependents[dependency_index].append(index)

        for (
            terminal_index,
            upsert_index,
        ) in SqlAlchemySchoolFactPersistence._residence_order_dependencies(
            change_set.changes,
            residence_students_by_terminal,
        ):
            dependents[terminal_index].append(upsert_index)
            indegrees[upsert_index] += 1

        def ready_key(index: int) -> tuple[int, int]:
            change = pairs[index][1]
            terminal_first = (
                change.fact_kind in F2_MODELS or change.fact_kind in EDUCATION_KINDS
            ) and change.operation in {"end", "revoke", "cancel"}
            return (0 if terminal_first else 1, index)

        ready = [ready_key(index) for index, indegree in enumerate(indegrees) if indegree == 0]
        heapq.heapify(ready)
        ordered_indexes: list[int] = []
        while ready:
            _priority, index = heapq.heappop(ready)
            ordered_indexes.append(index)
            for dependent in dependents[index]:
                indegrees[dependent] -= 1
                if indegrees[dependent] == 0:
                    heapq.heappush(ready, ready_key(dependent))
        if len(ordered_indexes) != len(pairs):
            raise SchoolFactChangeRejected("规范事实引用形成循环依赖。")
        return tuple(pairs[index] for index in ordered_indexes)

    @staticmethod
    def _residence_order_dependencies(
        changes: tuple[CanonicalFactChangeV1, ...],
        residence_students_by_terminal: Mapping[str, str],
    ) -> tuple[tuple[int, int], ...]:
        """按学生建立线性屏障：该生所有显式终止先于其新入住切片。"""

        terminals_by_student: dict[str, list[int]] = {}
        upserts_by_student: dict[str, list[int]] = {}
        missing_terminal_sources: list[str] = []
        for index, change in enumerate(changes):
            if change.fact_kind != "student_bed_residence":
                continue
            if change.operation in {"end", "revoke"}:
                student_source_key = residence_students_by_terminal.get(change.source_key)
                if student_source_key is None:
                    missing_terminal_sources.append(change.source_key)
                    continue
                terminals_by_student.setdefault(student_source_key, []).append(index)
            elif change.operation == "upsert":
                content = change.content
                if content is None or content.student_source_key is None:
                    raise SchoolFactChangeRejected("入住事实缺少学生稳定标识。")
                upserts_by_student.setdefault(str(content.student_source_key), []).append(index)
        if missing_terminal_sources:
            raise SchoolFactChangeRejected("入住终止目标无法解析学生稳定标识。")

        dependencies: list[tuple[int, int]] = []
        for student_source_key in sorted(terminals_by_student.keys() & upserts_by_student.keys()):
            ordered_indexes = (
                *terminals_by_student[student_source_key],
                *upserts_by_student[student_source_key],
            )
            dependencies.extend(zip(ordered_indexes, ordered_indexes[1:], strict=False))
        return tuple(dependencies)

    @staticmethod
    def _validate_location_terminal_residences(
        *,
        session: Session,
        school_id: UUID,
        change_set: CanonicalFactChangeSetV1,
    ) -> None:
        """在整批 unit 写完后守住地点失效时的 Residence 最终态。"""

        terminal_locations = tuple(
            change
            for change in change_set.changes
            if change.fact_kind == "location" and change.operation in {"end", "revoke"}
        )
        if not terminal_locations:
            return
        invalid_residences = session.execute(
            text(
                "SELECT count(*) FROM student_bed_residences AS residence "
                "JOIN dorm_beds AS bed ON bed.school_id = residence.school_id "
                "AND bed.id = residence.bed_id "
                "WHERE residence.school_id = :school_id "
                "AND residence.revoked_at IS NULL "
                "AND NOT app.school_fact_location_history_interval_is_resolvable("
                "residence.school_id, bed.room_location_id, "
                "residence.valid_from, residence.valid_to)"
            ),
            {"school_id": school_id},
        ).scalar_one()
        if int(invalid_residences) > 0:
            raise SchoolFactChangeRejected("地点终止后不得留下不成立的入住历史。")

    @staticmethod
    def _ensure_batch_source(
        session: Session, batch: MasterDataImportBatch, *, schema_version: int = 1
    ) -> UUID:
        source_key = f"change-set:{batch.id}"
        existing = session.execute(
            text(
                "SELECT id FROM school_fact_sources "
                "WHERE school_id = :school_id AND source_key = :source_key"
            ),
            {"school_id": batch.school_id, "source_key": source_key},
        ).scalar_one_or_none()
        if existing is not None:
            return UUID(str(existing))
        source_id = uuid4()
        session.execute(
            text(
                "INSERT INTO school_fact_sources "
                "(id, school_id, source_key, source_kind, source_ref, schema_version) "
                "VALUES (:id, :school_id, :source_key, :source_kind, :source_ref, :schema_version)"
            ),
            {
                "id": source_id,
                "school_id": batch.school_id,
                "source_key": source_key,
                "source_kind": batch.source_kind,
                "source_ref": batch.source_ref,
                "schema_version": schema_version,
            },
        )
        return source_id

    @staticmethod
    def _bulk_apply_new_policies(
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_sha256: str,
        pairs: tuple[tuple[MasterDataImportUnit, CanonicalFactChangeV1], ...],
    ) -> None:
        """以有界 executemany 写入独立制度目录，仍留在同一正式事务与同一写核。"""

        policy_rows: list[dict[str, object]] = []
        lineage_rows: list[dict[str, object]] = []
        for unit, change in pairs:
            target_id = uuid4()
            policy_rows.append(
                {
                    "id": target_id,
                    "school_id": school_id,
                    "source_key": change.source_key,
                }
            )
            lineage_rows.append(
                {
                    "id": uuid4(),
                    "school_id": school_id,
                    "batch_id": batch_id,
                    "unit_id": unit.id,
                    "fact_kind": change.fact_kind,
                    "fact_source_key": change.source_key,
                    "operation": change.operation,
                    "target_id": target_id,
                    "target_version": 1,
                    "source_sha256": source_sha256,
                    "sheet_code": change.origin.sheet_code,
                    "row_number": change.origin.row_number,
                }
            )
            unit.status = "applied"
            unit.target_id = target_id
            unit.target_version = 1
            unit.version += 1

        policy_statement = text(
            "INSERT INTO school_policies (id, school_id, source_key) "
            "VALUES (:id, :school_id, :source_key)"
        )
        lineage_statement = text(
            "INSERT INTO school_fact_change_lineage "
            "(id, school_id, batch_id, unit_id, fact_kind, fact_source_key, operation, "
            "target_id, target_version, source_sha256, sheet_code, row_number) VALUES "
            "(:id, :school_id, :batch_id, :unit_id, :fact_kind, :fact_source_key, "
            ":operation, :target_id, :target_version, :source_sha256, :sheet_code, :row_number)"
        )
        for offset in range(0, len(policy_rows), 5_000):
            session.execute(policy_statement, policy_rows[offset : offset + 5_000])
            session.execute(lineage_statement, lineage_rows[offset : offset + 5_000])
        session.flush()

    def _apply_change(
        self,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
    ) -> tuple[UUID, int]:
        if change.fact_kind in EDUCATION_KINDS:
            return self._apply_education(
                session=session,
                school_id=school_id,
                batch_id=batch_id,
                source_id=source_id,
                change=change,
            )
        current = self._lock_target(session, school_id, change.fact_kind, change.source_key)
        current_id, current_version = current if current is not None else (None, 0)
        if current_version != change.expected_version:
            raise SchoolFactChangeRejected("事实版本已变化。")
        if change.fact_kind in F6_FACILITY_KINDS:
            return self._apply_residence_facility(
                session=session,
                school_id=school_id,
                batch_id=batch_id,
                source_id=source_id,
                change=change,
                current_id=current_id,
                current_version=current_version,
            )
        if change.fact_kind == "student_school_relationship" and change.operation == "correct":
            return self._correct_student_school_relationship(
                session=session,
                school_id=school_id,
                batch_id=batch_id,
                source_id=source_id,
                change=change,
                current_id=current_id,
                current_version=current_version,
            )
        if (
            current_id is not None
            and (change.fact_kind in F2_MODELS or change.fact_kind == "policy_version")
            and change.operation == "upsert"
        ):
            if self._latest_lineage_operation(
                session, school_id, change.fact_kind, change.source_key
            ) in {"end", "revoke"}:
                raise SchoolFactChangeRejected("Terminated F2 relations require a new source key.")
        root_is_effective = (
            self._root_is_effective_at(
                session,
                school_id=school_id,
                fact_kind=change.fact_kind,
                target_id=current_id,
                effective_at=change.effective_at,
            )
            if current_id is not None and change.fact_kind in _ROOT_FACTS
            else None
        )
        if change.operation == "reactivate":
            if current_id is None or change.fact_kind not in _ROOT_FACTS:
                raise SchoolFactChangeRejected("只有已停用的根身份事实可以显式重新启用。")
            if root_is_effective:
                raise SchoolFactChangeRejected("仍有效的根身份事实不能提前重新启用。")
            if change.fact_kind == "school":
                marker = require_school_reactivation_context(session)
                if (
                    self._latest_lineage_operation(
                        session,
                        school_id,
                        change.fact_kind,
                        change.source_key,
                    )
                    != "revoke"
                ):
                    raise SchoolFactChangeRejected("学校只能在正式撤销后重新启用。")
                if (
                    self._root_status(session, change.fact_kind, current_id) != "active"
                    or marker.school_id != school_id
                    or marker.batch_id != batch_id
                    or marker.fact_source_key != change.source_key
                    or marker.expected_version != change.expected_version
                ):
                    raise SchoolFactChangeRejected("学校重新启用上下文与已准备单元不匹配。")
            else:
                marker = require_root_reactivation_context(session)
                if (
                    self._root_status(session, change.fact_kind, current_id) != "inactive"
                    or marker.school_id != school_id
                    or marker.batch_id != batch_id
                    or marker.fact_kind != change.fact_kind
                    or marker.fact_source_key != change.source_key
                    or marker.expected_version != change.expected_version
                    or marker.target_id != current_id
                ):
                    raise SchoolFactChangeRejected("根身份重新启用上下文与已准备单元不匹配。")
            return self._upsert(
                session=session,
                school_id=school_id,
                batch_id=batch_id,
                source_id=source_id,
                change=change,
                current_id=current_id,
                current_version=current_version,
            )
        if change.fact_kind == "guardian_relationship" and change.operation in {"end", "revoke"}:
            family_actor = self._family_actor(
                action=change.operation, target=str(change.source_key)
            )
            guardian_person_id = session.execute(
                text(
                    "SELECT guardian_person_id FROM guardian_relationships "
                    "WHERE school_id=:school_id AND id=:id"
                ),
                {"school_id": school_id, "id": current_id},
            ).scalar_one_or_none()
            if guardian_person_id == family_actor.actor_person_id:
                raise SchoolFactChangeRejected("核验人员不能停用自己的家校关系。")
        if change.operation != "upsert":
            if current_id is None:
                raise SchoolFactChangeRejected("待结束或撤销的事实不存在。")
            latest_operation = self._latest_lineage_operation(
                session,
                school_id,
                change.fact_kind,
                change.source_key,
            )
            is_residence_end_correction = (
                change.fact_kind == "student_bed_residence"
                and latest_operation == "end"
                and change.operation == "revoke"
            )
            if latest_operation in {"end", "revoke"} and not is_residence_end_correction:
                raise SchoolFactChangeRejected("已终止的事实不能再次结束或撤销。")
            if (
                change.fact_kind in _ROOT_FACTS
                and self._root_status(session, change.fact_kind, current_id) != "active"
            ):
                raise SchoolFactChangeRejected("已停用的根身份不能再次结束或撤销。")
            self._end_or_revoke(
                session,
                change=change,
                target_id=current_id,
                target_version=current_version,
            )
            return current_id, current_version + 1
        if (
            current_id is not None
            and change.fact_kind in _ROOT_FACTS
            and (
                not root_is_effective
                or self._latest_lineage_operation(
                    session,
                    school_id,
                    change.fact_kind,
                    change.source_key,
                )
                in {"end", "revoke"}
            )
        ):
            raise SchoolFactChangeRejected("已停用的根身份不能被普通 upsert 重新启用。")
        return self._upsert(
            session=session,
            school_id=school_id,
            batch_id=batch_id,
            source_id=source_id,
            change=change,
            current_id=current_id,
            current_version=current_version,
        )

    def _apply_education(
        self,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
    ) -> tuple[UUID, int]:
        """Only invoked by the existing confirmed transaction and CAS apply kernel."""
        root = session.execute(
            select(EducationFact)
            .where(
                EducationFact.school_id == school_id,
                EducationFact.kind == change.fact_kind,
                EducationFact.source_key == change.source_key,
            )
            .with_for_update()
        ).scalar_one_or_none()
        current_version = root.version if root is not None else 0
        if current_version != change.expected_version:
            raise SchoolFactChangeRejected("教育事实版本已变化。")
        if root is None:
            if change.operation != "upsert":
                raise SchoolFactChangeRejected("待变更的教育事实不存在。")
            root = EducationFact(
                id=uuid4(),
                school_id=school_id,
                kind=change.fact_kind,
                source_key=change.source_key,
                version=1,
            )
            session.add(root)
            session.flush()
        old = tuple(
            session.execute(
                select(EducationVersion)
                .where(
                    EducationVersion.school_id == school_id,
                    EducationVersion.fact_id == root.id,
                    EducationVersion.superseded_at.is_(None),
                )
                .order_by(EducationVersion.valid_from)
                .with_for_update()
            ).scalars()
        )
        now = datetime.now(UTC)
        content = safe_content_payload(change.content)
        until = change.content.valid_to if change.content is not None else None
        if content is not None:
            content = {key: value for key, value in content.items() if key != "valid_to"}
        cancellation_prior = ()
        if change.operation == "cancel" and any(
            row.valid_from < change.effective_at for row in old
        ):
            # Restore only the prior revision cut by this exact latest future upsert.
            # An independent correction/termination changes the latest operation and fails closed.
            if (
                change.effective_at <= now
                or self._latest_lineage_operation(
                    session, school_id, change.fact_kind, change.source_key
                )
                != "upsert"
                or not any(row.valid_from == change.effective_at for row in old)
            ):
                raise SchoolFactChangeRejected("该未来替代已经另行修改，必须重新核对完整影响。")
            cancellation_prior = tuple(
                session.execute(
                    select(EducationVersion).where(
                        EducationVersion.school_id == school_id,
                        EducationVersion.fact_id == root.id,
                        EducationVersion.revision == current_version - 1,
                    )
                ).scalars()
            )
            if not cancellation_prior:
                raise SchoolFactChangeRejected("取消缺少原替代之前的权威版本。")
        try:
            revised = (
                tuple(
                    EducationSlice(
                        segment.effective_at,
                        segment.valid_to,
                        safe_content_payload(segment.content) or {},
                    )
                    for segment in change.replacement_segments
                )
                if change.replacement_segments is not None
                else tuple(
                    EducationSlice(row.valid_from, row.valid_to, row.payload)
                    for row in cancellation_prior
                )
                if cancellation_prior
                else revise_timeline(
                    tuple(EducationSlice(row.valid_from, row.valid_to, row.payload) for row in old),
                    operation=change.operation,
                    at=change.effective_at,
                    until=until,
                    payload=content,
                    now=now,
                )
            )
        except ValueError as exc:
            raise SchoolFactChangeRejected(str(exc)) from exc
        if change.operation == "correct" and not old:
            # A cancelled fact is not silently revived by an ordinary correction.
            raise SchoolFactChangeRejected("已取消安排不能通过补正自动恢复。")
        for row in old:
            row.superseded_at = now
        session.flush()
        for segment in revised:
            previous = next(
                (
                    row
                    for row in (cancellation_prior or old)
                    if row.payload == segment.payload
                    and row.valid_from <= segment.start
                    and (row.valid_to is None or row.valid_to > segment.start)
                ),
                None,
            )
            segment_cause = (change.cause_source_key, change.cause_version)
            if change.replacement_segments is not None:
                supplied = next(
                    item
                    for item in change.replacement_segments
                    if item.effective_at == segment.start
                )
                unchanged = (
                    previous is not None
                    and previous.valid_from == segment.start
                    and previous.valid_to == segment.end
                )
                permitted = (
                    (previous.cause_source_key, previous.cause_version)
                    if unchanged
                    else segment_cause
                )
                requested = (supplied.cause_source_key, supplied.cause_version)
                if requested != (None, None) and requested != permitted:
                    raise SchoolFactChangeRejected("替换段的因果来源与原段或本次明确来源不符。")
                segment_cause = permitted
            elif segment_cause == (None, None) and previous is not None:
                segment_cause = (previous.cause_source_key, previous.cause_version)
            version = EducationVersion(
                id=uuid4(),
                school_id=school_id,
                fact_id=root.id,
                fact_kind=root.kind,
                revision=current_version + 1,
                valid_from=segment.start,
                valid_to=segment.end,
                recorded_at=now,
                payload=dict(segment.payload),
                source_id=previous.source_id if previous is not None else source_id,
                import_batch_id=batch_id,
                cause_source_key=segment_cause[0],
                cause_version=segment_cause[1],
                correction_reason=change.correction_reason,
            )
            session.add(version)
            session.flush()
            self._write_education_references(session, school_id, version)
        root.version = current_version + 1
        session.flush()
        return root.id, root.version

    @classmethod
    def _education_id(
        cls,
        session: Session,
        school_id: UUID,
        kind: str,
        key: str,
        at: datetime,
        until: datetime | None = None,
    ) -> UUID:
        root = session.execute(
            select(EducationFact).where(
                EducationFact.school_id == school_id,
                EducationFact.kind == kind,
                EducationFact.source_key == key,
            )
        ).scalar_one_or_none()
        if root is None:
            raise SchoolFactChangeRejected("教育引用不存在或不属于本校。")
        periods = tuple(
            session.execute(
                select(EducationVersion.valid_from, EducationVersion.valid_to)
                .where(
                    EducationVersion.school_id == school_id,
                    EducationVersion.fact_id == root.id,
                    EducationVersion.superseded_at.is_(None),
                )
                .order_by(EducationVersion.valid_from)
            ).all()
        )
        cursor = at
        for start, end in periods:
            if end is not None and end <= cursor:
                continue
            if start > cursor:
                break
            if end is None or (until is not None and end >= until):
                return root.id
            cursor = end
        raise SchoolFactChangeRejected("教育引用未覆盖完整适用期间。")

    @classmethod
    def _write_education_references(
        cls, session: Session, school_id: UUID, version: EducationVersion
    ) -> None:
        fields = {
            **EDUCATION_REFERENCES,
            "student_source_key": "student",
            "assignment_source_key": "assignment",
            "organization_source_key": "organization",
            "campus_source_key": "location",
            "location_source_key": "location",
        }
        references = [
            (field, 0, kind, str(version.payload[field]))
            for field, kind in fields.items()
            if version.payload.get(field) is not None
        ]
        for array_field, array_kind in {
            "grade_source_keys": "education_grade",
            "stage_source_keys": "education_stage",
            "subject_source_keys": "teaching_subject",
        }.items():
            references.extend(
                (array_field, index, array_kind, str(key))
                for index, key in enumerate(version.payload.get(array_field, ()))
            )
        for field, ordinal, kind, key in references:
            if kind in EDUCATION_KINDS:
                if field == "predecessor_class_source_key":
                    target = session.execute(
                        select(EducationFact.id).where(
                            EducationFact.school_id == school_id,
                            EducationFact.kind == kind,
                            EducationFact.source_key == key,
                        )
                    ).scalar_one_or_none()
                    if target is None:
                        raise SchoolFactChangeRejected("新班来历所指原班不属于本校。")
                else:
                    target = cls._education_id(
                        session, school_id, kind, key, version.valid_from, version.valid_to
                    )
                column = "education_id"
            elif kind == "student_school_relationship":
                # Resolve through the original F3 ownership and its real valid-time rows.
                target = session.execute(
                    text(
                        "SELECT relation.id FROM student_school_relationships relation "
                        "LEFT JOIN school_fact_change_lineage lineage ON "
                        "lineage.school_id=relation.school_id AND "
                        "lineage.target_id=relation.id AND "
                        "lineage.fact_kind='student_school_relationship' "
                        "WHERE relation.school_id=:school AND relation.superseded_at IS NULL "
                        "AND COALESCE(relation.source_key,lineage.fact_source_key)=:key AND "
                        "relation.valid_from<=:at "
                        "AND (relation.valid_to IS NULL OR relation.valid_to>:at) "
                        "ORDER BY lineage.target_version DESC LIMIT 1"
                    ),
                    {
                        "school": school_id,
                        "key": key,
                        "at": version.valid_from,
                        "until": version.valid_to,
                    },
                ).scalar_one_or_none()
                if target is None:
                    raise SchoolFactChangeRejected("在校关系未覆盖归班期间。")
                target, column = UUID(str(target)), "relationship_id"
            else:
                target = cls._source_root_id(
                    session, school_id, kind, key, effective_at=version.valid_from
                )
                column = {
                    "student": "student_id",
                    "assignment": "assignment_id",
                    "organization": "organization_id",
                    "location": "location_id",
                }[kind]
                if kind == "assignment":
                    cls._require_f2_reference_interval(
                        session, school_id, kind, target, version.valid_from, version.valid_to
                    )
            session.add(
                EducationReference(
                    school_id=school_id,
                    version_id=version.id,
                    field_name=field,
                    ordinal=ordinal,
                    education_kind=kind if column == "education_id" else None,
                    **{column: target},
                )
            )

    def education_preflight_codes(self, school_id, changes) -> tuple[str, ...]:
        if not any(
            c.fact_kind in EDUCATION_KINDS
            or c.fact_kind in {"student_school_relationship", "assignment", "organization"}
            for c in changes
        ):
            return ()
        with self._request_bound_session() as session:
            try:
                self._validate_education_state(session, school_id, proposed_changes=changes)
            except (SchoolFactChangeRejected, ValueError):
                return ("education.invariants_invalid",)
        return ()

    def residence_preflight_codes(self, school_id, changes) -> tuple[str, ...]:
        if not any(
            c.fact_kind
            in {"dorm_room", "dorm_bed", "student_bed_residence", "student_school_relationship"}
            for c in changes
        ):
            return ()
        with self._request_bound_session() as session:
            try:
                return self._residence_candidate_codes(session, school_id, changes)
            except (SchoolFactChangeRejected, ValueError):
                return ("residence.timeline_requires_explicit_resolution",)

    def residence_preflight_issues(self, school_id, changes):
        details, dependencies = {}, {}
        with self._request_bound_session() as session:
            try:
                codes = self._residence_candidate_codes(
                    session, school_id, changes, issue_targets=details, dependencies=dependencies
                )
            except (SchoolFactChangeRejected, ValueError):
                return {
                    (c.fact_kind, c.source_key): (
                        "residence.timeline_requires_explicit_resolution",
                    )
                    for c in changes
                    if c.fact_kind
                    in F6_FACILITY_KINDS | {"student_bed_residence", "student_school_relationship"}
                }
        attributed = {
            target: tuple(code for code in codes if refs & details.get(code, set()))
            for target, refs in dependencies.items()
            if any(refs & details.get(code, set()) for code in codes)
        }
        assigned = {code for found in attributed.values() for code in found}
        if set(codes) - assigned:
            # Legacy rows may lack a resolvable source identity. Never turn an
            # observed conflict into a clean preview or expose that private key.
            for change in changes:
                if change.fact_kind not in {
                    "dorm_room",
                    "dorm_bed",
                    "student_bed_residence",
                    "student_school_relationship",
                }:
                    continue
                target = (change.fact_kind, change.source_key)
                attributed[target] = (
                    *attributed.get(target, ()),
                    "residence.timeline_requires_explicit_resolution",
                )
        return attributed

    @staticmethod
    def _residence_candidate_codes(
        session, school_id, changes, *, issue_targets=None, dependencies=None
    ) -> tuple[str, ...]:
        """Read only the changed students/rooms and their potential competing intervals."""
        from collections import defaultdict

        from app.domains.students.residence_changes import residence_candidate_issues

        kinds = {"dorm_room", "dorm_bed", "student_bed_residence", "student_school_relationship"}
        local = tuple(c for c in changes if c.fact_kind in kinds)
        if not local:
            return ()
        targets = {(c.fact_kind, c.source_key) for c in local}
        contents = [
            content
            for c in local
            for content in (
                tuple(s.content for s in c.replacement_segments)
                if c.replacement_segments is not None
                else (c.content,)
                if c.content is not None
                else ()
            )
        ]
        location_keys = {c.room_source_key for c in contents if c.room_source_key}
        student_keys = {c.student_source_key for c in contents if c.student_source_key}
        bed_keys = {c.bed_source_key for c in contents if c.bed_source_key} | {
            c.source_key for c in local if c.fact_kind == "dorm_bed"
        }
        selected = session.execute(
            text(
                "SELECT DISTINCT ON (fact_kind,fact_source_key) "
                "fact_kind,fact_source_key,target_id "
                "FROM school_fact_change_lineage WHERE school_id=:school AND fact_kind=ANY(:kinds) "
                "AND fact_source_key=ANY(:keys) ORDER BY "
                "fact_kind,fact_source_key,target_version DESC"
            ),
            dict(school=school_id, kinds=sorted(kinds), keys=sorted({key for _, key in targets})),
        ).all()
        ids = defaultdict(list)
        for row in selected:
            if (row.fact_kind, row.fact_source_key) in targets:
                ids[row.fact_kind].append(row.target_id)
        params = dict(
            school=school_id,
            students=sorted(student_keys),
            beds=sorted(bed_keys),
            locations=sorted(location_keys),
            ssr_ids=ids["student_school_relationship"],
            stay_ids=ids["student_bed_residence"],
            bed_ids=ids["dorm_bed"],
            room_ids=ids["dorm_room"],
        )
        student_ids = list(
            session.execute(
                text(
                    "SELECT s.id FROM students s WHERE s.school_id=:school AND "
                    "(s.source_key=ANY(:students) "
                    "OR s.id IN (SELECT student_id FROM student_school_relationships WHERE "
                    "school_id=:school "
                    "AND id=ANY(CAST(:ssr_ids AS uuid[]))) OR s.id IN (SELECT student_id FROM "
                    "student_bed_residences WHERE school_id=:school AND "
                    "id=ANY(CAST(:stay_ids AS uuid[]))))"
                ),
                params,
            ).scalars()
        )
        params["student_ids"] = student_ids
        room_ids = list(
            session.execute(
                text(
                    "SELECT l.id FROM campus_locations l WHERE l.school_id=:school AND "
                    "(l.source_key=ANY(:locations) "
                    "OR l.id IN (SELECT room_location_id FROM dorm_room_profile_versions "
                    "WHERE school_id=:school "
                    "AND id=ANY(CAST(:room_ids AS uuid[]))) OR l.id IN (SELECT "
                    "b.room_location_id FROM dorm_beds b "
                    "WHERE b.school_id=:school AND (b.source_key=ANY(:beds) OR "
                    "b.id=ANY(CAST(:bed_ids AS uuid[])) "
                    "OR b.id IN (SELECT bed_id FROM student_bed_residences WHERE school_id=:school "
                    "AND (id=ANY(CAST(:stay_ids AS uuid[])) OR "
                    "student_id=ANY(CAST(:student_ids AS uuid[])))))))"
                ),
                params,
            ).scalars()
        )
        params["affected_rooms"] = room_ids
        states = {kind: defaultdict(list) for kind in kinds}
        queries = {
            "dorm_room": (
                "SELECT p.valid_from,p.valid_to,COALESCE(p.source_key,(SELECT "
                "l.fact_source_key FROM "
                "school_fact_change_lineage l WHERE l.school_id=p.school_id AND "
                "l.fact_kind='dorm_room' "
                "AND l.target_id=p.id ORDER BY l.target_version DESC LIMIT "
                "1),'legacy:room:'||p.id::text) AS fact_key,"
                "loc.source_key AS room_source_key,p.authorized_bed_capacity,p.is_available "
                "FROM dorm_room_profile_versions p JOIN campus_locations loc ON "
                "loc.school_id=p.school_id "
                "AND loc.id=p.room_location_id WHERE p.school_id=:school AND "
                "p.superseded_at IS NULL "
                "AND p.room_location_id=ANY(CAST(:affected_rooms AS uuid[])) LIMIT 100001"
            ),
            "dorm_bed": (
                "SELECT v.valid_from,v.valid_to,b.source_key AS fact_key,loc.source_key AS "
                "room_source_key,"
                "v.availability FROM dorm_bed_versions v JOIN dorm_beds b ON "
                "b.school_id=v.school_id "
                "AND b.id=v.bed_id JOIN campus_locations loc ON loc.school_id=b.school_id "
                "AND loc.id=b.room_location_id "
                "WHERE v.school_id=:school AND v.superseded_at IS NULL "
                "AND b.room_location_id=ANY(CAST(:affected_rooms AS uuid[])) LIMIT 100001"
            ),
            "student_bed_residence": (
                "SELECT r.valid_from,r.valid_to,COALESCE((SELECT l.fact_source_key FROM "
                "school_fact_change_lineage l "
                "WHERE l.school_id=r.school_id AND l.fact_kind='student_bed_residence' AND "
                "l.target_id=r.id "
                "ORDER BY l.target_version DESC LIMIT 1),'legacy:stay:'||r.id::text) AS fact_key,"
                "s.source_key AS student_source_key,b.source_key AS bed_source_key FROM "
                "student_bed_residences r "
                "JOIN students s ON s.school_id=r.school_id AND s.id=r.student_id JOIN dorm_beds b "
                "ON b.school_id=r.school_id AND b.id=r.bed_id WHERE r.school_id=:school AND"
                " r.revoked_at IS NULL "
                "AND (r.student_id=ANY(CAST(:student_ids AS uuid[])) OR "
                "b.room_location_id=ANY(CAST(:affected_rooms AS uuid[]))) LIMIT 100001"
            ),
            "student_school_relationship": (
                "SELECT r.valid_from,r.valid_to,COALESCE(r.source_key,(SELECT "
                "l.fact_source_key FROM "
                "school_fact_change_lineage l WHERE l.school_id=r.school_id AND "
                "l.fact_kind='student_school_relationship' "
                "AND l.target_id=r.id ORDER BY l.target_version DESC LIMIT "
                "1),'legacy:ssr:'||r.id::text) AS fact_key,"
                "s.source_key AS student_source_key,r.enrollment_status,r.residence_mode "
                "FROM student_school_relationships r JOIN students s ON "
                "s.school_id=r.school_id AND s.id=r.student_id "
                "WHERE r.school_id=:school AND r.superseded_at IS NULL AND "
                "s.source_key=ANY(:students) LIMIT 100001"
            ),
        }
        for kind in (
            "dorm_room",
            "dorm_bed",
            "student_bed_residence",
            "student_school_relationship",
        ):
            if kind == "student_school_relationship":
                params["students"] = sorted(student_keys)
            rows = session.execute(text(queries[kind]), params).mappings().all()
            if len(rows) > 100000:
                raise SchoolFactChangeRejected("寄宿完整影响超过已验证容量，不能截断预检。")
            for row in rows:
                payload = {
                    key: value
                    for key, value in row.items()
                    if key not in {"fact_key", "valid_from", "valid_to"}
                }
                states[kind][row["fact_key"]].append(
                    EducationSlice(row["valid_from"], row["valid_to"], payload)
                )
                if "student_source_key" in payload:
                    student_keys.add(payload["student_source_key"])
        for change in local:
            current = tuple(states[change.fact_kind].get(change.source_key, ()))
            if dependencies is not None:
                references = {(change.fact_kind, change.source_key)}
                payloads = [row.payload for row in current]
                payloads.extend(
                    safe_content_payload(s.content) for s in change.replacement_segments or ()
                )
                if change.content is not None:
                    payloads.append(safe_content_payload(change.content))
                for payload in payloads:
                    for field, kind in (
                        ("student_source_key", "student"),
                        ("bed_source_key", "dorm_bed"),
                        ("room_source_key", "location"),
                    ):
                        if payload.get(field):
                            references.add((kind, payload[field]))
                dependencies[change.fact_kind, change.source_key] = references
            if change.fact_kind == "student_bed_residence":
                if change.operation == "revoke":
                    revised = ()
                elif change.operation == "end" and len(current) == 1:
                    old = current[0]
                    if (
                        change.effective_at <= old.start
                        or old.end is not None
                        and change.effective_at >= old.end
                    ):
                        raise ValueError("Invalid residence ending")
                    revised = (EducationSlice(old.start, change.effective_at, old.payload),)
                elif change.operation == "upsert" and not current and change.expected_version == 0:
                    revised = (
                        EducationSlice(
                            change.effective_at,
                            change.content.valid_to,
                            safe_content_payload(change.content),
                        ),
                    )
                else:
                    raise ValueError("Immutable residence requires an explicit new identity")
            elif change.replacement_segments is not None:
                revised = tuple(
                    EducationSlice(s.effective_at, s.valid_to, safe_content_payload(s.content))
                    for s in change.replacement_segments
                )
            else:
                revised = revise_timeline(
                    current,
                    operation=change.operation,
                    at=change.effective_at,
                    until=change.content.valid_to if change.content else None,
                    payload=safe_content_payload(change.content),
                    now=datetime.now(UTC),
                )
            states[change.fact_kind][change.source_key] = list(revised)
        return residence_candidate_issues(
            rooms=states["dorm_room"],
            beds=states["dorm_bed"],
            enrollments=states["student_school_relationship"],
            residences=states["student_bed_residence"],
            issue_targets=issue_targets,
        )

    @staticmethod
    def _validate_education_state(
        session: Session, school_id: UUID, *, proposed_changes=()
    ) -> None:
        """Bounded postcondition over the entire batch; errors roll back every unit."""
        rows = tuple(
            session.execute(
                select(EducationFact.kind, EducationFact.source_key, EducationVersion)
                .join(
                    EducationVersion,
                    and_(
                        EducationVersion.fact_id == EducationFact.id,
                        EducationVersion.school_id == EducationFact.school_id,
                    ),
                )
                .where(
                    EducationFact.school_id == school_id, EducationVersion.superseded_at.is_(None)
                )
                .limit(100001)
            ).all()
        )
        if len(rows) > 100000:
            raise SchoolFactChangeRejected("教育批次超出本阶段已验证容量。")
        from types import SimpleNamespace

        for change in proposed_changes:
            if change.fact_kind not in EDUCATION_KINDS:
                continue
            current = tuple(
                row
                for kind, key, row in rows
                if (kind, key) == (change.fact_kind, change.source_key)
            )
            payload = safe_content_payload(change.content)
            until = change.content.valid_to if change.content is not None else None
            if payload is not None:
                payload = {key: value for key, value in payload.items() if key != "valid_to"}
            prior = ()
            if change.operation == "cancel" and any(
                row.valid_from < change.effective_at for row in current
            ):
                if (
                    change.effective_at <= datetime.now(UTC)
                    or SqlAlchemySchoolFactPersistence._latest_lineage_operation(
                        session, school_id, change.fact_kind, change.source_key
                    )
                    != "upsert"
                    or not any(row.valid_from == change.effective_at for row in current)
                ):
                    raise SchoolFactChangeRejected("未来替代已发生独立改动。")
                prior = tuple(
                    session.execute(
                        select(EducationVersion)
                        .join(EducationFact, EducationFact.id == EducationVersion.fact_id)
                        .where(
                            EducationFact.school_id == school_id,
                            EducationFact.kind == change.fact_kind,
                            EducationFact.source_key == change.source_key,
                            EducationVersion.revision == EducationFact.version - 1,
                        )
                    ).scalars()
                )
                if not prior:
                    raise SchoolFactChangeRejected("取消缺少前态证据。")
            revised = (
                tuple(
                    EducationSlice(
                        segment.effective_at,
                        segment.valid_to,
                        safe_content_payload(segment.content) or {},
                    )
                    for segment in change.replacement_segments
                )
                if change.replacement_segments is not None
                else tuple(EducationSlice(r.valid_from, r.valid_to, r.payload) for r in prior)
                if prior
                else revise_timeline(
                    tuple(EducationSlice(r.valid_from, r.valid_to, r.payload) for r in current),
                    operation=change.operation,
                    at=change.effective_at,
                    until=until,
                    payload=payload,
                    now=datetime.now(UTC),
                )
            )
            rows = tuple(
                (kind, key, row)
                for kind, key, row in rows
                if (kind, key) != (change.fact_kind, change.source_key)
            ) + tuple(
                (
                    change.fact_kind,
                    change.source_key,
                    SimpleNamespace(valid_from=r.start, valid_to=r.end, payload=r.payload),
                )
                for r in revised
            )
        from app.domains.students.education_validation import (
            EducationRecord,
            validate_education_rules,
        )
        from app.domains.students.teaching_contracts import validate_teaching_rules

        try:
            validate_teaching_rules(
                tuple(
                    EducationRecord(kind, key, row.valid_from, row.valid_to, row.payload)
                    for kind, key, row in rows
                )
            )
            validate_education_rules(
                tuple(
                    EducationRecord(kind, key, row.valid_from, row.valid_to, row.payload)
                    for kind, key, row in rows
                )
            )
        except ValueError as exc:
            raise SchoolFactChangeRejected(str(exc)) from exc

        def overlaps(left, right):
            return (left.valid_to is None or right.valid_from < left.valid_to) and (
                right.valid_to is None or left.valid_from < right.valid_to
            )

        def covers(periods, start, end):
            cursor = start
            for lower, upper in sorted(periods, key=lambda p: p[0]):
                if upper is not None and upper <= cursor:
                    continue
                if lower > cursor:
                    return False
                if upper is None or (end is not None and upper >= end):
                    return True
                cursor = upper
            return False

        from collections import defaultdict

        educational_periods = defaultdict(list)
        for kind, key, row in rows:
            educational_periods[kind, key].append((row.valid_from, row.valid_to))
        for _kind, _key, row in rows:
            references = [
                (field, kind, row.payload.get(field))
                for field, kind in EDUCATION_REFERENCES.items()
            ]
            for array_field, array_kind in {
                "grade_source_keys": "education_grade",
                "stage_source_keys": "education_stage",
                "subject_source_keys": "teaching_subject",
            }.items():
                references.extend(
                    (array_field, array_kind, key) for key in row.payload.get(array_field, ())
                )
            for field, reference_kind, reference_key in references:
                if reference_kind not in EDUCATION_KINDS or reference_key is None:
                    continue
                periods = educational_periods[reference_kind, str(reference_key)]
                if not periods or (
                    field != "predecessor_class_source_key"
                    and not covers(periods, row.valid_from, row.valid_to)
                ):
                    raise SchoolFactChangeRejected("教育事实结束后仍有未处理的关联关系。")
        for kind, _key, row in rows:
            if kind not in {"teaching_assignment", "teaching_group", "teaching_organization_scope"}:
                continue
            reference_kind = "assignment" if kind == "teaching_assignment" else "organization"
            reference_field = reference_kind + "_source_key"
            overlays = tuple(
                c
                for c in proposed_changes
                if c.fact_kind == reference_kind and c.source_key == row.payload[reference_field]
            )
            target = (
                None
                if any(c.expected_version == 0 for c in overlays)
                else SqlAlchemySchoolFactPersistence._source_root_id(
                    session,
                    school_id,
                    reference_kind,
                    str(row.payload[reference_field]),
                    effective_at=row.valid_from,
                )
            )
            SqlAlchemySchoolFactPersistence._require_f2_reference_interval(
                session,
                school_id,
                reference_kind,
                target,
                row.valid_from,
                row.valid_to,
                overlays=overlays,
            )
        memberships = [(key, row) for kind, key, row in rows if kind == "student_class_membership"]
        placements = [
            (key, row)
            for kind, key, row in rows
            if kind in {"class_placement", "student_grade_placement"}
        ]
        teaching_memberships = [
            (key, row) for kind, key, row in rows if kind == "teaching_group_member"
        ]
        if memberships or teaching_memberships:
            school_periods = defaultdict(list)
            facts = session.execute(
                text(
                    "SELECT COALESCE(r.source_key,l.fact_source_key) AS "
                    "source_key,s.source_key AS student_key,r.valid_from,r.valid_to,"
                    "r.enrollment_status "
                    "FROM student_school_relationships r JOIN students s ON "
                    "s.id=r.student_id AND s.school_id=r.school_id "
                    "LEFT JOIN school_fact_change_lineage l ON l.target_id=r.id AND "
                    "l.school_id=r.school_id AND l.fact_kind='student_school_relationship' "
                    "WHERE r.school_id=:school AND r.superseded_at IS NULL"
                ),
                {"school": school_id},
            ).all()
            school_versions = defaultdict(list)
            for fact in facts:
                school_versions[fact.source_key, fact.student_key].append(
                    EducationSlice(
                        fact.valid_from,
                        fact.valid_to,
                        {"enrollment_status": fact.enrollment_status},
                    )
                )
            for change in proposed_changes:
                if change.fact_kind != "student_school_relationship":
                    continue
                keys = [key for key in school_versions if key[0] == change.source_key]
                payload = safe_content_payload(change.content)
                if payload is not None:
                    keys = [(change.source_key, payload["student_source_key"])]
                if len(keys) != 1:
                    raise SchoolFactChangeRejected("在校关系预检缺少唯一原事实。")
                key = keys[0]
                school_versions[key] = list(
                    tuple(
                        EducationSlice(
                            segment.effective_at,
                            segment.valid_to,
                            safe_content_payload(segment.content) or {},
                        )
                        for segment in change.replacement_segments
                    )
                    if change.replacement_segments is not None
                    else revise_timeline(
                        tuple(school_versions[key]),
                        operation=change.operation,
                        at=change.effective_at,
                        until=change.content.valid_to if change.content is not None else None,
                        payload=payload,
                        now=datetime.now(UTC),
                    )
                )
            for key, versions in school_versions.items():
                school_periods[key] = [
                    (r.start, r.end)
                    for r in versions
                    if r.payload["enrollment_status"] in {"enrolled", "suspended"}
                ]
            for _key, row in memberships + teaching_memberships:
                periods = school_periods[
                    str(row.payload["school_relationship_source_key"]),
                    str(row.payload["student_source_key"]),
                ]
                if not covers(periods, row.valid_from, row.valid_to):
                    raise SchoolFactChangeRejected(
                        "学生行政归班超出本人有效在校期间，必须同批处理。"
                    )
        for collection, identity in ((memberships, "student_source_key"), (placements, None)):
            grouped = defaultdict(list)
            for key, row in collection:
                field = identity or (
                    "class_source_key"
                    if "class_source_key" in row.payload
                    else "student_source_key"
                )
                grouped[field, str(row.payload[field])].append((key, row))
            for group in grouped.values():
                ordered = sorted(group, key=lambda p: p[1].valid_from)
                for (key, row), (other_key, other) in zip(ordered, ordered[1:], strict=False):
                    if key != other_key and overlaps(row, other):
                        raise SchoolFactChangeRejected("同一期间存在重复主要归班或教育起点。")
        for kind, _key, row in rows:
            if kind != "student_class_position":
                continue
            cursor = row.valid_from
            candidates = sorted(
                (
                    member
                    for _key, member in memberships
                    if member.payload.get("student_source_key")
                    == row.payload.get("student_source_key")
                    and member.payload.get("class_source_key")
                    == row.payload.get("class_source_key")
                    and member.payload.get("membership_status") == "assigned"
                ),
                key=lambda x: x.valid_from,
            )
            covered = False
            for member in candidates:
                if member.valid_to is not None and member.valid_to <= cursor:
                    continue
                if member.valid_from > cursor:
                    break
                if member.valid_to is None or (
                    row.valid_to is not None and member.valid_to >= row.valid_to
                ):
                    covered = True
                    break
                cursor = member.valid_to
            if not covered:
                raise SchoolFactChangeRejected("学生岗位超出本班成员期间，必须同批处理关联任职。")

    @staticmethod
    def _root_status(session: Session, fact_kind: str, target_id: UUID) -> str:
        table_name, _key_column = _ROOT_FACTS[fact_kind]
        status = session.execute(
            text(
                f"SELECT status FROM {table_name} WHERE id = :target_id"  # noqa: S608
            ),
            {"target_id": target_id},
        ).scalar_one_or_none()
        if not isinstance(status, str):
            raise SchoolFactChangeRejected("根身份事实状态不可用。")
        return status

    @staticmethod
    def _root_is_effective_at(
        session: Session,
        *,
        school_id: UUID,
        fact_kind: str,
        target_id: UUID,
        effective_at: datetime,
    ) -> bool:
        if fact_kind == "location":
            return bool(
                session.execute(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM campus_location_versions AS content "
                        "WHERE content.school_id = :school_id "
                        "AND content.location_id = :target_id "
                        "AND content.status = 'confirmed' "
                        "AND content.valid_from <= :effective_at "
                        "AND (content.valid_to IS NULL OR content.valid_to > :effective_at) "
                        "AND NOT EXISTS ("
                        "SELECT 1 FROM app.school_fact_content_version_terminal("
                        ":school_id, 'location', :target_id, content.version) AS terminal "
                        "WHERE terminal.operation = 'revoke' "
                        "OR terminal.effective_at IS NULL "
                        "OR terminal.effective_at <= :effective_at))"
                    ),
                    {
                        "school_id": school_id,
                        "target_id": target_id,
                        "effective_at": effective_at,
                    },
                ).scalar_one()
            )
        return bool(
            session.execute(
                text(
                    "SELECT app.school_fact_root_is_effective_at("
                    ":school_id, :fact_kind, :target_id, :effective_at)"
                ),
                {
                    "school_id": school_id,
                    "fact_kind": fact_kind,
                    "target_id": target_id,
                    "effective_at": effective_at,
                },
            ).scalar_one()
        )

    @staticmethod
    def _latest_lineage_operation(
        session: Session,
        school_id: UUID,
        fact_kind: str,
        source_key: str,
    ) -> str | None:
        operation = session.execute(
            text(
                "SELECT operation FROM school_fact_change_lineage "
                "WHERE school_id = :school_id AND fact_kind = :fact_kind "
                "AND fact_source_key = :source_key "
                "ORDER BY target_version DESC LIMIT 1"
            ),
            {
                "school_id": school_id,
                "fact_kind": fact_kind,
                "source_key": source_key,
            },
        ).scalar_one_or_none()
        return None if operation is None else str(operation)

    def _lock_target(
        self, session: Session, school_id: UUID, fact_kind: str, source_key: str
    ) -> tuple[UUID, int] | None:
        if fact_kind in EDUCATION_KINDS:
            row = session.execute(
                select(EducationFact.id, EducationFact.version)
                .where(
                    EducationFact.school_id == school_id,
                    EducationFact.kind == fact_kind,
                    EducationFact.source_key == source_key,
                )
                .with_for_update()
            ).one_or_none()
            return None if row is None else (row.id, row.version)
        root = _ROOT_FACTS.get(fact_kind)
        if root is not None:
            table_name, key_column = root
            school_filter = "id = :school_id" if fact_kind == "school" else "school_id = :school_id"
            row = session.execute(
                text(
                    f"SELECT id, version FROM {table_name} "  # noqa: S608
                    f"WHERE {school_filter} AND {key_column} = :source_key FOR UPDATE"
                ),
                {"school_id": school_id, "source_key": source_key},
            ).one_or_none()
            return None if row is None else (UUID(str(row[0])), int(row[1]))
        row = session.execute(
            text(
                "SELECT target_id, target_version FROM school_fact_change_lineage "
                "WHERE school_id = :school_id AND fact_kind = :fact_kind "
                "AND fact_source_key = :source_key ORDER BY target_version DESC LIMIT 1"
            ),
            {"school_id": school_id, "fact_kind": fact_kind, "source_key": source_key},
        ).one_or_none()
        return None if row is None else (UUID(str(row[0])), int(row[1]))

    def _upsert(
        self,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        content = safe_content_payload(change.content)
        if content is None:
            raise SchoolFactChangeRejected("upsert 缺少规范内容。")
        handler = (
            self._upsert_f2
            if change.fact_kind in F2_MODELS
            else getattr(self, f"_upsert_{change.fact_kind}", None)
        )
        if handler is None:
            raise SchoolFactChangeRejected("事实类别尚未登记写核。")
        return handler(
            session=session,
            school_id=school_id,
            batch_id=batch_id,
            source_id=source_id,
            change=change,
            content=content,
            current_id=current_id,
            current_version=current_version,
        )

    def _upsert_f2(
        self,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        model = F2_MODELS[change.fact_kind]
        table = cast(Table, model.__table__)
        if current_id is not None:
            previous = session.execute(
                select(table.c.valid_from, table.c.valid_to, table.c.revoked_at)
                .where(table.c.school_id == school_id, table.c.id == current_id)
                .with_for_update()
            ).one()
            if previous.revoked_at is not None or (
                previous.valid_to is not None and previous.valid_to <= change.effective_at
            ):
                raise SchoolFactChangeRejected("Terminated F2 relations require a new source key.")
            if change.effective_at <= previous.valid_from:
                raise SchoolFactChangeRejected(
                    "Replacement must begin after its preceding content version."
                )
            session.execute(
                update(table)
                .where(table.c.school_id == school_id, table.c.id == current_id)
                .values(valid_to=change.effective_at)
            )
        refs = {
            "organization_source_key": ("organization_unit_id", "organization"),
            "person_source_key": ("person_id", "person"),
            "assignment_source_key": ("assignment_id", "assignment"),
            "location_source_key": ("location_id", "location"),
            "official_contact_source_key": ("official_contact_id", "official_contact"),
            "policy_source_key": ("policy_id", "policy"),
        }
        values: dict[str, object] = {}
        assert change.content is not None
        for field in change.content.model_fields_set:
            value = getattr(change.content, field)
            if field in refs and value is not None:
                column, kind = refs[field]
                if kind == "official_contact":
                    # A service binds one channel content version over its whole window.
                    values[column], _ = self._f2_content_at(
                        session,
                        school_id,
                        kind,
                        str(value),
                        change.effective_at,
                        until=change.content.valid_to,
                        require_whole_window=True,
                    )
                    continue
                values[column] = self._source_root_id(
                    session, school_id, kind, value, effective_at=change.effective_at
                )
                if kind in {"assignment", "policy"}:
                    self._require_f2_reference_interval(
                        session,
                        school_id,
                        kind,
                        values[column],
                        change.effective_at,
                        change.content.valid_to,
                    )
            elif field in table.c:
                values[field] = (
                    value.model_dump(mode="json") if field == "coverage_basis" and value is not None
                    else value
                )
        if change.fact_kind == "fact_quality":
            if content.get("education_target_source_key") is not None:
                values["education_target_id"] = self._education_id(
                    session,
                    school_id,
                    str(content["education_target_kind"]),
                    str(content["education_target_source_key"]),
                    change.effective_at,
                    change.content.valid_to,
                )
                if content.get("position_source_key") is not None:
                    values["education_position_id"] = self._education_id(
                        session,
                        school_id,
                        "class_position_definition",
                        str(content["position_source_key"]),
                        change.effective_at,
                        change.content.valid_to,
                    )
                for field in ("target_source_key", "replacement_source_key"):
                    if content.get(field) is not None:
                        historical_target = (
                            content.get("quality_kind") == "correction"
                            and field == "target_source_key"
                        )
                        if historical_target:
                            target = session.execute(
                                select(EducationFact.id).where(
                                    EducationFact.school_id == school_id,
                                    EducationFact.kind == str(content["target_fact_kind"]),
                                    EducationFact.source_key == str(content[field]),
                                )
                            ).scalar_one_or_none()
                            if target is None or self._latest_lineage_operation(
                                session,
                                school_id,
                                str(content["target_fact_kind"]),
                                str(content[field]),
                            ) not in {"end", "revoke", "cancel", "correct"}:
                                raise SchoolFactChangeRejected(
                                    "纠正关系缺少原事实的正式结束或补正证据。"
                                )
                        else:
                            target = self._education_id(
                                session,
                                school_id,
                                str(content["target_fact_kind"]),
                                str(content[field]),
                                change.effective_at,
                                change.content.valid_to,
                            )
                        owner_key = str(content["education_target_source_key"])
                        candidate_query = select(EducationVersion.payload).where(
                            EducationVersion.fact_id == target,
                            EducationVersion.school_id == school_id,
                        )
                        if not historical_target:
                            candidate_query = candidate_query.where(
                                EducationVersion.superseded_at.is_(None),
                                EducationVersion.valid_from <= change.effective_at,
                                (
                                    EducationVersion.valid_to.is_(None)
                                    | (EducationVersion.valid_to > change.effective_at)
                                ),
                            )
                        candidate = session.execute(
                            candidate_query.order_by(
                                EducationVersion.revision.desc(), EducationVersion.valid_from.desc()
                            ).limit(1)
                        ).scalar_one()
                        if target != values["education_target_id"] and owner_key not in [
                            v for k, v in candidate.items() if k.endswith("_source_key")
                        ]:
                            raise SchoolFactChangeRejected("质量目标不属于明确教育对象。")
            else:
                self._validate_quality_targets(session, school_id, change, values)
            self._verify_complete_coverage_basis(session, school_id, change, values)
        target_id = uuid4()
        values.update(
            id=target_id,
            school_id=school_id,
            source_key=change.source_key,
            version=current_version + 1,
            valid_from=change.effective_at,
            source_id=source_id,
            import_batch_id=batch_id,
        )
        session.execute(insert(table).values(**values))
        return target_id, current_version + 1

    @staticmethod
    def _verify_complete_coverage_basis(
        session: Session,
        school_id: UUID,
        change: CanonicalFactChangeV1,
        values: Mapping[str, object],
    ) -> None:
        """Recompute the exact mother set inside the Writer's school lock.

        Unsupported family shapes fail closed. In particular an old v2/v3
        payload cannot create a new unverified complete declaration, although
        historical rows and receipts remain untouched.
        """
        content = change.content
        if (
            content is None
            or content.quality_kind != "coverage"
            or content.coverage_status != "complete"
        ):
            return
        basis = getattr(content, "coverage_basis", None)
        if basis is None or basis.school_id != school_id:
            raise SchoolFactChangeRejected("完整覆盖缺少本校受控母集核查依据。")
        current_school_version = SqlAlchemySchoolFactPersistence._current_global_fact_version(
            session, school_id
        )
        if basis.school_fact_version != current_school_version:
            raise SchoolFactChangeRejected("完整覆盖依据的学校事实版本已变化。")
        if isinstance(basis, CoverageBasisV2):
            # This one fixed F4 collection is selected by formal fields, never
            # by a supplied list or a page of the maintainer's readable rows.
            rows = session.execute(
                text(
                    "SELECT f.source_key,f.version FROM school_education_facts f "
                    "JOIN school_education_versions v ON v.school_id=f.school_id "
                    "AND v.fact_id=f.id "
                    "WHERE f.school_id=:school AND f.kind='education_grade_group' "
                    "AND v.superseded_at IS NULL AND v.valid_from<=:at "
                    "AND (v.valid_to IS NULL OR :at<v.valid_to) "
                    "AND v.payload->>'campus_source_key'=:campus "
                    "AND v.payload->>'academic_year'=:academic_year "
                    "ORDER BY f.source_key LIMIT 101"
                ),
                {
                    "school": school_id,
                    "at": change.effective_at,
                    "campus": basis.campus_source_key,
                    "academic_year": str(basis.academic_year),
                },
            ).all()
            members = tuple((str(row.source_key), int(row.version)) for row in rows)
            if tuple(key for key, _version in members) != basis.authorized_source_keys:
                raise SchoolFactChangeRejected("完整覆盖母集已变化，请重新预检。")
            # A scheduled addition, departure, or transfer in this same
            # campus/year makes a spanning complete claim invalid even before
            # the boundary arrives. Later publications invalidate by version.
            future_change = session.execute(
                text(
                    "SELECT 1 FROM school_education_facts f "
                    "JOIN school_education_versions v ON v.school_id=f.school_id "
                    "AND v.fact_id=f.id "
                    "WHERE f.school_id=:school AND f.kind='education_grade_group' "
                    "AND v.superseded_at IS NULL "
                    "AND v.payload->>'campus_source_key'=:campus "
                    "AND v.payload->>'academic_year'=:academic_year "
                    "AND ((v.valid_from>:at AND (CAST(:until AS timestamptz) IS NULL "
                    "OR v.valid_from<CAST(:until AS timestamptz))) "
                    "OR (v.valid_to>:at AND (CAST(:until AS timestamptz) IS NULL "
                    "OR v.valid_to<CAST(:until AS timestamptz)))) "
                    "LIMIT 1"
                ),
                {
                    "school": school_id,
                    "at": change.effective_at,
                    "until": basis.valid_to,
                    "campus": basis.campus_source_key,
                    "academic_year": str(basis.academic_year),
                },
            ).scalar_one_or_none()
            if future_change is not None:
                raise SchoolFactChangeRejected("完整覆盖母集已变化，请重新预检。")
        elif (
            content.target_fact_kind == "student"
            and content.owner_kind == "person"
            and content.target_source_key is not None
        ):
            version = session.execute(
                text(
                    "SELECT s.version FROM students s WHERE s.school_id=:school "
                    "AND s.source_key=:target AND s.person_id=:owner AND s.status='active' "
                    "AND app.school_fact_root_is_effective_at(:school,'student',s.id,:at)"
                ),
                {
                    "school": school_id,
                    "target": content.target_source_key,
                    "owner": values.get("person_id"),
                    "at": change.effective_at,
                },
            ).scalar_one_or_none()
            if type(version) is not int:
                raise SchoolFactChangeRejected("完整覆盖目标在该时点不可核实。")
            members = ((content.target_source_key, version),)
        elif (
            content.target_fact_kind in F7_FAMILY_QUALITY_TARGET_KINDS - {"student"}
            and content.owner_kind == "person"
            and content.target_source_key is not None
        ):
            target_id = SqlAlchemySchoolFactPersistence._f3_quality_target_at(
                session, school_id, content.target_fact_kind,
                content.target_source_key, change.effective_at,
            )
            # Each fixed query mirrors one formal F3 target slice. A quality
            # statement concerns this one authorized fact, never all children,
            # contacts, or mappings owned by the person.
            exact_sql = {
                "student_school_relationship": (
                    "SELECT t.version FROM student_school_relationships t JOIN students s "
                    "ON s.school_id=t.school_id AND s.id=t.student_id "
                    "WHERE t.school_id=:school AND t.id=:target_id "
                    "AND s.person_id=:owner AND t.superseded_at IS NULL "
                    "AND t.valid_from<=:at AND (t.valid_to IS NULL OR :at<t.valid_to)"
                ),
                "login_mapping": (
                    "SELECT t.target_version FROM account_person_binding_versions t "
                    "WHERE t.school_id=:school AND t.binding_id=:target_id "
                    "AND t.person_id=:owner AND t.status='confirmed' "
                    "AND t.revoked_at IS NULL AND t.effective_from<=:at "
                    "AND (t.effective_to IS NULL OR :at<t.effective_to)"
                ),
                "student_contact": (
                    "SELECT t.version FROM student_contact_relationships t JOIN students s "
                    "ON s.school_id=t.school_id AND s.id=t.student_id "
                    "WHERE t.school_id=:school AND t.id=:target_id "
                    "AND s.person_id=:owner AND t.valid_from<=:at "
                    "AND (t.valid_to IS NULL OR :at<t.valid_to)"
                ),
                "guardian_relationship": (
                    "SELECT t.version FROM guardian_relationships t JOIN students s "
                    "ON s.school_id=t.school_id AND s.id=t.student_id "
                    "WHERE t.school_id=:school AND t.id=:target_id "
                    "AND s.person_id=:owner AND t.authority_version=1 "
                    "AND t.revoked_at IS NULL AND t.valid_from<=:at "
                    "AND (t.valid_to IS NULL OR :at<t.valid_to)"
                ),
                "person_region": (
                    "SELECT t.version FROM person_regions t "
                    "WHERE t.school_id=:school AND t.id=:target_id "
                    "AND t.person_id=:owner AND t.valid_from<=:at "
                    "AND (t.valid_to IS NULL OR :at<t.valid_to)"
                ),
            }
            version = session.execute(
                text(exact_sql[content.target_fact_kind]),
                {
                    "school": school_id,
                    "target_id": target_id,
                    "owner": values.get("person_id"),
                    "at": change.effective_at,
                },
            ).scalar_one_or_none()
            if type(version) is not int:
                raise SchoolFactChangeRejected("完整覆盖目标在该时点不可核实。")
            members = ((content.target_source_key, version),)
        elif (
            content.target_fact_kind in F2_QUALITY_TARGET_KINDS
            and content.target_source_key is not None
        ):
            _, version = SqlAlchemySchoolFactPersistence._f2_content_at(
                session,
                school_id,
                content.target_fact_kind,
                content.target_source_key,
                change.effective_at,
            )
            members = ((content.target_source_key, version),)
        elif (
            content.target_fact_kind in EDUCATION_KINDS
            and content.owner_kind == "education"
            and content.target_source_key is not None
        ):
            if values.get("education_target_id") is None:
                raise SchoolFactChangeRejected("完整覆盖缺少教育归属对象。")
            version = session.execute(
                text(
                    "SELECT f.version FROM school_education_facts f "
                    "WHERE f.school_id=:school AND f.kind=:kind AND f.source_key=:target "
                    "AND EXISTS (SELECT 1 FROM school_education_versions v "
                    "WHERE v.school_id=f.school_id AND v.fact_id=f.id "
                    "AND v.fact_kind=f.kind AND v.superseded_at IS NULL "
                    "AND v.valid_from<=:at AND (v.valid_to IS NULL OR :at<v.valid_to))"
                ),
                {
                    "school": school_id,
                    "kind": content.target_fact_kind,
                    "target": content.target_source_key,
                    "at": change.effective_at,
                },
            ).scalar_one_or_none()
            if type(version) is not int:
                raise SchoolFactChangeRejected("完整覆盖目标在该时点不可核实。")
            members = ((content.target_source_key, version),)
        elif (
            content.target_fact_kind in F6_FACILITY_KINDS | {"student_bed_residence"}
            and content.target_source_key is not None
        ):
            # Mirror the four fixed F6 Reader target selections. A stable key
            # and one active formal version are required; a page or a parent
            # object's rows are never evidence for an exact-target claim.
            exact_sql = {
                "dormitory_building_profile": (
                    "SELECT v.version FROM dormitory_building_profile_versions v "
                    "WHERE v.school_id=:school AND v.source_key=:target "
                    "AND v.superseded_at IS NULL AND v.valid_from<=:at "
                    "AND (v.valid_to IS NULL OR :at<v.valid_to)"
                ),
                "dorm_room": (
                    "SELECT v.version FROM dorm_room_profile_versions v "
                    "WHERE v.school_id=:school AND (v.source_key=:target OR "
                    "(v.source_key IS NULL AND :target=(SELECT l.fact_source_key "
                    "FROM school_fact_change_lineage l JOIN master_data_import_batches b "
                    "ON b.school_id=l.school_id AND b.id=l.batch_id AND b.status='applied' "
                    "WHERE l.school_id=v.school_id AND l.fact_kind='dorm_room' "
                    "AND l.target_id=v.id ORDER BY l.target_version DESC LIMIT 1))) "
                    "AND v.superseded_at IS NULL AND v.valid_from<=:at "
                    "AND (v.valid_to IS NULL OR :at<v.valid_to)"
                ),
                "dorm_bed": (
                    "SELECT v.version FROM dorm_beds b JOIN dorm_bed_versions v "
                    "ON v.school_id=b.school_id AND v.bed_id=b.id "
                    "WHERE b.school_id=:school AND b.source_key=:target "
                    "AND v.superseded_at IS NULL AND v.valid_from<=:at "
                    "AND (v.valid_to IS NULL OR :at<v.valid_to)"
                ),
                "student_bed_residence": (
                    "SELECT r.version FROM student_bed_residences r "
                    "WHERE r.school_id=:school AND r.revoked_at IS NULL "
                    "AND r.valid_from<=:at AND (r.valid_to IS NULL OR :at<r.valid_to) "
                    "AND EXISTS (SELECT 1 FROM school_fact_change_lineage l "
                    "JOIN master_data_import_batches b "
                    "ON b.school_id=l.school_id AND b.id=l.batch_id AND b.status='applied' "
                    "WHERE l.school_id=r.school_id AND l.fact_kind='student_bed_residence' "
                    "AND l.fact_source_key=:target AND l.target_id=r.id)"
                ),
            }
            version = session.execute(
                text(exact_sql[content.target_fact_kind]),
                {
                    "school": school_id,
                    "target": content.target_source_key,
                    "at": change.effective_at,
                },
            ).scalar_one_or_none()
            if type(version) is not int:
                raise SchoolFactChangeRejected("完整覆盖目标在该时点不可核实。")
            members = ((content.target_source_key, version),)
        elif (
            content.target_fact_kind in {"organization_location", "organization_membership"}
            and content.owner_kind == "organization"
            and content.target_source_key is None
            and (
                content.target_fact_kind == "organization_membership"
                and content.purpose is None
                or content.target_fact_kind == "organization_location"
                and content.purpose in {"office", "reception"}
            )
        ):
            table = (
                "organization_memberships"
                if content.target_fact_kind == "organization_membership"
                else "organization_locations"
            )
            purpose_filter = "" if table == "organization_memberships" else "AND purpose=:purpose "
            rows = session.execute(
                text(
                    f"SELECT source_key,version FROM {table} "  # noqa: S608 - fixed allowlist
                    "WHERE school_id=:school AND organization_unit_id=:owner "
                    f"{purpose_filter}AND valid_from<=:at "  # noqa: S608 - fixed fragment
                    "AND (valid_to IS NULL OR :at<valid_to) "
                    "AND (revoked_at IS NULL OR :at<revoked_at) "
                    "ORDER BY source_key,version LIMIT 50001"
                ),
                {
                    "school": school_id,
                    "owner": values.get("organization_unit_id"),
                    "purpose": content.purpose,
                    "at": change.effective_at,
                },
            ).all()
            members = tuple((str(row.source_key), int(row.version)) for row in rows)
        else:
            raise SchoolFactChangeRejected("该事实族尚无可核实的完整母集合同。")
        try:
            count, digest = mother_set_digest(members)
        except ValueError as exc:
            raise SchoolFactChangeRejected("完整覆盖母集范围不可核实。") from exc
        if basis.member_count != count or basis.member_sha256 != digest:
            raise SchoolFactChangeRejected("完整覆盖母集已变化，请重新预检。")

    @staticmethod
    def _f2_content_at(
        session: Session,
        school_id: UUID,
        kind: str,
        key: str,
        at: datetime,
        *,
        until: datetime | None = None,
        require_whole_window: bool = False,
    ) -> tuple[UUID, int]:
        """Resolve a unique usable content version at the declared factual boundary."""
        definitions = {
            "school": ("school_versions", "school_id", "version", "valid_from", "valid_to"),
            "person": (
                "school_person_profile_versions",
                "person_id",
                "version",
                "valid_from",
                "valid_to",
            ),
            "organization": (
                "organization_unit_versions",
                "organization_unit_id",
                "version",
                "valid_from",
                "valid_to",
            ),
            "location": (
                "campus_location_versions",
                "location_id",
                "version",
                "valid_from",
                "valid_to",
            ),
            "assignment": (
                "staff_assignment_versions",
                "assignment_id",
                "target_version",
                "effective_from",
                "effective_to",
            ),
            "organization_campus_relationship": (
                "organization_campus_relationships",
                "id",
                "version",
                "valid_from",
                "valid_to",
            ),
            "location_alias": (
                "campus_location_aliases",
                "id",
                "version",
                "valid_from",
                "valid_to",
            ),
            "person_contact": ("person_contact_points", "id", "version", "valid_from", "valid_to"),
            "official_contact": (
                "official_contact_points",
                "id",
                "version",
                "valid_from",
                "valid_to",
            ),
            "policy_scope": ("school_policy_scopes", "id", "version", "valid_from", "valid_to"),
            "policy_version": (
                "school_policy_versions",
                "id",
                "version",
                "effective_from",
                "effective_to",
            ),
            "policy": ("school_policies", "id", "version", "created_at", None),
        }
        if kind in F2_MODELS:
            definition = (F2_MODELS[kind].__tablename__, "id", "version", "valid_from", "valid_to")
        else:
            definition = definitions.get(kind)
        if definition is None:
            raise SchoolFactChangeRejected("Quality target kind has no temporal contract.")
        table_name, root, version, start, end = definition
        conditions = [f"f.{start} <= :at"]
        if kind == "policy":
            # Stable identity begins at its first declared publication time;
            # later no-content upserts cannot reset that lifetime.
            conditions = [
                "l.target_version=(SELECT min(initial.target_version) FROM "
                "school_fact_change_lineage initial WHERE initial.school_id=l.school_id "
                "AND initial.fact_kind='policy' AND initial.target_id=l.target_id "
                "AND initial.operation='upsert')"
            ]
        if end is not None:
            conditions.append(f"(f.{end} IS NULL OR f.{end} > :at)")
            if require_whole_window:
                conditions.append(
                    f"(f.{end} IS NULL OR (CAST(:until AS timestamptz) IS NOT NULL "
                    f"AND f.{end} >= :until))"
                )
        if kind in F2_MODELS or kind in {"assignment", "policy_version"}:
            conditions.append("(f.revoked_at IS NULL OR f.revoked_at > :at)")
        if kind in {"school", "person", "official_contact"}:
            conditions.append("f.lifecycle_status='active'")
        if kind == "assignment":
            conditions.extend(
                ("f.status='confirmed'", "(f.planned_valid_to IS NULL OR f.planned_valid_to > :at)")
            )
        if kind == "policy_version":
            conditions.extend(("f.valid_from <= :at", "(f.valid_to IS NULL OR f.valid_to > :at)"))
            if require_whole_window:
                conditions.extend(
                    f"(f.{bound} IS NULL OR (CAST(:until AS timestamptz) IS NOT NULL "
                    f"AND f.{bound} >= :until))"
                    for bound in ("valid_to", "revoked_at")
                )
        # Policy records retain their unique upsert identity even when legacy
        # terminal commands advanced the physical row's version counter.
        version_join = (
            "" if kind in {"policy", "policy_version"} else f" AND f.{version}=l.target_version"
        )
        terminal_boundary = (
            "(CAST(:until AS timestamptz) IS NULL OR "
            "(tu.payload->>'effective_at')::timestamptz < :until)"
            if require_whole_window
            else "(tu.payload->>'effective_at')::timestamptz <= :at"
        )
        rows = session.execute(
            text(
                "SELECT l.target_id, l.target_version FROM school_fact_change_lineage l "
                "JOIN master_data_import_units u ON u.school_id=l.school_id AND u.id=l.unit_id "
                f"JOIN {table_name} f ON f.school_id=l.school_id "
                f"AND f.{root}=l.target_id{version_join} "
                "WHERE l.school_id=:school AND l.fact_kind=:kind AND l.fact_source_key=:key "
                "AND l.operation IN ('upsert','reactivate') "
                "AND (u.payload->>'effective_at')::timestamptz <= :at AND "
                + " AND ".join(conditions)
                + " AND NOT EXISTS (SELECT 1 FROM school_fact_change_lineage t "
                "JOIN master_data_import_units tu ON tu.school_id=t.school_id AND tu.id=t.unit_id "
                "WHERE t.school_id=l.school_id AND t.fact_kind=l.fact_kind "
                "AND t.fact_source_key=l.fact_source_key AND t.target_version>l.target_version "
                "AND t.operation IN ('end','revoke') AND " + terminal_boundary + ") LIMIT 2"
            ),
            {"school": school_id, "kind": kind, "key": key, "at": at, "until": until},
        ).all()
        if len(rows) != 1:
            raise SchoolFactChangeRejected(
                "F2 reference is unavailable or ambiguous at the requested time."
            )
        return UUID(str(rows[0].target_id)), int(rows[0].target_version)

    @staticmethod
    def _require_f2_reference_interval(
        session: Session,
        school_id: UUID,
        kind: str,
        target_id: object,
        at: datetime,
        until: datetime | None,
        *,
        overlays=(),
    ) -> None:
        if kind in {"organization", "assignment"}:
            if kind == "organization":
                query = (
                    "SELECT v.valid_from, LEAST(v.valid_to, (SELECT min(t.effective_at) "
                    "FROM app.school_fact_content_version_terminal("
                    ":school,'organization',:id,v.version) t "
                    "WHERE t.operation IN ('end','revoke'))) AS valid_to "
                    "FROM organization_unit_versions v WHERE v.school_id=:school "
                    "AND v.organization_unit_id=:id AND v.status='confirmed' "
                    "AND NOT EXISTS (SELECT 1 FROM app.school_fact_content_version_terminal("
                    ":school,'organization',:id,v.version) t WHERE t.operation='revoke') "
                    "ORDER BY v.valid_from"
                )
            else:
                query = (
                    "SELECT v.effective_from AS valid_from, "
                    "LEAST(v.effective_to,v.planned_valid_to,(SELECT min(t.effective_at) "
                    "FROM app.school_fact_content_version_terminal("
                    ":school,'assignment',:id,v.target_version) t "
                    "WHERE t.operation IN ('end','revoke'))) AS valid_to "
                    "FROM staff_assignment_versions v WHERE v.school_id=:school "
                    "AND v.assignment_id=:id AND v.status='confirmed' AND v.revoked_at IS NULL "
                    "AND NOT EXISTS (SELECT 1 FROM app.school_fact_content_version_terminal("
                    ":school,'assignment',:id,v.target_version) t WHERE t.operation='revoke') "
                    "ORDER BY v.effective_from"
                )
            periods = (
                list(session.execute(text(query), {"school": school_id, "id": target_id}).all())
                if target_id is not None
                else []
            )
            for change in overlays:
                boundary = change.effective_at
                if change.operation == "revoke":
                    periods = []
                elif change.operation in {"end", "upsert", "reactivate"}:
                    periods = [
                        (lower, boundary if upper is None or upper > boundary else upper)
                        for lower, upper in periods
                        if lower < boundary
                    ]
                    if change.operation != "end" and change.content is not None:
                        periods.append((boundary, change.content.valid_to))
                else:
                    raise SchoolFactChangeRejected("教学身份引用不支持此种同批时间调整。")
            periods.sort(key=lambda period: period[0])
            cursor = at
            for lower, upper in periods:
                if upper is not None and upper <= cursor:
                    continue
                if lower > cursor:
                    break
                if upper is None or (until is not None and upper >= until):
                    return
                cursor = upper
            raise SchoolFactChangeRejected("教学身份引用未覆盖完整有效期间。")
        if kind == "official_contact":
            query = (
                "SELECT valid_from, valid_to FROM official_contact_points WHERE "
                "school_id=:school AND id=:id AND lifecycle_status='active' AND "
                "valid_from <= :at AND (valid_to IS NULL OR valid_to > :at)"
            )
        else:
            query = (
                "SELECT initial.valid_from, initial.valid_to FROM ("
                "SELECT (u.payload->>'effective_at')::timestamptz AS valid_from, "
                "NULL::timestamptz AS valid_to FROM school_policies p "
                "JOIN school_fact_change_lineage l ON l.school_id=p.school_id "
                "AND l.target_id=p.id AND l.fact_kind='policy' AND l.operation='upsert' "
                "JOIN master_data_import_units u ON u.school_id=l.school_id AND u.id=l.unit_id "
                "WHERE p.school_id=:school AND p.id=:id "
                "ORDER BY l.target_version LIMIT 1) initial WHERE initial.valid_from <= :at"
            )
        row = session.execute(
            text(query), {"school": school_id, "id": target_id, "at": at}
        ).one_or_none()
        if row is None or (row.valid_to is not None and (until is None or until > row.valid_to)):
            raise SchoolFactChangeRejected(
                "F2 reference is unavailable for the requested interval."
            )

    @staticmethod
    def _require_f3_quality_owner(
        session: Session,
        school_id: UUID,
        target_kind: str,
        target_id: UUID,
        person_id: UUID,
    ) -> None:
        # A family quality statement is scoped to the actual student's person,
        # or to the mapped person for a login mapping/region. Static SQL is used
        # before Writer accepts a target; a caller cannot choose a table/column.
        queries = {
            "student": "SELECT t.person_id FROM students t WHERE t.school_id=:school AND t.id=:id",
            "student_school_relationship": (
                "SELECT s.person_id FROM student_school_relationships t JOIN students s "
                "ON s.school_id=t.school_id AND s.id=t.student_id "
                "WHERE t.school_id=:school AND t.id=:id"
            ),
            "login_mapping": (
                "SELECT t.person_id FROM account_person_bindings t "
                "WHERE t.school_id=:school AND t.id=:id"
            ),
            "student_contact": (
                "SELECT s.person_id FROM student_contact_relationships t JOIN students s "
                "ON s.school_id=t.school_id AND s.id=t.student_id "
                "WHERE t.school_id=:school AND t.id=:id"
            ),
            "guardian_relationship": (
                "SELECT s.person_id FROM guardian_relationships t JOIN students s "
                "ON s.school_id=t.school_id AND s.id=t.student_id "
                "WHERE t.school_id=:school AND t.id=:id"
            ),
            "person_region": (
                "SELECT t.person_id FROM person_regions t "
                "WHERE t.school_id=:school AND t.id=:id"
            ),
        }
        if target_kind not in F7_FAMILY_QUALITY_TARGET_KINDS:
            raise ValueError("Unregistered family quality target.")
        actual_owner = session.execute(
            text(queries[target_kind]), {"school": school_id, "id": target_id}
        ).scalar_one_or_none()
        if actual_owner != person_id:
            raise SchoolFactChangeRejected("Quality scope does not own the referenced fact.")

    @staticmethod
    def _f3_quality_target_at(
        session: Session,
        school_id: UUID,
        kind: str,
        key: str,
        at: datetime,
    ) -> UUID:
        """Resolve a replacement from durable F3 event history at the correction time."""
        if kind not in F7_FAMILY_QUALITY_TARGET_KINDS:
            raise SchoolFactChangeRejected("Unregistered F3 quality target.")
        rows = session.execute(
            text(
                "SELECT l.target_id FROM school_fact_change_lineage l "
                "JOIN master_data_import_units u ON u.school_id=l.school_id "
                "AND u.id=l.unit_id "
                "WHERE l.school_id=:school AND l.fact_kind=:kind "
                "AND l.fact_source_key=:key AND l.operation IN ('upsert','reactivate') "
                "AND (u.payload->>'effective_at')::timestamptz<=:at "
                "AND NOT EXISTS (SELECT 1 FROM school_fact_change_lineage t "
                "JOIN master_data_import_units tu ON tu.school_id=t.school_id "
                "AND tu.id=t.unit_id WHERE t.school_id=l.school_id "
                "AND t.fact_kind=l.fact_kind AND t.fact_source_key=l.fact_source_key "
                "AND t.target_version>l.target_version "
                "AND t.operation IN ('end','revoke') "
                "AND (tu.payload->>'effective_at')::timestamptz<=:at) "
                "ORDER BY l.target_version DESC LIMIT 1"
            ),
            {"school": school_id, "kind": kind, "key": key, "at": at},
        ).all()
        if len(rows) != 1:
            raise SchoolFactChangeRejected("F3 correction replacement is unavailable at this time.")
        return UUID(str(rows[0].target_id))

    @staticmethod
    def _validate_quality_targets(
        session: Session, school_id: UUID, change: CanonicalFactChangeV1, values: dict[str, object]
    ) -> None:
        # Candidate evidence is not a competing authoritative fact. Only explicit stable
        # target coordinates are resolved; corrections also require a terminated old fact.
        for field in ("target_source_key", "replacement_source_key"):
            key = values.get(field)
            if key is None:
                continue
            correction = values.get("quality_kind") == "correction"
            temporal_join = (
                " JOIN master_data_import_units u ON u.school_id=l.school_id AND u.id=l.unit_id "
                if correction
                else " "
            )
            temporal_filter = (
                " AND (u.payload->>'effective_at')::timestamptz <= :at " if correction else " "
            )
            row = session.execute(
                text(
                    "SELECT l.target_id, l.operation FROM school_fact_change_lineage l"
                    + temporal_join
                    + "WHERE l.school_id=:school AND l.fact_kind=:kind AND l.fact_source_key=:key"
                    + temporal_filter
                    + "ORDER BY l.target_version DESC LIMIT 1"
                ),
                {
                    "school": school_id,
                    "kind": values["target_fact_kind"],
                    "key": key,
                    "at": change.effective_at,
                },
            ).one_or_none()
            if row is None:
                raise SchoolFactChangeRejected("Quality target does not exist in this school.")
            target_id = row.target_id
            if correction:
                if field == "target_source_key":
                    if row.operation not in {"end", "revoke"}:
                        raise SchoolFactChangeRejected(
                            "Correction precedes the original fact's terminal boundary."
                        )
                elif values["target_fact_kind"] in F6_FACILITY_KINDS | {"student_bed_residence"}:
                    SqlAlchemySchoolFactPersistence._require_residence_quality_interval(
                        session,
                        school_id,
                        str(values["target_fact_kind"]),
                        target_id,
                        str(key),
                        change.effective_at,
                        change.content.valid_to,
                    )
                elif values["target_fact_kind"] in F7_FAMILY_QUALITY_TARGET_KINDS:
                    target_id = SqlAlchemySchoolFactPersistence._f3_quality_target_at(
                        session,
                        school_id,
                        str(values["target_fact_kind"]),
                        str(key),
                        change.effective_at,
                    )
                else:
                    target_id, _ = SqlAlchemySchoolFactPersistence._f2_content_at(
                        session,
                        school_id,
                        str(values["target_fact_kind"]),
                        str(key),
                        change.effective_at,
                    )
            owner = str(values["owner_kind"])
            if owner != "school":
                if values["target_fact_kind"] in F7_FAMILY_QUALITY_TARGET_KINDS:
                    if owner != "person" or values.get("person_id") is None:
                        raise SchoolFactChangeRejected(
                            "F3 quality requires the exact person owner."
                        )
                    SqlAlchemySchoolFactPersistence._require_f3_quality_owner(
                        session,
                        school_id,
                        str(values["target_fact_kind"]),
                        target_id,
                        values["person_id"],
                    )
                    continue
                if values["target_fact_kind"] == "student_bed_residence":
                    if owner != "person":
                        raise SchoolFactChangeRejected(
                            "Residence quality must identify its student."
                        )
                    actual_owner = session.execute(
                        text(
                            "SELECT s.person_id FROM student_bed_residences r JOIN students s "
                            "ON s.school_id=r.school_id AND s.id=r.student_id "
                            "WHERE r.school_id=:school AND r.id=:id"
                        ),
                        {"school": school_id, "id": target_id},
                    ).scalar_one_or_none()
                    if actual_owner != values.get("person_id"):
                        raise SchoolFactChangeRejected(
                            "Quality scope does not own the referenced fact."
                        )
                    continue
                columns_by_kind = {
                    "person": ("school_people", {"person": "id"}),
                    "organization": ("organization_units", {"organization": "id"}),
                    "location": ("campus_locations", {"location": "id"}),
                    "assignment": (
                        "staff_assignments",
                        {"person": "person_id", "organization": "organization_unit_id"},
                    ),
                    "organization_campus_relationship": (
                        "organization_campus_relationships",
                        {"organization": "organization_unit_id", "location": "campus_location_id"},
                    ),
                    "location_alias": ("campus_location_aliases", {"location": "location_id"}),
                    "person_contact": ("person_contact_points", {"person": "person_id"}),
                    "official_contact": (
                        "official_contact_points",
                        {"organization": "organization_unit_id", "location": "location_id"},
                    ),
                    "policy_scope": (
                        "school_policy_scopes",
                        {"organization": "organization_unit_id", "location": "campus_location_id"},
                    ),
                    "policy_version": (
                        "school_policy_versions",
                        {"organization": "publishing_organization_unit_id"},
                    ),
                    "organization_profile": (
                        "organization_profiles",
                        {"organization": "organization_unit_id"},
                    ),
                    "organization_membership": (
                        "organization_memberships",
                        {"person": "person_id", "organization": "organization_unit_id"},
                    ),
                    "organization_location": (
                        "organization_locations",
                        {"organization": "organization_unit_id", "location": "location_id"},
                    ),
                    "person_workplace": (
                        "person_workplaces",
                        {"person": "person_id", "location": "location_id"},
                    ),
                    "school_service": ("school_services", {"organization": "organization_unit_id"}),
                    "dormitory_building_profile": (
                        "dormitory_building_profile_versions",
                        {"location": "location_id"},
                    ),
                    "dorm_room": ("dorm_room_profile_versions", {"location": "room_location_id"}),
                    "dorm_bed": ("dorm_beds", {"location": "room_location_id"}),
                }
                table_name, owner_columns = columns_by_kind.get(
                    str(values["target_fact_kind"]), ("", {})
                )
                column = owner_columns.get(owner)
                if column is None:
                    raise SchoolFactChangeRejected(
                        "Quality scope does not own the referenced fact."
                    )
                actual_owner = session.execute(
                    text(
                        f"SELECT {column} FROM {table_name} WHERE school_id=:school AND id=:id"  # noqa: S608 - fixed allowlist
                    ),
                    {"school": school_id, "id": target_id},
                ).scalar_one_or_none()
                expected_owner = values.get(
                    {
                        "person": "person_id",
                        "organization": "organization_unit_id",
                        "location": "location_id",
                    }[owner]
                )
                if actual_owner != expected_owner:
                    raise SchoolFactChangeRejected(
                        "Quality scope does not own the referenced fact."
                    )

    @staticmethod
    def _require_residence_quality_interval(
        session, school_id, kind, target_id, source_key, at, until
    ):
        tables = {
            "dormitory_building_profile": (
                "dormitory_building_profile_versions",
                "source_key=:key AND superseded_at IS NULL",
            ),
            "dorm_room": (
                "dorm_room_profile_versions",
                "(source_key=:key OR id=:id) AND superseded_at IS NULL",
            ),
            "dorm_bed": ("dorm_bed_versions", "bed_id=:id AND superseded_at IS NULL"),
            "student_bed_residence": ("student_bed_residences", "id=:id AND revoked_at IS NULL"),
        }
        table, condition = tables[kind]
        covered = session.execute(
            text(
                "SELECT coalesce(range_agg(tstzrange(valid_from,coalesce(valid_to,"
                "'infinity'::timestamptz),'[)')) @> tstzrange(:at,coalesce(:until,"
                "'infinity'::timestamptz),'[)'),false) FROM "
                f"{table} WHERE school_id=:school AND {condition}"  # noqa: S608 - fixed allowlist
            ),
            dict(school=school_id, id=target_id, key=source_key, at=at, until=until),
        ).scalar_one()
        if not covered:
            raise SchoolFactChangeRejected(
                "Replacement residence fact does not cover the requested interval."
            )

    @staticmethod
    def _upsert_school(
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        if current_id != school_id:
            raise SchoolFactChangeRejected("学校稳定来源键不属于当前学校。")
        session.execute(
            text(
                "UPDATE school_versions SET valid_to = :effective_at "
                "WHERE school_id = :school_id AND valid_to IS NULL"
            ),
            {"school_id": school_id, "effective_at": change.effective_at},
        )
        version_id = uuid4()
        session.execute(
            text(
                "INSERT INTO school_versions "
                "(id, school_id, business_code, display_name, short_name, timezone_name, "
                "lifecycle_status, valid_from, valid_to, version, source_id, import_batch_id) "
                "VALUES (:id, :school_id, :business_code, :display_name, :short_name, "
                ":timezone_name, :lifecycle_status, :effective_at, NULL, :version, "
                ":source_id, :batch_id)"
            ),
            {
                "id": version_id,
                "school_id": school_id,
                "business_code": content["business_code"],
                "display_name": content["display_name"],
                "short_name": content.get("short_name"),
                "timezone_name": content["timezone_name"],
                "lifecycle_status": content["lifecycle_status"],
                "effective_at": change.effective_at,
                "version": current_version + 1,
                "source_id": source_id,
                "batch_id": batch_id,
            },
        )
        session.execute(
            text(
                "UPDATE schools SET school_code = :business_code, display_name = :display_name, "
                "timezone_name = :timezone_name, status = :status, version = :version "
                "WHERE id = :school_id"
            ),
            {
                "school_id": school_id,
                "business_code": content["business_code"],
                "display_name": content["display_name"],
                "timezone_name": content["timezone_name"],
                "status": content["lifecycle_status"],
                "version": current_version + 1,
            },
        )
        return school_id, current_version + 1

    @staticmethod
    def _upsert_person(
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        person_id = current_id or uuid4()
        next_version = current_version + 1
        if current_id is None:
            session.execute(
                text(
                    "INSERT INTO school_people "
                    "(id, school_id, source_key, status, version) "
                    "VALUES (:id, :school_id, :source_key, :status, :version)"
                ),
                {
                    "id": person_id,
                    "school_id": school_id,
                    "source_key": change.source_key,
                    "status": content["lifecycle_status"],
                    "version": next_version,
                },
            )
        else:
            session.execute(
                text(
                    "UPDATE school_people SET status = :status, version = :version WHERE id = :id"
                ),
                {
                    "id": person_id,
                    "status": content["lifecycle_status"],
                    "version": next_version,
                },
            )
            session.execute(
                text(
                    "UPDATE school_person_names SET valid_to = :effective_at "
                    "WHERE school_id = :school_id AND person_id = :person_id "
                    "AND valid_to IS NULL"
                ),
                {
                    "school_id": school_id,
                    "person_id": person_id,
                    "effective_at": change.effective_at,
                },
            )
            session.execute(
                text(
                    "UPDATE school_person_profile_versions SET valid_to = :effective_at "
                    "WHERE school_id = :school_id AND person_id = :person_id "
                    "AND valid_to IS NULL"
                ),
                {
                    "school_id": school_id,
                    "person_id": person_id,
                    "effective_at": change.effective_at,
                },
            )
        session.execute(
            text(
                "INSERT INTO school_person_names "
                "(id, school_id, person_id, display_name, valid_from, valid_to, "
                "source_kind, source_ref, status, version) VALUES "
                "(:id, :school_id, :person_id, :display_name, :effective_at, NULL, "
                "'manual', :source_ref, 'confirmed', :version)"
            ),
            {
                "id": uuid4(),
                "school_id": school_id,
                "person_id": person_id,
                "display_name": content["display_name"],
                "effective_at": change.effective_at,
                "source_ref": f"change-set:{batch_id}",
                "version": next_version,
            },
        )
        session.execute(
            text(
                "INSERT INTO school_person_profile_versions "
                "(id, school_id, person_id, business_code, person_kind, lifecycle_status, "
                "valid_from, valid_to, version, source_id, import_batch_id) VALUES "
                "(:id, :school_id, :person_id, :business_code, :person_kind, "
                ":lifecycle_status, :effective_at, NULL, :version, :source_id, :batch_id)"
            ),
            {
                "id": uuid4(),
                "school_id": school_id,
                "person_id": person_id,
                "business_code": content.get("business_code"),
                "person_kind": content["person_kind"],
                "lifecycle_status": content["lifecycle_status"],
                "effective_at": change.effective_at,
                "version": next_version,
                "source_id": source_id,
                "batch_id": batch_id,
            },
        )
        return person_id, next_version

    @staticmethod
    def _source_root_id(
        session: Session,
        school_id: UUID,
        fact_kind: str,
        source_key: object,
        *,
        effective_at: datetime,
    ) -> UUID:
        if not isinstance(source_key, str):
            raise SchoolFactChangeRejected("事实引用缺少稳定来源键。")
        table_name = _SOURCE_ROOT_TABLES.get(fact_kind)
        if fact_kind == "policy_version":
            # A superseded edition may already be expired. Bind its historical
            # record as known at this canonical time, never a future revision.
            row = session.execute(
                text(
                    "SELECT f.id FROM school_policy_versions f "
                    "JOIN school_fact_change_lineage l ON l.school_id=f.school_id "
                    "AND l.target_id=f.id "
                    "WHERE l.school_id=:school_id AND l.fact_kind='policy_version' "
                    "AND l.fact_source_key=:source_key AND l.operation='upsert' "
                    "AND f.valid_from<=:effective_at "
                    "ORDER BY f.valid_from DESC, f.version DESC LIMIT 1"
                ),
                {"school_id": school_id, "source_key": source_key, "effective_at": effective_at},
            ).scalar_one_or_none()
        elif table_name is None:
            row = session.execute(
                text(
                    "SELECT target_id FROM school_fact_change_lineage "
                    "WHERE school_id = :school_id AND fact_kind = :fact_kind "
                    "AND fact_source_key = :source_key "
                    "ORDER BY target_version DESC LIMIT 1"
                ),
                {
                    "school_id": school_id,
                    "fact_kind": fact_kind,
                    "source_key": source_key,
                },
            ).scalar_one_or_none()
        else:
            school_filter = "id = :school_id" if fact_kind == "school" else "school_id = :school_id"
            temporal_filter = (
                " AND app.school_fact_root_is_effective_at("
                ":school_id, :fact_kind, id, :effective_at)"
                if fact_kind in _ROOT_FACTS
                else ""
            )
            row = session.execute(
                text(
                    f"SELECT id FROM {table_name} "  # noqa: S608 - fixed allowlist
                    f"WHERE {school_filter} AND source_key = :source_key"  # noqa: S608
                    f"{temporal_filter}"  # noqa: S608 - fixed fragments
                ),
                {
                    "school_id": school_id,
                    "fact_kind": fact_kind,
                    "source_key": source_key,
                    "effective_at": effective_at,
                },
            ).scalar_one_or_none()
        if row is None:
            raise SchoolFactChangeRejected("事实引用不存在。")
        return UUID(str(row))

    @classmethod
    def _upsert_student(
        cls,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        del batch_id, source_id
        person_id = cls._source_root_id(
            session,
            school_id,
            "person",
            content["person_source_key"],
            effective_at=change.effective_at,
        )
        student_id = current_id or uuid4()
        next_version = current_version + 1
        if current_id is None:
            session.execute(
                text(
                    "INSERT INTO students (id, school_id, person_id, source_key, status, version) "
                    "VALUES (:id, :school_id, :person_id, :source_key, :status, :version)"
                ),
                {
                    "id": student_id,
                    "school_id": school_id,
                    "person_id": person_id,
                    "source_key": change.source_key,
                    "status": content["status"],
                    "version": next_version,
                },
            )
        else:
            current_person_id = session.execute(
                text("SELECT person_id FROM students WHERE id = :id"),
                {"id": student_id},
            ).scalar_one()
            if UUID(str(current_person_id)) != person_id:
                raise SchoolFactChangeRejected("学生稳定身份不得改绑到另一人员。")
            session.execute(
                text("UPDATE students SET status = :status, version = :version WHERE id = :id"),
                {
                    "id": student_id,
                    "status": content["status"],
                    "version": next_version,
                },
            )
        return student_id, next_version

    @classmethod
    def _correct_student_school_relationship(
        cls,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        """Version the original source timeline for a correction or ordinary replacement."""
        from app.domains.students.models import StudentSchoolRelationship

        if current_id is None or (change.content is None and change.replacement_segments is None):
            raise SchoolFactChangeRejected("待补正在校事实不存在。")
        old = tuple(
            session.execute(
                select(StudentSchoolRelationship)
                .where(
                    StudentSchoolRelationship.school_id == school_id,
                    (
                        (StudentSchoolRelationship.source_key == change.source_key)
                        | (StudentSchoolRelationship.id == current_id)
                    ),
                    StudentSchoolRelationship.superseded_at.is_(None),
                )
                .order_by(StudentSchoolRelationship.valid_from)
                .with_for_update()
            ).scalars()
        )
        columns = (
            "student_id",
            "student_number",
            "enrollment_status",
            "residence_mode",
            "primary_campus_location_id",
            "administrative_campus_location_id",
        )

        def resolve_replacement(content, at):
            replacement = {
                field: getattr(content, field)
                for field in ("student_number", "enrollment_status", "residence_mode")
            }
            for field, key, kind in (
                ("student_id", "student_source_key", "student"),
                ("primary_campus_location_id", "primary_campus_source_key", "location"),
                (
                    "administrative_campus_location_id",
                    "administrative_campus_source_key",
                    "location",
                ),
            ):
                replacement[field] = cls._source_root_id(
                    session, school_id, kind, getattr(content, key), effective_at=at
                )
            if not old or any(row.student_id != replacement["student_id"] for row in old):
                raise SchoolFactChangeRejected("在校补正不能改绑学生。")
            return replacement

        if not old:
            raise SchoolFactChangeRejected("已清空的在校安排不能通过补正自动恢复。")
        now = datetime.now(UTC)
        slices = tuple(
            EducationSlice(
                row.valid_from, row.valid_to, {field: getattr(row, field) for field in columns}
            )
            for row in old
        )
        try:
            revised = (
                tuple(
                    EducationSlice(
                        item.effective_at,
                        item.valid_to,
                        resolve_replacement(item.content, item.effective_at),
                    )
                    for item in change.replacement_segments
                )
                if change.replacement_segments is not None
                else revise_timeline(
                    slices,
                    operation=change.operation,
                    at=change.effective_at,
                    until=change.content.valid_to,
                    payload=resolve_replacement(change.content, change.effective_at),
                    now=now,
                )
            )
        except ValueError as exc:
            raise SchoolFactChangeRejected(str(exc)) from exc
        for row in old:
            row.superseded_at = now
        session.flush()
        target = None
        for segment in revised:
            previous = next(
                (
                    row
                    for row in old
                    if all(getattr(row, k) == v for k, v in segment.payload.items())
                ),
                None,
            )
            row = StudentSchoolRelationship(
                id=uuid4(),
                school_id=school_id,
                source_key=change.source_key,
                version=current_version + 1,
                valid_from=segment.start,
                valid_to=segment.end,
                source_id=previous.source_id if previous else source_id,
                import_batch_id=batch_id,
                **dict(segment.payload),
            )
            session.add(row)
            if target is None or segment.start == change.effective_at:
                target = row.id
        if not revised:
            # A system-superseded audit target retains CAS identity for an explicitly empty
            # original-owner timeline; it is never a current enrollment-state segment.
            tombstone = StudentSchoolRelationship(
                id=uuid4(),
                school_id=school_id,
                source_key=change.source_key,
                version=current_version + 1,
                valid_from=old[-1].valid_from,
                valid_to=old[-1].valid_to,
                superseded_at=now,
                source_id=source_id,
                import_batch_id=batch_id,
                **{field: getattr(old[-1], field) for field in columns},
            )
            session.add(tombstone)
            target = tombstone.id
        session.flush()
        if target is None:
            raise SchoolFactChangeRejected("在校补正未产生明确目标段。")
        return target, current_version + 1

    @classmethod
    def _upsert_student_school_relationship(
        cls,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        if current_id is not None:
            # The latest lineage target may be a bounded middle segment after correction.
            # Revision is scoped to the stable source, never to that one physical segment.
            return cls._correct_student_school_relationship(
                session=session,
                school_id=school_id,
                batch_id=batch_id,
                source_id=source_id,
                change=change,
                current_id=current_id,
                current_version=current_version,
            )
        relationship_id = uuid4()
        next_version = current_version + 1
        student_id = cls._source_root_id(
            session,
            school_id,
            "student",
            content["student_source_key"],
            effective_at=change.effective_at,
        )
        primary_campus_id = cls._source_root_id(
            session,
            school_id,
            "location",
            content["primary_campus_source_key"],
            effective_at=change.effective_at,
        )
        administrative_campus_id = cls._source_root_id(
            session,
            school_id,
            "location",
            content["administrative_campus_source_key"],
            effective_at=change.effective_at,
        )
        session.execute(
            text(
                "INSERT INTO student_school_relationships "
                "(id, school_id, source_key, student_id, student_number, "
                "enrollment_status, residence_mode, "
                "primary_campus_location_id, administrative_campus_location_id, "
                "valid_from, valid_to, version, source_id, import_batch_id) VALUES "
                "(:id, :school_id, :source_key, :student_id, :student_number, :enrollment_status, "
                ":residence_mode, :primary_campus_id, :administrative_campus_id, "
                ":effective_at, :valid_to, :version, :source_id, :batch_id)"
            ),
            {
                "id": relationship_id,
                "school_id": school_id,
                "source_key": change.source_key,
                "valid_to": content.get("valid_to"),
                "student_id": student_id,
                "student_number": content["student_number"],
                "enrollment_status": content["enrollment_status"],
                "residence_mode": content["residence_mode"],
                "primary_campus_id": primary_campus_id,
                "administrative_campus_id": administrative_campus_id,
                "effective_at": change.effective_at,
                "version": next_version,
                "source_id": source_id,
                "batch_id": batch_id,
            },
        )
        return relationship_id, next_version

    def _upsert_login_mapping(
        self,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        if current_id is not None:
            if self._latest_lineage_operation(
                session,
                school_id,
                "login_mapping",
                change.source_key,
            ) in {"end", "revoke"}:
                raise SchoolFactChangeRejected("已终止的登录映射不能被同来源键普通 upsert 重开。")
            active = session.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM account_person_binding_versions "
                    "WHERE school_id = :school_id AND binding_id = :id "
                    "AND effective_from <= :effective_at "
                    "AND (effective_to IS NULL OR effective_to > :effective_at) "
                    "AND revoked_at IS NULL AND status = 'confirmed') "
                    "FROM account_person_bindings WHERE id = :id FOR UPDATE"
                ),
                {
                    "school_id": school_id,
                    "id": current_id,
                    "effective_at": change.effective_at,
                },
            ).scalar_one()
            if not active:
                raise SchoolFactChangeRejected("已终止的登录映射不能被同来源键普通 upsert 重开。")
        person_id = self._source_root_id(
            session,
            school_id,
            "person",
            content["person_source_key"],
            effective_at=change.effective_at,
        )
        user_id = self._identity_tuple_validator.lock_existing_user_id(
            session,
            provider_code=cast(str, content["provider_code"]),
            provider_tenant_key=cast(str, content["provider_tenant_key"]),
            external_subject=cast(str, content["external_subject"]),
        )
        if user_id is None:
            raise SchoolFactChangeRejected("登录缓存账号尚不存在，不能建立正式登录映射。")
        binding_id = current_id or uuid4()
        next_version = current_version + 1
        if current_id is None:
            session.execute(
                text(
                    "INSERT INTO account_person_bindings "
                    "(id, school_id, source_key, account_user_id, person_id, valid_from, "
                    "valid_to, status, version) VALUES "
                    "(:id, :school_id, :source_key, :user_id, :person_id, :effective_at, "
                    "NULL, :status, :version)"
                ),
                {
                    "id": binding_id,
                    "school_id": school_id,
                    "source_key": change.source_key,
                    "user_id": user_id,
                    "person_id": person_id,
                    "effective_at": change.effective_at,
                    "status": content["status"],
                    "version": next_version,
                },
            )
        else:
            session.execute(
                text(
                    "UPDATE account_person_bindings SET account_user_id = :user_id, "
                    "person_id = :person_id, status = :status, "
                    "version = version + 1 WHERE id = :id"
                ),
                {
                    "id": binding_id,
                    "user_id": user_id,
                    "person_id": person_id,
                    "effective_at": change.effective_at,
                    "status": content["status"],
                    "version": next_version,
                },
            )
        next_effective_from = session.execute(
            text(
                "SELECT min(effective_from) FROM account_person_binding_versions "
                "WHERE school_id = :school_id AND binding_id = :id "
                "AND effective_from > :effective_at"
            ),
            {
                "school_id": school_id,
                "id": binding_id,
                "effective_at": change.effective_at,
            },
        ).scalar_one()
        if current_id is not None:
            session.execute(
                text(
                    "UPDATE account_person_binding_versions SET effective_to = :effective_at, "
                    "updated_at = now() WHERE school_id = :school_id AND binding_id = :id "
                    "AND effective_from < :effective_at "
                    "AND (effective_to IS NULL OR effective_to > :effective_at)"
                ),
                {
                    "school_id": school_id,
                    "id": binding_id,
                    "effective_at": change.effective_at,
                },
            )
        session.execute(
            text(
                "INSERT INTO account_person_binding_versions "
                "(id, school_id, binding_id, account_user_id, person_id, provider_code, "
                "provider_tenant_key, external_subject, effective_from, "
                "effective_to, revoked_at, status, target_version, source_ref, source_id, "
                "import_batch_id) VALUES "
                "(:version_id, :school_id, :id, :user_id, :person_id, :provider_code, "
                ":provider_tenant_key, :external_subject, :effective_at, "
                ":effective_to, NULL, :status, :version, :source_ref, :source_id, :batch_id)"
            ),
            {
                "version_id": uuid4(),
                "school_id": school_id,
                "id": binding_id,
                "user_id": user_id,
                "person_id": person_id,
                "effective_at": change.effective_at,
                "effective_to": next_effective_from,
                "version": next_version,
                "source_ref": f"change-set:{batch_id}",
                "source_id": source_id,
                "batch_id": batch_id,
                **content,
            },
        )
        return binding_id, next_version

    @classmethod
    def _upsert_organization(
        cls,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        del source_id
        parent_id = (
            cls._source_root_id(
                session,
                school_id,
                "organization",
                content["parent_source_key"],
                effective_at=change.effective_at,
            )
            if content.get("parent_source_key") is not None
            else None
        )
        unit_id = current_id or uuid4()
        next_version = current_version + 1
        if current_id is None:
            session.execute(
                text(
                    "INSERT INTO organization_units "
                    "(id, school_id, source_key, parent_organization_unit_id, kind, "
                    "status, version) "
                    "VALUES (:id, :school_id, :source_key, :parent_id, :kind, :status, :version)"
                ),
                {
                    "id": unit_id,
                    "school_id": school_id,
                    "source_key": change.source_key,
                    "parent_id": parent_id,
                    "kind": content["kind"],
                    "status": content["status"],
                    "version": next_version,
                },
            )
        else:
            session.execute(
                text(
                    "UPDATE organization_units SET parent_organization_unit_id = :parent_id, "
                    "kind = :kind, status = :status, version = :version WHERE id = :id"
                ),
                {
                    "id": unit_id,
                    "parent_id": parent_id,
                    "kind": content["kind"],
                    "status": content["status"],
                    "version": next_version,
                },
            )
            session.execute(
                text(
                    "UPDATE organization_unit_versions SET valid_to = :effective_at "
                    "WHERE school_id = :school_id AND organization_unit_id = :id "
                    "AND valid_to IS NULL"
                ),
                {"school_id": school_id, "id": unit_id, "effective_at": change.effective_at},
            )
        session.execute(
            text(
                "INSERT INTO organization_unit_versions "
                "(id, school_id, organization_unit_id, parent_organization_unit_id, "
                "business_code, display_name, short_name, display_path, valid_from, valid_to, "
                "source_kind, source_ref, status, version) "
                "VALUES (:version_id, :school_id, :id, :parent_id, :business_code, "
                ":display_name, :short_name, :display_path, :effective_at, NULL, 'manual', "
                ":source_ref, 'confirmed', :version)"
            ),
            {
                "version_id": uuid4(),
                "school_id": school_id,
                "id": unit_id,
                "parent_id": parent_id,
                "business_code": content.get("business_code"),
                "display_name": content["display_name"],
                "short_name": content.get("short_name"),
                "display_path": content["display_path"],
                "effective_at": change.effective_at,
                "source_ref": f"change-set:{batch_id}",
                "version": next_version,
            },
        )
        return unit_id, next_version

    @classmethod
    def _upsert_assignment(
        cls,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        if current_id is not None:
            if cls._latest_lineage_operation(
                session,
                school_id,
                "assignment",
                change.source_key,
            ) in {"end", "revoke"}:
                raise SchoolFactChangeRejected("已终止的任职不能被同来源键普通 upsert 重开。")
            existing = session.execute(
                text(
                    "SELECT version.planned_valid_to "
                    "FROM staff_assignments AS stable "
                    "JOIN staff_assignment_versions AS version "
                    "ON version.school_id = stable.school_id "
                    "AND version.assignment_id = stable.id "
                    "WHERE stable.school_id = :school_id AND stable.id = :id "
                    "AND version.effective_from <= :effective_at "
                    "AND (version.effective_to IS NULL "
                    "OR version.effective_to > :effective_at) "
                    "AND (version.planned_valid_to IS NULL "
                    "OR version.planned_valid_to > :effective_at) "
                    "AND version.revoked_at IS NULL AND version.status = 'confirmed' "
                    "ORDER BY version.effective_from DESC LIMIT 1 FOR UPDATE OF stable"
                ),
                {
                    "school_id": school_id,
                    "id": current_id,
                    "effective_at": change.effective_at,
                },
            ).one_or_none()
            if existing is None:
                raise SchoolFactChangeRejected("已终止的任职不能被同来源键普通 upsert 重开。")
            existing_valid_to = existing.planned_valid_to
            if existing_valid_to is not None and content.get("valid_to") is None:
                raise SchoolFactChangeRejected("普通 upsert 不能清除任职的既有计划到期日。")
        assignment_id = current_id or uuid4()
        next_version = current_version + 1
        params = {
            "id": assignment_id,
            "school_id": school_id,
            "source_key": change.source_key,
            "person_id": cls._source_root_id(
                session,
                school_id,
                "person",
                content["person_source_key"],
                effective_at=change.effective_at,
            ),
            "organization_id": cls._source_root_id(
                session,
                school_id,
                "organization",
                content["organization_source_key"],
                effective_at=change.effective_at,
            ),
            "effective_at": change.effective_at,
            "valid_to": content.get("valid_to"),
            "version": next_version,
            **content,
        }
        if current_id is None:
            session.execute(
                text(
                    "INSERT INTO staff_assignments "
                    "(id, school_id, source_key, person_id, organization_unit_id, "
                    "position_title, semantic_role_code, assignment_kind, appointment_source, "
                    "approval_reference, valid_from, valid_to, revoked_at, status, version) "
                    "VALUES (:id, :school_id, :source_key, :person_id, :organization_id, "
                    ":position_title, :semantic_role_code, :assignment_kind, :appointment_source, "
                    ":approval_reference, :effective_at, :valid_to, NULL, :status, :version)"
                ),
                params,
            )
        else:
            session.execute(
                text(
                    "UPDATE staff_assignments SET person_id = :person_id, "
                    "organization_unit_id = :organization_id, position_title = :position_title, "
                    "semantic_role_code = :semantic_role_code, assignment_kind = :assignment_kind, "
                    "appointment_source = :appointment_source, "
                    "approval_reference = :approval_reference, "
                    "valid_to = :valid_to, "
                    "status = :status, version = version + 1 WHERE id = :id"
                ),
                params,
            )
        next_effective_from = session.execute(
            text(
                "SELECT min(effective_from) FROM staff_assignment_versions "
                "WHERE school_id = :school_id AND assignment_id = :id "
                "AND effective_from > :effective_at"
            ),
            {
                "school_id": school_id,
                "id": assignment_id,
                "effective_at": change.effective_at,
            },
        ).scalar_one()
        if current_id is not None:
            session.execute(
                text(
                    "UPDATE staff_assignment_versions SET effective_to = :effective_at, "
                    "updated_at = now() WHERE school_id = :school_id AND assignment_id = :id "
                    "AND effective_from < :effective_at "
                    "AND (effective_to IS NULL OR effective_to > :effective_at)"
                ),
                {
                    "school_id": school_id,
                    "id": assignment_id,
                    "effective_at": change.effective_at,
                },
            )
        session.execute(
            text(
                "INSERT INTO staff_assignment_versions "
                "(id, school_id, assignment_id, person_id, organization_unit_id, "
                "position_title, semantic_role_code, assignment_kind, appointment_source, "
                "approval_reference, effective_from, effective_to, planned_valid_to, "
                "revoked_at, status, target_version, source_ref, source_id, import_batch_id) "
                "VALUES (:version_id, :school_id, :id, :person_id, :organization_id, "
                ":position_title, :semantic_role_code, :assignment_kind, :appointment_source, "
                ":approval_reference, :effective_at, :effective_to, :valid_to, NULL, :status, "
                ":version, :source_ref, :source_id, :batch_id)"
            ),
            {
                **params,
                "version_id": uuid4(),
                "effective_to": next_effective_from,
                "source_ref": f"change-set:{batch_id}",
                "source_id": source_id,
                "batch_id": batch_id,
            },
        )
        return assignment_id, next_version

    @classmethod
    def _upsert_organization_campus_relationship(
        cls,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        if current_id is not None:
            if cls._latest_lineage_operation(
                session,
                school_id,
                "organization_campus_relationship",
                change.source_key,
            ) in {"end", "revoke"}:
                raise SchoolFactChangeRejected(
                    "已终止的组织校区关系不能被同来源键普通 upsert 重开。"
                )
            active = session.execute(
                text(
                    "SELECT valid_to IS NULL OR valid_to > :effective_at "
                    "FROM organization_campus_relationships "
                    "WHERE id = :id FOR UPDATE"
                ),
                {"id": current_id, "effective_at": change.effective_at},
            ).scalar_one()
            if not active:
                raise SchoolFactChangeRejected("已终止的组织校区关系不能被普通 upsert 重开。")
            session.execute(
                text(
                    "UPDATE organization_campus_relationships SET valid_to = :effective_at "
                    "WHERE id = :id"
                ),
                {"id": current_id, "effective_at": change.effective_at},
            )
        target_id = uuid4()
        next_version = current_version + 1
        params = {
            "id": target_id,
            "school_id": school_id,
            "source_key": change.source_key,
            "organization_id": cls._source_root_id(
                session,
                school_id,
                "organization",
                content["organization_source_key"],
                effective_at=change.effective_at,
            ),
            "campus_id": cls._source_root_id(
                session,
                school_id,
                "location",
                content["campus_source_key"],
                effective_at=change.effective_at,
            ),
            "effective_at": change.effective_at,
            "valid_to": content.get("valid_to"),
            "version": next_version,
            "source_id": source_id,
            "batch_id": batch_id,
        }
        session.execute(
            text(
                "INSERT INTO organization_campus_relationships "
                "(id, school_id, source_key, organization_unit_id, campus_location_id, "
                "valid_from, valid_to, version, source_id, import_batch_id) VALUES "
                "(:id, :school_id, :source_key, :organization_id, :campus_id, "
                ":effective_at, :valid_to, :version, :source_id, :batch_id)"
            ),
            params,
        )
        return target_id, next_version

    @classmethod
    def _upsert_location(
        cls,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        del source_id
        parent_id = (
            cls._source_root_id(
                session,
                school_id,
                "location",
                content["parent_source_key"],
                effective_at=change.effective_at,
            )
            if content.get("parent_source_key") is not None
            else None
        )
        location_id = current_id or uuid4()
        next_version = current_version + 1
        if current_id is None:
            session.execute(
                text(
                    "INSERT INTO campus_locations "
                    "(id, school_id, source_key, parent_location_id, kind, status, version) "
                    "VALUES (:id, :school_id, :source_key, :parent_id, :kind, :status, :version)"
                ),
                {
                    "id": location_id,
                    "school_id": school_id,
                    "source_key": change.source_key,
                    "parent_id": parent_id,
                    "kind": content["kind"],
                    "status": content["status"],
                    "version": next_version,
                },
            )
        else:
            session.execute(
                text(
                    "UPDATE campus_locations SET parent_location_id = :parent_id, kind = :kind, "
                    "status = :status, version = :version WHERE id = :id"
                ),
                {
                    "id": location_id,
                    "parent_id": parent_id,
                    "kind": content["kind"],
                    "status": content["status"],
                    "version": next_version,
                },
            )
            session.execute(
                text(
                    "UPDATE campus_location_versions SET valid_to = :effective_at "
                    "WHERE school_id = :school_id AND location_id = :id AND valid_to IS NULL"
                ),
                {"school_id": school_id, "id": location_id, "effective_at": change.effective_at},
            )
        session.execute(
            text(
                "INSERT INTO campus_location_versions "
                "(id, school_id, location_id, parent_location_id, kind, business_code, "
                "display_name, aliases, display_path, valid_from, valid_to, source_kind, "
                "source_ref, status, version) VALUES "
                "(:version_id, :school_id, :id, :parent_id, :kind, :business_code, "
                ":display_name, :aliases, :display_path, :effective_at, NULL, 'manual', "
                ":source_ref, 'confirmed', :version)"
            ),
            {
                "version_id": uuid4(),
                "school_id": school_id,
                "id": location_id,
                "parent_id": parent_id,
                "kind": content["kind"],
                "business_code": content.get("business_code"),
                "display_name": content["display_name"],
                "aliases": cls._string_list(content.get("aliases", ())),
                "display_path": content["display_path"],
                "effective_at": change.effective_at,
                "source_ref": f"change-set:{batch_id}",
                "version": next_version,
            },
        )
        return location_id, next_version

    @classmethod
    def _upsert_location_alias(
        cls,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        if current_id is not None:
            session.execute(
                text(
                    "UPDATE campus_location_aliases SET valid_to = :effective_at "
                    "WHERE school_id = :school_id AND id = :id AND valid_to IS NULL"
                ),
                {"school_id": school_id, "id": current_id, "effective_at": change.effective_at},
            )
        target_id = uuid4()
        next_version = current_version + 1
        session.execute(
            text(
                "INSERT INTO campus_location_aliases "
                "(id, school_id, location_id, alias, alias_kind, valid_from, valid_to, version, "
                "source_id, import_batch_id) VALUES "
                "(:id, :school_id, :location_id, :alias, :alias_kind, :effective_at, NULL, "
                ":version, :source_id, :batch_id)"
            ),
            {
                "id": target_id,
                "school_id": school_id,
                "location_id": cls._source_root_id(
                    session,
                    school_id,
                    "location",
                    content["location_source_key"],
                    effective_at=change.effective_at,
                ),
                "alias": content["alias"],
                "alias_kind": content["alias_kind"],
                "effective_at": change.effective_at,
                "version": next_version,
                "source_id": source_id,
                "batch_id": batch_id,
            },
        )
        return target_id, next_version

    @classmethod
    def _apply_residence_facility(
        cls,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        from app.domains.campus.models import (
            DormBed,
            DormBedVersion,
            DormitoryBuildingProfileVersion,
            DormRoomProfileVersion,
        )

        kind = change.fact_kind
        model = {
            "dorm_room": DormRoomProfileVersion,
            "dorm_bed": DormBedVersion,
            "dormitory_building_profile": DormitoryBuildingProfileVersion,
        }[kind]
        identity_field = (
            "bed_id"
            if kind == "dorm_bed"
            else ("room_location_id" if kind == "dorm_room" else "location_id")
        )
        fields = {
            "dorm_room": (
                "authorized_bed_capacity",
                "accommodation_zone",
                "is_available",
                "accessibility_tags",
            ),
            "dorm_bed": ("business_code", "display_name", "availability"),
            "dormitory_building_profile": (
                "usage_kind",
                "display_name",
                "description",
                "is_available",
            ),
        }[kind]
        condition = (
            model.bed_id == current_id
            if kind == "dorm_bed"
            else ((model.source_key == change.source_key) | (model.id == current_id))
        )
        old = (
            tuple(
                session.execute(
                    select(model)
                    .where(model.school_id == school_id, condition, model.superseded_at.is_(None))
                    .order_by(model.valid_from)
                    .with_for_update()
                ).scalars()
            )
            if current_id
            else ()
        )
        if current_id and not old:
            raise SchoolFactChangeRejected("已清空设施事实不能隐式恢复，请新建明确事实。")
        if not current_id and change.operation != "upsert":
            raise SchoolFactChangeRejected("待纠正或结束的设施事实不存在。")
        bed_id = current_id if kind == "dorm_bed" else None

        def resolve(content, at):
            nonlocal bed_id
            location_key = (
                content.location_source_key
                if kind == "dormitory_building_profile"
                else content.room_source_key
            )
            location_id = cls._source_root_id(
                session, school_id, "location", location_key, effective_at=at
            )
            if kind == "dorm_bed":
                if bed_id is None:
                    bed_id = uuid4()
                    session.add(
                        DormBed(
                            id=bed_id,
                            school_id=school_id,
                            room_location_id=location_id,
                            source_key=change.source_key,
                        )
                    )
                    session.flush()
                elif (
                    session.execute(
                        select(DormBed.room_location_id).where(
                            DormBed.school_id == school_id, DormBed.id == bed_id
                        )
                    ).scalar_one()
                    != location_id
                ):
                    raise SchoolFactChangeRejected("床位稳定身份不能改写所属房间。")
                root_id = bed_id
            else:
                root_id = location_id
            if old and any(getattr(row, identity_field) != root_id for row in old):
                raise SchoolFactChangeRejected("设施完整纠正不能改绑稳定地点。")
            payload = {field: getattr(content, field) for field in fields}
            if kind == "dorm_room":
                payload["accessibility_tags"] = list(content.accessibility_tags or ())
            payload[identity_field] = root_id
            return payload

        now = datetime.now(UTC)
        slices = tuple(
            EducationSlice(
                row.valid_from,
                row.valid_to,
                {field: getattr(row, field) for field in (*fields, identity_field)},
            )
            for row in old
        )
        try:
            revised = (
                tuple(
                    EducationSlice(
                        segment.effective_at,
                        segment.valid_to,
                        resolve(segment.content, segment.effective_at),
                    )
                    for segment in change.replacement_segments
                )
                if change.replacement_segments is not None
                else revise_timeline(
                    slices,
                    operation=change.operation,
                    at=change.effective_at,
                    until=change.content.valid_to if change.content else None,
                    payload=resolve(change.content, change.effective_at)
                    if change.content
                    else None,
                    now=now,
                )
            )
        except ValueError as exc:
            raise SchoolFactChangeRejected(str(exc)) from exc
        for row in old:
            row.superseded_at = now
        session.flush()
        target = None
        for segment in revised:
            previous = next(
                (
                    row
                    for row in old
                    if row.valid_from <= segment.start
                    and (row.valid_to is None or segment.start < row.valid_to)
                    and all(getattr(row, k) == v for k, v in segment.payload.items())
                ),
                None,
            )
            extra = {} if kind == "dorm_bed" else {"source_key": change.source_key}
            row = model(
                id=uuid4(),
                school_id=school_id,
                version=current_version + 1,
                valid_from=segment.start,
                valid_to=segment.end,
                source_id=previous.source_id if previous else source_id,
                import_batch_id=batch_id,
                **extra,
                **dict(segment.payload),
            )
            session.add(row)
            if target is None or segment.start == change.effective_at:
                target = row.id
        if not revised:
            previous = old[-1]
            row = model(
                id=uuid4(),
                school_id=school_id,
                version=current_version + 1,
                valid_from=previous.valid_from,
                valid_to=previous.valid_to,
                superseded_at=now,
                source_id=source_id,
                import_batch_id=batch_id,
                **({} if kind == "dorm_bed" else {"source_key": change.source_key}),
                **{field: getattr(previous, field) for field in (*fields, identity_field)},
            )
            session.add(row)
            target = row.id
        session.flush()
        return (bed_id if kind == "dorm_bed" else target), current_version + 1

    @classmethod
    def _upsert_dorm_room(
        cls,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        if current_id is not None:
            session.execute(
                text(
                    "UPDATE dorm_room_profile_versions SET valid_to = :effective_at "
                    "WHERE school_id = :school_id AND id = :id AND valid_to IS NULL"
                ),
                {"school_id": school_id, "id": current_id, "effective_at": change.effective_at},
            )
        target_id = uuid4()
        next_version = current_version + 1
        session.execute(
            text(
                "INSERT INTO dorm_room_profile_versions "
                "(id, school_id, room_location_id, authorized_bed_capacity, accommodation_zone, "
                "is_available, accessibility_tags, valid_from, valid_to, version, source_id, "
                "import_batch_id) VALUES "
                "(:id, :school_id, :room_id, :capacity, :zone, :is_available, :tags, "
                ":effective_at, NULL, :version, :source_id, :batch_id)"
            ),
            {
                "id": target_id,
                "school_id": school_id,
                "room_id": cls._source_root_id(
                    session,
                    school_id,
                    "location",
                    content["room_source_key"],
                    effective_at=change.effective_at,
                ),
                "capacity": content["authorized_bed_capacity"],
                "zone": content["accommodation_zone"],
                "is_available": content["is_available"],
                "tags": cls._string_list(content.get("accessibility_tags", ())),
                "effective_at": change.effective_at,
                "version": next_version,
                "source_id": source_id,
                "batch_id": batch_id,
            },
        )
        return target_id, next_version

    @classmethod
    def _upsert_dorm_bed(
        cls,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        bed_id = current_id or uuid4()
        room_id = cls._source_root_id(
            session,
            school_id,
            "location",
            content["room_source_key"],
            effective_at=change.effective_at,
        )
        next_version = current_version + 1
        if current_id is None:
            session.execute(
                text(
                    "INSERT INTO dorm_beds (id, school_id, room_location_id, source_key) "
                    "VALUES (:id, :school_id, :room_id, :source_key)"
                ),
                {
                    "id": bed_id,
                    "school_id": school_id,
                    "room_id": room_id,
                    "source_key": change.source_key,
                },
            )
        else:
            current_room = session.execute(
                text("SELECT room_location_id FROM dorm_beds WHERE id = :id"), {"id": bed_id}
            ).scalar_one()
            if UUID(str(current_room)) != room_id:
                raise SchoolFactChangeRejected("床位稳定身份不能改写所属房间。")
            session.execute(
                text(
                    "UPDATE dorm_bed_versions SET valid_to = :effective_at "
                    "WHERE school_id = :school_id AND bed_id = :id AND valid_to IS NULL"
                ),
                {"school_id": school_id, "id": bed_id, "effective_at": change.effective_at},
            )
        session.execute(
            text(
                "INSERT INTO dorm_bed_versions "
                "(id, school_id, bed_id, business_code, display_name, availability, valid_from, "
                "valid_to, version, source_id, import_batch_id) VALUES "
                "(:version_id, :school_id, :id, :business_code, :display_name, :availability, "
                ":effective_at, NULL, :version, :source_id, :batch_id)"
            ),
            {
                "version_id": uuid4(),
                "school_id": school_id,
                "id": bed_id,
                "business_code": content["business_code"],
                "display_name": content["display_name"],
                "availability": content["availability"],
                "effective_at": change.effective_at,
                "version": next_version,
                "source_id": source_id,
                "batch_id": batch_id,
            },
        )
        return bed_id, next_version

    @classmethod
    def _upsert_student_bed_residence(
        cls,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        if current_id is not None:
            raise SchoolFactChangeRejected("住宿稳定引用不能改写，请结束或撤销后登记新事实。")
        target_id = uuid4()
        next_version = current_version + 1
        session.execute(
            text(
                "INSERT INTO student_bed_residences "
                "(id, school_id, student_id, bed_id, valid_from, valid_to, revoked_at, version, "
                "source_id, import_batch_id) VALUES "
                "(:id, :school_id, :student_id, :bed_id, :effective_at, :valid_to, NULL, "
                ":version, :source_id, :batch_id)"
            ),
            {
                "id": target_id,
                "school_id": school_id,
                "student_id": cls._source_root_id(
                    session,
                    school_id,
                    "student",
                    content["student_source_key"],
                    effective_at=change.effective_at,
                ),
                "bed_id": cls._source_root_id(
                    session,
                    school_id,
                    "dorm_bed",
                    content["bed_source_key"],
                    effective_at=change.effective_at,
                ),
                "effective_at": change.effective_at,
                "valid_to": content.get("valid_to"),
                "version": next_version,
                "source_id": source_id,
                "batch_id": batch_id,
            },
        )
        return target_id, next_version

    @classmethod
    def _upsert_person_contact(
        cls,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        if current_id is not None:
            session.execute(
                text(
                    "UPDATE person_contact_points SET valid_to = :effective_at "
                    "WHERE school_id = :school_id AND id = :id AND valid_to IS NULL"
                ),
                {"school_id": school_id, "id": current_id, "effective_at": change.effective_at},
            )
        target_id = uuid4()
        next_version = current_version + 1
        session.execute(
            text(
                "INSERT INTO person_contact_points "
                "(id, school_id, person_id, contact_kind, contact_value, is_primary, "
                "visibility_scope, audience_code, consent_reference, valid_from, valid_to, "
                "version, source_id, import_batch_id) VALUES "
                "(:id, :school_id, :person_id, :contact_kind, :contact_value, :is_primary, "
                ":visibility_scope, :audience_code, :consent_reference, :effective_at, NULL, "
                ":version, :source_id, :batch_id)"
            ),
            {
                "id": target_id,
                "school_id": school_id,
                "person_id": cls._source_root_id(
                    session,
                    school_id,
                    "person",
                    content["person_source_key"],
                    effective_at=change.effective_at,
                ),
                "contact_kind": content["contact_kind"],
                "contact_value": content["contact_value"],
                "is_primary": content["is_primary"],
                "visibility_scope": content["visibility_scope"],
                "audience_code": content.get("audience_code"),
                "consent_reference": content.get("consent_reference"),
                "effective_at": change.effective_at,
                "version": next_version,
                "source_id": source_id,
                "batch_id": batch_id,
            },
        )
        return target_id, next_version

    @classmethod
    def _upsert_student_contact(
        cls,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        if current_id is not None:
            session.execute(
                text(
                    "UPDATE student_contact_relationships SET valid_to = :effective_at "
                    "WHERE school_id = :school_id AND id = :id AND valid_to IS NULL"
                ),
                {"school_id": school_id, "id": current_id, "effective_at": change.effective_at},
            )
        target_id = uuid4()
        next_version = current_version + 1
        session.execute(
            text(
                "INSERT INTO student_contact_relationships "
                "(id, school_id, student_id, contact_person_id, relationship_kind, priority, "
                "is_emergency, valid_from, valid_to, version, source_id, import_batch_id) VALUES "
                "(:id, :school_id, :student_id, :person_id, :relationship_kind, :priority, "
                ":is_emergency, :effective_at, NULL, :version, :source_id, :batch_id)"
            ),
            {
                "id": target_id,
                "school_id": school_id,
                "student_id": cls._source_root_id(
                    session,
                    school_id,
                    "student",
                    content["student_source_key"],
                    effective_at=change.effective_at,
                ),
                "person_id": cls._source_root_id(
                    session,
                    school_id,
                    "person",
                    content["contact_person_source_key"],
                    effective_at=change.effective_at,
                ),
                "relationship_kind": content["relationship_kind"],
                "priority": content["priority"],
                "is_emergency": content["is_emergency"],
                "effective_at": change.effective_at,
                "version": next_version,
                "source_id": source_id,
                "batch_id": batch_id,
            },
        )
        return target_id, next_version

    def _family_actor(self, *, action: str, target: str):
        context = self._request_context
        if (
            context is None
            or type(context.actor) is not StaffActorContext
            or self._family_actor_validator is None
        ):
            raise SchoolFactChangeRejected("家校关系变更缺少当前正式职责核验。")
        checked_at = datetime.now(UTC)
        validation = self._family_actor_validator.validate(
            actor=context.actor,
            action=action,
            target=target,
            checked_at=checked_at,
        )
        if (
            validation.actor_person_id != context.actor.person_id
            or validation.actor_assignment_id != context.actor.assignment_id
            or validation.action != action
            or validation.checked_at != checked_at
        ):
            raise SchoolFactChangeRejected("家校关系职责核验结果与当前操作者不匹配。")
        return validation

    def _upsert_guardian_relationship(
        self,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        desired = str(content["status"])
        action = "request" if desired == "pending_verification" else "verify"
        actor = self._family_actor(action=action, target=str(change.source_key))
        guardian_id = self._source_root_id(
            session,
            school_id,
            "person",
            content["guardian_person_source_key"],
            effective_at=change.effective_at,
        )
        student_id = self._source_root_id(
            session,
            school_id,
            "student",
            content["student_source_key"],
            effective_at=change.effective_at,
        )
        if actor.actor_person_id == guardian_id:
            raise SchoolFactChangeRejected("核验人员不能维护自己的家校关系。")
        requester_person = actor.actor_person_id
        requester_assignment = actor.actor_assignment_id
        requested_at = actor.checked_at
        verified_person = None
        verified_assignment = None
        verified_at = None
        review_id = None
        if current_id is not None:
            row = session.execute(
                text(
                    "SELECT authority_version,status,guardian_person_id,student_id,"
                    "requested_by_person_id,requested_by_assignment_id,review_of_relationship_id "
                    "FROM guardian_relationships WHERE school_id=:school_id AND id=:id FOR UPDATE"
                ),
                {"school_id": school_id, "id": current_id},
            ).one()
            state = FamilyRelationshipState(
                source_key=str(change.source_key),
                authority_version=row.authority_version,
                status=row.status,
                guardian_person_id=row.guardian_person_id,
                student_id=row.student_id,
                requested_by_person_id=row.requested_by_person_id,
                requested_by_assignment_id=row.requested_by_assignment_id,
                review_of_relationship_id=row.review_of_relationship_id,
            )
            try:
                validate_family_transition(previous=state, desired_status=desired, actor=actor)
            except ValueError as exc:
                raise SchoolFactChangeRejected(str(exc)) from exc
            if (guardian_id, student_id) != (row.guardian_person_id, row.student_id):
                raise SchoolFactChangeRejected("待核验关系对象不得在确认时改绑。")
            requester_person = row.requested_by_person_id
            requester_assignment = row.requested_by_assignment_id
            verified_person = actor.actor_person_id
            verified_assignment = actor.actor_assignment_id
            verified_at = actor.checked_at
            review_id = row.review_of_relationship_id
            session.execute(
                text(
                    "UPDATE guardian_relationships SET valid_to=:at,status='ended' "
                    "WHERE school_id=:school_id AND id=:id"
                ),
                {"at": change.effective_at, "school_id": school_id, "id": current_id},
            )
        elif desired != "pending_verification":
            raise SchoolFactChangeRejected("家校关系必须先申请再由另一人核验。")
        else:
            predecessor = None
            if content.get("review_of_relationship_source_key") is not None:
                old = session.execute(
                    text(
                        "SELECT relationship.id,relationship.authority_version,relationship.status,"
                        "relationship.guardian_person_id,relationship.student_id,"
                        "relationship.requested_by_person_id,relationship.requested_by_assignment_id,"
                        "relationship.review_of_relationship_id "
                        "FROM guardian_relationships relationship "
                        "JOIN school_fact_change_lineage lineage "
                        "ON lineage.school_id=relationship.school_id "
                        "AND lineage.target_id=relationship.id "
                        "WHERE lineage.school_id=:school_id "
                        "AND lineage.fact_kind='guardian_relationship' "
                        "AND lineage.fact_source_key=:source_key "
                        "ORDER BY lineage.target_version DESC LIMIT 1"
                    ),
                    {
                        "school_id": school_id,
                        "source_key": content["review_of_relationship_source_key"],
                    },
                ).one_or_none()
                if old is None:
                    raise SchoolFactChangeRejected("跨人改绑引用的旧关系不存在。")
                review_id = old.id
                predecessor = FamilyRelationshipState(
                    source_key=str(content["review_of_relationship_source_key"]),
                    authority_version=old.authority_version,
                    status=old.status,
                    guardian_person_id=old.guardian_person_id,
                    student_id=old.student_id,
                    requested_by_person_id=old.requested_by_person_id,
                    requested_by_assignment_id=old.requested_by_assignment_id,
                    review_of_relationship_id=old.review_of_relationship_id,
                )
            try:
                validate_new_family_request(
                    actor=actor,
                    guardian_person_id=guardian_id,
                    student_id=student_id,
                    predecessor=predecessor,
                )
            except ValueError as exc:
                raise SchoolFactChangeRejected(str(exc)) from exc
        target_id = uuid4()
        version = current_version + 1
        session.execute(
            text(
                "INSERT INTO guardian_relationships "
                "(id,school_id,guardian_person_id,student_id,relationship_kind,authority_version,"
                "access_purpose,verification_source,verification_reference,requested_by_person_id,"
                "requested_by_assignment_id,requested_at,verified_by_person_id,"
                "verified_by_assignment_id,verified_at,review_of_relationship_id,valid_from,valid_to,"
                "revoked_at,status,version,source_id,import_batch_id) VALUES "
                "(:id,:school_id,:guardian_id,:student_id,:relationship_kind,1,:access_purpose,"
                ":verification_source,:verification_reference,:requester_person,:requester_assignment,"
                ":requested_at,:verified_person,:verified_assignment,:verified_at,:review_id,"
                ":valid_from,NULL,NULL,:status,:version,:source_id,:batch_id)"
            ),
            {
                "id": target_id,
                "school_id": school_id,
                "guardian_id": guardian_id,
                "student_id": student_id,
                "relationship_kind": content["relationship_kind"],
                "access_purpose": content["access_purpose"],
                "verification_source": content["verification_source"],
                "verification_reference": content["verification_reference"],
                "requester_person": requester_person,
                "requester_assignment": requester_assignment,
                "requested_at": requested_at,
                "verified_person": verified_person,
                "verified_assignment": verified_assignment,
                "verified_at": verified_at,
                "review_id": review_id,
                "valid_from": change.effective_at,
                "status": desired,
                "version": version,
                "source_id": source_id,
                "batch_id": batch_id,
            },
        )
        return target_id, version

    @classmethod
    def _upsert_person_region(
        cls,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        if current_id is not None:
            session.execute(
                text(
                    "UPDATE person_regions SET valid_to=:at WHERE school_id=:school_id "
                    "AND id=:id AND valid_to IS NULL AND revoked_at IS NULL"
                ),
                {"at": change.effective_at, "school_id": school_id, "id": current_id},
            )
        target_id = uuid4()
        version = current_version + 1
        session.execute(
            text(
                "INSERT INTO person_regions "
                "(id,school_id,person_id,district_code,district_name,region_purpose,valid_from,"
                "valid_to,revoked_at,version,source_id,import_batch_id) VALUES "
                "(:id,:school_id,:person_id,:district_code,:district_name,:purpose,:valid_from,"
                "NULL,NULL,:version,:source_id,:batch_id)"
            ),
            {
                "id": target_id,
                "school_id": school_id,
                "person_id": cls._source_root_id(
                    session,
                    school_id,
                    "person",
                    content["person_source_key"],
                    effective_at=change.effective_at,
                ),
                "district_code": content["district_code"],
                "district_name": content["district_name"],
                "purpose": content["region_purpose"],
                "valid_from": change.effective_at,
                "version": version,
                "source_id": source_id,
                "batch_id": batch_id,
            },
        )
        return target_id, version

    @classmethod
    def _upsert_official_contact(
        cls,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        if current_id is not None:
            session.execute(
                text(
                    "UPDATE official_contact_points SET valid_to = :effective_at "
                    "WHERE school_id = :school_id AND id = :id AND valid_to IS NULL"
                ),
                {"school_id": school_id, "id": current_id, "effective_at": change.effective_at},
            )
        target_id = uuid4()
        next_version = current_version + 1
        organization_id = (
            cls._source_root_id(
                session,
                school_id,
                "organization",
                content["organization_source_key"],
                effective_at=change.effective_at,
            )
            if content.get("organization_source_key") is not None
            else None
        )
        location_id = (
            cls._source_root_id(
                session,
                school_id,
                "location",
                content["location_source_key"],
                effective_at=change.effective_at,
            )
            if content.get("location_source_key") is not None
            else None
        )
        session.execute(
            text(
                "INSERT INTO official_contact_points "
                "(id, school_id, owner_kind, organization_unit_id, location_id, contact_kind, "
                "contact_value, service_hours, lifecycle_status, visibility_scope, audience_code, "
                "valid_from, valid_to, version, source_id, import_batch_id) VALUES "
                "(:id, :school_id, :owner_kind, :organization_id, :location_id, :contact_kind, "
                ":contact_value, :service_hours, :lifecycle_status, :visibility_scope, "
                ":audience_code, :effective_at, NULL, :version, :source_id, :batch_id)"
            ),
            {
                "id": target_id,
                "school_id": school_id,
                "owner_kind": content["owner_kind"],
                "organization_id": organization_id,
                "location_id": location_id,
                "contact_kind": content["contact_kind"],
                "contact_value": content["contact_value"],
                "service_hours": content.get("service_hours"),
                "lifecycle_status": content["lifecycle_status"],
                "visibility_scope": content["visibility_scope"],
                "audience_code": content.get("audience_code"),
                "effective_at": change.effective_at,
                "version": next_version,
                "source_id": source_id,
                "batch_id": batch_id,
            },
        )
        return target_id, next_version

    @staticmethod
    def _upsert_policy(
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        del batch_id, source_id, content
        policy_id = current_id or uuid4()
        if current_id is None:
            session.execute(
                text(
                    "INSERT INTO school_policies (id, school_id, source_key) "
                    "VALUES (:id, :school_id, :source_key)"
                ),
                {"id": policy_id, "school_id": school_id, "source_key": change.source_key},
            )
        return policy_id, current_version + 1

    @classmethod
    def _upsert_policy_scope(
        cls,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        if current_id is not None:
            if cls._latest_lineage_operation(
                session,
                school_id,
                "policy_scope",
                change.source_key,
            ) in {"end", "revoke"}:
                raise SchoolFactChangeRejected(
                    "已终止的制度适用范围不能被同来源键普通 upsert 重开。"
                )
            active = session.execute(
                text(
                    "SELECT valid_to IS NULL OR valid_to > :effective_at "
                    "FROM school_policy_scopes WHERE id = :id FOR UPDATE"
                ),
                {"id": current_id, "effective_at": change.effective_at},
            ).scalar_one()
            if not active:
                raise SchoolFactChangeRejected("已终止的制度适用范围不能被普通 upsert 重开。")
            session.execute(
                text("UPDATE school_policy_scopes SET valid_to = :effective_at WHERE id = :id"),
                {"id": current_id, "effective_at": change.effective_at},
            )
        target_id = uuid4()
        next_version = current_version + 1
        organization_id = (
            cls._source_root_id(
                session,
                school_id,
                "organization",
                content["organization_source_key"],
                effective_at=change.effective_at,
            )
            if content.get("organization_source_key") is not None
            else None
        )
        campus_id = (
            cls._source_root_id(
                session,
                school_id,
                "location",
                content["campus_source_key"],
                effective_at=change.effective_at,
            )
            if content.get("campus_source_key") is not None
            else None
        )
        education_id = (
            cls._education_id(
                session,
                school_id,
                str(content["education_target_kind"]),
                str(content["education_target_source_key"]),
                change.effective_at,
                change.content.valid_to if change.content else None,
            )
            if content.get("education_target_source_key") is not None
            else None
        )
        params = {
            "education_id": education_id,
            "academic_year": content.get("academic_year"),
            "id": target_id,
            "school_id": school_id,
            "source_key": change.source_key,
            "policy_id": cls._source_root_id(
                session,
                school_id,
                "policy",
                content["policy_source_key"],
                effective_at=change.effective_at,
            ),
            "organization_id": organization_id,
            "campus_id": campus_id,
            "effective_at": change.effective_at,
            "valid_to": content.get("valid_to"),
            "version": next_version,
            "source_id": source_id,
            "batch_id": batch_id,
        }
        session.execute(
            text(
                "INSERT INTO school_policy_scopes "
                "(id, school_id, source_key, policy_id, organization_unit_id, "
                "campus_location_id, valid_from, valid_to, version, source_id, "
                "import_batch_id,education_target_id,academic_year) VALUES "
                "(:id, :school_id, :source_key, :policy_id, :organization_id, :campus_id, "
                ":effective_at, :valid_to, :version, :source_id, "
                ":batch_id,:education_id,:academic_year)"
            ),
            params,
        )
        return target_id, next_version

    @classmethod
    def _upsert_policy_version(
        cls,
        *,
        session: Session,
        school_id: UUID,
        batch_id: UUID,
        source_id: UUID,
        change: CanonicalFactChangeV1,
        content: dict[str, object],
        current_id: UUID | None,
        current_version: int,
    ) -> tuple[UUID, int]:
        # Canonical time versions the record; official publication and applicability
        # dates are immutable content of each revision, not slicing coordinates.
        source_records = (
            "SELECT l.target_id FROM school_fact_change_lineage l "
            "WHERE l.school_id=:school_id AND l.fact_kind='policy_version' "
            "AND l.fact_source_key=:source_key AND l.operation='upsert'"
        )
        coordinates = {
            "school_id": school_id,
            "source_key": change.source_key,
            "effective_at": change.effective_at,
        }
        duplicate = session.execute(
            text(
                "SELECT id FROM school_policy_versions WHERE school_id=:school_id "
                "AND valid_from=:effective_at AND id IN (" + source_records + ") LIMIT 1"
            ),
            coordinates,
        ).scalar_one_or_none()
        if duplicate is not None:
            raise SchoolFactChangeRejected("Policy revision time already exists.")
        next_start = session.execute(
            text(
                "SELECT min(valid_from) FROM school_policy_versions WHERE school_id=:school_id "
                "AND valid_from>:effective_at AND id IN (" + source_records + ")"
            ),
            coordinates,
        ).scalar_one()
        if current_id is not None:
            session.execute(
                text(
                    "UPDATE school_policy_versions SET valid_to=:effective_at "
                    "WHERE school_id=:school_id AND valid_from<:effective_at "
                    "AND (valid_to IS NULL OR valid_to>:effective_at) AND id IN ("
                    + source_records
                    + ")"
                ),
                coordinates,
            )
        target_id = uuid4()
        next_version = current_version + 1
        publishing_id = (
            cls._source_root_id(
                session,
                school_id,
                "organization",
                content["publishing_organization_source_key"],
                effective_at=change.effective_at,
            )
            if content.get("publishing_organization_source_key") is not None
            else None
        )
        supersedes_id = (
            cls._source_root_id(
                session,
                school_id,
                "policy_version",
                content["supersedes_policy_version_source_key"],
                effective_at=change.effective_at,
            )
            if content.get("supersedes_policy_version_source_key") is not None
            else None
        )
        session.execute(
            text(
                "INSERT INTO school_policy_versions "
                "(id, school_id, policy_id, business_code, title, category_code, classification, "
                "publishing_organization_unit_id, published_at, effective_from, effective_to, "
                "content_text, version_note, supersedes_policy_version_id, version, source_id, "
                "import_batch_id, valid_from, valid_to) VALUES "
                "(:id, :school_id, :policy_id, :business_code, :title, :category_code, "
                ":classification, :publishing_id, :published_at, :effective_from, :effective_to, "
                ":content_text, :version_note, :supersedes_id, :version, :source_id, :batch_id, "
                ":record_from, :record_to)"
            ),
            {
                "id": target_id,
                "school_id": school_id,
                "policy_id": cls._source_root_id(
                    session,
                    school_id,
                    "policy",
                    content["policy_source_key"],
                    effective_at=change.effective_at,
                ),
                "business_code": content["business_code"],
                "title": content["display_name"],
                "category_code": content["category_code"],
                "classification": content["classification"],
                "publishing_id": publishing_id,
                "published_at": content["published_at"],
                "effective_from": content["effective_from"],
                "effective_to": content.get("effective_to"),
                "content_text": content.get("content_text"),
                "version_note": content.get("version_note"),
                "supersedes_id": supersedes_id,
                "record_from": change.effective_at,
                "record_to": next_start,
                "version": next_version,
                "source_id": source_id,
                "batch_id": batch_id,
            },
        )
        return target_id, next_version

    @staticmethod
    def _string_list(value: object) -> list[str]:
        if not isinstance(value, (tuple, list)):
            raise SchoolFactChangeRejected("规范事实字符串列表无效。")
        strings: list[str] = []
        for item in cast(list[object] | tuple[object, ...], value):
            if not isinstance(item, str):
                raise SchoolFactChangeRejected("规范事实字符串列表无效。")
            strings.append(item)
        return strings

    @staticmethod
    def _end_or_revoke(
        session: Session,
        *,
        change: CanonicalFactChangeV1,
        target_id: UUID,
        target_version: int,
    ) -> None:
        if change.fact_kind in F2_MODELS:
            table = cast(Table, F2_MODELS[change.fact_kind].__table__)
            row = session.execute(
                select(table.c.valid_from, table.c.valid_to, table.c.revoked_at)
                .where(table.c.id == target_id)
                .with_for_update()
            ).one()
            if row.revoked_at is not None:
                raise SchoolFactChangeRejected("F2 relation is already revoked.")
            if change.operation == "end":
                if change.effective_at <= row.valid_from or (
                    row.valid_to is not None and change.effective_at > row.valid_to
                ):
                    raise SchoolFactChangeRejected("Invalid F2 termination interval.")
                values = {"valid_to": change.effective_at}
            else:
                values = {"revoked_at": change.effective_at}
            session.execute(update(table).where(table.c.id == target_id).values(**values))
            return
        interval_tables = {
            "student_school_relationship": "student_school_relationships",
            "organization_campus_relationship": "organization_campus_relationships",
            "policy_scope": "school_policy_scopes",
            "location_alias": "campus_location_aliases",
            "dorm_room": "dorm_room_profile_versions",
            "person_contact": "person_contact_points",
            "student_contact": "student_contact_relationships",
            "guardian_relationship": "guardian_relationships",
            "person_region": "person_regions",
            "official_contact": "official_contact_points",
        }
        if change.fact_kind == "guardian_relationship":
            status = "revoked" if change.operation == "revoke" else "ended"
            revoked_sql = ", revoked_at=:effective_at" if change.operation == "revoke" else ""
            session.execute(
                text(
                    "UPDATE guardian_relationships SET valid_to=:effective_at,status=:status"
                    + revoked_sql
                    + " WHERE id=:target_id"
                ),
                {
                    "effective_at": change.effective_at,
                    "status": status,
                    "target_id": target_id,
                },
            )
            return
        interval_table = interval_tables.get(change.fact_kind)
        if interval_table is not None:
            existing_endpoint = session.execute(
                text(
                    f"SELECT valid_to FROM {interval_table} WHERE id = :target_id "  # noqa: S608
                    "FOR UPDATE"
                ),
                {"target_id": target_id},
            ).scalar_one()
            SqlAlchemySchoolFactPersistence._reject_terminal_endpoint_extension(
                change=change,
                existing_endpoint=existing_endpoint,
            )
            if (
                change.operation == "revoke"
                and existing_endpoint is not None
                and existing_endpoint <= change.effective_at
            ):
                return
            session.execute(
                text(
                    f"UPDATE {interval_table} SET valid_to = :effective_at "  # noqa: S608
                    "WHERE id = :target_id"
                ),
                {"effective_at": change.effective_at, "target_id": target_id},
            )
            return
        if change.fact_kind == "policy_version":
            # End/revoke terminates the selected record, preserving official dates.
            # A backdated command must not leave an already scheduled later slice alive.
            applicable = session.execute(
                text(
                    "SELECT id FROM school_policy_versions WHERE id=:target_id "
                    "AND valid_from<:effective_at AND revoked_at IS NULL "
                    "AND (valid_to IS NULL OR valid_to>:effective_at) "
                    "AND (effective_to IS NULL OR effective_to>:effective_at) "
                    "AND NOT EXISTS (SELECT 1 FROM school_policy_versions future "
                    "JOIN school_fact_change_lineage l ON l.school_id=future.school_id "
                    "AND l.target_id=future.id AND l.fact_kind='policy_version' "
                    "AND l.operation='upsert' WHERE l.fact_source_key=:source_key "
                    "AND future.school_id=school_policy_versions.school_id "
                    "AND future.valid_from>:effective_at) FOR UPDATE"
                ),
                {
                    "target_id": target_id,
                    "effective_at": change.effective_at,
                    "source_key": change.source_key,
                },
            ).scalar_one_or_none()
            if applicable is None:
                raise SchoolFactChangeRejected("Invalid policy record termination interval.")
            terminal_column = ", revoked_at=:effective_at" if change.operation == "revoke" else ""
            session.execute(
                text(
                    "UPDATE school_policy_versions SET valid_to=:effective_at"
                    + terminal_column
                    + " WHERE id=:target_id"
                ),
                {"effective_at": change.effective_at, "target_id": target_id},
            )
            return
        if change.fact_kind == "student_bed_residence":
            existing_endpoint = session.execute(
                text(
                    "SELECT valid_to FROM student_bed_residences WHERE id = :target_id FOR UPDATE"
                ),
                {"target_id": target_id},
            ).scalar_one()
            SqlAlchemySchoolFactPersistence._reject_terminal_endpoint_extension(
                change=change,
                existing_endpoint=existing_endpoint,
            )
            column = "revoked_at" if change.operation == "revoke" else "valid_to"
            session.execute(
                text(
                    f"UPDATE student_bed_residences SET {column} = :effective_at "  # noqa: S608
                    "WHERE id = :target_id"
                ),
                {"effective_at": change.effective_at, "target_id": target_id},
            )
            return
        if change.fact_kind == "dorm_bed":
            existing_endpoint = session.execute(
                text(
                    "SELECT valid_to FROM dorm_bed_versions "
                    "WHERE bed_id = :target_id AND version = :target_version FOR UPDATE"
                ),
                {"target_id": target_id, "target_version": target_version},
            ).scalar_one()
            SqlAlchemySchoolFactPersistence._reject_terminal_endpoint_extension(
                change=change,
                existing_endpoint=existing_endpoint,
            )
            if (
                change.operation == "revoke"
                and existing_endpoint is not None
                and existing_endpoint <= change.effective_at
            ):
                return
            session.execute(
                text(
                    "UPDATE dorm_bed_versions SET valid_to = :effective_at "
                    "WHERE bed_id = :target_id AND version = :target_version"
                ),
                {
                    "effective_at": change.effective_at,
                    "target_id": target_id,
                    "target_version": target_version,
                },
            )
            return
        if change.fact_kind in {"login_mapping", "assignment"}:
            table_name = (
                "account_person_bindings"
                if change.fact_kind == "login_mapping"
                else "staff_assignments"
            )
            version_table = (
                "account_person_binding_versions"
                if change.fact_kind == "login_mapping"
                else "staff_assignment_versions"
            )
            target_column = "binding_id" if change.fact_kind == "login_mapping" else "assignment_id"
            endpoint_column = (
                "effective_to" if change.fact_kind == "login_mapping" else "planned_valid_to"
            )
            existing_endpoint = session.execute(
                text(
                    f"SELECT {endpoint_column} FROM {version_table} "  # noqa: S608
                    f"WHERE {target_column} = :target_id "  # noqa: S608
                    "AND effective_from <= :effective_at "
                    "AND (effective_to IS NULL OR effective_to > :effective_at) "
                    "ORDER BY effective_from DESC LIMIT 1 FOR UPDATE"
                ),
                {"target_id": target_id, "effective_at": change.effective_at},
            ).scalar_one_or_none()
            SqlAlchemySchoolFactPersistence._reject_terminal_endpoint_extension(
                change=change,
                existing_endpoint=existing_endpoint,
            )
            status = "rejected" if change.operation == "revoke" else "confirmed"
            if change.operation == "revoke":
                session.execute(
                    text(
                        f"UPDATE {version_table} SET revoked_at = :effective_at, "  # noqa: S608
                        "updated_at = now() "
                        f"WHERE {target_column} = :target_id"  # noqa: S608
                    ),
                    {"effective_at": change.effective_at, "target_id": target_id},
                )
            else:
                session.execute(
                    text(
                        f"UPDATE {version_table} SET effective_to = :effective_at, "  # noqa: S608
                        "updated_at = now() "
                        f"WHERE {target_column} = :target_id "  # noqa: S608
                        "AND effective_from < :effective_at "
                        "AND (effective_to IS NULL OR effective_to > :effective_at)"
                    ),
                    {"effective_at": change.effective_at, "target_id": target_id},
                )
                session.execute(
                    text(
                        f"UPDATE {version_table} SET status = 'rejected', "  # noqa: S608
                        "updated_at = now() "
                        f"WHERE {target_column} = :target_id "  # noqa: S608
                        "AND effective_from >= :effective_at "
                        "AND status = 'confirmed'"
                    ),
                    {"effective_at": change.effective_at, "target_id": target_id},
                )
            if change.fact_kind == "assignment":
                terminal_write = (
                    "revoked_at = :effective_at"
                    if change.operation == "revoke"
                    else "valid_to = valid_to"
                )
            elif change.operation == "revoke":
                terminal_write = "valid_to = valid_to"
            else:
                terminal_write = "valid_to = :effective_at"
            session.execute(
                text(
                    f"UPDATE {table_name} SET {terminal_write}, status = :status, "  # noqa: S608
                    "version = version + 1 WHERE id = :target_id"
                ),
                {
                    "effective_at": change.effective_at,
                    "status": status,
                    "target_id": target_id,
                },
            )
            return
        if change.fact_kind == "policy":
            raise SchoolFactChangeRejected("制度目录稳定身份不能结束或撤销，请结束具体制度版本。")
        root = _ROOT_FACTS.get(change.fact_kind)
        if root is not None:
            table_name, _ = root
            session.execute(
                text(
                    f"UPDATE {table_name} SET status = :status, version = version + 1 "  # noqa: S608
                    "WHERE id = :target_id"
                ),
                {
                    "status": "inactive",
                    "target_id": target_id,
                },
            )
            return
        raise SchoolFactChangeRejected("该关系事实的结束写核尚未登记。")

    @staticmethod
    def _reject_terminal_endpoint_extension(
        *,
        change: CanonicalFactChangeV1,
        existing_endpoint: datetime | None,
    ) -> None:
        if (
            change.operation == "end"
            and existing_endpoint is not None
            and change.effective_at >= existing_endpoint
        ):
            raise SchoolFactChangeRejected("结束操作不得延长或重复既有事实终点。")

    @staticmethod
    def _append_terminal_outcome(
        *,
        session: Session,
        batch: MasterDataImportBatch,
        actor: StaffActorContext,
        context: CommandExecutionContext,
        sequences: list[int],
    ) -> None:
        record_id = uuid4()
        key_hash = sha256(context.idempotency_key.encode("utf-8")).hexdigest()
        request_hash = str(batch.plan_sha256)
        session.execute(
            text(
                "INSERT INTO command_idempotency_records "
                "(id, school_id, action_code, actor_user_id, actor_person_id, "
                "actor_context_type, actor_context_key, actor_assignment_id, "
                "actor_assignment_version, resource_type, resource_id, "
                "idempotency_key_hash, request_sha256, status, result_type, result_payload) "
                "VALUES (:id, :school_id, 'school_facts.confirm', :user_id, :person_id, "
                "'assignment', :actor_key, :assignment_id, :assignment_version, "
                "'master_data.import_batch', :resource_id, :key_hash, :request_hash, "
                "'succeeded', 'master_data.import_outcome', "
                "jsonb_build_object('resource_type', 'master_data.import_batch', "
                "'resource_id', CAST(:payload_resource_id AS text), "
                "'resource_version', CAST(:batch_version AS bigint), "
                "'outcome_code', 'applied'))"
            ),
            {
                "id": record_id,
                "school_id": batch.school_id,
                "user_id": actor.account_user_id,
                "person_id": actor.person_id,
                "actor_key": f"assignment:{actor.assignment_id}:{actor.assignment_version}",
                "assignment_id": actor.assignment_id,
                "assignment_version": actor.assignment_version,
                "resource_id": str(batch.id),
                "payload_resource_id": str(batch.id),
                "key_hash": key_hash,
                "request_hash": request_hash,
                "batch_version": batch.version,
            },
        )
        session.execute(
            text(
                "INSERT INTO master_data_import_command_outcomes "
                "(id, school_id, idempotency_record_id, batch_id, action_code, status, "
                "batch_version, succeeded_units, failed_units, executed_units) "
                "VALUES (:id, :school_id, :record_id, :batch_id, 'school_facts.confirm', "
                "'applied', :batch_version, :sequences, ARRAY[]::integer[], :sequences)"
            ),
            {
                "id": uuid4(),
                "school_id": batch.school_id,
                "record_id": record_id,
                "batch_id": batch.id,
                "batch_version": batch.version,
                "sequences": sequences,
            },
        )

    @staticmethod
    def _append_audit(
        session: Session,
        batch: MasterDataImportBatch,
        actor: StaffActorContext,
        context: CommandExecutionContext,
        *,
        changed_count: int,
    ) -> None:
        session.execute(
            text(
                "INSERT INTO audit_logs "
                "(actor_user_id, school_id, actor_person_id, actor_assignment_id, "
                "actor_assignment_version, actor_context_type, idempotency_key_hash, "
                "event_type, target_type, target_id, before_summary, after_summary, "
                "request_id, reason) VALUES "
                "(:user_id, :school_id, :person_id, :assignment_id, :assignment_version, "
                "'assignment', :idempotency_hash, 'school_facts.change_set.applied', "
                "'master_data_import_batch', :target_id, '{}'::jsonb, "
                "jsonb_build_object('state_code', 'change_set.applied', "
                "'changed_count', :changed_count), :request_id, 'manual.school-facts-confirmed')"
            ),
            {
                "user_id": actor.account_user_id,
                "school_id": actor.school_id,
                "person_id": actor.person_id,
                "assignment_id": actor.assignment_id,
                "assignment_version": actor.assignment_version,
                "idempotency_hash": sha256(context.idempotency_key.encode("utf-8")).hexdigest(),
                "target_id": str(batch.id),
                "changed_count": changed_count,
                "request_id": str(context.request_id),
            },
        )

    @staticmethod
    def _applied_plan_query(
        school_id: SchoolId | UUID,
        source_sha256: str,
        plan_sha256: str,
        *,
        exclude_batch_id: UUID | None = None,
    ) -> Select[tuple[MasterDataImportBatch]]:
        statement = select(MasterDataImportBatch).where(
            MasterDataImportBatch.school_id == school_id,
            MasterDataImportBatch.source_sha256 == source_sha256,
            MasterDataImportBatch.plan_sha256 == plan_sha256,
            MasterDataImportBatch.status == "applied",
            MasterDataImportBatch.result_payload.is_not(None),
        )
        if exclude_batch_id is not None:
            statement = statement.where(MasterDataImportBatch.id != exclude_batch_id)
        return statement.order_by(
            MasterDataImportBatch.confirmed_at.asc(),
            MasterDataImportBatch.created_at.asc(),
            MasterDataImportBatch.id.asc(),
        ).limit(1)

    @staticmethod
    def _result_from_batch(
        batch: MasterDataImportBatch | None,
        *,
        replayed: bool,
    ) -> SchoolFactApplyResult | None:
        if batch is None or batch.result_payload is None:
            return None
        target_version = batch.result_payload.get("target_version")
        changed_count = batch.result_payload.get("changed_count")
        if type(target_version) is not int or type(changed_count) is not int:
            return None
        return SchoolFactApplyResult(
            batch_id=batch.id,
            target_version=target_version,
            changed_count=changed_count,
            replayed=replayed,
        )

    def _probe_result(self, credential_sha256: str) -> SchoolFactApplyResult | None:
        with self._request_bound_session() as session:
            batch = session.execute(
                select(MasterDataImportBatch).where(
                    MasterDataImportBatch.confirmation_token_sha256 == credential_sha256
                )
            ).scalar_one_or_none()
            return self._result_from_batch(batch, replayed=True)

    @staticmethod
    def _is_commit_unknown(exc: DBAPIError) -> bool:
        sqlstate = getattr(exc.orig, "sqlstate", None)
        return exc.connection_invalidated or (
            isinstance(sqlstate, str) and sqlstate.startswith("08")
        )

    @staticmethod
    def _commit_transaction(transaction: SessionTransaction) -> None:
        transaction.commit()

    @staticmethod
    def _raise_db_error(exc: DBAPIError) -> Never:
        raise SchoolFactPersistenceError("学校事实事务执行失败。") from None
