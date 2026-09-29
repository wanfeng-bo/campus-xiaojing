from __future__ import annotations

import argparse
import ast
import os
import re
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

import pytest

PROJECT_ROOT: Final = Path(__file__).resolve().parents[3]
SKIP_AUDIT: Final = PROJECT_ROOT / "docs" / "engineering" / "Pre-K1基线跳过项审计.md"
POSTGRES_INTEGRATION_FLAG: Final = "RUN_POSTGRES_INTEGRATION"
DINGTALK_POSTGRES_INTEGRATION_FLAG: Final = "RUN_DINGTALK_POSTGRES_INTEGRATION"
_DINGTALK_POSTGRES_AUDIT_GROUP: Final = "正式钉钉独立 PostgreSQL 16 门（15 项）"
_DINGTALK_POSTGRES_NODES: Final = {
    "tests.test_live_school_scope_postgres::"
    "test_live_school_scope_metadata_guards_and_complete_keyset",
    "tests.test_live_school_scope_postgres::"
    "test_live_school_education_repository_reads_beyond_one_key_page",
    "tests.test_live_school_scope_writer_postgres::"
    "test_live_school_scope_writer_read_update_and_revoke",
    "tests.test_live_governance_postgres::test_live_batch_real_writer_after_dual_closure",
    "tests.test_live_initialization_postgres::"
    "test_formal_initialization_closes_and_preserves_upgrade",
    "tests.test_live_initialization_postgres::"
    "test_deferred_initialization_closes_without_publication_and_preserves_upgrade",
    "tests.test_live_initialization_postgres::"
    "test_deferred_initialization_rejects_publication_started_after_precheck",
    "tests.test_live_initialization_postgres::"
    "test_deferred_initialization_serializes_concurrent_publication",
    "tests.test_dingtalk_product_identity_postgres::"
    "test_formal_pg56_context_without_legacy_roles_returns_connections_and_logout_rejects",
    *(
        "tests.test_dingtalk_product_identity_postgres::"
        f"test_formal_pg56_current_change_rejects_retained_model_identity[{change}]"
        for change in (
            "provider",
            "corp",
            "security_version",
            "disabled",
            "mapping_revoked",
            "assignment_revoked",
        )
    ),
}

WINDOWS_ONLY_SYMLINK_SKIP: Final = (
    "tests/test_dingtalk_capacity_orchestrator.py::"
    "test_report_rejects_every_unsafe_target_before_writing[symlink]"
)
EXPECTED_PYTEST_OPTIONS: Final = {
    "testpaths": ["tests"],
    "addopts": "-ra",
    "markers": [
        "integration: requires the local PostgreSQL integration database",
        "destructive_migration: requires a function-scoped disposable migration database",
    ],
}
_FORBIDDEN_COLLECTION_HOOKS: Final = frozenset(
    {
        "pytest_collection",
        "pytest_collection_modifyitems",
        "pytest_collect_directory",
        "pytest_collect_file",
        "pytest_ignore_collect",
        "pytest_make_collect_report",
        "pytest_pycollect_makeitem",
    }
)
_FORBIDDEN_COLLECTION_ASSIGNMENTS: Final = frozenset(
    {"collect_ignore", "collect_ignore_glob", "pytest_plugins"}
)
_UUID = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_AUDITED_NODE = re.compile(r"^- `(?P<node>tests\.[^`]+)`$", re.MULTILINE)
_AUDIT_SECTION = re.compile(
    r"^### (?P<heading>[^\r\n]+)\r?\n(?P<body>.*?)(?=^### |\Z)",
    re.MULTILINE | re.DOTALL,
)
_POSTGRES_AUDIT_GROUPS: Final = {
    "共享主数据 PostgreSQL 角色门（162 项）": 162,
    "本地 PostgreSQL 集成门（21 项）": 21,
}

_observed_skips: Counter[str] = Counter()
_xfailed: list[str] = []
_deselected: list[str] = []


def _normalize_nodeid(nodeid: str) -> str:
    return _UUID.sub("{uuid}", nodeid.replace("\\", "/"))


def _audit_nodeid_to_pytest(nodeid: str) -> str:
    module, separator, test_name = nodeid.partition("::")
    assert separator, f"invalid audited node id: {nodeid}"
    return f"{module.replace('.', '/')}.py::{test_name}"


