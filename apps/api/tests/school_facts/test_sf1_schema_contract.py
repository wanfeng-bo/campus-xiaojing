from __future__ import annotations

import ast
import importlib
import importlib.util
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import CheckConstraint, Table, UniqueConstraint
from sqlalchemy.dialects.postgresql import ExcludeConstraint

from app.db.base import Base
from app.domains.campus import models as campus_models  # noqa: F401
from app.domains.students import models as student_models  # noqa: F401
from app.platform.calendar import models as calendar_models  # noqa: F401
from app.platform.directory import models as directory_models  # noqa: F401
from app.platform.identity import models as identity_models  # noqa: F401
from app.platform.master_data_imports import models as import_models  # noqa: F401
from app.platform.organization import models as organization_models  # noqa: F401
from app.platform.tenancy import models as tenancy_models  # noqa: F401

AUTHORITATIVE_TABLES = {
    "schools",
    "school_versions",
    "school_people",
    "school_person_names",
    "school_person_profile_versions",
    "users",
    "account_person_bindings",
    "account_person_binding_versions",
    "organization_units",
    "organization_unit_versions",
    "staff_assignments",
    "staff_assignment_versions",
    "campus_locations",
    "campus_location_versions",
    "campus_location_aliases",
    "dorm_room_profile_versions",
    "dorm_beds",
    "dorm_bed_versions",
    "students",
    "student_school_relationships",
    "student_bed_residences",
    "person_contact_points",
    "official_contact_points",
    "student_contact_relationships",
    "school_policies",
    "school_policy_versions",
    "school_fact_sources",
}

SF1_CREATED_TABLES = {
    "school_versions",
    "school_person_profile_versions",
    "person_contact_points",
    "official_contact_points",
    "campus_location_aliases",
    "dorm_room_profile_versions",
    "dorm_beds",
    "dorm_bed_versions",
    "student_school_relationships",
    "student_bed_residences",
    "student_contact_relationships",
    "school_policies",
    "school_policy_versions",
    "school_fact_sources",
}

STABLE_ROOTS = {
    "schools",
    "school_people",
    "organization_units",
    "campus_locations",
    "dorm_beds",
    "students",
    "school_policies",
}

VERSIONED_FACTS = {
    "school_versions",
    "school_person_profile_versions",
    "campus_location_aliases",
    "dorm_room_profile_versions",
    "dorm_bed_versions",
    "student_school_relationships",
    "student_bed_residences",
    "person_contact_points",
    "official_contact_points",
    "student_contact_relationships",
    "school_policy_versions",
}


def _load_policy_models() -> None:
    try:
        spec = importlib.util.find_spec("app.platform.policies.models")
    except ModuleNotFoundError:
        spec = None
    assert spec is not None, "SF1 必须提供制度资料 ORM 包"
    importlib.import_module("app.platform.policies.models")


def _table(name: str) -> Table:
    _load_policy_models()
    assert name in Base.metadata.tables, f"缺少 SF1 表：{name}"
    return cast(Table, Base.metadata.tables[name])


def _check_texts(table: Table) -> tuple[str, ...]:
    return tuple(
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    )


def test_sf1_registers_only_the_approved_authoritative_fact_families() -> None:
    _load_policy_models()
    missing = AUTHORITATIVE_TABLES - set(Base.metadata.tables)

    assert not missing, f"缺少 SF1 权威事实表：{sorted(missing)}"

    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "20260819_15_school_facts_schema.py"
    )
    migration_tree = ast.parse(migration_path.read_text(encoding="utf-8"))
    created_tables = {
        call.args[0].value
        for call in ast.walk(migration_tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "op"
        and call.func.attr == "create_table"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    }
    for call in ast.walk(migration_tree):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "op"
            and call.func.attr == "execute"
        ):
            continue
        for node in ast.walk(call):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            created_tables.update(
                match.group(1)
                for match in re.finditer(
                    r"\bCREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+"
                    r"(?:public\.)?([a-z_][a-z0-9_]*)",
                    node.value,
                    flags=re.IGNORECASE,
                )
            )
    assert created_tables == SF1_CREATED_TABLES


