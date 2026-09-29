from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

_EXPECTED_FILES = frozenset({"assistant.py", "assistant_schemas.py"})
_ALLOWED_IMPORTS = {
    "assistant.py": frozenset(
        {
            "__future__",
            "collections.abc",
            "dataclasses",
            "typing",
            "uuid",
            "fastapi",
            "fastapi.responses",
            "starlette.types",
            "anyio",
            "app.platform.assistant.contracts",
            "app.platform.assistant.diagnostics",
            "app.interfaces.http.assistant_schemas",
            "app.interfaces.http.errors",
            "app.interfaces.http.framework_request",
            "app.platform.assistant.runtime",
            "app.platform.assistant.orchestrator",
            "app.platform.assistant.controls",
        }
    ),
    "assistant_schemas.py": frozenset(
        {
            "__future__",
            "typing",
            "pydantic",
            "app.platform.assistant.contracts",
        }
    ),
}


def _assistant_files(root: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in root.rglob("*.py")
            if path.relative_to(root).parts[0].startswith("assistant")
        )
    )


def _imported_modules(tree: ast.AST) -> Iterable[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                yield f"{'.' * node.level}{node.module or ''}"
            else:
                yield node.module or "<module>"


def _uses_dynamic_import_backdoor(tree: ast.AST) -> bool:
    return any(
        (
            isinstance(node, ast.Name)
            and node.id
            in {
                "__builtins__",
                "__import__",
                "compile",
                "eval",
                "exec",
                "open",
            }
        )
        or (isinstance(node, ast.Constant) and node.value == "__import__")
        for node in ast.walk(tree)
    )


def inspect_assistant_http_boundary(root: Path) -> list[str]:
    """机械限定 K3 HTTP 边界的文件集合和依赖集合。"""

    violations: list[str] = []
    files = _assistant_files(root)
    actual = {path.relative_to(root).as_posix() for path in files}
    violations.extend(f"missing expected file: {name}" for name in sorted(_EXPECTED_FILES - actual))
    violations.extend(
        f"unexpected assistant HTTP file: {name}" for name in sorted(actual - _EXPECTED_FILES)
    )

    for path in files:
        relative = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            violations.append(f"{relative}: invalid syntax at line {exc.lineno}")
            continue
        allowed = _ALLOWED_IMPORTS.get(relative, frozenset())
        if _uses_dynamic_import_backdoor(tree):
            violations.append(f"{relative}: dynamic execution or import is forbidden")
        for imported in _imported_modules(tree):
            if imported not in allowed:
                violations.append(f"{relative}: forbidden dependency: {imported}")
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            if isinstance(call.func, ast.Name) and call.func.id in {
                "compile",
                "eval",
                "exec",
                "open",
            }:
                violations.append(f"{relative}: unsafe runtime call: {call.func.id}")
    return violations


__all__ = ["inspect_assistant_http_boundary"]
