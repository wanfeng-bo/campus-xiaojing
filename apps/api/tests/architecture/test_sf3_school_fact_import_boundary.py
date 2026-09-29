from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from app.main import app as production_app

API_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = API_ROOT / "app"
PYPROJECT = API_ROOT / "pyproject.toml"
WEB_ROOT = API_ROOT.parent / "web"
WEB_SOURCE_ROOT = WEB_ROOT / "src"
WEB_PRODUCTION_ENTRY = WEB_SOURCE_ROOT / "main.ts"

SF3_CHANNEL_MODULES = {
    "app.composition.school_facts",
    "app.interfaces.http.school_facts",
    "app.interfaces.http.school_fact_schemas",
    "app.platform.master_data_imports.school_facts_excel",
    "app.platform.master_data_imports.source_mapping",
    "app.platform.master_data_imports.status_mappings",
}
FORBIDDEN_BUSINESS_PREFIXES = (
    "app.domains.repairs",
    "app.platform.assistant",
    "app.platform.responsibility",
)

_WEB_IMPORT_PATTERN = re.compile(r"(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s*)[\"']([^\"']+)[\"']")


def _module_name(path: Path, api_root: Path) -> str:
    relative = path.relative_to(api_root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(
    path: Path,
    *,
    module: str | None = None,
    api_root: Path = API_ROOT,
) -> tuple[str, ...]:
    modules: list[str] = []
    current_module = module or _module_name(path, api_root)
    current_package = (
        current_module if path.name == "__init__.py" else current_module.rpartition(".")[0]
    )
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package_parts = current_package.split(".") if current_package else []
                parents = node.level - 1
                if parents >= len(package_parts):
                    continue
                base_parts = package_parts[: len(package_parts) - parents]
                if node.module:
                    base_parts.extend(node.module.split("."))
                imported_module = ".".join(base_parts)
            else:
                imported_module = node.module or ""
            if imported_module:
                modules.append(imported_module)
                modules.extend(
                    f"{imported_module}.{alias.name}" for alias in node.names if alias.name != "*"
                )
    return tuple(dict.fromkeys(modules))


def _app_module_path(module: str, api_root: Path = API_ROOT) -> Path | None:
    if not module.startswith("app"):
        return None
    parts = module.split(".")
    file_path = api_root.joinpath(*parts).with_suffix(".py")
    if file_path.is_file():
        return file_path
    package_path = api_root.joinpath(*parts, "__init__.py")
    return package_path if package_path.is_file() else None


def _production_import_graph(
    *,
    api_root: Path = API_ROOT,
    entry_module: str = "app.main",
) -> set[str]:
    pending = [entry_module]
    visited: set[str] = set()
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        visited.add(module)
        path = _app_module_path(module, api_root)
        if path is None:
            continue
        pending.extend(
            imported
            for imported in _imports(path, module=module, api_root=api_root)
            if imported.startswith("app.")
        )
    return visited


def _web_import_graph(
    entry: Path,
    *,
    source_root: Path,
    aliases: dict[str, Path],
) -> set[Path]:
    resolved_root = source_root.resolve()
    extension_candidates = (".ts", ".tsx", ".js", ".jsx", ".vue")

    def resolve_import(importer: Path, specifier: str) -> Path | None:
        clean_specifier = specifier.split("?", 1)[0].split("#", 1)[0]
        candidate: Path | None = None
        if clean_specifier.startswith("."):
            candidate = importer.parent / clean_specifier
        else:
            for alias, target in aliases.items():
                if clean_specifier == alias or clean_specifier.startswith(f"{alias}/"):
                    suffix = clean_specifier[len(alias) :].lstrip("/")
                    candidate = target / suffix
                    break
        if candidate is None:
            return None

        candidates = [candidate]
        if not candidate.suffix:
            candidates.extend(
                candidate.with_suffix(extension) for extension in extension_candidates
            )
            candidates.extend(candidate / f"index{extension}" for extension in extension_candidates)
        for possible in candidates:
            resolved = possible.resolve()
            if not resolved.is_relative_to(resolved_root):
                raise AssertionError(f"前端导入越出源码根目录：{specifier}")
            if resolved.is_file():
                return resolved
        raise AssertionError(f"前端源码导入无法解析：{importer.name}:{specifier}")

    pending = [entry.resolve()]
    visited: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        if not current.is_relative_to(resolved_root) or not current.is_file():
            raise AssertionError(f"前端入口图包含无效路径：{current}")
        visited.add(current)
        source = current.read_text(encoding="utf-8")
        for specifier in _WEB_IMPORT_PATTERN.findall(source):
            imported = resolve_import(current, specifier)
            if imported is not None and imported not in visited:
                pending.append(imported)
    return visited


def test_sf3_school_fact_package_is_in_the_distribution_manifest() -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    packages = set(project["tool"]["setuptools"]["packages"])

    assert "app.platform.school_facts" in packages


def test_sf3_channel_is_absent_from_production_routes_and_recursive_import_graph() -> None:
    production_paths = {
        route.path
        for route in production_app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/v1/school-facts")
    }

    assert production_paths == set()
    assert _production_import_graph().isdisjoint(SF3_CHANNEL_MODULES)


@pytest.mark.parametrize(
    ("main_source", "bridge_source"),
    [
        ("from .interfaces.http.school_facts import create_router\n", None),
        (
            "from .composition import bridge\n",
            "from ..interfaces.http.school_facts import create_router\n",
        ),
    ],
    ids=("relative-one-hop", "relative-two-hop"),
)
def test_sf3_backend_import_graph_detects_relative_mounts(
    tmp_path: Path,
    main_source: str,
    bridge_source: str | None,
) -> None:
    app_root = tmp_path / "app"
    (app_root / "interfaces/http").mkdir(parents=True)
    (app_root / "composition").mkdir()
    for package in (
        app_root,
        app_root / "composition",
        app_root / "interfaces",
        app_root / "interfaces/http",
    ):
        (package / "__init__.py").write_text("", encoding="utf-8")
    (app_root / "main.py").write_text(main_source, encoding="utf-8")
    if bridge_source is not None:
        (app_root / "composition/bridge.py").write_text(bridge_source, encoding="utf-8")
    (app_root / "interfaces/http/school_facts.py").write_text(
        "def create_router():\n    return None\n",
        encoding="utf-8",
    )

    graph = _production_import_graph(api_root=tmp_path, entry_module="app.main")

    assert "app.interfaces.http.school_facts" in graph


def test_sf3_web_channel_is_absent_from_recursive_production_import_graph() -> None:
    production_graph = _web_import_graph(
        WEB_PRODUCTION_ENTRY,
        source_root=WEB_SOURCE_ROOT,
        aliases={"@": WEB_SOURCE_ROOT},
    )

    assert not any(
        path.is_relative_to(WEB_SOURCE_ROOT / "features/school_facts") for path in production_graph
    )


def test_sf3_web_import_graph_detects_alias_and_dynamic_two_hop_mount(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    (source_root / "app").mkdir(parents=True)
    (source_root / "features/school_facts").mkdir(parents=True)
    entry = source_root / "main.ts"
    bridge = source_root / "app/bridge.ts"
    forbidden = source_root / "features/school_facts/SchoolFactAdmin.vue"
    entry.write_text('import "@/app/bridge";\n', encoding="utf-8")
    bridge.write_text(
        'export const load = () => import("../features/school_facts/SchoolFactAdmin.vue");\n',
        encoding="utf-8",
    )
    forbidden.write_text("<template><main /></template>\n", encoding="utf-8")

    graph = _web_import_graph(entry, source_root=source_root, aliases={"@": source_root})

    assert forbidden.resolve() in graph


def test_sf3_channel_does_not_import_business_domains_or_reverse_composition_layers() -> None:
    violations: list[str] = []
    for module in sorted(SF3_CHANNEL_MODULES):
        path = _app_module_path(module)
        assert path is not None
        imports = _imports(path)
        for imported in imports:
            if imported.startswith(FORBIDDEN_BUSINESS_PREFIXES):
                violations.append(f"{module}:{imported}")
        if module == "app.interfaces.http.school_facts":
            violations.extend(
                f"{module}:{imported}"
                for imported in imports
                if imported.startswith("app.composition")
            )
        if module.startswith("app.platform.master_data_imports"):
            violations.extend(
                f"{module}:{imported}"
                for imported in imports
                if imported.startswith(("app.db", "app.composition", "sqlalchemy"))
            )

    assert violations == []


def test_sf3_http_and_adapters_can_only_reach_the_sf2_service_ports() -> None:
    http_source = (APP_ROOT / "interfaces/http/school_facts.py").read_text(encoding="utf-8")
    adapter_source = (APP_ROOT / "platform/master_data_imports/source_mapping.py").read_text(
        encoding="utf-8"
    )
    excel_source = (APP_ROOT / "platform/master_data_imports/school_facts_excel.py").read_text(
        encoding="utf-8"
    )

    assert "SchoolFactChangeBoundary" in http_source
    assert "SchoolFactChangeService" not in http_source
    for forbidden in (
        "SchoolFactTransactionalWriter",
        "SqlAlchemySchoolFactPersistence",
        "session.execute",
        "Repository",
        "Writer",
    ):
        assert forbidden not in adapter_source
        assert forbidden not in excel_source
