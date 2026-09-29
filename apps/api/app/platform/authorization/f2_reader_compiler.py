# pyright: strict
"""Fixed F2 SQL; exact family, source-key, temporal and field predicates precede IO.

The registry chooses all table and field names. Selectors contain only bound values.
Canonical coordinates are private DTO fields, never a general database interface.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import ColumnElement, Select
from sqlalchemy.sql.elements import ColumnClause
from sqlalchemy.sql.selectable import ScalarSelect, TableClause

from app.platform.contracts import StaffActorContext

from .secure_reader_registry import ReadOperation

if TYPE_CHECKING:
    from .secure_reader_compiler import CompiledReadRelation

from sqlalchemy import and_, column, false, func, literal, or_, select, table, true

from app.platform.school_facts.f2_policy import F2FactScope

from .f2_reader_contracts import F2_ROW_FIELDS, F2FactReadSelection
from .thin_contracts import ScopePlan


@dataclass(frozen=True)
class Definition:
    table_name: str
    fields: tuple[str, ...]
    refs: tuple[tuple[str, str, str], ...] = ()
    root_id: str = "id"
    start: str = "valid_from"
    end: str | None = "valid_to"
    version: str = "version"
    derived_name_fields: tuple[str, ...] = ()


def _fields(value: str) -> tuple[str, ...]:
    return tuple(value.split())


def _counted_metadata(base: Select[Any], *, after_id: UUID | None, page_size: int) -> Select[Any]:
    # Materialize only the authorized metadata relation within this statement;
    # count before the cursor and page lookahead share the same joins/predicates.
    shared = base.cte("f2_authorized_metadata").prefix_with("MATERIALIZED")
    total = select(func.count().label("total_count")).select_from(shared).subquery()
    page = select(literal(True).label("present"), *shared.c)
    if after_id is not None:
        page = page.where(shared.c.row_id > after_id)
    selected = page.order_by(shared.c.row_id).limit(page_size + 1).subquery()
    return (
        select(total.c.total_count, *selected.c)
        .select_from(total.outerjoin(selected, true()))
        .order_by(selected.c.row_id)
    )


F2_DEFINITIONS: dict[str, Definition] = {
    "school": Definition(
        "school_versions",
        _fields("business_code display_name short_name timezone_name lifecycle_status"),
        root_id="school_id",
    ),
    "person": Definition(
        "school_person_profile_versions",
        _fields("business_code person_kind lifecycle_status"),
        root_id="person_id",
        derived_name_fields=("display_name",),
    ),
    "organization": Definition(
        "organization_unit_versions",
        _fields("business_code display_name short_name display_path"),
        (("parent_source_key", "organization", "parent_organization_unit_id"),),
        root_id="organization_unit_id",
    ),
    "assignment": Definition(
        "staff_assignment_versions",
        _fields(
            "position_title semantic_role_code assignment_kind appointment_source "
            "approval_reference status"
        ),
        (
            ("person_source_key", "person", "person_id"),
            ("organization_source_key", "organization", "organization_unit_id"),
        ),
        root_id="assignment_id",
        start="effective_from",
        end="effective_to",
        version="target_version",
    ),
    "location": Definition(
        "campus_location_versions",
        _fields("kind business_code display_name display_path aliases"),
        (("parent_source_key", "location", "parent_location_id"),),
        root_id="location_id",
    ),
    "location_alias": Definition(
        "campus_location_aliases",
        _fields("alias alias_kind"),
        (("location_source_key", "location", "location_id"),),
    ),
    "organization_campus_relationship": Definition(
        "organization_campus_relationships",
        (),
        (
            ("organization_source_key", "organization", "organization_unit_id"),
            ("campus_source_key", "location", "campus_location_id"),
        ),
    ),
    "person_contact": Definition(
        "person_contact_points",
        _fields(
            "contact_kind contact_value is_primary visibility_scope audience_code consent_reference"
        ),
        (("person_source_key", "person", "person_id"),),
    ),
    "official_contact": Definition(
        "official_contact_points",
        _fields(
            "owner_kind contact_kind contact_value service_hours lifecycle_status "
            "visibility_scope audience_code"
        ),
        (
            ("organization_source_key", "organization", "organization_unit_id"),
            ("location_source_key", "location", "location_id"),
        ),
    ),
    "policy": Definition("school_policies", (), start="created_at", end=None, version="unused"),
    "policy_scope": Definition(
        "school_policy_scopes",
        (),
        (
            ("policy_source_key", "policy", "policy_id"),
            ("organization_source_key", "organization", "organization_unit_id"),
            ("campus_source_key", "location", "campus_location_id"),
        ),
    ),
    "policy_version": Definition(
        "school_policy_versions",
        _fields(
            "business_code category_code classification published_at effective_from "
            "effective_to content_text version_note"
        ),
        (
            ("policy_source_key", "policy", "policy_id"),
            (
                "publishing_organization_source_key",
                "organization",
                "publishing_organization_unit_id",
            ),
            (
                "supersedes_policy_version_source_key",
                "policy_version",
                "supersedes_policy_version_id",
            ),
        ),
        start="effective_from",
        end="effective_to",
        derived_name_fields=("display_name",),
    ),
    "organization_profile": Definition(
        "organization_profiles",
        _fields("organization_type function_summary basis_reference"),
        (("organization_source_key", "organization", "organization_unit_id"),),
    ),
    "organization_membership": Definition(
        "organization_memberships",
        _fields("membership_kind basis_reference"),
        (
            ("person_source_key", "person", "person_id"),
            ("organization_source_key", "organization", "organization_unit_id"),
        ),
    ),
    "organization_location": Definition(
        "organization_locations",
        _fields("purpose is_primary basis_reference"),
        (
            ("organization_source_key", "organization", "organization_unit_id"),
            ("location_source_key", "location", "location_id"),
        ),
    ),
    "person_workplace": Definition(
        "person_workplaces",
        _fields("purpose is_primary basis_reference"),
        (
            ("person_source_key", "person", "person_id"),
            ("assignment_source_key", "assignment", "assignment_id"),
            ("location_source_key", "location", "location_id"),
        ),
    ),
    "school_service": Definition(
        "school_services",
        _fields("display_name audience_description service_summary basis_reference"),
        (
            ("organization_source_key", "organization", "organization_unit_id"),
            ("official_contact_source_key", "official_contact", "official_contact_id"),
            ("policy_source_key", "policy", "policy_id"),
        ),
    ),
    "fact_quality": Definition(
        "school_fact_qualities",
        _fields(
            "quality_kind target_fact_kind owner_kind basis_reference responsibility "
            "verified_at target_source_key replacement_source_key purpose contact_kind "
            "evidence_reference coverage_status coverage_basis negative_code dispute_status "
            "source_status last_success_at review_due_at"
        ),
        (
            ("organization_source_key", "organization", "organization_unit_id"),
            ("person_source_key", "person", "person_id"),
            ("location_source_key", "location", "location_id"),
        ),
    ),
}
_LINEAGE = table(
    "school_fact_change_lineage",
    *map(
        column,
        _fields(
            "id school_id fact_kind fact_source_key operation target_id target_version "
            "batch_id created_at"
        ),
    ),
)
_SOURCES = table("school_fact_sources", *map(column, _fields("id school_id source_kind")))


def _table(definition: Definition, kind: str) -> TableClause:
    names = (
        set(definition.fields)
        | {ref[2] for ref in definition.refs}
        | {"id", "school_id", definition.root_id, definition.start, definition.version}
    )
    if definition.end:
        names.add(definition.end)
    if kind == "policy_version":
        names.update(("title", "valid_from", "valid_to", "revoked_at"))
    if kind == "assignment":
        names.update(("planned_valid_to", "revoked_at"))
    if kind in {
        "organization_profile",
        "organization_membership",
        "organization_location",
        "person_workplace",
        "school_service",
        "fact_quality",
    }:
        names.add("revoked_at")
    return table(
        definition.table_name, *(cast(ColumnClause[Any], column(name)) for name in sorted(names))
    )


def _window(
    start: ColumnElement[Any], end: ColumnElement[Any] | None, as_of: datetime
) -> ColumnElement[bool]:
    return (
        and_(start <= as_of, or_(end.is_(None), end > as_of)) if end is not None else start <= as_of
    )


def _source_key(kind: str, target: ColumnClause[Any], school: UUID) -> ScalarSelect[Any]:
    ref = _LINEAGE.alias("reference_" + kind + "_" + target.key)
    return (
        select(ref.c.fact_source_key)
        .where(ref.c.school_id == school, ref.c.fact_kind == kind, ref.c.target_id == target)
        .order_by(ref.c.target_version.desc())
        .limit(1)
        .correlate(target.table)
        .scalar_subquery()
    )


_NAME_FIELDS = ("display_name", "alias", "position_title", "function_summary", "service_summary")


def query_selector_support(kind: str) -> dict[str, object]:
    """Static executable selector contract from the same fixed SQL definitions."""
    definition = F2_DEFINITIONS[kind]
    names = [
        field
        for field in _NAME_FIELDS
        if field in definition.fields or field in definition.derived_name_fields
    ]
    related = list(dict.fromkeys(refkind for _, refkind, _ in definition.refs))
    if kind == "fact_quality":
        # Quality predicates also select the target family (and school owner).
        related = list(F2_DEFINITIONS)
    return {
        "name_fields": names,
        "name_match": "exact",
        "related_kinds": related,
        "reference": True,
        "all_authorized": True,
        "related_match": "direct",
    }


def query_selector_error(
    kind: str, *, name: str | None = None, related_kind: str | None = None
) -> str | None:
    support = query_selector_support(kind)
    if name is not None and not support["name_fields"]:
        return "name_not_supported"
    if related_kind is not None and related_kind not in cast(list[str], support["related_kinds"]):
        return "related_kind_not_supported"
    return None


def compile_f2(
    *,
    operation: ReadOperation,
    actor: StaffActorContext,
    scope_plan: ScopePlan,
    as_of: datetime,
    after_id: UUID | None,
    page_size: int,
    selection: object,
) -> CompiledReadRelation:
    from .secure_reader_compiler import CompiledReadRelation

    parts = operation.code.split(".")
    if len(parts) != 4 or parts[:2] != ["school_facts", "f2"]:
        raise ValueError("Unregistered F2 operation.")
    kind, purpose = parts[2:]
    if (
        kind not in F2_DEFINITIONS
        or purpose not in {"read", "maintain", "self"}
        or purpose == "self"
        and kind != "person_contact"
        or operation.module_name != "school_facts.f2"
        or operation.action_code != operation.code
        or operation.projection_name != f"f2_{kind}_{purpose}"
        or not 1 <= page_size <= 100
        or scope_plan.version != 1
        or as_of.utcoffset() is None
    ):
        raise ValueError("Invalid fixed F2 projection.")
    if selection is None:
        selection = F2FactReadSelection()
    if type(selection) is not F2FactReadSelection:
        raise ValueError("Invalid F2 selection.")
    if selection.related_kind is not None and selection.related_kind not in F2_DEFINITIONS:
        raise ValueError("Unregistered related fact kind.")
    selector_error = query_selector_error(
        kind, name=selection.name, related_kind=selection.related_kind
    )
    if selector_error is not None:
        raise ValueError(selector_error)
    school = UUID(str(actor.school_id))
    scope = None
    target_scopes = ()
    quality_target_kind: str | None = None
    if purpose == "self":
        if (
            scope_plan.owner != "school_facts.f2.self"
            or scope_plan.reference != f"person:{actor.person_id}"
        ):
            raise ValueError("Invalid self-person scope.")
    else:
        if scope_plan.owner != "school_facts.f2":
            raise ValueError("Invalid F2 scope owner.")
        if kind == "fact_quality":
            raw_envelope = json.loads(scope_plan.reference)
            envelope = cast(dict[str, Any], raw_envelope)
            if (
                type(envelope) is not dict
                or set(envelope)
                not in (
                    {"quality_scope", "target_scopes"},
                    {"quality_scope", "target_scopes", "target_fact_kind"},
                )
                or type(envelope["target_scopes"]) is not list
            ):
                raise ValueError("Quality reads require underlying current authorization.")
            if "target_fact_kind" in envelope:
                bound_kind = envelope["target_fact_kind"]
                if (
                    type(bound_kind) is not str
                    or bound_kind not in F2_DEFINITIONS
                    or bound_kind == "fact_quality"
                ):
                    raise ValueError("Invalid quality target family.")
                quality_target_kind = bound_kind
            scope = F2FactScope.from_reference(envelope["quality_scope"])
            target_scopes = tuple(
                F2FactScope.from_reference(ref)
                for ref in cast(list[str], envelope["target_scopes"])
            )
            if not target_scopes or any(
                target.school_id != school or target.fact_kind == "fact_quality"
                for target in target_scopes
            ):
                raise ValueError("Quality target scopes are invalid.")
        else:
            scope = F2FactScope.from_reference(scope_plan.reference)
        if scope.school_id != school or scope.fact_kind != kind:
            raise ValueError("F2 scope belongs to another school or family.")
    if selection.creation_key is not None:
        if (
            purpose != "maintain"
            or scope is None
            or not scope.allows_key(selection.creation_key, is_creation=True)
            or kind == "school"
        ):
            raise ValueError("Creation target is not explicitly authorized.")
        existing = (
            select(_LINEAGE.c.id)
            .where(
                _LINEAGE.c.school_id == school,
                _LINEAGE.c.fact_kind == kind,
                _LINEAGE.c.fact_source_key == selection.creation_key,
            )
            .exists()
        )
        base = select(
            literal(UUID(int=0)).label("row_id"),
            literal(kind).label("fact_kind"),
            literal(selection.creation_key).label("source_key"),
            func.jsonb_build_object().label("content"),
            literal(0).label("expected_version"),
            func.app.school_fact_current_version(school).label("school_fact_version"),
            literal(as_of).label("valid_from"),
            literal(None).label("valid_to"),
            literal("pending").label("source_kind"),
            literal(UUID(int=0)).label("publication_id"),
            literal(as_of).label("published_at"),
            literal(0).label("fact_version"),
        ).where(~existing)
        return CompiledReadRelation(
            operation.code,
            as_of,
            page_size,
            F2_ROW_FIELDS,
            select(func.count()).select_from(base.subquery()),
            base.limit(1),
        )

    definition = F2_DEFINITIONS[kind]
    fact = _table(definition, kind)
    publication = _LINEAGE.alias("publication")
    stable = _LINEAGE.alias("stable_reference")
    latest = _LINEAGE.alias("latest_version")
    is_version_root = definition.root_id != "id"
    publication_match = and_(
        publication.c.school_id == fact.c.school_id,
        publication.c.fact_kind == kind,
        publication.c.target_id == fact.c[definition.root_id],
        publication.c.operation.in_(
            ("upsert", "reactivate", "end", "revoke")
            if purpose == "maintain"
            else ("upsert", "reactivate")
        ),
    )
    if is_version_root:
        publication_match = and_(
            publication_match, publication.c.target_version == fact.c[definition.version]
        )
    chosen = (
        select(publication.c.id)
        .where(publication_match)
        .order_by(publication.c.target_version.desc())
        .limit(1)
        .correlate(fact)
        .scalar_subquery()
    )
    relation = fact.join(
        _LINEAGE, and_(_LINEAGE.c.school_id == fact.c.school_id, _LINEAGE.c.id == chosen)
    )
    stable_id = (
        select(stable.c.id)
        .where(
            stable.c.school_id == school,
            stable.c.fact_kind == kind,
            stable.c.fact_source_key == _LINEAGE.c.fact_source_key,
        )
        .order_by(stable.c.target_version, stable.c.created_at, stable.c.id)
        .limit(1)
        .correlate(_LINEAGE)
        .scalar_subquery()
    )
    current_version = (
        select(func.max(latest.c.target_version))
        .where(
            latest.c.school_id == school,
            latest.c.fact_kind == kind,
            latest.c.fact_source_key == _LINEAGE.c.fact_source_key,
        )
        .correlate(_LINEAGE)
        .scalar_subquery()
    )
    values: dict[str, ColumnElement[Any]] = {field: fact.c[field] for field in definition.fields}
    values.update(
        {
            field: _source_key(refkind, fact.c[col], school)
            for field, refkind, col in definition.refs
        }
    )
    valid_from: ColumnElement[Any] = fact.c[definition.start]
    valid_to: ColumnElement[Any] = fact.c[definition.end] if definition.end else literal(None)
    if kind == "policy":
        # The first formal upsert declares stable directory business time.
        # The database insertion clock remains system metadata only.
        valid_from = func.app.school_fact_f2_reader_policy_valid_from(school, fact.c.id)
    elif kind == "assignment":
        valid_to = func.least(fact.c.effective_to, fact.c.planned_valid_to, fact.c.revoked_at)
    elif kind == "policy_version":
        valid_from = func.greatest(fact.c.valid_from, fact.c.effective_from)
        valid_to = func.least(fact.c.valid_to, fact.c.effective_to, fact.c.revoked_at)
    predicates = [fact.c.school_id == school]
    if purpose == "maintain":
        predicates.append(_LINEAGE.c.target_version == current_version)
    else:
        predicates.append(_window(valid_from, valid_to if definition.end else None, as_of))
    if kind == "fact_quality":
        if quality_target_kind is not None:
            if not any(target.fact_kind == quality_target_kind for target in target_scopes):
                raise ValueError("Missing quality target authority.")
            predicates.append(fact.c.target_fact_kind == quality_target_kind)
        targeted = or_(
            *(
                and_(
                    fact.c.target_fact_kind == target.fact_kind,
                    true()
                    if target.is_school_wide
                    else fact.c.target_source_key.in_(target.object_keys),
                )
                for target in target_scopes
            )
        )
        replacement_authorized = or_(
            fact.c.replacement_source_key.is_(None),
            *(
                and_(
                    fact.c.target_fact_kind == target.fact_kind,
                    true()
                    if target.is_school_wide
                    else fact.c.replacement_source_key.in_(target.object_keys),
                )
                for target in target_scopes
            ),
        )
        owner_conditions: list[ColumnElement[bool]] = []
        for owner_scope in target_scopes:
            if owner_scope.fact_kind == "school" and (
                owner_scope.is_school_wide or owner_scope.object_keys
            ):
                owner_conditions.append(fact.c.owner_kind == "school")
            elif owner_scope.fact_kind in {"organization", "person", "location"}:
                owner_conditions.append(
                    and_(
                        fact.c.owner_kind == owner_scope.fact_kind,
                        true()
                        if owner_scope.is_school_wide
                        else values[owner_scope.fact_kind + "_source_key"].in_(
                            owner_scope.object_keys
                        ),
                    )
                )
        owner_assertion = and_(
            fact.c.target_source_key.is_(None),
            fact.c.target_fact_kind.in_(tuple(target.fact_kind for target in target_scopes)),
            or_(*owner_conditions) if owner_conditions else false(),
        )
        predicates.append(
            and_(
                or_(targeted, owner_assertion),
                or_(*owner_conditions) if owner_conditions else false(),
                replacement_authorized,
            )
        )
    if scope is not None:
        if not scope.is_school_wide:
            predicates.append(_LINEAGE.c.fact_source_key.in_(scope.object_keys))
    else:
        predicates.append(fact.c.person_id == UUID(str(actor.person_id)))
    if selection.source_key is not None:
        if scope is not None and not scope.allows_key(selection.source_key):
            raise ValueError("Selected object is outside the exact authorized scope.")
        predicates.append(_LINEAGE.c.fact_source_key == selection.source_key)
    name_identity = literal(None)
    name_version = literal(None)
    if kind == "person":
        names = table(
            "school_person_names",
            *map(
                column,
                _fields("id school_id person_id display_name valid_from valid_to status version"),
            ),
        )
        relation = relation.join(
            names,
            and_(
                names.c.school_id == fact.c.school_id,
                names.c.person_id == fact.c.person_id,
                (names.c.version == fact.c.version)
                if purpose == "maintain"
                else _window(names.c.valid_from, names.c.valid_to, as_of),
                names.c.status == "confirmed",
            ),
        )
        values["display_name"] = names.c.display_name
        name_identity = names.c.id
        name_version = names.c.version
    if kind in {"organization", "location"}:
        roots = table(
            "organization_units" if kind == "organization" else "campus_locations",
            *map(column, _fields("id school_id kind status")),
        )
        # Root effectiveness is resolved at the fact time; current root status must not
        # erase history.
        if purpose != "maintain":
            predicates.append(
                (
                    func.app.school_fact_location_history_is_resolvable(
                        school, fact.c[definition.root_id], as_of
                    )
                    if kind == "location"
                    else func.app.school_fact_reader_identity_root_is_effective_at(
                        school, kind, fact.c[definition.root_id], as_of
                    )
                ).is_(True)
            )
        values["status"] = literal("active")
        if kind == "organization":
            relation = relation.join(
                roots,
                and_(
                    roots.c.school_id == fact.c.school_id, roots.c.id == fact.c.organization_unit_id
                ),
            )
            if purpose == "maintain":
                values["kind"] = roots.c.kind
                values["status"] = roots.c.status
    if kind in {"school", "person"} and purpose != "maintain":
        predicates.append(fact.c.lifecycle_status == "active")
    if kind == "policy_version":
        values["display_name"] = fact.c.title
        if purpose != "maintain":
            predicates.extend(
                (
                    _window(fact.c.valid_from, fact.c.valid_to, as_of),
                    or_(fact.c.revoked_at.is_(None), fact.c.revoked_at > as_of),
                )
            )
    if kind == "official_contact" and purpose != "maintain":
        predicates.append(fact.c.lifecycle_status == "active")
    if kind == "assignment":
        if purpose != "maintain":
            predicates.extend(
                (
                    fact.c.status == "confirmed",
                    or_(fact.c.planned_valid_to.is_(None), fact.c.planned_valid_to > as_of),
                    or_(fact.c.revoked_at.is_(None), fact.c.revoked_at > as_of),
                )
            )
        values["valid_to"] = fact.c.planned_valid_to
    if kind in {
        "organization_profile",
        "organization_membership",
        "organization_location",
        "person_workplace",
        "school_service",
        "fact_quality",
    }:
        if purpose == "maintain":
            # Maintenance selects the latest content only, so retain its explicit
            # relation end. Never inherit an older version slice's closed interval.
            values["valid_to"] = fact.c.valid_to
        else:
            predicates.append(or_(fact.c.revoked_at.is_(None), fact.c.revoked_at > as_of))
    if kind in {"organization_campus_relationship", "policy_scope"}:
        values["valid_to"] = fact.c.valid_to
    if selection.related_kind is not None:
        referenced = [
            values[field]
            for field, refkind, _ in definition.refs
            if refkind == selection.related_kind
        ]
        if kind == "fact_quality":
            referenced.append(
                and_(
                    fact.c.target_fact_kind == selection.related_kind,
                    fact.c.target_source_key == selection.related_source_key,
                )
            )
        conditions = (
            [value == selection.related_source_key for value in referenced[:-1]] + [referenced[-1]]
            if kind == "fact_quality"
            else [value == selection.related_source_key for value in referenced]
        )
        if kind == "fact_quality" and selection.related_kind == "school":
            conditions.append(fact.c.owner_kind == "school")
        predicates.append(or_(*conditions) if conditions else false())
    if selection.name is not None:
        searchable = [
            values[field] for field in cast(list[str], query_selector_support(kind)["name_fields"])
        ]
        predicates.append(or_(*(value == selection.name for value in searchable)))
    # Metadata originates in the selected publication. Do not select arbitrary source text.
    if selection.content_fields is not None:
        if purpose != "read" or not set(selection.content_fields) <= set(values):
            raise ValueError("Fields must be a subset of the existing fixed read projection.")
        values = {key: value for key, value in values.items() if key in selection.content_fields}
    fields: list[ColumnElement[Any]] = []
    for key, value in values.items():
        fields.extend((literal(key), value))
    content = func.jsonb_strip_nulls(func.jsonb_build_object(*fields))
    base = (
        select(
            stable_id.label("row_id"),
            literal(kind).label("fact_kind"),
            _LINEAGE.c.fact_source_key.label("source_key"),
            content.label("content"),
            current_version.label("expected_version"),
            func.app.school_fact_current_version(school).label("school_fact_version"),
            valid_from.label("valid_from"),
            valid_to.label("valid_to"),
            literal("formal_publication").label("source_kind"),
            _LINEAGE.c.batch_id.label("publication_id"),
            _LINEAGE.c.created_at.label("published_at"),
            _LINEAGE.c.target_version.label("fact_version"),
        )
        .select_from(relation)
        .where(*predicates)
    )
    count = select(func.count()).select_from(base.subquery())
    metadata_columns = (
        stable_id.label("row_id"),
        fact.c.id.label("content_row_id"),
        name_identity.label("name_row_id"),
        name_version.label("name_version"),
        _LINEAGE.c.batch_id.label("publication_id"),
        _LINEAGE.c.target_version.label("fact_version"),
    )
    counted_metadata = _counted_metadata(
        base.with_only_columns(*metadata_columns, maintain_column_froms=True),
        after_id=after_id,
        page_size=page_size,
    )
    if after_id is not None:
        base = base.where(stable_id > after_id)
    data = base.order_by(stable_id).limit(page_size + 1)
    # Same FROM, joins, scope, selector, time, ordering and lookahead as the
    # authorized page. Stable object IDs alone miss temporal content/name changes.
    metadata = data.with_only_columns(
        *metadata_columns,
        maintain_column_froms=True,
    )
    return CompiledReadRelation(
        operation.code, as_of, page_size, F2_ROW_FIELDS, count, data, metadata, counted_metadata
    )
