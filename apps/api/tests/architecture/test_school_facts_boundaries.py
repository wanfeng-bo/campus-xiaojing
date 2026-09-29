from __future__ import annotations

import ast
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[2]

SCOPED_MODEL_FILES = (
    API_ROOT / "app/platform/tenancy/models.py",
    API_ROOT / "app/platform/directory/models.py",
    API_ROOT / "app/platform/organization/models.py",
    API_ROOT / "app/platform/identity/models.py",
    API_ROOT / "app/platform/master_data_imports/models.py",
    API_ROOT / "app/platform/policies/models.py",
    API_ROOT / "app/domains/campus/models.py",
    API_ROOT / "app/domains/students/models.py",
)

NEW_SF1_TABLES = {
    "school_versions",
    "school_person_profile_versions",
    "campus_location_aliases",
    "dorm_room_profile_versions",
    "dorm_beds",
    "dorm_bed_versions",
    "student_school_relationships",
    "student_bed_residences",
    "person_contact_points",
    "official_contact_points",
    "student_contact_relationships",
    "school_policies",
    "school_policy_versions",
    "school_fact_sources",
}

FORBIDDEN_IMPORT_PREFIXES = (
    "app.domains.repairs",
    "app.platform.assistant",
    "app.platform.responsibility",
)

FORBIDDEN_TABLE_PARTS = (
    "workflow",
    "admission",
    "graduation",
    "repair",
    "attendance",
    "course",
    "grade",
    "approval_chain",
)

FORBIDDEN_WORKFLOW_STATES = {
    "draft",
    "submitted",
    "pending",
    "reviewing",
    "applying",
    "failed",
    "completed",
}


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _school_fact_violations(source: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    violations: list[str] = []
    current_table: str | None = None

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(FORBIDDEN_IMPORT_PREFIXES):
                violations.append(f"forbidden import:{node.module}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(FORBIDDEN_IMPORT_PREFIXES):
                    violations.append(f"forbidden import:{alias.name}")

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        current_table = None
        class_strings: set[str] = set()
        for item in ast.walk(node):
            value = _literal_string(item)
            if value is not None:
                class_strings.add(value.lower())
            if isinstance(item, ast.Assign):
                if any(
                    isinstance(target, ast.Name) and target.id == "__tablename__"
                    for target in item.targets
                ):
                    current_table = _literal_string(item.value)
        if current_table is None:
            continue
        lowered_table = current_table.lower()
        if any(part in lowered_table for part in FORBIDDEN_TABLE_PARTS):
            violations.append(f"forbidden table:{current_table}")
        if current_table not in NEW_SF1_TABLES:
            continue
        for value in class_strings:
            for state in FORBIDDEN_WORKFLOW_STATES:
                if f"'{state}'" in value or f'"{state}"' in value:
                    violations.append(f"workflow state:{current_table}:{state}")
    return tuple(sorted(set(violations)))


def _declared_table_names(path: Path) -> set[str]:
    table_names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__tablename__" for target in node.targets
        ):
            continue
        value = _literal_string(node.value)
        if value is not None:
            table_names.add(value)
    return table_names


@pytest.mark.parametrize(
    "source",
    [
        "from app.domains.repairs.models import RepairTicket\n",
        "from app.platform.responsibility.models import ResponsibilityGroup\n",
        "class AdmissionWorkflow:\n    __tablename__ = 'student_admission_workflow'\n",
        (
            "class SchoolVersion:\n"
            "    __tablename__ = 'school_versions'\n"
            "    CheckConstraint(\"status IN ('draft', 'completed')\")\n"
        ),
    ],
)
def test_school_fact_guard_rejects_business_or_workflow_counterexamples(source: str) -> None:
    assert _school_fact_violations(source)


def test_real_sf1_models_have_no_business_dependencies_or_management_flows() -> None:
    missing = [str(path) for path in SCOPED_MODEL_FILES if not path.is_file()]
    assert not missing, f"缺少 SF1 模型文件：{missing}"

    violations: list[str] = []
    for path in SCOPED_MODEL_FILES:
        violations.extend(_school_fact_violations(path.read_text(encoding="utf-8")))
    assert not violations, violations


def test_sf1_does_not_create_excluded_business_tables() -> None:
    table_names: set[str] = set()
    for path in SCOPED_MODEL_FILES:
        assert path.is_file(), path
        table_names.update(_declared_table_names(path))

    excluded_exact = {
        "student_admissions",
        "student_graduations",
        "student_transfer_workflows",
        "dorm_transfer_workflows",
        "repair_tickets",
        "repair_responsibility_grants",
        "courses",
        "class_memberships",
        "attendance_records",
        "grade_records",
    }
    assert excluded_exact.isdisjoint(table_names)
    campus_tables = _declared_table_names(API_ROOT / "app/domains/campus/models.py")
    student_tables = _declared_table_names(API_ROOT / "app/domains/students/models.py")
    assert "student_bed_residences" in student_tables
    assert "student_bed_residences" not in campus_tables
