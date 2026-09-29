# pyright: strict

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import ColumnElement, Select, column, func, select, table, true

from app.platform.authorization.thin_contracts import ScopePlan
from app.platform.contracts import StaffActorContext

from .f2_reader_contracts import F2FactReadSelection
from .secure_reader_contracts import SchoolFactReadSelection
from .secure_reader_registry import ReadOperation

_MAX_PAGE_SIZE = 100

_ASSIGNMENTS = table(
    "staff_assignments",
    column("id"),
    column("school_id"),
    column("person_id"),
)
_ASSIGNMENT_VERSIONS = table(
    "staff_assignment_versions",
    column("id"),
    column("school_id"),
    column("assignment_id"),
    column("person_id"),
    column("organization_unit_id"),
    column("position_title"),
    column("semantic_role_code"),
    column("assignment_kind"),
    column("effective_from"),
    column("effective_to"),
    column("planned_valid_to"),
    column("revoked_at"),
    column("status"),
    column("target_version"),
)
_LOCATIONS = table(
    "campus_locations",
    column("source_key"),
    column("id"),
    column("school_id"),
    column("status"),
)
_LOCATION_VERSIONS = table(
    "campus_location_versions",
    column("school_id"),
    column("location_id"),
    column("kind"),
    column("business_code"),
    column("display_name"),
    column("display_path"),
    column("valid_from"),
    column("valid_to"),
    column("status"),
    column("version"),
)
_OFFICIAL_CONTACTS = table(
    "official_contact_points",
    column("id"),
    column("school_id"),
    column("owner_kind"),
    column("organization_unit_id"),
    column("location_id"),
    column("contact_kind"),
    column("contact_value"),
    column("service_hours"),
    column("lifecycle_status"),
    column("visibility_scope"),
    column("valid_from"),
    column("valid_to"),
    column("version"),
    column("source_id"),
    column("audience_code"),
)

_SOURCES = table(
    "school_fact_sources",
    column("id"),
    column("school_id"),
    column("source_kind"),
)
_LINEAGE = table(
    "school_fact_change_lineage",
    column("id"),
    column("school_id"),
    column("fact_kind"),
    column("fact_source_key"),
    column("operation"),
    column("target_id"),
    column("target_version"),
    column("batch_id"),
    column("created_at"),
)


@dataclass(frozen=True, slots=True)
class CompiledReadRelation:
    operation_code: str
    as_of: datetime
    page_size: int
    projected_fields: tuple[str, ...]
    count_statement: Select[Any]
    data_statement: Select[Any]
    metadata_statement: Select[Any] | None = None
    counted_metadata_statement: Select[Any] | None = None