def test_stable_roots_separate_internal_ids_from_immutable_source_keys() -> None:
    for table_name in STABLE_ROOTS:
        table = _table(table_name)
        assert {"id", "source_key"} <= set(table.c.keys()), table_name
        unique_column_sets = {
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        expected = ("source_key",) if table_name == "schools" else ("school_id", "source_key")
        assert expected in unique_column_sets, table_name

    versioned_business_codes = {
        "school_versions": "business_code",
        "school_person_profile_versions": "business_code",
        "organization_unit_versions": "business_code",
        "campus_location_versions": "business_code",
        "dorm_bed_versions": "business_code",
        "student_school_relationships": "student_number",
        "school_policy_versions": "business_code",
    }
    for table_name, column_name in versioned_business_codes.items():
        assert column_name in _table(table_name).c, table_name


def test_versioned_facts_have_positive_versions_half_open_periods_and_lineage() -> None:
    for table_name in VERSIONED_FACTS:
        table = _table(table_name)
        assert {"valid_from", "valid_to", "version", "source_id", "import_batch_id"} <= set(
            table.c.keys()
        ), table_name
        checks = " ".join(_check_texts(table)).lower()
        assert "version >= 1" in checks, table_name
        assert "valid_to is null or valid_to > valid_from" in checks, table_name


def test_login_identity_is_namespaced_and_never_uses_display_name_as_authority() -> None:
    users = _table("users")
    assert {"provider_code", "provider_tenant_key", "external_subject"} <= set(users.c.keys())
    assert users.c.display_name.info.get("school_facts_authority") == "legacy_login_label"
    unique_column_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in users.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("provider_code", "provider_tenant_key", "external_subject") in unique_column_sets
    assert ("external_subject",) not in unique_column_sets

    bindings = _table("account_person_bindings")
    assert "source_key" in bindings.c
    assert "display_name" not in bindings.c


def test_assignment_and_login_content_slices_separate_technical_and_business_time() -> None:
    assignment = _table("staff_assignment_versions")
    assert {
        "assignment_id",
        "person_id",
        "organization_unit_id",
        "position_title",
        "semantic_role_code",
        "assignment_kind",
        "appointment_source",
        "approval_reference",
        "effective_from",
        "effective_to",
        "planned_valid_to",
        "revoked_at",
        "target_version",
        "source_ref",
        "source_id",
        "import_batch_id",
    } <= set(assignment.c.keys())
    login = _table("account_person_binding_versions")
    assert {
        "binding_id",
        "account_user_id",
        "person_id",
        "provider_code",
        "provider_tenant_key",
        "external_subject",
        "effective_from",
        "effective_to",
        "revoked_at",
        "target_version",
        "source_ref",
        "source_id",
        "import_batch_id",
    } <= set(login.c.keys())
    assert "display_name" not in login.c
    for table in (assignment, login):
        checks = " ".join(_check_texts(table)).lower()
        assert "effective_to is null or effective_to > effective_from" in checks
        assert "target_version >= 1" in checks

    assignment_exclusions = {
        constraint.name
        for constraint in assignment.constraints
        if isinstance(constraint, ExcludeConstraint)
    }
    assert {
        "excl_staff_assignment_versions_confirmed_period",
        "excl_staff_assignment_versions_confirmed_slot_period",
    } <= assignment_exclusions
    login_exclusions = {
        constraint.name
        for constraint in login.constraints
        if isinstance(constraint, ExcludeConstraint)
    }
    assert {
        "excl_account_person_binding_versions_confirmed_period",
        "excl_account_person_binding_versions_account_period",
        "excl_account_person_binding_versions_person_period",
    } <= login_exclusions


def test_housing_relations_are_owned_by_students_and_do_not_duplicate_occupancy() -> None:
    campus_tables = {
        table.name
        for table in Base.metadata.tables.values()
        if table.name.startswith(("campus_", "dorm_"))
    }
    assert "student_bed_residences" not in campus_tables

    residences = _table("student_bed_residences")
    assert {"student_id", "bed_id"} <= set(residences.c.keys())
    assert {
        "transfer_group_id",
        "transfer_role",
        "arrived_from_residence_id",
        "departed_to_residence_id",
    }.isdisjoint(residences.c.keys())
    assert "display_name" not in residences.c
    unique_column_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in residences.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("school_id", "id") in unique_column_sets

    beds = _table("dorm_beds")
    bed_versions = _table("dorm_bed_versions")
    assert "occupancy_status" not in beds.c
    assert "occupancy_status" not in bed_versions.c
    availability_checks = " ".join(_check_texts(bed_versions)).lower()
    assert "occupied" not in availability_checks


def test_student_contact_is_neutral_and_carries_no_guardian_or_permission_fact() -> None:
    relationship = _table("student_contact_relationships")
    assert {
        "student_id",
        "contact_person_id",
        "relationship_kind",
        "priority",
        "is_emergency",
    } <= set(relationship.c.keys())
    forbidden = {
        "guardian_status",
        "custody_status",
        "can_view_student",
        "can_approve_leave",
        "permission_code",
    }
    assert forbidden.isdisjoint(relationship.c.keys())


def test_contact_points_record_visibility_without_granting_business_permissions() -> None:
    personal = _table("person_contact_points")
    official = _table("official_contact_points")

    assert {"visibility_scope", "audience_code", "consent_reference"} <= set(personal.c.keys())
    assert {"visibility_scope", "audience_code"} <= set(official.c.keys())
    personal_checks = " ".join(_check_texts(personal)).lower()
    official_checks = " ".join(_check_texts(official)).lower()
    assert getattr(personal.c.visibility_scope.default, "arg", None) == "school_authenticated"
    assert (
        str(getattr(personal.c.visibility_scope.server_default, "arg", ""))
        == "school_authenticated"
    )
    assert "visibility_scope = 'school_authenticated'" in personal_checks
    assert "consent_reference is not null" not in personal_checks
    assert "visibility_scope = 'restricted'" in official_checks
    assert "audience_code is not null" in official_checks


def test_approved_assignment_student_and_bed_vocabularies_are_exact() -> None:
    assignments = _table("staff_assignments")
    assignment_checks = " ".join(_check_texts(assignments)).lower()
    student_checks = " ".join(_check_texts(_table("student_school_relationships"))).lower()
    bed_checks = " ".join(_check_texts(_table("dorm_bed_versions"))).lower()

    assert "('substantive', 'part_time', 'acting')" in assignment_checks
    assert "regular" not in assignment_checks
    assert getattr(assignments.c.assignment_kind.default, "arg", None) == "substantive"
    assert str(getattr(assignments.c.assignment_kind.server_default, "arg", "")) == "substantive"
    assert (
        "('enrolled', 'suspended', 'transferred_out', 'withdrawn', 'graduated')" in student_checks
    )
    assert "'leave'" not in student_checks
    assert "'transferred'" not in student_checks
    assert "('available', 'unavailable', 'inactive')" in bed_checks
    assert "retired" not in bed_checks


def test_legacy_classes_stay_disabled_and_f3_guardian_is_authoritative() -> None:
    legacy_tables = {
        "administrative_classes",
        "administrative_class_versions",
        "student_enrollments",
        "school_calendar_versions",
        "school_calendar_days",
    }
    for table_name in legacy_tables:
        table = cast(Table, Base.metadata.tables[table_name])
        assert table.info.get("school_facts_authority") == "legacy_disabled", table_name
    # F3 promoted the original Guardian owner; F4 never promotes legacy class tables.
    guardian = cast(Table, Base.metadata.tables["guardian_relationships"])
    assert guardian.info.get("school_facts_authority") != "legacy_disabled"


def test_policy_contract_and_reader_are_strict_read_only_public_boundaries() -> None:
    try:
        contracts_spec = importlib.util.find_spec("app.platform.policies.contracts")
        ports_spec = importlib.util.find_spec("app.platform.policies.ports")
    except ModuleNotFoundError:
        contracts_spec = None
        ports_spec = None
    assert contracts_spec is not None
    assert ports_spec is not None

    contracts = importlib.import_module("app.platform.policies.contracts")
    ports = importlib.import_module("app.platform.policies.ports")
    snapshot_type = contracts.PolicyVersionSnapshot

    assert {
        "policy_id",
        "policy_version_id",
        "school_id",
        "business_code",
        "title",
        "classification",
        "effective_from",
        "effective_to",
        "supersedes_policy_version_id",
    } <= set(snapshot_type.model_fields)
    assert hasattr(ports.PolicyVersionReader, "get_effective_snapshot")
    assert not hasattr(ports.PolicyVersionReader, "add")
    assert not hasattr(ports.PolicyVersionReader, "save")

    snapshot = snapshot_type(
        policy_id=uuid4(),
        policy_version_id=uuid4(),
        school_id=uuid4(),
        business_code="POL-1",
        title="制度一",
        classification="internal",
        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
        effective_to=None,
        supersedes_policy_version_id=None,
        captured_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    invalid = snapshot.model_dump()
    invalid["unexpected"] = "authority"
    try:
        snapshot_type.model_validate(invalid)
    except ValidationError as error:
        assert any(item["type"] == "extra_forbidden" for item in error.errors())
    else:
        raise AssertionError("制度资料公开契约必须拒绝额外权威字段")