def documented_skip_inventory(
    audit_text: str,
    *,
    windows_symlink_unavailable: bool,
    postgres_integration_enabled: bool = False,
    dingtalk_postgres_integration_enabled: bool = False,
) -> Counter[str]:
    raw_nodes = _AUDITED_NODE.findall(audit_text)
    assert len(raw_nodes) == 217, "the approved skip inventory must contain 217 nodes"
    inventory = Counter(_normalize_nodeid(_audit_nodeid_to_pytest(nodeid)) for nodeid in raw_nodes)
    dingtalk_groups = {
        match.group("heading"): match.group("body")
        for match in _AUDIT_SECTION.finditer(audit_text)
        if f"{DINGTALK_POSTGRES_INTEGRATION_FLAG}=1" in match.group("body")
    }
    assert set(dingtalk_groups) == {_DINGTALK_POSTGRES_AUDIT_GROUP}, (
        "the DingTalk PostgreSQL skip gate must be independently and exactly audited"
    )
    dingtalk_nodes = _AUDITED_NODE.findall(dingtalk_groups[_DINGTALK_POSTGRES_AUDIT_GROUP])
    assert len(dingtalk_nodes) == 15 and set(dingtalk_nodes) == _DINGTALK_POSTGRES_NODES, (
        "the DingTalk PostgreSQL skip gate must contain only its fifteen exact nodes"
    )
    if dingtalk_postgres_integration_enabled:
        inventory -= Counter(_audit_nodeid_to_pytest(node) for node in dingtalk_nodes)
    if postgres_integration_enabled:
        sections = {
            match.group("heading"): match.group("body")
            for match in _AUDIT_SECTION.finditer(audit_text)
        }
        assert len(sections) == len(list(_AUDIT_SECTION.finditer(audit_text))), (
            "the approved Pre-K1 inventory must not contain duplicate section headings"
        )
        postgres_sections = {
            heading
            for heading, body in sections.items()
            if f"{POSTGRES_INTEGRATION_FLAG}=1" in body
        }
        assert postgres_sections == set(_POSTGRES_AUDIT_GROUPS), (
            "the approved PostgreSQL skip groups must match the two exact audited groups"
        )
        for heading, declared_count in _POSTGRES_AUDIT_GROUPS.items():
            assert heading in sections, f"missing approved PostgreSQL skip group: {heading}"
            body = sections[heading]
            assert "RUN_POSTGRES_INTEGRATION=1" in body, (
                f"approved PostgreSQL skip group lost its explicit enable condition: {heading}"
            )
            group_nodes = _AUDITED_NODE.findall(body)
            assert len(group_nodes) == declared_count, (
                f"approved PostgreSQL skip group count drifted: {heading}"
            )
            group_inventory = Counter(
                _normalize_nodeid(_audit_nodeid_to_pytest(nodeid)) for nodeid in group_nodes
            )
            assert not (group_inventory - inventory), (
                f"approved PostgreSQL skip group contains an unknown node: {heading}"
            )
            inventory -= group_inventory
    if not windows_symlink_unavailable:
        inventory.pop(WINDOWS_ONLY_SYMLINK_SKIP, None)
    return inventory


def inventory_issues(
    *,
    expected: Counter[str],
    observed: Counter[str],
    xfailed: list[str],
    deselected: list[str],
) -> list[str]:
    issues: list[str] = []
    unexpected = observed - expected
    missing = expected - observed
    if unexpected:
        issues.append(f"unexpected skips: {sorted(unexpected.items())}")
    if missing:
        issues.append(f"approved skips no longer observed: {sorted(missing.items())}")
    if xfailed:
        issues.append(f"xfail/xpass is forbidden: {sorted(xfailed)}")
    if deselected:
        issues.append(f"deselected tests are forbidden: {sorted(deselected)}")
    return issues


def _expression_name(expression: ast.expr) -> str:
    parts: list[str] = []
    current = expression
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _call_name(call: ast.Call) -> str:
    return _expression_name(call.func)


@dataclass(frozen=True)
class _FunctionRecord:
    symbol: str
    relative: str
    module: str
    module_is_package: bool
    qualname: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    has_destructive_marker: bool


