from __future__ import annotations

from pathlib import Path

from .assistant_contract_guard import inspect_assistant_contract_root

EXPECTED_FILES = frozenset(
    {
        "__init__.py",
        "catalog.py",
        "contracts.py",
        "controls.py",
        "orchestrator.py",
        "ports.py",
        "presentation.py",
        "runtime.py",
        "tool_loop.py",
    }
)


def _write(root: Path, relative: str, source: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")


def _legal_fixture(root: Path) -> None:
    _write(root, "tool_loop.py", "from time import monotonic\nfrom .runtime import RuntimeSnapshot\n")
    _write(root, "__init__.py", '"""Pure assistant boundary."""\n')
    _write(
        root,
        "contracts.py",
        """from pydantic import BaseModel, ConfigDict

class Envelope(BaseModel):
    model_config = ConfigDict(extra=\"forbid\", frozen=True, strict=True)
    version: str
""",
    )
    _write(
        root,
        "ports.py",
        """from typing import Protocol
from .contracts import Envelope

class EnvelopePort(Protocol):
    def handle(self, value: Envelope) -> Envelope: ...
""",
    )
    _write(
        root,
        "catalog.py",
        """from dataclasses import dataclass
from .ports import EnvelopePort

@dataclass(frozen=True)
class CatalogEntry:
    port: EnvelopePort
""",
    )
    _write(
        root,
        "controls.py",
        """from dataclasses import dataclass
from .catalog import CatalogEntry
from .contracts import Envelope
from .ports import EnvelopePort
from .presentation import Presentation

@dataclass(frozen=True)
class ControlService:
    entry: CatalogEntry

types = (Envelope, EnvelopePort, Presentation)
""",
    )
    _write(
        root,
        "orchestrator.py",
        """from typing import cast
from uuid import UUID
from .catalog import CatalogEntry
from .contracts import Envelope
from .controls import ControlService
from .ports import EnvelopePort
from .runtime import RuntimeSnapshot

types = (cast, UUID, CatalogEntry, ControlService, Envelope, EnvelopePort, RuntimeSnapshot)
""",
    )
    _write(
        root,
        "presentation.py",
        """from .contracts import Envelope

Presentation = Envelope
""",
    )
    _write(
        root,
        "runtime.py",
        """from dataclasses import dataclass
from .contracts import Envelope

@dataclass(frozen=True)
class RuntimeSnapshot:
    envelope: Envelope
""",
    )


def test_guard_accepts_a_complete_pure_contract_fixture(tmp_path: Path) -> None:
    _legal_fixture(tmp_path)

    assert inspect_assistant_contract_root(tmp_path, EXPECTED_FILES) == []


def test_guard_rejects_business_and_runtime_dependencies_in_renamed_files(
    tmp_path: Path,
) -> None:
    _legal_fixture(tmp_path)
    _write(
        tmp_path,
        "contracts.py",
        """from app.domains.repairs.contracts import SubmitRepair
from sqlalchemy.orm import Session

RepairEnvelope = SubmitRepair
DatabaseSession = Session
""",
    )

    violations = inspect_assistant_contract_root(tmp_path, EXPECTED_FILES)

    assert any("app.domains.repairs.contracts" in item for item in violations)
    assert any("sqlalchemy.orm" in item for item in violations)


def test_guard_rejects_real_runtime_business_and_data_dependencies(tmp_path: Path) -> None:
    _legal_fixture(tmp_path)
    _write(
        tmp_path,
        "runtime.py",
        """from app.domains.repairs.application import SubmitRepairHandler
from app.platform.directory.repository import SqlAlchemyPersonRepository
from app.platform.master_data_imports.ports import MasterDataImportAuditWriter
from app.platform.responsibility.repository import SqlAlchemyResponsibilityRepository

DEPENDENCIES = (
    SubmitRepairHandler,
    SqlAlchemyPersonRepository,
    MasterDataImportAuditWriter,
    SqlAlchemyResponsibilityRepository,
)
""",
    )

    violations = inspect_assistant_contract_root(tmp_path, EXPECTED_FILES)

    assert any("app.domains.repairs.application" in item for item in violations)
    assert any("app.platform.directory.repository" in item for item in violations)
    assert any("app.platform.master_data_imports.ports" in item for item in violations)
    assert any("app.platform.responsibility.repository" in item for item in violations)


def test_guard_rejects_orchestrator_and_catalog_business_data_dependencies(
    tmp_path: Path,
) -> None:
    _legal_fixture(tmp_path)
    _write(
        tmp_path,
        "orchestrator.py",
        """from app.domains.repairs.application import SubmitRepairHandler
from app.platform.directory.repository import SqlAlchemyPersonRepository
from app.platform.master_data_imports.ports import MasterDataImportAuditWriter
dependencies = (SubmitRepairHandler, SqlAlchemyPersonRepository, MasterDataImportAuditWriter)
""",
    )
    _write(
        tmp_path,
        "catalog.py",
        """from app.platform.responsibility.repository import SqlAlchemyResponsibilityRepository
dependency = SqlAlchemyResponsibilityRepository
""",
    )

    violations = inspect_assistant_contract_root(tmp_path, EXPECTED_FILES)

    assert any("orchestrator.py" in item and "app.domains.repairs" in item for item in violations)
    assert any(
        "orchestrator.py" in item and "app.platform.directory" in item for item in violations
    )
    assert any("master_data_imports" in item for item in violations)
    assert any("catalog.py" in item and "responsibility" in item for item in violations)


def test_guard_rejects_all_forbidden_dependencies_from_new_presentation_file(
    tmp_path: Path,
) -> None:
    _legal_fixture(tmp_path)
    _write(
        tmp_path,
        "presentation.py",
        """from app.domains.repairs.application import SubmitRepairHandler
from app.platform.directory.repository import SqlAlchemyPersonRepository
from app.platform.master_data_imports.ports import MasterDataImportAuditWriter
from app.platform.responsibility.repository import SqlAlchemyResponsibilityRepository
from sqlalchemy.orm import Session

database = __import__("app.db")
dependencies = (
    SubmitRepairHandler,
    SqlAlchemyPersonRepository,
    MasterDataImportAuditWriter,
    SqlAlchemyResponsibilityRepository,
    Session,
    database,
)
""",
    )

    violations = inspect_assistant_contract_root(tmp_path, EXPECTED_FILES)

    assert any("presentation.py" in item and "app.domains.repairs" in item for item in violations)
    assert any(
        "presentation.py" in item and "app.platform.directory" in item for item in violations
    )
    assert any("presentation.py" in item and "master_data_imports" in item for item in violations)
    assert any("presentation.py" in item and "responsibility" in item for item in violations)
    assert any("presentation.py" in item and "sqlalchemy.orm" in item for item in violations)
    assert any("presentation.py" in item and "dynamic import" in item for item in violations)


def test_guard_rejects_business_data_redis_and_lock_dependencies_from_controls(
    tmp_path: Path,
) -> None:
    _legal_fixture(tmp_path)
    _write(
        tmp_path,
        "controls.py",
        """from app.domains.repairs.application import SubmitRepairHandler
from app.platform.directory.repository import SqlAlchemyPersonRepository
from app.platform.master_data_imports.ports import MasterDataImportAuditWriter
from app.platform.responsibility.repository import SqlAlchemyResponsibilityRepository
from redis import Redis
from sqlalchemy.orm import Session
from threading import Lock

database = __import__("app.db")
dependencies = (
    SubmitRepairHandler,
    SqlAlchemyPersonRepository,
    MasterDataImportAuditWriter,
    SqlAlchemyResponsibilityRepository,
    Redis,
    Session,
    Lock,
    database,
)
""",
    )

    violations = inspect_assistant_contract_root(tmp_path, EXPECTED_FILES)

    assert any("controls.py" in item and "app.domains.repairs" in item for item in violations)
    assert any("controls.py" in item and "app.platform.directory" in item for item in violations)
    assert any("controls.py" in item and "master_data_imports" in item for item in violations)
    assert any("controls.py" in item and "responsibility" in item for item in violations)
    assert any("controls.py" in item and "redis" in item for item in violations)
    assert any("controls.py" in item and "sqlalchemy.orm" in item for item in violations)
    assert any("controls.py" in item and "threading" in item for item in violations)
    assert any("controls.py" in item and "dynamic import" in item for item in violations)


def test_redis_control_store_is_outside_kernel_and_has_no_distributed_lock() -> None:
    api_root = Path(__file__).resolve().parents[2]
    kernel_source = (api_root / "app/platform/assistant/controls.py").read_text(encoding="utf-8")
    adapter_source = (api_root / "app/composition/assistant_controls.py").read_text(
        encoding="utf-8"
    )

    assert "redis" not in kernel_source.casefold()
    assert "redis.call" in adapter_source
    assert "lock" not in adapter_source.casefold()


def test_runtime_stdlib_allowlist_does_not_widen_k1_contract_or_port_files(
    tmp_path: Path,
) -> None:
    _legal_fixture(tmp_path)
    _write(
        tmp_path,
        "contracts.py",
        """from pydantic import BaseModel
from threading import RLock

class Envelope(BaseModel):
    version: str

runtime_lock = RLock()
""",
    )
    _write(
        tmp_path,
        "ports.py",
        """from typing import Protocol
from uuid import uuid4
from .contracts import Envelope

class EnvelopePort(Protocol):
    def handle(self, value: Envelope) -> Envelope: ...
""",
    )

    violations = inspect_assistant_contract_root(tmp_path, EXPECTED_FILES)

    assert any("contracts.py: forbidden dependency: threading" in item for item in violations)
    assert any("ports.py: forbidden dependency: uuid" in item for item in violations)


def test_guard_rejects_aliased_and_builtins_dynamic_import_backdoors(
    tmp_path: Path,
) -> None:
    _legal_fixture(tmp_path)
    _write(
        tmp_path,
        "runtime.py",
        """loader = __import__
repairs = loader("app.domains.repairs.application")
builtins_loader = getattr(__builtins__, "__import__")
responsibility = builtins_loader("app.platform.responsibility.repository")
""",
    )

    violations = inspect_assistant_contract_root(tmp_path, EXPECTED_FILES)

    assert any("dynamic import is forbidden" in item for item in violations)


def test_guard_rejects_relative_parent_and_dynamic_import_backdoors(tmp_path: Path) -> None:
    _legal_fixture(tmp_path)
    _write(
        tmp_path,
        "ports.py",
        """from typing import Protocol
from ..responsibility import RepairResponsibilityRepository

HiddenRepository = __import__(\"app.db\")

class EnvelopePort(Protocol):
    def handle(self, value: object) -> object: ...
""",
    )

    violations = inspect_assistant_contract_root(tmp_path, EXPECTED_FILES)

    assert any("relative parent dependency" in item for item in violations)
    assert any("dynamic import" in item for item in violations)


def test_guard_rejects_protocol_alias_spoofing_and_hidden_runtime_functions(
    tmp_path: Path,
) -> None:
    _legal_fixture(tmp_path)
    _write(tmp_path, "__init__.py", "def boot():\n    return None\n")
    _write(
        tmp_path,
        "contracts.py",
        """def run_contract_runtime():
    return None
""",
    )
    _write(
        tmp_path,
        "ports.py",
        """from pydantic import BaseModel as Protocol

class FakePort(Protocol):
    def handle(self, value: object) -> object: ...
""",
    )

    violations = inspect_assistant_contract_root(tmp_path, EXPECTED_FILES)

    assert any("__init__.py" in item and "executable declaration" in item for item in violations)
    assert any("contracts.py" in item and "top-level implementation" in item for item in violations)
    assert any("FakePort must inherit typing.Protocol" in item for item in violations)


def test_guard_rejects_runtime_methods_hidden_inside_contract_models(tmp_path: Path) -> None:
    _legal_fixture(tmp_path)
    _write(
        tmp_path,
        "contracts.py",
        """from pydantic import BaseModel

class HiddenRuntime(BaseModel):
    def run(self) -> None:
        print(\"must not execute\")

class DetachedContract:
    pass
""",
    )

    violations = inspect_assistant_contract_root(tmp_path, EXPECTED_FILES)

    assert any("HiddenRuntime.run is not a contract validator" in item for item in violations)
    assert any(
        "DetachedContract does not inherit pydantic.BaseModel" in item for item in violations
    )
    assert any("unsafe runtime call: print" in item for item in violations)


def test_guard_rejects_executable_members_hidden_inside_protocols(tmp_path: Path) -> None:
    _legal_fixture(tmp_path)
    _write(
        tmp_path,
        "ports.py",
        """from typing import Protocol

class FakePort(Protocol):
    hidden = eval(\"40 + 2\")

    def handle(self, value: object) -> object: ...
""",
    )

    violations = inspect_assistant_contract_root(tmp_path, EXPECTED_FILES)

    assert any("FakePort contains executable class member" in item for item in violations)
    assert any("unsafe runtime call: eval" in item for item in violations)


def test_guard_rejects_a_concrete_port_implementation(tmp_path: Path) -> None:
    _legal_fixture(tmp_path)
    _write(
        tmp_path,
        "ports.py",
        """from .contracts import Envelope

class ConcretePort:
    def handle(self, value: Envelope) -> Envelope:
        return value

def hidden_runtime(value: Envelope) -> Envelope:
    return value
""",
    )

    violations = inspect_assistant_contract_root(tmp_path, EXPECTED_FILES)

    assert any("Protocol" in item for item in violations)
    assert any("implementation body" in item for item in violations)
    assert any("top-level implementation" in item for item in violations)


def test_guard_integrity_rejects_missing_or_unlisted_python_files(tmp_path: Path) -> None:
    _legal_fixture(tmp_path)
    (tmp_path / "contracts.py").unlink()

    missing = inspect_assistant_contract_root(tmp_path, EXPECTED_FILES)

    assert any("missing expected file: contracts.py" in item for item in missing)

    _legal_fixture(tmp_path)
    _write(tmp_path, "nested/runtime.py", "def run():\n    return None\n")

    unexpected = inspect_assistant_contract_root(tmp_path, EXPECTED_FILES)

    assert any("unexpected Python file: nested/runtime.py" in item for item in unexpected)


def test_real_k3b_assistant_root_is_complete_and_dependency_safe() -> None:
    assistant_root = Path(__file__).resolve().parents[2] / "app" / "platform" / "assistant"

    assert inspect_assistant_contract_root(assistant_root, EXPECTED_FILES) == []


def test_tool_loop_still_rejects_provider_and_business_dependencies(tmp_path: Path) -> None:
    _legal_fixture(tmp_path)
    _write(tmp_path, "tool_loop.py", "import httpx\nfrom app.composition import school_facts\n")
    violations = inspect_assistant_contract_root(tmp_path, EXPECTED_FILES)
    assert any("forbidden dependency: httpx" in item for item in violations)
    assert any("forbidden dependency: app.composition" in item for item in violations)
