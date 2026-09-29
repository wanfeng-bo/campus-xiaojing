from __future__ import annotations

import ast
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[2]
PURE_ROOT = API_ROOT / "app/platform/school_facts"
COMPOSITION_FILE = API_ROOT / "app/composition/school_fact_persistence.py"

_PURE_ALLOWED_ROOTS = {
    "__future__",
    "collections",
    "dataclasses",
    "datetime",
    "hashlib",
    "json",
    "re",
    "secrets",
    "typing",
    "uuid",
    "pydantic",
    "app.platform.contracts",
    "app.platform.school_facts.contracts",
    "app.platform.school_facts.ports",
}
_FORBIDDEN_CONSUMER_PREFIXES = (
    "app.platform.assistant",
    "app.domains.repairs",
    "app.platform.responsibility",
)
_PRIVATE_SCHOOL_FACT_PREFIXES = (
    "app.platform.school_facts.application",
    "app.platform.school_facts.ports",
    "app.composition.school_fact_persistence",
)


def _imports(source: str) -> tuple[str, ...]:
    modules: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Name) and node.id == "__import__":
            modules.append("dynamic:__import__")
    return tuple(modules)


def _pure_violations(source: str, *, policy: bool = False) -> tuple[str, ...]:
    allowed_roots = _PURE_ALLOWED_ROOTS | (
        {"app.platform.authorization.thin_contracts", "app.platform.governance.contracts"}
        if policy
        else set()
    )
    violations: list[str] = []
    for module in _imports(source):
        if module.startswith("dynamic:") or not any(
            module == allowed or module.startswith(f"{allowed}.") for allowed in allowed_roots
        ):
            violations.append(module)
    return tuple(sorted(set(violations)))


@pytest.mark.parametrize(
    "source",
    [
        "from sqlalchemy.orm import Session\n",
        "from app.db.session import get_session\n",
        "from app.domains.repairs.models import RepairTicket\n",
        "from app.platform.directory.models import SchoolPerson\n",
        "loader = __import__\nmodule = loader('app.db.session')\n",
    ],
)
def test_sf2_pure_guard_rejects_persistence_or_business_counterexamples(
    source: str,
) -> None:
    assert _pure_violations(source)


def test_sf2_contract_ports_and_application_are_pure() -> None:
    expected = {
        "__init__.py",
        "application.py",
        "change_boundary.py",
        "contracts.py",
        "executor_contracts.py",
        "initialization_contracts.py",
        "read_policy.py",
        "ports.py",
    }
    assert {path.name for path in PURE_ROOT.glob("*.py")} == expected
    violations: dict[str, tuple[str, ...]] = {}
    for path in PURE_ROOT.glob("*.py"):
        found = _pure_violations(
            path.read_text(encoding="utf-8"), policy=path.name == "read_policy.py"
        )
        if found:
            violations[path.name] = found
    assert violations == {}


def test_sf2_writer_and_confirmation_ports_are_not_imported_by_business_consumers() -> None:
    violations: list[str] = []
    for prefix in _FORBIDDEN_CONSUMER_PREFIXES:
        root = API_ROOT / Path(*prefix.split("."))
        for path in root.rglob("*.py"):
            for module in _imports(path.read_text(encoding="utf-8")):
                if module.startswith(_PRIVATE_SCHOOL_FACT_PREFIXES):
                    violations.append(f"{path.relative_to(API_ROOT)}:{module}")
    assert violations == []


def test_sf2_persistence_is_composition_only_and_not_attached_to_production_http() -> None:
    assert COMPOSITION_FILE.is_file()
    production_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            API_ROOT / "app/main.py",
            API_ROOT / "app/interfaces/http/admin.py",
            API_ROOT / "app/interfaces/http/assistant.py",
        )
        if path.is_file()
    )
    assert "school_fact_persistence" not in production_sources
    assert "SchoolFactChangeService" not in production_sources


def test_fact_policy_can_import_only_thin_and_governance_contracts_without_widening_writer():
    source = "from app.platform.authorization.thin_contracts import ThinAuthorizationRequest"
    assert _pure_violations(source)
    assert not _pure_violations(source, policy=True)
    for module in (
        "app.platform.authorization.thin_service",
        "app.platform.governance.reader",
        "app.platform.governance.application",
        "app.platform.assistant.runtime",
        "sqlalchemy",
    ):
        assert _pure_violations(f"import {module}", policy=True)