class _ScopeVisitor(ast.NodeVisitor):
    """Visit one function/module scope without borrowing nested definitions."""

    def __init__(self) -> None:
        self.calls: list[ast.Call] = []
        self.imports: list[ast.Import | ast.ImportFrom] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast visitor API
        self.calls.append(node)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802 - ast visitor API
        self.imports.append(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802 - ast visitor API
        self.imports.append(node)

    def visit_FunctionDef(self, _node: ast.FunctionDef) -> None:  # noqa: N802
        return

    def visit_AsyncFunctionDef(self, _node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        return

    def visit_ClassDef(self, _node: ast.ClassDef) -> None:  # noqa: N802
        return

    def visit_Lambda(self, _node: ast.Lambda) -> None:  # noqa: N802
        return


def _module_from_relative(relative: str) -> tuple[str, bool] | None:
    path = PurePosixPath(relative.replace("\\", "/"))
    if path.suffix != ".py":
        return None
    parts = list(path.with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    if not parts:
        return None
    return ".".join(parts), is_package


def _function_records(
    *,
    relative: str,
    module: str,
    module_is_package: bool,
    tree: ast.Module,
) -> list[_FunctionRecord]:
    records: list[_FunctionRecord] = []

    def visit(
        statements: list[ast.stmt],
        prefix: tuple[str, ...],
        inherited_class_marker: bool,
    ) -> None:
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = ".".join((*prefix, statement.name))
                records.append(
                    _FunctionRecord(
                        symbol=f"{module}.{qualname}",
                        relative=relative,
                        module=module,
                        module_is_package=module_is_package,
                        qualname=qualname,
                        node=statement,
                        has_destructive_marker=(
                            inherited_class_marker
                            or _has_pytest_marker(statement, "destructive_migration")
                        ),
                    )
                )
                visit(statement.body, (*prefix, statement.name), inherited_class_marker)
            elif isinstance(statement, ast.ClassDef):
                visit(
                    statement.body,
                    (*prefix, statement.name),
                    inherited_class_marker
                    or _has_pytest_marker(statement, "destructive_migration"),
                )

    visit(tree.body, (), False)
    return records


def _scope_nodes(statements: list[ast.stmt]) -> _ScopeVisitor:
    visitor = _ScopeVisitor()
    for statement in statements:
        visitor.visit(statement)
    return visitor


def _absolute_import_module(
    node: ast.ImportFrom,
    *,
    module: str,
    module_is_package: bool,
) -> str:
    if not node.level:
        return node.module or ""
    current_package = module if module_is_package else module.rpartition(".")[0]
    package_parts = current_package.split(".") if current_package else []
    parents = node.level - 1
    if parents >= len(package_parts):
        return ""
    base_parts = package_parts[: len(package_parts) - parents]
    if node.module:
        base_parts.extend(node.module.split("."))
    return ".".join(base_parts)


def _import_bindings(
    imports: list[ast.Import | ast.ImportFrom],
    *,
    module: str,
    module_is_package: bool,
) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for imported in imports:
        if isinstance(imported, ast.Import):
            for alias in imported.names:
                local_name = alias.asname or alias.name.split(".", 1)[0]
                bindings[local_name] = alias.name if alias.asname else local_name
            continue
        base = _absolute_import_module(
            imported,
            module=module,
            module_is_package=module_is_package,
        )
        if not base:
            continue
        for alias in imported.names:
            if alias.name == "*":
                continue
            bindings[alias.asname or alias.name] = f"{base}.{alias.name}"
    return bindings


def _literal_strings(call: ast.Call) -> set[str]:
    nodes: list[ast.AST] = [*call.args, *(keyword.value for keyword in call.keywords)]
    return {
        child.value
        for node in nodes
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


_PROCESS_RUNNER_NAMES = frozenset({"call", "check_call", "check_output", "popen", "run"})


def _is_alembic_base_process_call(call: ast.Call) -> bool:
    final_name = _call_name(call).rsplit(".", 1)[-1].lower()
    if final_name not in _PROCESS_RUNNER_NAMES or not call.args:
        return False
    command = call.args[0]
    if not isinstance(command, (ast.List, ast.Tuple)):
        return False
    tokens = [
        element.value.lower()
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
        else None
        for element in command.elts
    ]
    for index, token in enumerate(tokens):
        if token != "alembic":
            continue
        is_executable = index == 0 or (index >= 1 and tokens[index - 1] == "-m")
        if is_executable and "base" in tokens[index + 1 :]:
            return True
    return False


def _call_is_directly_destructive(call: ast.Call) -> bool:
    final_name = _call_name(call).rsplit(".", 1)[-1].lower()
    literals = {value.lower() for value in _literal_strings(call)}
    return (
        final_name == "downgrade"
        or "downgrade" in literals
        or (final_name == "stamp" and "base" in literals)
        or _is_alembic_base_process_call(call)
    )


_RISKY_HELPER = re.compile(
    r"(?i)(?:^|[._])(?:downgrade|run_alembic|reset_(?:db|database|schema)|"
    r"(?:db|database|schema)_reset|migration_round_trip)(?:$|[._])"
)


def _looks_like_destructive_helper(name: str) -> bool:
    return bool(_RISKY_HELPER.search(name)) or (
        "reset" in name.lower() and "schema" in name.lower()
    )


def _dynamic_destructive_helper(call: ast.Call) -> str | None:
    if _call_name(call):
        return None
    for node in ast.walk(call.func):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _looks_like_destructive_helper(node.value)
        ):
            return node.value
    return None


def _resolve_call_symbol(
    call: ast.Call,
    *,
    record: _FunctionRecord,
    imports: dict[str, str],
    records: dict[str, _FunctionRecord],
) -> tuple[str | None, str | None]:
    raw_name = _call_name(call)
    if not raw_name:
        dynamic = _dynamic_destructive_helper(call)
        return None, dynamic

    parts = raw_name.split(".")
    imported = imports.get(parts[0])
    if imported:
        candidate = ".".join((imported, *parts[1:]))
        if candidate in records:
            return candidate, None
        if _looks_like_destructive_helper(candidate) and not _call_is_directly_destructive(call):
            return None, candidate
        return None, None

    if parts[0] in {"self", "cls"}:
        parent = record.qualname.rpartition(".")[0]
        candidate = ".".join(part for part in (record.module, parent, *parts[1:]) if part)
        if candidate in records:
            return candidate, None

    lexical_parts = record.qualname.split(".")[:-1]
    for depth in range(len(lexical_parts), -1, -1):
        candidate = ".".join((record.module, *lexical_parts[:depth], raw_name))
        if candidate in records:
            return candidate, None
    if raw_name in records:
        return raw_name, None
    if _looks_like_destructive_helper(raw_name) and not _call_is_directly_destructive(call):
        return None, raw_name
    return None, None


def _has_pytest_marker(
    function: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    marker_name: str,
) -> bool:
    expected = f"pytest.mark.{marker_name}"
    for decorator in function.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        parts: list[str] = []
        current: ast.expr = target
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        if ".".join(reversed(parts)) == expected:
            return True
    return False


def destructive_migration_boundary_issues(python_sources: dict[str, str]) -> list[str]:
    """Require every real downgrade path to use one marked, disposable database."""

    issues: list[str] = []
    parsed_modules: dict[str, tuple[str, bool, ast.Module]] = {}
    for relative, source in python_sources.items():
        module_identity = _module_from_relative(relative)
        if module_identity is None:
            continue
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError:
            continue
        module, module_is_package = module_identity
        if module in parsed_modules:
            issues.append(f"duplicate Python module in destructive boundary: {module}")
            continue
        parsed_modules[module] = (relative, module_is_package, tree)

    records: dict[str, _FunctionRecord] = {}
    module_imports: dict[str, dict[str, str]] = {}
    for module, (relative, module_is_package, tree) in parsed_modules.items():
        module_imports[module] = _import_bindings(
            _scope_nodes(tree.body).imports,
            module=module,
            module_is_package=module_is_package,
        )
        for record in _function_records(
            relative=relative,
            module=module,
            module_is_package=module_is_package,
            tree=tree,
        ):
            if record.symbol in records:
                issues.append(f"duplicate function in destructive boundary: {record.symbol}")
                continue
            records[record.symbol] = record

    edges: dict[str, set[str]] = {symbol: set() for symbol in records}
    direct_destructive: set[str] = set()
    unresolved_helpers: dict[str, set[str]] = {symbol: set() for symbol in records}
    for symbol, record in records.items():
        scope = _scope_nodes(record.node.body)
        imports = module_imports[record.module] | _import_bindings(
            scope.imports,
            module=record.module,
            module_is_package=record.module_is_package,
        )
        for call in scope.calls:
            if _call_is_directly_destructive(call):
                direct_destructive.add(symbol)
            target, unresolved = _resolve_call_symbol(
                call,
                record=record,
                imports=imports,
                records=records,
            )
            if target is not None:
                edges[symbol].add(target)
            if unresolved is not None:
                unresolved_helpers[symbol].add(unresolved)

    destructive = direct_destructive | {
        symbol for symbol, helpers in unresolved_helpers.items() if helpers
    }
    destructive.update(
        symbol
        for symbol, record in records.items()
        if record.node.name.startswith("test_") and record.has_destructive_marker
    )
    reachable_unresolved = {
        symbol: helpers.copy() for symbol, helpers in unresolved_helpers.items()
    }
    changed = True
    while changed:
        changed = False
        for caller, callees in edges.items():
            if callees & destructive and caller not in destructive:
                destructive.add(caller)
                changed = True
            inherited = set().union(*(reachable_unresolved[callee] for callee in callees))
            if not inherited.issubset(reachable_unresolved[caller]):
                reachable_unresolved[caller].update(inherited)
                changed = True

    for symbol in sorted(destructive):
        record = records[symbol]
        if not record.node.name.startswith("test_"):
            continue
        parameters = {
            argument.arg
            for argument in (
                *record.node.args.posonlyargs,
                *record.node.args.args,
                *record.node.args.kwonlyargs,
            )
        }
        prefix = f"destructive migration test {record.relative}:{record.qualname}"
        if reachable_unresolved[symbol]:
            issues.append(
                f"{prefix} unresolved destructive helper: {sorted(reachable_unresolved[symbol])}"
            )
        if not record.has_destructive_marker:
            issues.append(f"{prefix} missing destructive_migration marker")
        if (
            not {
                "isolated_migration_schema",
                "isolated_revision19_schema",
            }
            & parameters
        ):
            issues.append(f"{prefix} requires isolated_migration_schema")
        if "current_master_data_schema" in parameters:
            issues.append(f"{prefix} forbids current_master_data_schema")
    return issues


def collection_boundary_issues(
    *,
    pytest_options: dict[str, object],
    python_sources: dict[str, str],
    alternate_configs: list[str],
) -> list[str]:
    issues: list[str] = []
    if pytest_options != EXPECTED_PYTEST_OPTIONS:
        issues.append("pytest ini options differ from the approved exact configuration")
    if alternate_configs:
        issues.append(f"alternate pytest configuration is forbidden: {alternate_configs}")
    for relative, source in python_sources.items():
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError:
            issues.append(f"unparseable Python source in collection boundary: {relative}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name in _FORBIDDEN_COLLECTION_HOOKS
            ):
                issues.append(f"forbidden pytest collection hook: {relative}:{node.name}")
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in (
                        _FORBIDDEN_COLLECTION_ASSIGNMENTS | _FORBIDDEN_COLLECTION_HOOKS
                    ):
                        issues.append(
                            f"forbidden pytest collection assignment: {relative}:{target.id}"
                        )
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    exposed_name = alias.asname or alias.name.rsplit(".", 1)[-1]
                    if exposed_name in (
                        _FORBIDDEN_COLLECTION_HOOKS | _FORBIDDEN_COLLECTION_ASSIGNMENTS
                    ):
                        issues.append(
                            f"forbidden pytest collection import: {relative}:{exposed_name}"
                        )
    issues.extend(destructive_migration_boundary_issues(python_sources))
    return issues


def verify_collection_boundary(api_root: Path) -> list[str]:
    parsed = tomllib.loads((api_root / "pyproject.toml").read_text(encoding="utf-8"))
    pytest_options = parsed.get("tool", {}).get("pytest", {}).get("ini_options", {})
    assert isinstance(pytest_options, dict)
    python_sources = {
        path.relative_to(api_root).as_posix(): path.read_text(encoding="utf-8")
        for path in (api_root / "tests").rglob("*.py")
    }
    root_conftest = api_root / "conftest.py"
    if root_conftest.exists():
        python_sources["conftest.py"] = root_conftest.read_text(encoding="utf-8")
    alternate_configs = [
        name for name in ("pytest.ini", "tox.ini", "setup.cfg") if (api_root / name).exists()
    ]
    return collection_boundary_issues(
        pytest_options=pytest_options,
        python_sources=python_sources,
        alternate_configs=alternate_configs,
    )


def pytest_runtest_logreport(report: Any) -> None:
    nodeid = _normalize_nodeid(str(report.nodeid))
    if getattr(report, "wasxfail", None) is not None:
        _xfailed.append(nodeid)
    elif report.skipped:
        _observed_skips[nodeid] += 1


def pytest_deselected(items: list[Any]) -> None:
    _deselected.extend(_normalize_nodeid(str(item.nodeid)) for item in items)


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    del exitstatus
    expected = documented_skip_inventory(
        SKIP_AUDIT.read_text(encoding="utf-8"),
        windows_symlink_unavailable=os.name == "nt",
        postgres_integration_enabled=os.environ.get(POSTGRES_INTEGRATION_FLAG) == "1",
        dingtalk_postgres_integration_enabled=(
            os.environ.get(DINGTALK_POSTGRES_INTEGRATION_FLAG) == "1"
        ),
    )
    issues = inventory_issues(
        expected=expected,
        observed=_observed_skips,
        xfailed=_xfailed,
        deselected=_deselected,
    )
    if not issues:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep("=", "audited skip inventory mismatch")
        for issue in issues:
            reporter.write_line(issue)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-collection", action="store_true", required=True)
    parser.parse_args()
    issues = verify_collection_boundary(Path(__file__).resolve().parents[1])
    if issues:
        for issue in issues:
            print(issue)
        return 1
    print("pytest collection boundary: approved exact configuration")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
