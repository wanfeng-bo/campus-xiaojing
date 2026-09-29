from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

_K1_ALLOWED_IMPORT_ROOTS = frozenset(
    {
        "__future__",
        "collections",
        "pydantic",
        "typing",
    }
)
_RUNTIME_ALLOWED_IMPORT_ROOTS = _K1_ALLOWED_IMPORT_ROOTS | {
    "dataclasses",
    "datetime",
    "math",
    "threading",
    "uuid",
}
_IMPLEMENTATION_ALLOWED_IMPORT_ROOTS = _K1_ALLOWED_IMPORT_ROOTS | {
    "dataclasses",
    "uuid",
}
_PRESENTATION_ALLOWED_IMPORT_ROOTS = _K1_ALLOWED_IMPORT_ROOTS | {
    "dataclasses",
    "json",
}
_CONTROL_ALLOWED_IMPORT_ROOTS = _K1_ALLOWED_IMPORT_ROOTS | {
    "base64",
    "dataclasses",
    "datetime",
    "hashlib",
    "hmac",
    "json",
    "secrets",
    "uuid",
}

_ALLOWED_SIBLING_IMPORTS = {
    "catalog.py": frozenset({"ports"}),
    "controls.py": frozenset({"catalog", "contracts", "ports", "presentation"}),
    "orchestrator.py": frozenset(
        {"catalog", "contracts", "controls", "ports", "presentation", "runtime"}
    ),
    "ports.py": frozenset({"contracts"}),
    "presentation.py": frozenset({"contracts"}),
    "runtime.py": frozenset({"contracts"}),
    "tool_loop.py": frozenset({"contracts", "controls", "runtime"}),
}


