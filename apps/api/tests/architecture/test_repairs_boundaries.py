from __future__ import annotations

import ast
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
REPAIRS_ROOT = API_ROOT / "app" / "domains" / "repairs"
DEEPSEEK_CLIENT = API_ROOT / "app" / "integrations" / "deepseek" / "client.py"
APP_MAIN = API_ROOT / "app" / "main.py"
PROJECT_ROOT = API_ROOT.parents[1]
RETIRED_REPAIRS_RUNTIME_PATHS = (
    "apps/api/app/interfaces/http/repairs.py",
    "apps/api/app/interfaces/http/repair_schemas.py",
    "apps/api/app/composition/repairs.py",
    "apps/api/app/composition/repair_persistence.py",
    "apps/api/app/domains/repairs/application.py",
    "apps/api/app/domains/repairs/card_edit.py",
    "apps/api/app/domains/repairs/draft_dialogue.py",
    "apps/api/app/domains/repairs/entry_interpretation.py",
    "apps/api/app/domains/repairs/policy.py",
    "apps/api/app/domains/repairs/presentation.py",
    "apps/api/app/domains/repairs/readers.py",
    "apps/api/app/domains/repairs/repository.py",
    "apps/api/app/domains/repairs/routing.py",
    "scripts/load_repair_alpha_demo.py",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_legacy_repairs_router_is_not_mounted_in_the_production_app() -> None:
    source = APP_MAIN.read_text(encoding="utf-8")

    assert "app.interfaces.http.repairs" not in _imports(APP_MAIN)
    assert "repairs_router" not in source


def test_retired_repairs_runtime_files_do_not_return_to_active_tree() -> None:
    assert [
        relative_path
        for relative_path in RETIRED_REPAIRS_RUNTIME_PATHS
        if (PROJECT_ROOT / relative_path).exists()
    ] == []


def test_repairs_use_public_owner_contracts_and_not_legacy_grants() -> None:
    violations: list[str] = []
    for path in REPAIRS_ROOT.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        imports = _imports(path)
        if "repair_responsibility_grants" in source:
            violations.append(f"{path.name}: references legacy repair grants")
        violations.extend(
            f"{path.name}: imports private owner implementation {module}"
            for module in imports
            if module.endswith(".models")
            and module
            not in {
                "app.domains.repairs.models",
            }
        )

    assert violations == []


def test_task3_does_not_create_a_generic_authority_platform() -> None:
    forbidden = {"AuthorityFence", "RepairCommandGuard", "TicketMutationGate"}
    declared: set[str] = set()
    for path in REPAIRS_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        declared.update(node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef))

    assert declared.isdisjoint(forbidden)


def test_deepseek_adapter_does_not_import_legacy_runtime_or_database_layers() -> None:
    model_imports = _imports(DEEPSEEK_CLIENT)

    assert not any(
        imported.startswith(
            (
                "app.interfaces",
                "app.domains.repairs.application",
                "app.domains.repairs.repository",
                "app.db",
            )
        )
        for imported in model_imports
    )


def test_repairs_ports_declare_no_authority_fence_or_guard_type() -> None:
    path = REPAIRS_ROOT / "ports.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    forbidden_fragments = ("authority", "fence", "guard")
    declared = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]

    assert [
        name
        for name in declared
        if any(fragment in name.lower() for fragment in forbidden_fragments)
    ] == []


def test_card_edit_model_port_is_single_and_has_no_registry_or_retry() -> None:
    path = REPAIRS_ROOT / "ports.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    edit_port = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "RepairCardEditModelPort"
        ),
        None,
    )

    assert edit_port is not None
    assert {
        node.name
        for node in edit_port.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    } == {"interpret_card_edit"}
    assert [
        forbidden for forbidden in ("Registry", "Planner", "Retry") if forbidden in source
    ] == []


def test_repair_presentation_port_exposes_no_write_operation() -> None:
    path = REPAIRS_ROOT / "ports.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    presentation_port = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RepairPresentationFactReader"
    )
    methods = {
        child.name
        for child in presentation_port.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert methods == {"get_ticket_projections", "list_relation_seeds"}
    assert not any(
        fragment in method.lower()
        for method in methods
        for fragment in ("add", "commit", "delete", "insert", "update", "write")
    )
