from __future__ import annotations

import ast
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = API_ROOT / "app"
MIGRATION_ROOT = API_ROOT / "alembic" / "versions"
AI_ROOTS = (
    "app/ai",
    "app/llm",
    "app/rag",
    "app/integrations/ai",
    "app/integrations/llm",
    "app/integrations/rag",
    "app/platform/ai",
    "app/platform/llm",
    "app/platform/rag",
)


def _write(root: Path, relative: str, source: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return target


def _python_files(root: Path) -> Iterable[Path]:
    return root.rglob("*.py") if root.exists() else ()


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolved_imports(path: Path, root: Path, tree: ast.AST) -> set[str]:
    imported: set[str] = set()
    package = _module_name(path, root).split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                module = ".".join((*base, *(node.module or "").split("."))).strip(".")
            else:
                module = node.module or ""
            if module:
                imported.add(module)
                imported.update(f"{module}.{alias.name}" for alias in node.names)
    return imported


def _legacy_imports(tree: ast.AST) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    return imported


def _import_aliases(path: Path, root: Path, tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    package = _module_name(path, root).split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                module = ".".join((*base, *(node.module or "").split("."))).strip(".")
            else:
                module = node.module or ""
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{module}.{alias.name}".strip(".")
    return aliases


def _dotted_name(node: ast.AST, aliases: dict[str, str] | None = None) -> str:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append((aliases or {}).get(current.id, current.id))
    return ".".join(reversed(parts))


def _field_names(node: ast.AST) -> set[str]:
    fields: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            fields.add(child.target.id)
        elif isinstance(child, ast.Assign):
            fields.update(target.id for target in child.targets if isinstance(target, ast.Name))
    return fields


def _violations(paths: Iterable[Path], check: Callable[[Path, ast.Module], list[str]]) -> list[str]:
    return [violation for path in paths for violation in check(path, _tree(path))]


def _is_public_contract_or_port(imported: str) -> bool:
    parts = imported.split(".")
    return "contracts" in parts or "ports" in parts


def _repair_violations(path: Path, root: Path, tree: ast.Module) -> list[str]:
    violations: list[str] = []
    for imported in _resolved_imports(path, root, tree):
        if not imported.startswith(("app.platform.", "app.domains.")):
            continue
        if imported.startswith("app.domains.repairs") or _is_public_contract_or_port(imported):
            continue
        violations.append(f"{path}: repairs may only import owner contracts/ports: {imported}")
    return violations


def _campus_violations(path: Path, root: Path, tree: ast.Module) -> list[str]:
    private_owners = ("app.platform.organization", "app.platform.responsibility")
    violations = [
        f"{path}: campus imports private owner implementation: {item}"
        for item in _resolved_imports(path, root, tree)
        if item.startswith(private_owners) and not _is_public_contract_or_port(item)
    ]
    forbidden_tokens = {
        "assignment",
        "employee",
        "person",
        "responsibility",
        "responsible",
        "staff",
        "teacher",
    }
    violations.extend(
        f"{path}: campus defines a people/responsibility field: {name}"
        for name in sorted(
            field
            for field in _field_names(tree)
            if forbidden_tokens & set(field.lower().split("_"))
        )
    )
    return violations


def _is_history_scope(path: Path, root: Path) -> bool:
    relative = path.relative_to(root).as_posix().lower()
    return any(segment in relative for segment in ("students", "campus", "organization"))


def _historical_reader_violations(path: Path, root: Path, tree: ast.Module) -> list[str]:
    if not _is_history_scope(path, root):
        return []
    violations: list[str] = []

    def inspect(node: ast.AST, inherited_history_context: bool = False) -> None:
        history_context = inherited_history_context
        if isinstance(node, ast.ClassDef):
            normalized_name = node.name.lower()
            history_context |= not normalized_name.startswith("current") and (
                normalized_name.endswith(("reader", "resolver"))
                or any(token in normalized_name for token in ("history", "historical", "snapshot"))
            )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            history_context |= any(token in node.name.lower() for token in ("history", "snapshot"))
            if history_context:
                for call in (
                    candidate for candidate in ast.walk(node) if isinstance(candidate, ast.Call)
                ):
                    if _dotted_name(call.func).split(".")[-1].startswith("get_current_"):
                        violations.append(f"{path}: historical reader calls current selector")
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                inspect(child, history_context)

    inspect(tree)
    return violations


CURRENT_SELECTOR_ALLOWLIST = {
    "app/domains/campus/contracts.py:get_current_location",
    "app/domains/campus/repository.py:get_current_location",
    "app/domains/students/contracts.py:get_current_student_relation",
    "app/domains/students/contracts.py:get_current_class_roster",
    "app/domains/students/contracts.py:get_current_grade_roster",
    "app/domains/students/repository.py:get_current_student_relation",
    "app/domains/students/repository.py:get_current_class_roster",
    "app/domains/students/repository.py:get_current_grade_roster",
    "app/platform/identity/context.py:get_current_user",
    "app/platform/organization/contracts.py:get_current_assignment",
    "app/platform/organization/repository.py:get_current_assignment",
    "app/platform/responsibility/contracts.py:get_current_repair_assignment",
    "app/platform/responsibility/repository.py:get_current_repair_assignment",
    "app/platform/tenancy/contracts.py:get_current_school",
    "app/platform/tenancy/repository.py:get_current_school",
}


def _current_selector_violations(root: Path, path: Path, tree: ast.Module) -> list[str]:
    relative = path.relative_to(root).as_posix()
    return [
        f"{path}: current selector is not explicitly allowlisted"
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("get_current_")
        and f"{relative}:{node.name}" not in CURRENT_SELECTOR_ALLOWLIST
    ]


def _is_ai_module(path: Path, root: Path) -> bool:
    relative = path.relative_to(root).as_posix()
    return relative.startswith("app/integrations/") or any(
        relative == candidate or relative.startswith(f"{candidate}/") for candidate in AI_ROOTS
    )


def _ai_files(root: Path) -> Iterable[Path]:
    return (path for path in _python_files(root / "app") if _is_ai_module(path, root))


def _ai_violations(path: Path, root: Path, tree: ast.Module) -> list[str]:
    if not _is_ai_module(path, root):
        return []
    aliases = _import_aliases(path, root, tree)
    candidates = set(_resolved_imports(path, root, tree))
    candidates.update(
        _dotted_name(node, aliases) for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    )
    safe_readers = {"AiSafeEmergencyGuidanceReader", "AiSafeContactAvailabilityReader"}
    forbidden_symbols = {
        "UnitOfWork",
        "ActorContext",
        "RequestExecutionContext",
        "CommandExecutionContext",
        "ContextProofSigner",
    }
    violations: list[str] = []
    for candidate in sorted(candidates):
        leaf = candidate.rsplit(".", 1)[-1]
        normalized_leaf = leaf.lower()
        if leaf in safe_readers:
            continue
        if (
            candidate.startswith("app.db.uow")
            or ".repository" in candidate
            or ".models" in candidate
            or leaf in forbidden_symbols
            or leaf.endswith("Repository")
            or normalized_leaf.endswith(("reader", "resolver"))
        ):
            violations.append(f"{path}: AI/RAG imports forbidden dependency: {candidate}")
    return violations


def _is_transaction_module(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return (
        path.stem in {"application", "handler", "uow", "transaction"}
        or (
            path.stem == "service"
            and bool({"master_data_imports", "repairs"} & set(relative.parts))
        )
        or bool({"transaction", "transactions", "uow"} & set(relative.parts))
    )


def _transaction_violations(path: Path, root: Path, tree: ast.Module) -> list[str]:
    if not _is_transaction_module(path, root):
        return []
    aliases = _import_aliases(path, root, tree)
    candidates = set(_resolved_imports(path, root, tree))
    candidates.update(
        _dotted_name(node, aliases) for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    )
    forbidden_prefixes = (
        "app.integrations.",
        "app.platform.messaging.sender",
        "app.platform.messaging.delivery",
        "requests",
        "httpx",
        "httpcore",
        "aiohttp",
        "urllib",
        "socket",
        "redis",
        "boto3",
        "minio",
        "openai",
    )
    return [
        f"{path}: transaction module imports an external delivery/model/storage client: {candidate}"
        for candidate in sorted(candidates)
        if candidate.startswith(forbidden_prefixes)
    ]


def _repository_port_violations(path: Path, tree: ast.Module) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in {"commit", "rollback"}:
                violations.append(
                    f"{path}: repository/port defines transaction lifecycle: {node.name}"
                )
            parameters = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            if any(parameter.arg == "session_factory" for parameter in parameters):
                violations.append(f"{path}: repository/port accepts session_factory")
        if isinstance(node, (ast.AnnAssign, ast.Assign)) and "session_factory" in _field_names(
            node
        ):
            violations.append(f"{path}: repository/port stores session_factory")
        if isinstance(node, ast.Call) and _dotted_name(node.func).split(".")[-1] in {
            "commit",
            "rollback",
            "Session",
            "sessionmaker",
            "async_sessionmaker",
            "create_engine",
        }:
            violations.append(
                f"{path}: repository/port manages transaction or session lifecycle: "
                f"{_dotted_name(node.func)}"
            )
    return violations


def _base_name(node: ast.expr, aliases: dict[str, str]) -> str:
    return _dotted_name(node, aliases)


def _http_schema_violations(path: Path, tree: ast.Module) -> list[str]:
    aliases = _import_aliases(path, path.parent, tree)
    pydantic_models = {
        binding
        for binding, target in aliases.items()
        if target in {"pydantic.BaseModel", "pydantic.main.BaseModel"}
    }
    pydantic_models.add("BaseModel")
    derived_models: set[str] = set()
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    changed = True
    while changed:
        changed = False
        for class_node in classes:
            if class_node.name in derived_models:
                continue
            bases = {_base_name(base, aliases).rsplit(".", 1)[-1] for base in class_node.bases}
            if bases & (pydantic_models | derived_models):
                derived_models.add(class_node.name)
                changed = True
    input_suffixes = ("Request", "Command", "Payload", "Input", "Body", "Create", "Update", "Patch")
    violations: list[str] = []
    for class_node in classes:
        if class_node.name not in derived_models or not class_node.name.endswith(input_suffixes):
            continue
        for field in sorted(_field_names(class_node)):
            if (
                field in {"school_id", "person_id", "assignment_id", "guardian_id"}
                or field.endswith(("_school_id", "_person_id", "_assignment_id", "_guardian_id"))
                or field.startswith(("is_", "can_", "has_", "allow_"))
                or field
                in {
                    "permission",
                    "permissions",
                }
            ):
                violations.append(
                    f"{path}: HTTP schema exposes raw identity/permission field: {field}"
                )
    return violations


def _contains_environment_read(node: ast.AST, aliases: dict[str, str]) -> bool:
    return any(
        isinstance(candidate, ast.Call)
        and _dotted_name(candidate.func, aliases) in {"os.getenv", "os.environ.get"}
        or isinstance(candidate, ast.Subscript)
        and _dotted_name(candidate.value, aliases) == "os.environ"
        for candidate in ast.walk(node)
    )


def _rls_revision_violations(path: Path, tree: ast.Module) -> list[str]:
    aliases = _import_aliases(path, path.parent, tree)
    violations: list[str] = []
    if _contains_environment_read(tree, aliases):
        violations.append(f"{path}: RLS revision reads environment")
    if any(
        isinstance(node, ast.Constant) and node.value == "ENABLE_RLS" for node in ast.walk(tree)
    ):
        violations.append(f"{path}: RLS revision switches on ENABLE_RLS")
    for conditional in (node for node in ast.walk(tree) if isinstance(node, ast.If)):
        schema_operation = any(
            isinstance(candidate, ast.Call)
            and _dotted_name(candidate.func).startswith("op.")
            and _dotted_name(candidate.func).split(".")[-1]
            in {"execute", "create_table", "drop_table", "add_column", "drop_column"}
            for candidate in ast.walk(conditional)
        )
        if _contains_environment_read(conditional.test, aliases) and schema_operation:
            violations.append(f"{path}: RLS revision conditionally changes schema")
    return violations


def test_repairs_imports_only_owner_contracts_ports_or_own_layers(tmp_path: Path) -> None:
    allowed = _write(
        tmp_path,
        "app/domains/repairs/application.py",
        "from .internal import RepairPlan\n"
        "from app.platform.organization.contracts import AssignmentId\n",
    )
    relative_escape = _write(
        tmp_path,
        "app/domains/repairs/child.py",
        "from ...platform.organization import models\n",
    )
    rejected = _write(
        tmp_path,
        "app/domains/repairs/rejected.py",
        "from app.platform.organization.models import StaffAssignment\n",
    )
    assert _repair_violations(allowed, tmp_path, _tree(allowed)) == []
    assert _repair_violations(relative_escape, tmp_path, _tree(relative_escape))
    assert _repair_violations(rejected, tmp_path, _tree(rejected))


def test_campus_uses_public_owner_contracts_but_no_people_semantic_fields(tmp_path: Path) -> None:
    allowed = _write(
        tmp_path,
        "app/domains/campus/allowed.py",
        "from app.platform.organization.contracts import AssignmentId\nlocation_id: str\n",
    )
    rejected = _write(
        tmp_path,
        "app/domains/campus/rejected.py",
        "from app.platform.responsibility.models import ResponsibilityGroup\n"
        "maintenance_assignment_id: str\n",
    )
    assert _campus_violations(allowed, tmp_path, _tree(allowed)) == []
    assert _campus_violations(rejected, tmp_path, _tree(rejected))


def test_history_readers_and_all_current_selectors_resist_rename_escapes(tmp_path: Path) -> None:
    allowed = _write(
        tmp_path,
        "app/domains/students/history.py",
        "class StudentSnapshotReader:\n    def load(self):\n        return snapshot\n",
    )
    rejected = _write(
        tmp_path,
        "app/platform/organization/history.py",
        "class AssignmentHistoryReader:\n"
        "    def load_without_history_name(self):\n"
        "        return reader.get_current_assignment()\n",
    )
    selector = _write(
        tmp_path,
        "app/domains/students/future.py",
        "def get_current_future_student():\n    return None\n",
    )
    approved_selector = _write(
        tmp_path,
        "app/domains/campus/contracts.py",
        "def get_current_location():\n    return None\n",
    )
    assert _historical_reader_violations(allowed, tmp_path, _tree(allowed)) == []
    assert _historical_reader_violations(rejected, tmp_path, _tree(rejected))
    assert _current_selector_violations(tmp_path, approved_selector, _tree(approved_selector)) == []
    assert _current_selector_violations(tmp_path, selector, _tree(selector))


def test_ai_roots_reject_aliases_and_allow_exact_safe_reader(tmp_path: Path) -> None:
    allowed = _write(
        tmp_path,
        "app/integrations/rag/reader.py",
        "from app.platform.repairs.contracts import AiSafeEmergencyGuidanceReader\n",
    )
    rejected = _write(
        tmp_path,
        "app/ai/agent.py",
        "import app.db as database\nresult = database.uow.UnitOfWork()\n",
    )
    assert _ai_violations(allowed, tmp_path, _tree(allowed)) == []
    assert _ai_violations(rejected, tmp_path, _tree(rejected))


def test_transaction_scope_blocks_delivery_and_network_without_domain_false_positive(
    tmp_path: Path,
) -> None:
    allowed = _write(
        tmp_path,
        "app/platform/master_data_imports/service.py",
        "from app.platform.master_data_imports.contracts import ImportCommand\n",
    )
    rejected = _write(
        tmp_path,
        "app/platform/master_data_imports/application.py",
        "import requests as transport\ntransport.post('https://example.invalid')\n",
    )
    assert _transaction_violations(allowed, tmp_path, _tree(allowed)) == []
    assert _transaction_violations(rejected, tmp_path, _tree(rejected))


def test_repositories_and_ports_reject_definition_and_session_lifecycle_escapes(
    tmp_path: Path,
) -> None:
    allowed = _write(tmp_path, "repository.py", "def add(session):\n    session.add(object())\n")
    rejected = _write(
        tmp_path,
        "ports.py",
        "session_factory: object\ndef rollback(*, session_factory):\n    return Session()\n",
    )
    assert _repository_port_violations(allowed, _tree(allowed)) == []
    assert _repository_port_violations(rejected, _tree(rejected))


def test_http_schema_aliases_and_inheritance_reject_raw_ids_and_permission_flags(
    tmp_path: Path,
) -> None:
    allowed = _write(
        tmp_path,
        "schemas.py",
        "from pydantic import BaseModel as ApiModel\n"
        "class ImportRequest(ApiModel):\n    context_ref: str\n",
    )
    rejected = _write(
        tmp_path,
        "input_models.py",
        "import pydantic as p\n"
        "class Mutation(p.BaseModel):\n    pass\n"
        "class ImportRequest(Mutation):\n"
        "    maintenance_assignment_id: str\n"
        "    can_override: bool\n",
    )
    assert _http_schema_violations(allowed, _tree(allowed)) == []
    assert _http_schema_violations(rejected, _tree(rejected))


def test_rls_revisions_are_environment_independent_without_sql_string_detection(
    tmp_path: Path,
) -> None:
    allowed = _write(
        tmp_path / "allowed", "20260808_09_master_data_imports.py", "op.create_table('fact')\n"
    )
    rejected = _write(
        tmp_path / "rejected",
        "20260808_09_master_data_imports.py",
        "enabled = os.environ['ENABLE_RLS']\nif enabled:\n    op.create_table('fact')\n",
    )
    assert _rls_revision_violations(allowed, _tree(allowed)) == []
    assert _rls_revision_violations(rejected, _tree(rejected))


def test_review_regressions_cover_current_reader_ai_input_schema_and_any_migration(
    tmp_path: Path,
) -> None:
    current_reader = _write(
        tmp_path,
        "app/domains/students/current.py",
        "class CurrentStudentRosterReader:\n"
        "    def load(self):\n"
        "        return reader.get_current_student_relation()\n",
    )
    ai_reader = _write(
        tmp_path,
        "app/llm/context.py",
        "from app.platform.directory.contracts import CurrentStudentRosterReader\n",
    )
    input_schema = _write(
        tmp_path,
        "input_models.py",
        "from pydantic import BaseModel\nclass ImportPayload(BaseModel):\n    school_id: str\n",
    )
    arbitrary_migration = _write(
        tmp_path,
        "20990101_99_future_migration.py",
        "if os.getenv('ENABLE_RLS'):\n    op.create_table('future_fact')\n",
    )

    assert _historical_reader_violations(current_reader, tmp_path, _tree(current_reader)) == []
    assert _ai_violations(ai_reader, tmp_path, _tree(ai_reader))
    assert _http_schema_violations(input_schema, _tree(input_schema))
    assert _rls_revision_violations(arbitrary_migration, _tree(arbitrary_migration))


def test_review_two_semantic_and_scope_escapes_are_rejected(tmp_path: Path) -> None:
    campus = _write(
        tmp_path,
        "app/domains/campus/semantic.py",
        "responsible_staff_id: str\n",
    )
    archived_reader = _write(
        tmp_path,
        "app/domains/students/archived.py",
        "class ArchivedStudentReader:\n"
        "    def load(self):\n"
        "        return reader.get_current_student_relation()\n",
    )
    safe_current_reader = _write(
        tmp_path,
        "app/domains/students/current.py",
        "class CurrentStudentReader:\n"
        "    def load(self):\n"
        "        return reader.get_current_student_relation()\n",
    )
    calendar_reader = _write(
        tmp_path,
        "app/integrations/calendar/context.py",
        "from app.platform.calendar.contracts import CalendarReader\n",
    )
    deepseek_reader = _write(
        tmp_path,
        "app/integrations/deepseek/context.py",
        "from app.domains.students.contracts import StudentHistoryReader\n",
    )
    transaction_service = _write(
        tmp_path,
        "app/platform/master_data_imports/service.py",
        "import requests\n",
    )
    safe_transaction_service = _write(
        tmp_path / "safe",
        "app/platform/master_data_imports/service.py",
        "from app.platform.master_data_imports.contracts import ImportCommand\n",
    )

    assert _campus_violations(campus, tmp_path, _tree(campus))
    assert _historical_reader_violations(archived_reader, tmp_path, _tree(archived_reader))
    assert (
        _historical_reader_violations(safe_current_reader, tmp_path, _tree(safe_current_reader))
        == []
    )
    assert _ai_violations(calendar_reader, tmp_path, _tree(calendar_reader))
    assert _ai_violations(deepseek_reader, tmp_path, _tree(deepseek_reader))
    assert _transaction_violations(transaction_service, tmp_path, _tree(transaction_service))
    assert (
        _transaction_violations(safe_transaction_service, tmp_path, _tree(safe_transaction_service))
        == []
    )


def test_review_two_lifecycle_input_and_environment_alias_escapes_are_rejected(
    tmp_path: Path,
) -> None:
    lifecycle = _write(
        tmp_path,
        "ports.py",
        "def save(session):\n    session.commit()\n    session.rollback()\n",
    )
    allowed_input = _write(
        tmp_path / "allowed",
        "app/platform/repairs/input_models.py",
        "from pydantic import BaseModel\nclass RepairInput(BaseModel):\n    repair_id: str\n",
    )
    rejected_input = _write(
        tmp_path / "rejected",
        "app/platform/repairs/input_models.py",
        "from pydantic import BaseModel\n"
        "class RepairInput(BaseModel):\n    maintenance_assignment_id: str\n",
    )
    getenv_alias = _write(
        tmp_path,
        "20990101_99_alias_getenv.py",
        "import os as environment\nenvironment.getenv('ENABLE_RLS')\n",
    )
    environ_alias = _write(
        tmp_path,
        "20990101_99_alias_environ.py",
        "from os import environ as env\nvalue = env['ENABLE_RLS']\n",
    )

    assert _repository_port_violations(lifecycle, _tree(lifecycle))
    assert _http_schema_violations(allowed_input, _tree(allowed_input)) == []
    assert _http_schema_violations(rejected_input, _tree(rejected_input))
    assert _rls_revision_violations(getenv_alias, _tree(getenv_alias))
    assert _rls_revision_violations(environ_alias, _tree(environ_alias))


def test_review_two_archived_reader_is_not_a_current_reader(tmp_path: Path) -> None:
    archived_reader = _write(
        tmp_path,
        "app/domains/students/archived.py",
        "class ArchivedStudentReader:\n"
        "    def load(self):\n"
        "        return reader.get_current_student_relation()\n",
    )
    assert _historical_reader_violations(archived_reader, tmp_path, _tree(archived_reader))


def test_ai_file_enumeration_includes_every_integration_provider(tmp_path: Path) -> None:
    deepseek = _write(tmp_path, "app/integrations/deepseek/context.py", "pass\n")
    assert deepseek in set(_ai_files(tmp_path))


@pytest.mark.parametrize(
    ("relative", "source"),
    [
        (
            "app/integrations/calendar/context.py",
            "from app.platform.calendar.contracts import CalendarReader\n",
        ),
        (
            "app/integrations/deepseek/context.py",
            "from app.domains.students.contracts import StudentHistoryReader\n",
        ),
    ],
)
def test_review_two_all_integration_readers_are_rejected(
    tmp_path: Path,
    relative: str,
    source: str,
) -> None:
    reader = _write(tmp_path, relative, source)
    assert _ai_violations(reader, tmp_path, _tree(reader))


def test_review_two_master_data_service_cannot_send_network_requests(tmp_path: Path) -> None:
    service = _write(
        tmp_path,
        "app/platform/master_data_imports/service.py",
        "import requests\n",
    )
    assert _transaction_violations(service, tmp_path, _tree(service))


def test_review_two_repository_call_lifecycle_is_rejected(tmp_path: Path) -> None:
    repository = _write(
        tmp_path,
        "repository.py",
        "def save(session):\n    session.commit()\n    session.rollback()\n",
    )
    assert _repository_port_violations(repository, _tree(repository))


def test_review_two_repair_id_remains_a_valid_http_input_field(tmp_path: Path) -> None:
    schema = _write(
        tmp_path,
        "app/platform/repairs/input_models.py",
        "from pydantic import BaseModel\nclass RepairInput(BaseModel):\n    repair_id: str\n",
    )
    assert _http_schema_violations(schema, _tree(schema)) == []


@pytest.mark.parametrize(
    "source",
    [
        "import os as environment\nenvironment.getenv('OTHER')\n",
        "from os import environ as env\nvalue = env['OTHER']\n",
    ],
)
def test_review_two_migration_environment_aliases_are_rejected(tmp_path: Path, source: str) -> None:
    migration = _write(tmp_path, "20990101_99_alias.py", source)
    assert _rls_revision_violations(migration, _tree(migration))


def test_current_sources_obey_the_master_data_architecture_boundaries() -> None:
    repairs_root = APP_ROOT / "domains" / "repairs"
    ai_paths = _ai_files(API_ROOT)
    transaction_paths = (
        path for path in _python_files(APP_ROOT) if _is_transaction_module(path, API_ROOT)
    )
    repository_and_port_files = [
        path for path in _python_files(APP_ROOT) if path.name in {"repository.py", "ports.py"}
    ]

    assert (
        _violations(
            _python_files(repairs_root), lambda path, tree: _repair_violations(path, API_ROOT, tree)
        )
        == []
    )
    assert (
        _violations(
            _python_files(APP_ROOT / "domains" / "campus"),
            lambda path, tree: _campus_violations(path, API_ROOT, tree),
        )
        == []
    )
    assert (
        _violations(
            _python_files(APP_ROOT),
            lambda path, tree: _historical_reader_violations(path, API_ROOT, tree),
        )
        == []
    )
    assert [
        violation
        for path in _python_files(APP_ROOT)
        for violation in _current_selector_violations(API_ROOT, path, _tree(path))
    ] == []
    assert _violations(ai_paths, lambda path, tree: _ai_violations(path, API_ROOT, tree)) == []
    assert (
        _violations(
            transaction_paths, lambda path, tree: _transaction_violations(path, API_ROOT, tree)
        )
        == []
    )
    assert _violations(repository_and_port_files, _repository_port_violations) == []
    assert _violations(_python_files(APP_ROOT), _http_schema_violations) == []
    assert _violations(_python_files(MIGRATION_ROOT), _rls_revision_violations) == []