def _python_files(root: Path) -> tuple[Path, ...]:
    if not root.is_dir():
        return ()
    return tuple(sorted(root.rglob("*.py")))


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _imported_modules(tree: ast.AST) -> Iterable[tuple[str, int]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from ((alias.name, 0) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield node.module or "<module>", node.level


def _typing_protocol_names(tree: ast.Module) -> set[str]:
    return {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "typing"
        for alias in node.names
        if alias.name == "Protocol"
    }


def _pydantic_validator_names(tree: ast.Module) -> set[str]:
    return {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "pydantic"
        for alias in node.names
        if alias.name == "model_validator"
    }


def _pydantic_base_model_names(tree: ast.Module) -> set[str]:
    return {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "pydantic"
        for alias in node.names
        if alias.name == "BaseModel"
    }


def _inherits_bound_base_model(
    node: ast.ClassDef,
    classes: dict[str, ast.ClassDef],
    base_model_names: set[str],
    visiting: frozenset[str] = frozenset(),
) -> bool:
    if node.name in visiting:
        return False
    next_visiting = visiting | {node.name}
    for base in node.bases:
        if not isinstance(base, ast.Name):
            continue
        if base.id in base_model_names:
            return True
        parent = classes.get(base.id)
        if parent is not None and _inherits_bound_base_model(
            parent,
            classes,
            base_model_names,
            next_visiting,
        ):
            return True
    return False


def _is_protocol_class(node: ast.ClassDef, protocol_names: set[str]) -> bool:
    return (
        len(node.bases) == 1
        and isinstance(node.bases[0], ast.Name)
        and (node.bases[0].id in protocol_names)
    )


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _decorator_name(node: ast.expr) -> str | None:
    target = node.func if isinstance(node, ast.Call) else node
    return target.id if isinstance(target, ast.Name) else None


def _is_ellipsis_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return (
        len(node.body) == 1
        and isinstance(node.body[0], ast.Expr)
        and isinstance(
            node.body[0].value,
            ast.Constant,
        )
        and node.body[0].value.value is Ellipsis
    )


def _uses_dynamic_import_backdoor(tree: ast.Module) -> bool:
    return any(
        (isinstance(node, ast.Name) and node.id in {"__builtins__", "__import__"})
        or (isinstance(node, ast.Constant) and node.value == "__import__")
        for node in ast.walk(tree)
    )


def inspect_assistant_contract_root(
    root: Path,
    expected_files: frozenset[str],
) -> list[str]:
    """机械证明 K4 目录完整，契约纯净且实现没有业务/数据依赖。"""

    violations: list[str] = []
    actual_paths = {_relative(path, root) for path in _python_files(root)}
    violations.extend(
        f"missing expected file: {relative}" for relative in sorted(expected_files - actual_paths)
    )
    violations.extend(
        f"unexpected Python file: {relative}" for relative in sorted(actual_paths - expected_files)
    )

    for path in _python_files(root):
        relative = _relative(path, root)
        if relative == "runtime.py":
            allowed_import_roots = _RUNTIME_ALLOWED_IMPORT_ROOTS
        elif relative == "tool_loop.py":
            allowed_import_roots = _IMPLEMENTATION_ALLOWED_IMPORT_ROOTS | {"json", "time"}
        elif relative == "controls.py":
            allowed_import_roots = _CONTROL_ALLOWED_IMPORT_ROOTS
        elif relative == "presentation.py":
            allowed_import_roots = _PRESENTATION_ALLOWED_IMPORT_ROOTS
        elif relative in {"catalog.py", "orchestrator.py"}:
            allowed_import_roots = _IMPLEMENTATION_ALLOWED_IMPORT_ROOTS
        else:
            allowed_import_roots = _K1_ALLOWED_IMPORT_ROOTS
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            violations.append(f"{relative}: invalid Python syntax at line {exc.lineno}")
            continue

        if _uses_dynamic_import_backdoor(tree):
            violations.append(f"{relative}: dynamic import is forbidden")

        for imported, level in _imported_modules(tree):
            if level:
                if level == 1 and imported in _ALLOWED_SIBLING_IMPORTS.get(relative, frozenset()):
                    continue
                kind = "relative parent dependency" if level > 1 else "forbidden sibling dependency"
                violations.append(f"{relative}: {kind}: {'.' * level}{imported}")
                continue
            if imported.split(".", maxsplit=1)[0] not in allowed_import_roots:
                violations.append(f"{relative}: forbidden dependency: {imported}")

        for call in (item for item in ast.walk(tree) if isinstance(item, ast.Call)):
            if isinstance(call.func, ast.Name) and call.func.id in {
                "compile",
                "eval",
                "exec",
                "open",
                "print",
            }:
                violations.append(f"{relative}: unsafe runtime call: {call.func.id}")

        if relative == "__init__.py":
            violations.extend(
                f"__init__.py:{type(node).__name__} is an executable declaration"
                for node in tree.body
                if not _is_docstring(node)
            )
            continue

        if relative == "contracts.py":
            violations.extend(
                f"contracts.py:{node.name} is a top-level implementation"
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
            validator_names = _pydantic_validator_names(tree)
            classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
            base_model_names = _pydantic_base_model_names(tree)
            for owner in classes.values():
                if not _inherits_bound_base_model(owner, classes, base_model_names):
                    violations.append(
                        f"contracts.py:{owner.name} does not inherit pydantic.BaseModel"
                    )
                for method in (
                    node
                    for node in owner.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                ):
                    if not any(
                        _decorator_name(decorator) in validator_names
                        for decorator in method.decorator_list
                    ):
                        violations.append(
                            f"contracts.py:{owner.name}.{method.name} is not a contract validator"
                        )
            continue

        if relative != "ports.py":
            continue
        violations.extend(
            f"ports.py:{getattr(node, 'name', type(node).__name__)} is a top-level implementation"
            for node in tree.body
            if not isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef))
            and not _is_docstring(node)
        )
        protocol_names = _typing_protocol_names(tree)
        for node in (item for item in tree.body if isinstance(item, ast.ClassDef)):
            if not _is_protocol_class(node, protocol_names):
                violations.append(f"ports.py:{node.name} must inherit typing.Protocol")
            if node.decorator_list or node.keywords:
                violations.append(f"ports.py:{node.name} contains executable class metadata")
            violations.extend(
                f"ports.py:{node.name} contains executable class member"
                for member in node.body
                if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not _is_docstring(member)
            )
            for method in (
                item
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            ):
                if method.decorator_list:
                    violations.append(
                        f"ports.py:{node.name}.{method.name} contains a method decorator"
                    )
                if not _is_ellipsis_body(method):
                    violations.append(
                        f"ports.py:{node.name}.{method.name} contains an implementation body"
                    )

    return violations
