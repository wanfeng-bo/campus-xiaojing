from __future__ import annotations

from pathlib import Path

from .assistant_http_guard import inspect_assistant_http_boundary


def _write(root: Path, name: str, source: str) -> None:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")


def _legal_fixture(root: Path) -> None:
    _write(
        root,
        "assistant.py",
        """from dataclasses import dataclass
from uuid import UUID
from fastapi import APIRouter
from app.interfaces.http.assistant_schemas import ConversationResponse
from app.interfaces.http.errors import ApiError
from app.interfaces.http.framework_request import FrameworkRequestContext
from app.platform.assistant.runtime import AssistantRuntime
from app.platform.assistant.orchestrator import AssistantTurnOrchestrator

router_factory = APIRouter
types = (
    ConversationResponse,
    ApiError,
    FrameworkRequestContext,
    AssistantRuntime,
    AssistantTurnOrchestrator,
    UUID,
)
""",
    )
    _write(
        root,
        "assistant_schemas.py",
        """from typing import Literal
from pydantic import BaseModel
from app.platform.assistant.contracts import OpaqueRef

class ConversationResponse(BaseModel):
    version: Literal[\"1\"]
    conversation_ref: OpaqueRef
""",
    )


def test_guard_accepts_complete_business_free_http_fixture(tmp_path: Path) -> None:
    _legal_fixture(tmp_path)

    assert inspect_assistant_http_boundary(tmp_path) == []


def test_guard_rejects_business_fact_writer_database_and_responsibility_imports(
    tmp_path: Path,
) -> None:
    _legal_fixture(tmp_path)
    _write(
        tmp_path,
        "assistant.py",
        """from app.domains.repairs.application import SubmitRepairHandler
from app.platform.directory.repository import SqlAlchemyPersonRepository
from app.platform.master_data_imports.ports import MasterDataImportAuditWriter
from app.platform.responsibility.repository import SqlAlchemyResponsibilityRepository
from sqlalchemy.orm import Session

dependencies = (
    SubmitRepairHandler,
    SqlAlchemyPersonRepository,
    MasterDataImportAuditWriter,
    SqlAlchemyResponsibilityRepository,
    Session,
)
""",
    )

    violations = inspect_assistant_http_boundary(tmp_path)

    assert any("app.domains.repairs.application" in item for item in violations)
    assert any("app.platform.directory.repository" in item for item in violations)
    assert any("app.platform.master_data_imports.ports" in item for item in violations)
    assert any("app.platform.responsibility.repository" in item for item in violations)
    assert any("sqlalchemy.orm" in item for item in violations)


def test_guard_rejects_dynamic_import_alias_and_incomplete_http_file_map(
    tmp_path: Path,
) -> None:
    _legal_fixture(tmp_path)
    _write(
        tmp_path,
        "assistant.py",
        """loader = __import__
repairs = loader(\"app.domains.repairs.application\")
runner = eval
database = runner(\"__import__('sqlalchemy')\")
""",
    )
    _write(tmp_path, "assistant_probe.py", "PROBE = True\n")
    _write(tmp_path, "assistant_probe/__init__.py", "PROBE = True\n")

    violations = inspect_assistant_http_boundary(tmp_path)

    assert any("dynamic execution or import is forbidden" in item for item in violations)
    assert any("unexpected assistant HTTP file: assistant_probe.py" in item for item in violations)
    assert any(
        "unexpected assistant HTTP file: assistant_probe/__init__.py" in item for item in violations
    )

    (tmp_path / "assistant_schemas.py").unlink()

    missing_violations = inspect_assistant_http_boundary(tmp_path)

    assert any("missing expected file: assistant_schemas.py" in item for item in missing_violations)


def test_real_assistant_http_boundary_has_only_approved_dependencies() -> None:
    http_root = Path(__file__).parents[2] / "app" / "interfaces" / "http"

    assert inspect_assistant_http_boundary(http_root) == []
