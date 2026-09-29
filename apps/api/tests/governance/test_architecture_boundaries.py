from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import get_type_hints

from app.platform.assistant.contracts import ActionOfferV1
from app.platform.authorization.thin_contracts import (
    ThinAuthorizationBasis,
    ThinSelfAuthorizationBasis,
)
from app.platform.authorization.thin_service import ThinAuthorizationService

API_ROOT = Path(__file__).resolve().parents[2]


def _imports(path: Path) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            if node.module is not None:
                imported.add(f"{prefix}{node.module}")
            else:
                imported.update(f"{prefix}{alias.name}" for alias in node.names)
    return imported


def test_governance_authority_does_not_absorb_school_facts_or_legacy_authority() -> None:
    governance_files = sorted((API_ROOT / "app" / "platform" / "governance").glob("*.py"))
    assert governance_files
    forbidden_import_prefixes = (
        "app.composition.school_fact_persistence",
        "app.platform.school_facts",
        "app.platform.responsibility",
        "app.platform.authorization.dependencies",
        "app.platform.authorization.models",
        "app.platform.authorization.router",
        "app.platform.authorization.service",
    )
    violations: dict[str, list[str]] = {}
    for path in governance_files:
        banned = sorted(
            imported
            for imported in _imports(path)
            if imported.startswith(forbidden_import_prefixes)
        )
        if banned:
            violations[path.name] = banned
        source = path.read_text(encoding="utf-8")
        assert "CanonicalFactChangeSet" not in source
        assert "school_fact_change_lineage" not in source
        assert "SchoolFactPersistence" not in source
    assert violations == {}

    adapter = API_ROOT / "app" / "composition" / "governance_dependencies.py"
    adapter_imports = _imports(adapter)
    assert {
        "app.platform.directory.repository",
        "app.platform.organization.repository",
    } <= adapter_imports
    assert all(not imported.startswith(forbidden_import_prefixes) for imported in adapter_imports)


def test_action_offer_is_not_a_governance_authorization_proof() -> None:
    assert set(ActionOfferV1.model_fields) == {"action_offer_ref", "safe_label"}
    assert {
        "governance",
        "authorization_basis",
        "assignment_epoch",
        "dependency_versions",
        "valid_until",
    }.isdisjoint(ActionOfferV1.model_fields)

    revalidate = inspect.signature(ThinAuthorizationService.revalidate)
    assert "action_offer" not in revalidate.parameters
    assert get_type_hints(ThinAuthorizationService.revalidate)["basis"] == (
        ThinAuthorizationBasis | ThinSelfAuthorizationBasis
    )

    assistant_files = sorted((API_ROOT / "app" / "platform" / "assistant").glob("*.py"))
    assert assistant_files
    for path in assistant_files:
        assert all(
            not imported.startswith("app.platform.governance") for imported in _imports(path)
        )