class ServerOwnedProjectionCompiler:
    """Compile only projections named by the server-owned operation registry."""

    def compile(
        self,
        *,
        operation: ReadOperation,
        actor: StaffActorContext,
        scope_plan: ScopePlan,
        as_of: datetime,
        after_id: UUID | None,
        page_size: int,
        selection: SchoolFactReadSelection | F2FactReadSelection | None = None,
    ) -> CompiledReadRelation:
        if operation.code.startswith("school_facts.f2."):
            from .f2_reader_compiler import compile_f2

            return compile_f2(
                operation=operation,
                actor=actor,
                scope_plan=scope_plan,
                as_of=as_of,
                after_id=after_id,
                page_size=page_size,
                selection=selection,
            )
        if selection is not None and type(selection) is not SchoolFactReadSelection:
            raise ValueError("Legacy projection requires its fixed selector.")
        school_id = UUID(str(actor.school_id))
        if operation.code in {
            "school_facts.official_contact_campuses",
            "school_facts.official_contacts",
            "school_facts.official_contact_maintenance_target",
        }:
            return self._compile_official_contact(
                operation=operation,
                actor=actor,
                scope_plan=scope_plan,
                as_of=as_of,
                after_id=after_id,
                page_size=page_size,
                selection=selection,
            )
        if selection is not None:
            raise ValueError("legacy projection does not accept a selection")

        approved_scope_form = (
            scope_plan.owner == "test-only.r1.school" and scope_plan.scope_id == school_id
        ) or (scope_plan.owner == "test-only.sf5a" and type(scope_plan.scope_id) is UUID)
        if (
            not approved_scope_form
            or scope_plan.owner not in {"test-only.r1.school", "test-only.sf5a"}
            or scope_plan.reference != f"school:{school_id}"
            or scope_plan.version < 1
        ):
            raise ValueError("scope plan is not one approved test-only school scope")
        if not 1 <= page_size <= _MAX_PAGE_SIZE:
            raise ValueError("page_size is outside the fixed reader bound")
        builders = {
            "self_current_assignment": self._self_current_assignment,
            "public_location": self._public_location,
            "official_office_contact": self._official_office_contact,
        }
        builder = builders.get(operation.projection_name)
        if builder is None:
            raise ValueError("operation projection is not registered in the compiler")
        base, row_id, fields = builder(actor=actor, school_id=school_id, as_of=as_of)
        if after_id is not None:
            base = base.where(row_id > after_id)
        stable_relation = base.order_by(row_id)
        count_statement = select(func.count()).select_from(base.subquery())
        data_statement = stable_relation.limit(page_size + 1)
        return CompiledReadRelation(
            operation_code=operation.code,
            as_of=as_of,
            page_size=page_size,
            projected_fields=fields,
            count_statement=count_statement,
            data_statement=data_statement,
        )

    def _compile_official_contact(
        self,
        *,
        operation: ReadOperation,
        actor: StaffActorContext,
        scope_plan: ScopePlan,
        as_of: datetime,
        after_id: UUID | None,
        page_size: int,
        selection: SchoolFactReadSelection | None,
    ) -> CompiledReadRelation:
        school_id = UUID(str(actor.school_id))
        is_maintenance = operation.code == "school_facts.official_contact_maintenance_target"
        expected_action = (
            "school_facts.official_contacts.maintain"
            if is_maintenance
            else "school_facts.official_contacts.read"
        )
        expected_projection = (
            "official_contact_maintenance_target"
            if is_maintenance
            else "official_contact_campuses"
            if operation.code == "school_facts.official_contact_campuses"
            else "published_official_contacts"
        )
        if (
            scope_plan.owner != "school_facts.school"
            or scope_plan.reference != f"school:{school_id}"
            or scope_plan.version != 1
            or operation.module_name != "school_facts.official_contacts"
            or operation.action_code != expected_action
            or operation.projection_name != expected_projection
            or type(selection) is not SchoolFactReadSelection
            or not 1 <= page_size <= _MAX_PAGE_SIZE
        ):
            raise ValueError("unapproved formal school fact projection")
        if is_maintenance:
            if selection.lineage_id is None:
                raise ValueError("maintenance target requires one controlled lineage reference")
            base, _row_id, _fields = self._published_official_contacts(
                school_id=school_id, campus_id=None, as_of=as_of
            )
            school_fact_version = func.app.school_fact_current_version(school_id)
            fields = (
                "row_id",
                "source_key",
                "expected_version",
                "campus_id",
                "campus_source_key",
                "school_fact_version",
                "owner_kind",
                "contact_kind",
                "contact_value",
                "service_hours",
                "lifecycle_status",
                "visibility_scope",
                "audience_code",
                "valid_from",
                "valid_to",
            )
            row_id = _LINEAGE.c.id
            base = base.with_only_columns(
                row_id.label("row_id"),
                _LINEAGE.c.fact_source_key.label("source_key"),
                base.selected_columns.version.label("expected_version"),
                _LOCATIONS.c.id.label("campus_id"),
                _LOCATIONS.c.source_key.label("campus_source_key"),
                school_fact_version.label("school_fact_version"),
                _OFFICIAL_CONTACTS.c.owner_kind,
                _OFFICIAL_CONTACTS.c.contact_kind,
                _OFFICIAL_CONTACTS.c.contact_value,
                _OFFICIAL_CONTACTS.c.service_hours,
                _OFFICIAL_CONTACTS.c.lifecycle_status,
                _OFFICIAL_CONTACTS.c.visibility_scope,
                _OFFICIAL_CONTACTS.c.audience_code,
                _OFFICIAL_CONTACTS.c.valid_from,
                _OFFICIAL_CONTACTS.c.valid_to,
            ).where(_LINEAGE.c.id == selection.lineage_id)
        elif expected_projection == "official_contact_campuses":
            if selection.lineage_id is not None:
                raise ValueError("campus selection cannot carry a lineage reference")

            base, row_id, _fields = self._public_location(
                actor=actor, school_id=school_id, as_of=as_of
            )
            fields = ("row_id", "display_name", "display_path", "valid_from", "valid_to", "version")
            base = base.with_only_columns(
                _LOCATIONS.c.id.label("row_id"),
                _LOCATION_VERSIONS.c.display_name,
                _LOCATION_VERSIONS.c.display_path,
                _LOCATION_VERSIONS.c.valid_from,
                _LOCATION_VERSIONS.c.valid_to,
                _LOCATION_VERSIONS.c.version,
            ).where(_LOCATION_VERSIONS.c.kind == "campus")
            if selection.campus_id is not None:
                base = base.where(_LOCATIONS.c.id == selection.campus_id)
            elif selection.campus_name is not None:
                base = base.where(_LOCATION_VERSIONS.c.display_name == selection.campus_name)
        else:
            if selection.campus_id is None or selection.campus_name is not None:
                raise ValueError("official contact read requires one resolved campus")
            base, row_id, fields = self._published_official_contacts(
                school_id=school_id, campus_id=selection.campus_id, as_of=as_of
            )
        if after_id is not None:
            base = base.where(row_id > after_id)
        return CompiledReadRelation(
            operation_code=operation.code,
            as_of=as_of,
            page_size=page_size,
            projected_fields=fields,
            count_statement=select(func.count()).select_from(base.subquery()),
            data_statement=base.order_by(row_id).limit(page_size + 1),
        )

    @staticmethod
    def _published_official_contacts(
        *, school_id: UUID, campus_id: UUID | None, as_of: datetime
    ) -> tuple[Select[Any], ColumnElement[Any], tuple[str, ...]]:
        published = _LINEAGE.alias("published")
        terminal = _LINEAGE.alias("terminal")
        canonical = _LINEAGE.alias("canonical")
        selected_publication = (
            select(published.c.id)
            .where(
                published.c.school_id == _OFFICIAL_CONTACTS.c.school_id,
                published.c.fact_kind == "official_contact",
                published.c.target_id == _OFFICIAL_CONTACTS.c.id,
                published.c.operation == "upsert",
            )
            .order_by(published.c.target_version.desc())
            .limit(1)
            .correlate(_OFFICIAL_CONTACTS)
            .scalar_subquery()
        )
        current_version = (
            select(func.max(canonical.c.target_version))
            .where(
                canonical.c.school_id == _LINEAGE.c.school_id,
                canonical.c.fact_kind == "official_contact",
                canonical.c.fact_source_key == _LINEAGE.c.fact_source_key,
            )
            .correlate(_LINEAGE)
            .scalar_subquery()
        )
        revoked = (
            select(terminal.c.id)
            .where(
                terminal.c.school_id == _OFFICIAL_CONTACTS.c.school_id,
                terminal.c.fact_kind == "official_contact",
                terminal.c.target_id == _OFFICIAL_CONTACTS.c.id,
                terminal.c.operation == "revoke",
            )
            .correlate(_OFFICIAL_CONTACTS)
            .exists()
        )
        relation = (
            _OFFICIAL_CONTACTS.join(
                _LOCATIONS,
                (_LOCATIONS.c.school_id == _OFFICIAL_CONTACTS.c.school_id)
                & (_LOCATIONS.c.id == _OFFICIAL_CONTACTS.c.location_id),
            )
            .join(
                _LOCATION_VERSIONS,
                (_LOCATION_VERSIONS.c.school_id == _LOCATIONS.c.school_id)
                & (_LOCATION_VERSIONS.c.location_id == _LOCATIONS.c.id),
            )
            .join(
                _SOURCES,
                (_SOURCES.c.school_id == _OFFICIAL_CONTACTS.c.school_id)
                & (_SOURCES.c.id == _OFFICIAL_CONTACTS.c.source_id),
            )
            .join(
                _LINEAGE,
                (_LINEAGE.c.school_id == _OFFICIAL_CONTACTS.c.school_id)
                & (_LINEAGE.c.id == selected_publication),
            )
        )
        fields = (
            "row_id",
            "campus_id",
            "campus_name",
            "contact_kind",
            "contact_value",
            "service_hours",
            "valid_from",
            "valid_to",
            "version",
            "source_kind",
            "lineage_id",
            "batch_id",
            "published_at",
        )
        statement = (
            select(
                _OFFICIAL_CONTACTS.c.id.label("row_id"),
                _LOCATIONS.c.id.label("campus_id"),
                _LOCATION_VERSIONS.c.display_name.label("campus_name"),
                _OFFICIAL_CONTACTS.c.contact_kind,
                _OFFICIAL_CONTACTS.c.contact_value,
                _OFFICIAL_CONTACTS.c.service_hours,
                _OFFICIAL_CONTACTS.c.valid_from,
                _OFFICIAL_CONTACTS.c.valid_to,
                current_version.label("version"),
                _SOURCES.c.source_kind,
                _LINEAGE.c.id.label("lineage_id"),
                _LINEAGE.c.batch_id,
                _LINEAGE.c.created_at.label("published_at"),
            )
            .select_from(relation)
            .where(
                _OFFICIAL_CONTACTS.c.school_id == school_id,
                _OFFICIAL_CONTACTS.c.owner_kind == "location",
                (_OFFICIAL_CONTACTS.c.location_id == campus_id)
                if campus_id is not None
                else true(),
                _OFFICIAL_CONTACTS.c.lifecycle_status == "active",
                _OFFICIAL_CONTACTS.c.visibility_scope.in_(("public", "school_authenticated")),
                _OFFICIAL_CONTACTS.c.valid_from <= as_of,
                (_OFFICIAL_CONTACTS.c.valid_to.is_(None) | (_OFFICIAL_CONTACTS.c.valid_to > as_of)),
                _LOCATIONS.c.status == "active",
                _LOCATION_VERSIONS.c.kind == "campus",
                _LOCATION_VERSIONS.c.status == "confirmed",
                _LOCATION_VERSIONS.c.valid_from <= as_of,
                (_LOCATION_VERSIONS.c.valid_to.is_(None) | (_LOCATION_VERSIONS.c.valid_to > as_of)),
                func.app.school_fact_location_history_is_resolvable(
                    _LOCATIONS.c.school_id,
                    _LOCATIONS.c.id,
                    as_of,
                ).is_(True),
                ~revoked,
            )
        )
        return statement, cast(ColumnElement[Any], _OFFICIAL_CONTACTS.c.id), fields

    @staticmethod
    def _self_current_assignment(
        *, actor: StaffActorContext, school_id: UUID, as_of: datetime
    ) -> tuple[Select[Any], ColumnElement[Any], tuple[str, ...]]:
        row_id = cast(ColumnElement[Any], _ASSIGNMENT_VERSIONS.c.assignment_id)
        fields = (
            "row_id",
            "organization_unit_id",
            "position_title",
            "semantic_role_code",
            "assignment_kind",
            "effective_from",
            "effective_to",
            "planned_valid_to",
            "version",
        )
        statement = select(
            _ASSIGNMENT_VERSIONS.c.assignment_id.label("row_id"),
            _ASSIGNMENT_VERSIONS.c.organization_unit_id,
            _ASSIGNMENT_VERSIONS.c.position_title,
            _ASSIGNMENT_VERSIONS.c.semantic_role_code,
            _ASSIGNMENT_VERSIONS.c.assignment_kind,
            _ASSIGNMENT_VERSIONS.c.effective_from,
            _ASSIGNMENT_VERSIONS.c.effective_to,
            _ASSIGNMENT_VERSIONS.c.planned_valid_to,
            _ASSIGNMENT_VERSIONS.c.target_version.label("version"),
        ).where(
            _ASSIGNMENT_VERSIONS.c.school_id == school_id,
            _ASSIGNMENT_VERSIONS.c.assignment_id == actor.assignment_id,
            _ASSIGNMENT_VERSIONS.c.person_id == actor.person_id,
            _ASSIGNMENT_VERSIONS.c.status == "confirmed",
            _ASSIGNMENT_VERSIONS.c.revoked_at.is_(None),
            _ASSIGNMENT_VERSIONS.c.effective_from <= as_of,
            (
                _ASSIGNMENT_VERSIONS.c.effective_to.is_(None)
                | (_ASSIGNMENT_VERSIONS.c.effective_to > as_of)
            ),
            (
                _ASSIGNMENT_VERSIONS.c.planned_valid_to.is_(None)
                | (_ASSIGNMENT_VERSIONS.c.planned_valid_to > as_of)
            ),
        )
        return statement, row_id, fields

    @staticmethod
    def _public_location(
        *, actor: StaffActorContext, school_id: UUID, as_of: datetime
    ) -> tuple[Select[Any], ColumnElement[Any], tuple[str, ...]]:
        row_id = cast(ColumnElement[Any], _LOCATIONS.c.id)
        fields = (
            "row_id",
            "kind",
            "business_code",
            "display_name",
            "display_path",
            "valid_from",
            "valid_to",
            "version",
        )
        relation = _LOCATIONS.join(
            _LOCATION_VERSIONS,
            (_LOCATION_VERSIONS.c.school_id == _LOCATIONS.c.school_id)
            & (_LOCATION_VERSIONS.c.location_id == _LOCATIONS.c.id),
        )
        statement = (
            select(
                _LOCATIONS.c.id.label("row_id"),
                _LOCATION_VERSIONS.c.kind,
                _LOCATION_VERSIONS.c.business_code,
                _LOCATION_VERSIONS.c.display_name,
                _LOCATION_VERSIONS.c.display_path,
                _LOCATION_VERSIONS.c.valid_from,
                _LOCATION_VERSIONS.c.valid_to,
                _LOCATION_VERSIONS.c.version,
            )
            .select_from(relation)
            .where(
                _LOCATIONS.c.school_id == school_id,
                _LOCATIONS.c.status == "active",
                _LOCATION_VERSIONS.c.status == "confirmed",
                _LOCATION_VERSIONS.c.valid_from <= as_of,
                (_LOCATION_VERSIONS.c.valid_to.is_(None) | (_LOCATION_VERSIONS.c.valid_to > as_of)),
                func.app.school_fact_location_history_is_resolvable(
                    _LOCATION_VERSIONS.c.school_id,
                    _LOCATION_VERSIONS.c.location_id,
                    as_of,
                ).is_(True),
            )
        )
        return statement, row_id, fields

    @staticmethod
    def _official_office_contact(
        *, actor: StaffActorContext, school_id: UUID, as_of: datetime
    ) -> tuple[Select[Any], ColumnElement[Any], tuple[str, ...]]:
        row_id = cast(ColumnElement[Any], _OFFICIAL_CONTACTS.c.id)
        fields = (
            "row_id",
            "owner_kind",
            "organization_unit_id",
            "location_id",
            "contact_kind",
            "contact_value",
            "service_hours",
            "version",
        )
        statement = select(
            _OFFICIAL_CONTACTS.c.id.label("row_id"),
            _OFFICIAL_CONTACTS.c.owner_kind,
            _OFFICIAL_CONTACTS.c.organization_unit_id,
            _OFFICIAL_CONTACTS.c.location_id,
            _OFFICIAL_CONTACTS.c.contact_kind,
            _OFFICIAL_CONTACTS.c.contact_value,
            _OFFICIAL_CONTACTS.c.service_hours,
            _OFFICIAL_CONTACTS.c.version,
        ).where(
            _OFFICIAL_CONTACTS.c.school_id == school_id,
            _OFFICIAL_CONTACTS.c.lifecycle_status == "active",
            _OFFICIAL_CONTACTS.c.visibility_scope.in_(("public", "school_authenticated")),
            _OFFICIAL_CONTACTS.c.valid_from <= as_of,
            (_OFFICIAL_CONTACTS.c.valid_to.is_(None) | (_OFFICIAL_CONTACTS.c.valid_to > as_of)),
        )
        return statement, row_id, fields


__all__ = ["CompiledReadRelation", "ServerOwnedProjectionCompiler"]
