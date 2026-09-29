from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import unicodedata
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Protocol, TypeAliasType, TypedDict, cast, get_args
from uuid import UUID, uuid4

import pytest
from fastapi import APIRouter, Depends, FastAPI, Header
from fastapi.dependencies.models import Dependant
from fastapi.routing import iter_route_contexts
from pydantic import AliasChoices, AliasPath, BaseModel, Field, TypeAdapter, ValidationError
from starlette.routing import BaseRoute, Host, Mount

from app.domains.campus.contracts import (
    CurrentLocationReader,
    LocationSnapshot,
    LocationSnapshotResolver,
)
from app.domains.students.contracts import (
    CurrentStudentRelationReader,
    StudentRelationInput,
    StudentRelationSnapshot,
    StudentRelationSnapshotResolver,
)
from app.main import app
from app.platform import contracts as platform_contracts
from app.platform.authorization.schemas import (
    PermissionGrantRequest,
    RoleAssignmentRequest,
    RoleRevocationRequest,
    ScopeAssignmentRequest,
)
from app.platform.calendar.contracts import (
    CalendarNotPublished,
    WorkCalendarReader,
    WorkCalendarSnapshot,
)
from app.platform.contracts import (
    ActorContext,
    AdministrativeClassId,
    AssignmentId,
    CalendarVersionId,
    CommandExecutionContext,
    EffectiveInterval,
    EnrollmentId,
    GuardianActorContext,
    GuardianRelationshipId,
    LocationId,
    OrganizationUnitId,
    PersonId,
    RequestExecutionContext,
    ResponsibilityGroupId,
    ResponsibilityTypeId,
    SchoolId,
    StaffActorContext,
    StudentId,
)
from app.platform.directory.contracts import PersonSnapshot, PersonSnapshotResolver
from app.platform.identity.schemas import DingTalkExchangeRequest
from app.platform.organization.contracts import (
    AssignmentSnapshot,
    AssignmentSnapshotResolver,
    CurrentAssignmentReader,
)
from app.platform.responsibility.contracts import (
    ResponsibilityGroupSnapshot,
    ResponsibilityGroupSnapshotResolver,
)
from app.platform.tenancy.contracts import CurrentSchoolReader, SchoolSnapshot

NOW = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=1)
SCHOOL_ID = SchoolId(uuid4())
PERSON_ID = PersonId(uuid4())
ORGANIZATION_UNIT_ID = OrganizationUnitId(uuid4())
ASSIGNMENT_ID = AssignmentId(uuid4())
RESPONSIBILITY_TYPE_ID = ResponsibilityTypeId(uuid4())
RESPONSIBILITY_GROUP_ID = ResponsibilityGroupId(uuid4())
LOCATION_ID = LocationId(uuid4())
STUDENT_ID = StudentId(uuid4())
ENROLLMENT_ID = EnrollmentId(uuid4())
ADMINISTRATIVE_CLASS_ID = AdministrativeClassId(uuid4())
GUARDIAN_RELATIONSHIP_ID = GuardianRelationshipId(uuid4())
CALENDAR_VERSION_ID = CalendarVersionId(uuid4())


class _AliasBackedAuthorityRequest(BaseModel):
    school_id: str = Field(alias="displayName")


type _AliasBackedAuthorityPayload = _AliasBackedAuthorityRequest
type _ForwardAliasBackedAuthorityPayload = "_AliasBackedAuthorityRequest"


class _AliasWrappedAuthorityRequest(BaseModel):
    payload: _AliasBackedAuthorityPayload


class _ForwardAliasWrappedAuthorityRequest(BaseModel):
    payload: _ForwardAliasBackedAuthorityPayload


class SnapshotCommon(TypedDict):
    school_id: SchoolId
    source_version: int
    captured_at: datetime
    effective_interval: EffectiveInterval
    snapshot_basis: str


class _StaffContextIssuer(Protocol):
    def __call__(
        self,
        *,
        account_user_id: int,
        school_id: SchoolId,
        person_id: PersonId,
        assignment_id: AssignmentId,
        assignment_version: int,
        authorization_version: int,
    ) -> StaffActorContext: ...


class _UntrustedStaffContextConstructor(Protocol):
    def __call__(
        self,
        *,
        account_user_id: int,
        school_id: SchoolId,
        person_id: PersonId,
        assignment_id: AssignmentId,
        assignment_version: int,
        authorization_version: int,
    ) -> StaffActorContext: ...


class _ForgedStaffContextConstructor(Protocol):
    def __call__(
        self,
        *,
        account_user_id: int,
        school_id: SchoolId,
        person_id: PersonId,
        assignment_id: AssignmentId,
        assignment_version: int,
        authorization_version: int,
        issuance_provenance: object,
    ) -> StaffActorContext: ...


class _GuardianContextIssuer(Protocol):
    def __call__(
        self,
        *,
        account_user_id: int,
        school_id: SchoolId,
        person_id: PersonId,
        guardian_relationship_id: GuardianRelationshipId,
        relationship_version: int,
        authorization_version: int,
    ) -> GuardianActorContext: ...


class _RequestContextIssuer(Protocol):
    def __call__(
        self,
        *,
        actor: ActorContext,
        request_id: UUID,
    ) -> RequestExecutionContext: ...


class _CommandContextIssuer(Protocol):
    def __call__(
        self,
        *,
        actor: ActorContext,
        request_id: UUID,
        idempotency_key: str,
    ) -> CommandExecutionContext: ...


def _interval() -> EffectiveInterval:
    return EffectiveInterval(valid_from=NOW, valid_to=LATER)


def _attempt_attribute_mutation(target: object, attribute: str, value: object) -> None:
    setattr(target, attribute, value)


def _issue_staff_context() -> StaffActorContext:
    issuer = cast(
        _StaffContextIssuer,
        platform_contracts._issue_staff_actor_context,  # pyright: ignore[reportPrivateUsage]
    )
    return issuer(
        account_user_id=7,
        school_id=SCHOOL_ID,
        person_id=PERSON_ID,
        assignment_id=ASSIGNMENT_ID,
        assignment_version=2,
        authorization_version=3,
    )


def _issue_guardian_context() -> GuardianActorContext:
    issuer = cast(
        _GuardianContextIssuer,
        platform_contracts._issue_guardian_actor_context,  # pyright: ignore[reportPrivateUsage]
    )
    return issuer(
        account_user_id=7,
        school_id=SCHOOL_ID,
        person_id=PERSON_ID,
        guardian_relationship_id=GUARDIAN_RELATIONSHIP_ID,
        relationship_version=4,
        authorization_version=3,
    )


def _issue_request_context(actor: ActorContext) -> RequestExecutionContext:
    issuer = cast(
        _RequestContextIssuer,
        platform_contracts._issue_request_execution_context,  # pyright: ignore[reportPrivateUsage]
    )
    return issuer(actor=actor, request_id=uuid4())


def _issue_command_context(actor: ActorContext) -> CommandExecutionContext:
    issuer = cast(
        _CommandContextIssuer,
        platform_contracts._issue_command_execution_context,  # pyright: ignore[reportPrivateUsage]
    )
    return issuer(
        actor=actor,
        request_id=uuid4(),
        idempotency_key="repair-create-20260805-0001",
    )


def _snapshot_common() -> SnapshotCommon:
    return {
        "school_id": SCHOOL_ID,
        "source_version": 1,
        "captured_at": NOW,
        "effective_interval": _interval(),
        "snapshot_basis": "occurred_at",
    }


def test_stable_ids_keep_uuid_runtime_values() -> None:
    stable_ids = (
        SCHOOL_ID,
        PERSON_ID,
        ORGANIZATION_UNIT_ID,
        ASSIGNMENT_ID,
        RESPONSIBILITY_TYPE_ID,
        RESPONSIBILITY_GROUP_ID,
        LOCATION_ID,
        STUDENT_ID,
        ENROLLMENT_ID,
        ADMINISTRATIVE_CLASS_ID,
        GUARDIAN_RELATIONSHIP_ID,
        CALENDAR_VERSION_ID,
    )

    assert all(isinstance(value, UUID) for value in stable_ids)
    assert len(set(stable_ids)) == len(stable_ids)


def test_pyright_rejects_cross_kind_stable_ids(tmp_path: Path) -> None:
    probe = tmp_path / "stable_id_negative_probe.py"
    probe.write_text(
        """
from uuid import uuid4
from app.platform.contracts import (
    AssignmentId,
    LocationId,
    PersonId,
    SchoolId,
    StudentId,
)

def needs_person(value: PersonId) -> None: ...
def needs_student(value: StudentId) -> None: ...
def needs_assignment(value: AssignmentId) -> None: ...
def needs_location(value: LocationId) -> None: ...
def needs_school(value: SchoolId) -> None: ...

school_id = SchoolId(uuid4())
person_id = PersonId(uuid4())
student_id = StudentId(uuid4())
assignment_id = AssignmentId(uuid4())
location_id = LocationId(uuid4())

needs_person(school_id)
needs_student(person_id)
needs_assignment(student_id)
needs_location(assignment_id)
needs_school(location_id)
""".strip(),
        encoding="utf-8",
    )

    # pyright-python may provision its private Node runtime on the first call.
    # nodeenv writes bootstrap diagnostics to stdout, so complete and verify that
    # bootstrap before invoking Pyright's machine-readable JSON contract.
    bootstrap = subprocess.run(
        [sys.executable, "-m", "pyright", "--version"],
        cwd=Path(__file__).resolve().parents[2],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert bootstrap.returncode == 0, bootstrap.stdout + bootstrap.stderr

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyright",
            "--outputjson",
            str(probe),
        ],
        cwd=Path(__file__).resolve().parents[2],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["summary"]["errorCount"] == 5
    assert all(
        "cannot be assigned to parameter" in diagnostic["message"]
        for diagnostic in report["generalDiagnostics"]
    )


def test_effective_interval_is_half_open_and_rejects_empty_range() -> None:
    assert _interval().valid_from == NOW

    with pytest.raises(ValidationError):
        EffectiveInterval(valid_from=NOW, valid_to=NOW)
    with pytest.raises(ValidationError):
        EffectiveInterval(valid_from=LATER, valid_to=NOW)


def test_effective_interval_requires_timezone_aware_values() -> None:
    with pytest.raises(ValidationError):
        EffectiveInterval(valid_from=datetime(2026, 8, 5, 8, 0), valid_to=None)


def test_effective_interval_is_frozen_and_forbids_unknown_fields() -> None:
    interval = _interval()

    with pytest.raises(ValidationError):
        EffectiveInterval.model_validate(
            {"valid_from": NOW, "valid_to": LATER, "inclusive_end": True}
        )
    with pytest.raises(ValidationError):
        interval.valid_to = None


def test_student_relation_input_cannot_be_confused_with_snapshot() -> None:
    relation = StudentRelationInput(student_id=STUDENT_ID)

    with pytest.raises(ValidationError):
        StudentRelationSnapshot.model_validate(relation.model_dump())


def test_snapshot_models_are_strict_frozen_and_flat() -> None:
    snapshots = (
        SchoolSnapshot(
            **_snapshot_common(),
            display_name="第一中学",
            timezone_name="Asia/Shanghai",
        ),
        PersonSnapshot(
            **_snapshot_common(),
            person_id=PERSON_ID,
            display_name="张老师",
        ),
        AssignmentSnapshot(
            **_snapshot_common(),
            assignment_id=ASSIGNMENT_ID,
            person_id=PERSON_ID,
            organization_unit_id=ORGANIZATION_UNIT_ID,
            organization_display_name="总务处",
            position_title="水电维修工",
            semantic_role_code="repair_worker",
            assignment_kind="substantive",
            appointment_source="manual",
            approval_reference="staff-record-001",
            planned_valid_to=None,
            status="confirmed",
        ),
        ResponsibilityGroupSnapshot(
            **_snapshot_common(),
            responsibility_group_id=RESPONSIBILITY_GROUP_ID,
            display_name="总务报修组",
        ),
        LocationSnapshot(
            **_snapshot_common(),
            location_id=LOCATION_ID,
            parent_location_id=None,
            kind="building",
            display_name="一号教学楼三楼卫生间",
            display_path="校本部 / 一号教学楼 / 三楼 / 卫生间",
        ),
        StudentRelationSnapshot(
            **_snapshot_common(),
            student_id=STUDENT_ID,
            enrollment_id=ENROLLMENT_ID,
            administrative_class_id=ADMINISTRATIVE_CLASS_ID,
            display_name="李明",
            administrative_class_display_name="初一（3）班",
            used_recorded_at_fallback=False,
        ),
        WorkCalendarSnapshot(
            **_snapshot_common(),
            calendar_version_id=CALENDAR_VERSION_ID,
            display_name="2026—2027 学年校历",
            timezone_name="Asia/Shanghai",
        ),
    )

    for snapshot in snapshots:
        assert snapshot.model_config.get("extra") == "forbid"
        assert snapshot.model_config.get("frozen") is True
        assert "__dict__" in dir(snapshot)
        assert all(not isinstance(value, (dict, list, set)) for value in snapshot.__dict__.values())
        with pytest.raises(ValidationError):
            snapshot.source_version = 2


def test_snapshot_models_reject_unknown_fields_and_invalid_versions() -> None:
    valid_person = {
        **_snapshot_common(),
        "person_id": PERSON_ID,
        "display_name": "张老师",
    }

    with pytest.raises(ValidationError):
        PersonSnapshot.model_validate({**valid_person, "current_phone": "13800000000"})
    with pytest.raises(ValidationError):
        PersonSnapshot.model_validate({**valid_person, "source_version": 0})
    with pytest.raises(ValidationError):
        PersonSnapshot.model_validate({**valid_person, "display_name": "名" * 201})


def test_location_snapshot_has_no_person_or_responsibility_fields() -> None:
    forbidden_fields = {
        "person_id",
        "assignment_id",
        "responsibility_type_id",
        "responsibility_group_id",
        "responsible_user_id",
    }

    assert forbidden_fields.isdisjoint(LocationSnapshot.model_fields)


def test_external_request_cannot_construct_execution_context() -> None:
    assert not issubclass(StaffActorContext, BaseModel)
    assert not issubclass(GuardianActorContext, BaseModel)
    assert not issubclass(RequestExecutionContext, BaseModel)
    assert not issubclass(CommandExecutionContext, BaseModel)


@pytest.mark.parametrize(
    ("request_model", "payload"),
    [
        (
            DingTalkExchangeRequest,
            {"auth_code": "one-time-code"},
        ),
        (
            RoleAssignmentRequest,
            {"reason": "测试未知字段拒绝", "role_code": "teacher"},
        ),
        (
            RoleRevocationRequest,
            {"reason": "测试未知字段拒绝"},
        ),
        (
            ScopeAssignmentRequest,
            {
                "reason": "测试未知字段拒绝",
                "scope_type": "location",
                "scope_value": "building-a",
            },
        ),
        (
            PermissionGrantRequest,
            {
                "reason": "测试未知字段拒绝",
                "action_code": "repair.create",
                "scope_type": "all",
            },
        ),
    ],
)
def test_http_request_models_reject_unknown_authority_fields(
    request_model: type[BaseModel], payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        request_model.model_validate({**payload, "school_id": str(SCHOOL_ID)})


def test_trusted_context_construction_requires_private_provenance() -> None:
    untrusted_constructor = cast(_UntrustedStaffContextConstructor, StaffActorContext)
    with pytest.raises(TypeError):
        untrusted_constructor(
            account_user_id=7,
            school_id=SCHOOL_ID,
            person_id=PERSON_ID,
            assignment_id=ASSIGNMENT_ID,
            assignment_version=2,
            authorization_version=3,
        )


def test_pydantic_cannot_deserialize_trusted_actor_context() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(StaffActorContext).validate_python(
            {
                "account_user_id": 7,
                "school_id": SCHOOL_ID,
                "person_id": PERSON_ID,
                "assignment_id": ASSIGNMENT_ID,
                "assignment_version": 2,
                "authorization_version": 3,
            }
        )


def test_trusted_context_does_not_export_a_reusable_provenance_singleton() -> None:
    assert not hasattr(platform_contracts, "_CONTEXT_ISSUANCE_PROVENANCE")

    forged_constructor = cast(_ForgedStaffContextConstructor, StaffActorContext)
    with pytest.raises(ValueError, match="只能由服务端身份解析器构造"):
        forged_constructor(
            account_user_id=7,
            school_id=SCHOOL_ID,
            person_id=PERSON_ID,
            assignment_id=ASSIGNMENT_ID,
            assignment_version=2,
            authorization_version=3,
            issuance_provenance=object(),
        )


def test_trusted_contexts_reject_runtime_subclassing() -> None:
    def bypass_provenance(_self: object, _issuance_provenance: object) -> None:
        return None

    trusted_context_types: tuple[type[object], ...] = (
        StaffActorContext,
        GuardianActorContext,
        RequestExecutionContext,
        CommandExecutionContext,
    )
    for context_type in trusted_context_types:
        with pytest.raises(TypeError, match="可信执行上下文禁止继承"):
            type(
                f"Forged{context_type.__name__}",
                (context_type,),
                {"__post_init__": bypass_provenance},
            )


def test_request_context_seal_state_is_not_mutable_class_data() -> None:
    assert "_subclassing_sealed" not in vars(RequestExecutionContext)


_PRIVATE_CONTEXT_SYMBOLS = {
    "_CONTEXT_ISSUANCE_PROVENANCE",
    "_ContextIssuanceProvenance",
    "_build_context_issuance_boundary",
    "_build_request_context_subclass_guard",
    "_request_context_subclass_guard",
    "_seal_request_context_subclassing",
    "_subclassing_sealed",
    "_require_context_issuance_provenance",
    "_issue_staff_actor_context",
    "_issue_guardian_actor_context",
    "_issue_request_execution_context",
    "_issue_command_execution_context",
}
_TRUSTED_CONTEXT_CONSTRUCTORS = {
    "StaffActorContext",
    "GuardianActorContext",
    "RequestExecutionContext",
    "CommandExecutionContext",
}


def _uses_private_context_symbol(tree: ast.AST) -> bool:
    return any(
        (isinstance(node, ast.Name) and node.id in _PRIVATE_CONTEXT_SYMBOLS)
        or (isinstance(node, ast.Attribute) and node.attr in _PRIVATE_CONTEXT_SYMBOLS)
        or (isinstance(node, ast.alias) and node.name in _PRIVATE_CONTEXT_SYMBOLS)
        for node in ast.walk(tree)
    )


def _constructs_trusted_context(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name) and function.id in _TRUSTED_CONTEXT_CONSTRUCTORS:
            return True
        if isinstance(function, ast.Attribute) and function.attr in _TRUSTED_CONTEXT_CONSTRUCTORS:
            return True
    return False


def _subclasses_trusted_context(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id in _TRUSTED_CONTEXT_CONSTRUCTORS:
                return True
            if isinstance(base, ast.Attribute) and base.attr in _TRUSTED_CONTEXT_CONSTRUCTORS:
                return True
    return False


def test_context_boundary_detector_rejects_provenance_and_direct_constructor_fixture() -> None:
    tree = ast.parse(
        """
from app.platform.contracts import _CONTEXT_ISSUANCE_PROVENANCE, StaffActorContext

class ForgedStaffActorContext(StaffActorContext):
    pass

forged = StaffActorContext(issuance_provenance=_CONTEXT_ISSUANCE_PROVENANCE)
"""
    )

    assert _uses_private_context_symbol(tree)
    assert _constructs_trusted_context(tree)
    assert _subclasses_trusted_context(tree)


def test_private_context_provenance_has_only_identity_owned_consumers() -> None:
    app_root = Path(__file__).resolve().parents[2] / "app"
    contracts_path = app_root / "platform" / "contracts.py"
    allowed_private_consumers = {
        contracts_path,
        app_root / "platform" / "identity" / "execution_context.py",
        app_root / "platform" / "identity" / "executor_context_codec.py",
    }
    violations: list[Path] = []

    for source_path in app_root.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        uses_private_symbol = (
            source_path not in allowed_private_consumers and _uses_private_context_symbol(tree)
        )
        directly_constructs_context = source_path != contracts_path and _constructs_trusted_context(
            tree
        )
        subclasses_context = source_path != contracts_path and _subclasses_trusted_context(tree)
        if uses_private_symbol or directly_constructs_context or subclasses_context:
            violations.append(source_path.relative_to(app_root))

    assert violations == []


_RAW_REQUEST_BODY_METHODS = {"body", "form", "json", "receive", "stream"}
_RAW_REQUEST_PARAMETER_SOURCES = {"cookies", "headers", "path_params", "query_params"}
_RAW_CONNECTION_TYPE_NAMES = {"HTTPConnection", "Request"}
_RAW_CONNECTION_FRAMEWORK_MODULES = {"fastapi", "starlette.requests"}
_RAW_CONNECTION_HOLDER_MODULES = {
    "app.core.request_id",
    "app.interfaces.http.framework_request",
    "app.platform.authorization.router",
    "app.platform.identity.context",
    "app.platform.identity.router",
}


def _annotation_mentions_request(
    annotation: ast.expr | None,
    request_type_names: set[str],
    request_module_names: set[str],
    typing_wrappers: dict[str, str],
) -> bool:
    if annotation is None:
        return False
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            forward_expression = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return False
        return _annotation_mentions_request(
            forward_expression,
            request_type_names,
            request_module_names,
            typing_wrappers,
        )
    if _is_request_type_expression(annotation, request_type_names, request_module_names):
        return True
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _annotation_mentions_request(
            annotation.left, request_type_names, request_module_names, typing_wrappers
        ) or _annotation_mentions_request(
            annotation.right, request_type_names, request_module_names, typing_wrappers
        )
    if not isinstance(annotation, ast.Subscript):
        return False
    wrapper_kind = typing_wrappers.get(_dotted_expression_name(annotation.value) or "")
    if wrapper_kind not in {"Annotated", "Optional", "Union"}:
        return False
    wrapped_types = (
        annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
    )
    if wrapper_kind == "Annotated":
        wrapped_types = wrapped_types[:1]
    return any(
        _annotation_mentions_request(
            wrapped_type,
            request_type_names,
            request_module_names,
            typing_wrappers,
        )
        for wrapped_type in wrapped_types
    )


def _typing_wrapper_names(tree: ast.AST) -> dict[str, str]:
    wrappers: dict[str, str] = {}
    supported_modules = {"typing", "typing_extensions"}
    supported_wrappers = {"Annotated", "Optional", "Union"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in supported_modules:
            for imported_name in node.names:
                if imported_name.name in supported_wrappers:
                    wrappers[imported_name.asname or imported_name.name] = imported_name.name
        elif isinstance(node, ast.Import):
            for imported_name in node.names:
                if imported_name.name in supported_modules:
                    qualifier = imported_name.asname or imported_name.name
                    for wrapper in supported_wrappers:
                        wrappers[f"{qualifier}.{wrapper}"] = wrapper
    return wrappers


def _dotted_expression_name(expression: ast.expr) -> str | None:
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        parent = _dotted_expression_name(expression.value)
        return f"{parent}.{expression.attr}" if parent is not None else None
    return None


def _is_request_type_expression(
    expression: ast.expr,
    request_type_names: set[str],
    request_module_names: set[str],
) -> bool:
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        try:
            forward_expression = ast.parse(expression.value, mode="eval").body
        except SyntaxError:
            return False
        return _is_request_type_expression(
            forward_expression, request_type_names, request_module_names
        )
    return (isinstance(expression, ast.Name) and expression.id in request_type_names) or (
        isinstance(expression, ast.Attribute)
        and expression.attr in _RAW_CONNECTION_TYPE_NAMES
        and _dotted_expression_name(expression.value) in request_module_names
    )


def _request_type_names(
    tree: ast.AST,
    request_module_names: set[str],
    typing_wrappers: dict[str, str],
) -> set[str]:
    request_type_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module not in {"fastapi", "starlette.requests"}:
            continue
        for imported_name in node.names:
            if imported_name.name in _RAW_CONNECTION_TYPE_NAMES:
                request_type_names.add(imported_name.asname or imported_name.name)

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.TypeAlias):
                value = node.value
                targets: list[ast.expr] = [node.name]
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            else:
                continue
            if value is None:
                continue
            if not _annotation_mentions_request(
                value, request_type_names, request_module_names, typing_wrappers
            ):
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in request_type_names:
                    request_type_names.add(target.id)
                    changed = True
    return request_type_names


def _request_module_names(tree: ast.AST) -> set[str]:
    module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported_name in node.names:
                if imported_name.name in {"fastapi", "starlette.requests"}:
                    module_names.add(imported_name.asname or imported_name.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "starlette":
            for imported_name in node.names:
                if imported_name.name == "requests":
                    module_names.add(imported_name.asname or imported_name.name)
    return module_names


def _has_disallowed_request_type_indirection(
    tree: ast.AST,
    request_type_names: set[str],
    request_module_names: set[str],
    typing_wrappers: dict[str, str],
) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            assigned_symbol = _dotted_expression_name(node.value)
            if (
                assigned_symbol in request_type_names
                or assigned_symbol in request_module_names
                or assigned_symbol in typing_wrappers
            ):
                return True
        elif isinstance(node, ast.TypeAlias) and _annotation_mentions_request(
            node.value,
            request_type_names,
            request_module_names,
            typing_wrappers,
        ):
            return True
        elif isinstance(node, ast.ClassDef) and any(
            _annotation_mentions_request(
                base,
                request_type_names,
                request_module_names,
                typing_wrappers,
            )
            for base in node.bases
        ):
            return True
    return False


def _function_arguments(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[ast.arg, ...]:
    return (
        *function.args.posonlyargs,
        *function.args.args,
        *function.args.kwonlyargs,
    )


def _function_body_nodes(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.AST]:
    nodes: list[ast.AST] = []

    def visit(node: ast.AST) -> None:
        nodes.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    for statement in function.body:
        visit(statement)
    return nodes


def _request_function_has_nested_scope(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    for statement in function.body:
        for node in ast.walk(statement):
            if isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef),
            ):
                return True
    return False


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> set[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return {target.id for target in targets if isinstance(target, ast.Name)}


def _is_request_value(expression: ast.expr, request_names: set[str]) -> bool:
    return isinstance(expression, ast.Name) and expression.id in request_names


def _module_compound_binding_names(statement: ast.stmt) -> set[str]:
    names: set[str] = set()

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            return
        if isinstance(
            node,
            (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp),
        ):
            return
        if isinstance(node, ast.Import):
            names.update(
                imported_name.asname or imported_name.name.split(".")[0]
                for imported_name in node.names
            )
            return
        if isinstance(node, ast.ImportFrom):
            names.update(imported_name.asname or imported_name.name for imported_name in node.names)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and isinstance(node.name, str):
            names.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and isinstance(node.name, str):
            names.add(node.name)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(statement)
    return names


def _settings_type_symbols(tree: ast.AST) -> tuple[set[str], set[str]]:
    if not isinstance(tree, ast.Module):
        return set(), set()
    bindings: dict[str, tuple[str, str | None]] = {}
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom):
            for imported_name in statement.names:
                local_name = imported_name.asname or imported_name.name
                is_trusted = (
                    statement.module == "app.core.settings" and imported_name.name == "Settings"
                )
                bindings[local_name] = ("type" if is_trusted else "other", None)
        elif isinstance(statement, ast.Import):
            for imported_name in statement.names:
                local_name = imported_name.asname or imported_name.name.split(".")[0]
                is_trusted = imported_name.name == "app.core.settings"
                qualifier = imported_name.asname or imported_name.name
                bindings[local_name] = (
                    "module" if is_trusted else "other",
                    qualifier if is_trusted else None,
                )
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bindings[statement.name] = ("other", None)
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                for node in ast.walk(target):
                    if isinstance(node, ast.Name):
                        bindings[node.id] = ("other", None)
        else:
            for name in _module_compound_binding_names(statement):
                bindings[name] = ("other", None)

    type_names = {name for name, binding in bindings.items() if binding[0] == "type"}
    module_names = {
        qualifier
        for binding in bindings.values()
        if binding[0] == "module"
        for qualifier in [binding[1]]
        if qualifier is not None
    }
    return type_names, module_names


def _annotation_is_exact_settings(
    annotation: ast.expr | None,
    settings_type_names: set[str],
    settings_module_names: set[str],
    typing_wrappers: dict[str, str],
) -> bool:
    if annotation is None:
        return False
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return False
    if isinstance(annotation, ast.Name):
        return annotation.id in settings_type_names
    if isinstance(annotation, ast.Attribute):
        return (
            annotation.attr == "Settings"
            and _dotted_expression_name(annotation.value) in settings_module_names
        )
    if not isinstance(annotation, ast.Subscript):
        return False
    wrapper_kind = typing_wrappers.get(_dotted_expression_name(annotation.value) or "")
    if wrapper_kind != "Annotated":
        return False
    wrapped_type = (
        annotation.slice.elts[0] if isinstance(annotation.slice, ast.Tuple) else annotation.slice
    )
    return _annotation_is_exact_settings(
        wrapped_type,
        settings_type_names,
        settings_module_names,
        typing_wrappers,
    )


def _is_approved_identity_cookie_access(
    node: ast.Attribute,
    parents: dict[ast.AST, ast.AST],
    settings_parameter_names: set[str],
) -> bool:
    method = parents.get(node)
    call = parents.get(method) if method is not None else None
    if (
        not isinstance(method, ast.Attribute)
        or method.attr != "get"
        or not isinstance(call, ast.Call)
        or call.func is not method
        or len(call.args) != 1
        or call.keywords
    ):
        return False
    cookie_name = call.args[0]
    return (
        isinstance(cookie_name, ast.Attribute)
        and cookie_name.attr == "identity_session_cookie_name"
        and isinstance(cookie_name.value, ast.Name)
        and cookie_name.value.id in settings_parameter_names
    )


def _assignment_escapes_request(node: ast.Assign | ast.AnnAssign, request_names: set[str]) -> bool:
    value = node.value
    if value is None:
        return False
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    if _is_request_value(value, request_names):
        return any(not isinstance(target, ast.Name) for target in targets)
    if not isinstance(value, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
        return False
    return any(
        isinstance(descendant, ast.Name) and descendant.id in request_names
        for descendant in ast.walk(value)
    )


def _rebound_names(nodes: list[ast.AST]) -> set[str]:
    rebound = {
        node.id
        for node in nodes
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
    }
    rebound.update(
        node.name
        for node in nodes
        if isinstance(node, ast.ExceptHandler) and isinstance(node.name, str)
    )
    rebound.update(
        node.name
        for node in nodes
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and isinstance(node.name, str)
    )
    rebound.update(
        node.name
        for node in nodes
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )
    for node in nodes:
        if isinstance(node, ast.Import):
            rebound.update(
                imported_name.asname or imported_name.name.split(".")[0]
                for imported_name in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            rebound.update(
                imported_name.asname or imported_name.name for imported_name in node.names
            )
    return rebound


def _direct_argument_call(node: ast.Name, parents: dict[ast.AST, ast.AST]) -> ast.Call | None:
    parent = parents.get(node)
    if isinstance(parent, ast.Call) and node in parent.args:
        return parent
    if isinstance(parent, ast.keyword) and parent.value is node:
        call = parents.get(parent)
        return call if isinstance(call, ast.Call) else None
    return None


def _is_direct_bool_call(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    call = parents.get(node)
    return (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "bool"
        and call.args == [node]
        and not call.keywords
    )


def _is_framework_presence_probe(
    attribute: ast.Attribute,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    if attribute.attr == "query_params":
        return _is_direct_bool_call(attribute, parents)
    if attribute.attr != "body":
        return False
    body_call = parents.get(attribute)
    if (
        not isinstance(body_call, ast.Call)
        or body_call.func is not attribute
        or body_call.args
        or body_call.keywords
    ):
        return False
    awaited = parents.get(body_call)
    return isinstance(awaited, ast.Await) and _is_direct_bool_call(awaited, parents)


def _imports_unapproved_raw_request_type(
    tree: ast.AST, *, allow_direct_framework_connection: bool = False
) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for imported_name in node.names:
                full_import_name = (
                    f"{node.module}.{imported_name.name}"
                    if node.module is not None
                    else imported_name.name
                )
                imports_holder_module = any(
                    full_import_name == holder_module
                    or holder_module.startswith(f"{full_import_name}.")
                    or (
                        imported_name.name == "*"
                        and node.module is not None
                        and (
                            node.module == holder_module
                            or holder_module.startswith(f"{node.module}.")
                        )
                    )
                    for holder_module in _RAW_CONNECTION_HOLDER_MODULES
                )
                imports_framework_module = full_import_name in (
                    _RAW_CONNECTION_FRAMEWORK_MODULES
                ) or (
                    imported_name.name == "*" and node.module in _RAW_CONNECTION_FRAMEWORK_MODULES
                )
                if imports_holder_module or imports_framework_module:
                    return True
                if imported_name.name not in _RAW_CONNECTION_TYPE_NAMES:
                    continue
                is_approved_direct_import = (
                    allow_direct_framework_connection
                    and node.module in _RAW_CONNECTION_FRAMEWORK_MODULES
                    and imported_name.asname is None
                )
                if not is_approved_direct_import:
                    return True
        if isinstance(node, ast.Import):
            for imported_name in node.names:
                imports_raw_module = any(
                    imported_name.name == raw_module
                    or raw_module.startswith(f"{imported_name.name}.")
                    for raw_module in (
                        _RAW_CONNECTION_FRAMEWORK_MODULES | _RAW_CONNECTION_HOLDER_MODULES
                    )
                )
                if imports_raw_module:
                    return True
    return False


def _uses_unmodeled_raw_request_body(
    tree: ast.AST,
    *,
    allow_identity_session_cookie: bool = False,
    allow_framework_request_presence: bool = False,
    allow_middleware_handoff: bool = False,
) -> bool:
    typing_wrappers = _typing_wrapper_names(tree)
    request_module_names = _request_module_names(tree)
    request_type_names = _request_type_names(tree, request_module_names, typing_wrappers)
    if _has_disallowed_request_type_indirection(
        tree, request_type_names, request_module_names, typing_wrappers
    ):
        return True
    settings_type_names, settings_module_names = _settings_type_symbols(tree)
    functions = [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    request_names_by_function = {
        id(function): {
            argument.arg
            for argument in _function_arguments(function)
            if _annotation_mentions_request(
                argument.annotation,
                request_type_names,
                request_module_names,
                typing_wrappers,
            )
        }
        for function in functions
    }
    bound_raw_readers_by_function: dict[int, set[str]] = {
        id(function): set() for function in functions
    }

    changed = True
    while changed:
        changed = False
        for function in functions:
            function_key = id(function)
            request_names = request_names_by_function[function_key]
            bound_readers = bound_raw_readers_by_function[function_key]
            nodes = _function_body_nodes(function)
            for node in nodes:
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                    assigned_names = _assigned_names(node)
                    if _is_request_value(node.value, request_names):
                        new_names = assigned_names - request_names
                        if new_names:
                            request_names.update(new_names)
                            changed = True
                    if (
                        isinstance(node.value, ast.Attribute)
                        and node.value.attr in _RAW_REQUEST_BODY_METHODS
                        and _is_request_value(node.value.value, request_names)
                    ):
                        new_readers = assigned_names - bound_readers
                        if new_readers:
                            bound_readers.update(new_readers)
                            changed = True
                    if isinstance(node.value, ast.Name) and node.value.id in bound_readers:
                        new_readers = assigned_names - bound_readers
                        if new_readers:
                            bound_readers.update(new_readers)
                            changed = True

    for function in functions:
        function_key = id(function)
        request_names = request_names_by_function[function_key]
        if not request_names:
            continue
        if _request_function_has_nested_scope(function):
            return True
        bound_readers = bound_raw_readers_by_function[function_key]
        nodes = _function_body_nodes(function)
        parents = {child: node for node in nodes for child in ast.iter_child_nodes(node)}
        rebound_names = _rebound_names(nodes)
        function_arguments = _function_arguments(function)
        function_parameter_names = {argument.arg for argument in function_arguments}
        settings_parameter_names = {
            argument.arg
            for argument in function_arguments
            if _annotation_is_exact_settings(
                argument.annotation,
                settings_type_names,
                settings_module_names,
                typing_wrappers,
            )
            and argument.arg not in rebound_names
        }
        for node in nodes:
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in request_names
            ):
                parent = parents.get(node)
                direct_alias = (
                    isinstance(parent, (ast.Assign, ast.AnnAssign))
                    and parent.value is node
                    and all(
                        isinstance(target, ast.Name)
                        for target in (
                            parent.targets if isinstance(parent, ast.Assign) else [parent.target]
                        )
                    )
                )
                server_state = (
                    isinstance(parent, ast.Attribute)
                    and parent.value is node
                    and parent.attr == "state"
                )
                framework_presence = (
                    allow_framework_request_presence
                    and function.name == "get_framework_request_presence"
                    and isinstance(parent, ast.Attribute)
                    and parent.value is node
                    and _is_framework_presence_probe(parent, parents)
                )
                identity_cookie = (
                    allow_identity_session_cookie
                    and isinstance(parent, ast.Attribute)
                    and parent.value is node
                    and parent.attr == "cookies"
                    and _is_approved_identity_cookie_access(
                        parent, parents, settings_parameter_names
                    )
                )
                call = _direct_argument_call(node, parents)
                middleware_handoff = (
                    allow_middleware_handoff
                    and function.name == "assign_request_id"
                    and "call_next" not in rebound_names
                    and call is not None
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "call_next"
                    and call.func.id in function_parameter_names
                )
                if not (
                    direct_alias
                    or server_state
                    or framework_presence
                    or identity_cookie
                    or middleware_handoff
                ):
                    return True
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and _assignment_escapes_request(
                node, request_names
            ):
                return True
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and (
                    node.func.attr in _RAW_REQUEST_BODY_METHODS
                    and _is_request_value(node.func.value, request_names)
                ):
                    framework_body_read = (
                        allow_framework_request_presence
                        and function.name == "get_framework_request_presence"
                        and _is_framework_presence_probe(node.func, parents)
                    )
                    if not framework_body_read:
                        return True
                if isinstance(node.func, ast.Name) and node.func.id in bound_readers:
                    return True
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2
                    and _is_request_value(node.args[0], request_names)
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value in _RAW_REQUEST_PARAMETER_SOURCES
                ):
                    return True
                passes_request = any(
                    _is_request_value(argument, request_names) for argument in node.args
                ) or any(
                    _is_request_value(keyword.value, request_names) for keyword in node.keywords
                )
                middleware_handoff = (
                    allow_middleware_handoff
                    and function.name == "assign_request_id"
                    and "call_next" not in rebound_names
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "call_next"
                    and node.func.id in function_parameter_names
                )
                if passes_request and not middleware_handoff:
                    return True
            if (
                isinstance(node, ast.Attribute)
                and node.attr in _RAW_REQUEST_PARAMETER_SOURCES
                and _is_request_value(node.value, request_names)
            ):
                if (
                    allow_framework_request_presence
                    and function.name == "get_framework_request_presence"
                    and _is_framework_presence_probe(node, parents)
                ):
                    continue
                if (
                    allow_identity_session_cookie
                    and node.attr == "cookies"
                    and _is_approved_identity_cookie_access(node, parents, settings_parameter_names)
                ):
                    continue
                return True
    return False


def test_raw_request_body_detector_rejects_unmodeled_fastapi_input_fixture() -> None:
    tree = ast.parse(
        """
from fastapi import Request

async def unsafe_endpoint(request: Request) -> object:
    return await request.json()
"""
    )

    assert _uses_unmodeled_raw_request_body(tree)


@pytest.mark.parametrize(
    "source",
    [
        """
from fastapi import Request

async def unsafe_endpoint(request: Request) -> object:
    req = request
    return await req.json()
""",
        """
from fastapi import Request

async def unsafe_endpoint(request: Request) -> object:
    reader = request.json
    return await reader()
""",
        """
from fastapi import Request

async def parse(req) -> object:
    return await req.json()

async def unsafe_endpoint(request: Request) -> object:
    return await parse(request)
""",
        """
from fastapi import Request

HttpRequest = Request

async def unsafe_endpoint(request: "HttpRequest") -> object:
    return await request.json()
""",
    ],
)
def test_raw_request_detector_follows_simple_alias_and_helper_flows(source: str) -> None:
    assert _uses_unmodeled_raw_request_body(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        """
from fastapi import Request

type HttpRequest = Request

async def unsafe_endpoint(request: HttpRequest) -> object:
    return await request.json()
""",
        """
from fastapi import Request

async def unsafe_endpoint(request: Request) -> object:
    (req,) = (request,)
    return await req.json()
""",
        """
from fastapi import Request

async def unsafe_endpoint(request: Request, holder: object) -> object:
    holder.req = request
    return await holder.req.json()
""",
        """
from fastapi import Request

async def unsafe_endpoint(request: Request) -> object:
    holder = [request]
    return await holder[0].json()
""",
    ],
)
def test_raw_request_detector_rejects_pep695_and_non_name_storage(source: str) -> None:
    assert _uses_unmodeled_raw_request_body(ast.parse(source))


@pytest.mark.parametrize(
    "statement",
    [
        "req = (request,)[0]",
        "consumer(*[request])",
        'consumer(**{"req": request})',
        "holder = factory([request])",
        "await request._receive()",
        "cached = request._body",
        'reader = request.__getattribute__("json")',
        "return request if condition else None",
    ],
)
def test_raw_request_detector_fails_closed_for_other_request_expressions(statement: str) -> None:
    tree = ast.parse(
        f"""
from fastapi import Request

async def unsafe_endpoint(request: Request, consumer, factory, condition) -> object:
    {statement}
    return None
"""
    )

    assert _uses_unmodeled_raw_request_body(tree)


def test_raw_request_detector_resolves_quoted_request_alias() -> None:
    tree = ast.parse(
        """
from fastapi import Request

type HttpRequest = "Request"

async def unsafe_endpoint(request: HttpRequest) -> object:
    return await request.json()
"""
    )

    assert _uses_unmodeled_raw_request_body(tree)


@pytest.mark.parametrize(
    "source",
    [
        """
from fastapi import Request

async def unsafe_endpoint(request: Request) -> object:
    reader = lambda: request.json()
    return await reader()
""",
        """
from fastapi import Request

async def unsafe_endpoint(request: Request, condition: bool) -> object:
    if condition:
        async def reader() -> object:
            return await request.json()
        return await reader()
    return None
""",
        """
from fastapi import Request

async def parse(*args) -> object:
    return None

async def unsafe_endpoint(request: Request) -> object:
    return await parse(request)
""",
        """
from fastapi import Request

async def parse(**kwargs) -> object:
    return None

async def unsafe_endpoint(request: Request) -> object:
    return await parse(request=request)
""",
        """
from fastapi import Request
from unsafe import parse

def unrelated() -> None:
    def parse(value) -> None:
        return None

async def unsafe_endpoint(request: Request) -> object:
    return await parse(request)
""",
    ],
)
def test_raw_request_detector_rejects_closure_and_ambiguous_helper_scopes(source: str) -> None:
    assert _uses_unmodeled_raw_request_body(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        """
from typing import Annotated as A
from fastapi import Request

async def unsafe_endpoint(request: A[Request, "marker"]) -> object:
    return await request.json()
""",
        """
import typing as t
from fastapi import Request

async def unsafe_endpoint(request: t.Annotated[Request, "marker"]) -> object:
    return await request.json()
""",
        """
from typing_extensions import Annotated as ExtendedAnnotated
from fastapi import Request

async def unsafe_endpoint(request: ExtendedAnnotated[Request, "marker"]) -> object:
    return await request.json()
""",
        """
from typing import Annotated
from fastapi import Request

type HttpRequest = Annotated[Request, "marker"]

async def unsafe_endpoint(request: HttpRequest) -> object:
    return await request.json()
""",
    ],
)
def test_raw_request_detector_resolves_supported_wrapper_aliases(source: str) -> None:
    assert _uses_unmodeled_raw_request_body(ast.parse(source))


def test_raw_request_detector_rejects_nested_class_scope() -> None:
    tree = ast.parse(
        """
from fastapi import Request

async def unsafe_endpoint(request: Request) -> object:
    class Leak:
        headers = request.headers
    return Leak.headers
"""
    )

    assert _uses_unmodeled_raw_request_body(tree)


def test_raw_request_detector_rejects_request_subclasses() -> None:
    tree = ast.parse(
        """
from fastapi import Request

class CampusRequest(Request):
    pass

async def unsafe_endpoint(request: CampusRequest) -> object:
    return request.query_params
"""
    )

    assert _uses_unmodeled_raw_request_body(tree)


def test_raw_request_import_gate_rejects_reexported_request_symbol() -> None:
    tree = ast.parse("from app.core.request_id import Request")

    assert _imports_unapproved_raw_request_type(tree)


def test_raw_request_import_gate_rejects_reexport_even_for_approved_consumer() -> None:
    tree = ast.parse("from app.core.request_id import Request as RawRequest")

    assert _imports_unapproved_raw_request_type(tree, allow_direct_framework_connection=True)


@pytest.mark.parametrize(
    "source",
    [
        "from app.core import request_id",
        "import app.core.request_id",
        "from starlette import requests",
        "from app.core.request_id import *",
        "from fastapi import *",
    ],
)
def test_raw_request_import_gate_rejects_module_form_access(source: str) -> None:
    assert _imports_unapproved_raw_request_type(ast.parse(source))


def test_raw_request_detector_covers_http_connection_injection() -> None:
    tree = ast.parse(
        """
from starlette.requests import HTTPConnection

def unsafe_endpoint(connection: HTTPConnection) -> object:
    return connection.headers["X-School-ID"]
"""
    )

    assert _uses_unmodeled_raw_request_body(tree)


@pytest.mark.parametrize(
    "alias_statement",
    [
        "A = Annotated",
        "api = fastapi",
    ],
)
def test_raw_request_detector_rejects_secondary_type_aliases(alias_statement: str) -> None:
    tree = ast.parse(
        f"""
import fastapi
from typing import Annotated

{alias_statement}
"""
    )

    assert _uses_unmodeled_raw_request_body(tree)


def test_raw_request_detector_does_not_confuse_httpx_request() -> None:
    tree = ast.parse(
        """
from httpx import Request

def safe_outbound_adapter(request: Request) -> object:
    return request.headers
"""
    )

    assert not _uses_unmodeled_raw_request_body(tree)


def test_raw_request_detector_rejects_forged_call_next_name() -> None:
    tree = ast.parse(
        """
from fastapi import Request
from unsafe import parse as call_next

async def unsafe_endpoint(request: Request) -> object:
    return await call_next(request)
"""
    )

    assert _uses_unmodeled_raw_request_body(tree, allow_middleware_handoff=True)


def test_identity_cookie_allowlist_rejects_forged_settings_owner() -> None:
    tree = ast.parse(
        """
from fastapi import Request

async def unsafe_endpoint(request: Request, payload: object) -> object:
    return request.cookies.get(payload.identity_session_cookie_name)
"""
    )

    assert _uses_unmodeled_raw_request_body(tree, allow_identity_session_cookie=True)


def test_middleware_allowlist_rejects_rebound_call_next_parameter() -> None:
    tree = ast.parse(
        """
from fastapi import Request

async def assign_request_id(request: Request, call_next, unsafe_parser) -> object:
    call_next = unsafe_parser
    return await call_next(request)
"""
    )

    assert _uses_unmodeled_raw_request_body(tree, allow_middleware_handoff=True)


def test_identity_cookie_allowlist_rejects_rebound_settings_parameter() -> None:
    tree = ast.parse(
        """
from fastapi import Request
from app.core.settings import Settings

async def unsafe_endpoint(request: Request, settings: Settings, payload: object) -> object:
    settings = payload
    return request.cookies.get(settings.identity_session_cookie_name)
"""
    )

    assert _uses_unmodeled_raw_request_body(tree, allow_identity_session_cookie=True)


@pytest.mark.parametrize(
    "binding",
    [
        "from unsafe import parse as call_next",
        "def call_next(value): return None",
        "class call_next: pass",
    ],
)
def test_middleware_allowlist_rejects_scope_bindings(binding: str) -> None:
    tree = ast.parse(
        f"""
from fastapi import Request

async def assign_request_id(request: Request, call_next) -> object:
    {binding}
    return await call_next(request)
"""
    )

    assert _uses_unmodeled_raw_request_body(tree, allow_middleware_handoff=True)


@pytest.mark.parametrize(
    "binding",
    [
        "import payload as settings",
        "class settings: identity_session_cookie_name = 'school_id'",
    ],
)
def test_identity_cookie_allowlist_rejects_scope_bindings(binding: str) -> None:
    tree = ast.parse(
        f"""
from fastapi import Request
from app.core.settings import Settings

async def unsafe_endpoint(request: Request, settings: Settings) -> object:
    {binding}
    return request.cookies.get(settings.identity_session_cookie_name)
"""
    )

    assert _uses_unmodeled_raw_request_body(tree, allow_identity_session_cookie=True)


def test_identity_cookie_allowlist_requires_exact_settings_annotation() -> None:
    tree = ast.parse(
        """
from typing import Callable
from fastapi import Request
from app.core.settings import Settings

async def unsafe_endpoint(
    request: Request,
    payload: Callable[[Settings], object],
) -> object:
    return request.cookies.get(payload.identity_session_cookie_name)
"""
    )

    assert _uses_unmodeled_raw_request_body(tree, allow_identity_session_cookie=True)


@pytest.mark.parametrize(
    "module_prefix",
    [
        (
            "from app.core.settings import Settings\n"
            "from app.input import CookieSelector as Settings"
        ),
        (
            "from app.input import CookieSelector as Settings\n"
            "def unrelated():\n    from app.core.settings import Settings"
        ),
    ],
)
def test_identity_cookie_allowlist_requires_unique_module_settings_binding(
    module_prefix: str,
) -> None:
    tree = ast.parse(
        f"""
from fastapi import Request
{module_prefix}

async def unsafe_endpoint(request: Request, settings: Settings) -> object:
    return request.cookies.get(settings.identity_session_cookie_name)
"""
    )

    assert _uses_unmodeled_raw_request_body(tree, allow_identity_session_cookie=True)


def test_identity_cookie_allowlist_rejects_compound_module_settings_rebind() -> None:
    tree = ast.parse(
        """
from fastapi import Request
from app.core.settings import Settings
from app.input import CookieSelector

if True:
    Settings = CookieSelector

async def unsafe_endpoint(request: Request, settings: Settings) -> object:
    return request.cookies.get(settings.identity_session_cookie_name)
"""
    )

    assert _uses_unmodeled_raw_request_body(tree, allow_identity_session_cookie=True)


@pytest.mark.parametrize(
    "helper_definition",
    [
        "@replace_with_unsafe\ndef get_server_request_id(request: Request): return None",
        "def get_server_request_id(request: Request): return None",
    ],
)
def test_request_helper_allowlist_rejects_decorated_or_outer_shadowed_binding(
    helper_definition: str,
) -> None:
    outer_prefix = (
        "def register(get_server_request_id):\n" if not helper_definition.startswith("@") else ""
    )
    endpoint_indent = "    " if outer_prefix else ""
    tree = ast.parse(
        f"""
from fastapi import Request
{helper_definition}

{outer_prefix}{endpoint_indent}async def unsafe_endpoint(request: Request) -> object:
{endpoint_indent}    return get_server_request_id(request)
"""
    )

    assert _uses_unmodeled_raw_request_body(tree)


@pytest.mark.parametrize(
    "expression",
    [
        'request.query_params.get("school_id")',
        'request.headers.get("X-School-ID")',
        'request.cookies.get("school_id")',
        'request.path_params["school_id"]',
    ],
)
def test_raw_request_detector_rejects_unmodeled_parameter_sources(expression: str) -> None:
    tree = ast.parse(
        f"""
from fastapi import Request

async def unsafe_endpoint(request: Request) -> object:
    return {expression}
"""
    )

    assert _uses_unmodeled_raw_request_body(tree)


_FRAMEWORK_REQUEST_ADAPTER_FUNCTIONS = (
    "api_error_handler",
    "get_framework_request_context",
    "get_framework_request_presence",
    "request_validation_error_handler",
)


def _raw_request_parameter_function_names(tree: ast.AST) -> tuple[str, ...]:
    typing_wrappers = _typing_wrapper_names(tree)
    request_module_names = _request_module_names(tree)
    request_type_names = _request_type_names(tree, request_module_names, typing_wrappers)
    return tuple(
        sorted(
            function.name
            for function in ast.walk(tree)
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                _annotation_mentions_request(
                    argument.annotation,
                    request_type_names,
                    request_module_names,
                    typing_wrappers,
                )
                for argument in _function_arguments(function)
            )
        )
    )


def _raw_request_contract_violations(app_root: Path) -> list[Path]:
    approved_middleware_handoff = app_root / "core" / "request_id.py"
    approved_framework_request_adapter = app_root / "interfaces" / "http" / "framework_request.py"
    approved_identity_cookie_consumers = {
        app_root / "platform" / "identity" / "context.py",
        app_root / "platform" / "identity" / "router.py",
    }
    approved_raw_request_consumers = approved_identity_cookie_consumers | {
        approved_middleware_handoff,
        approved_framework_request_adapter,
        app_root / "platform" / "authorization" / "router.py",
    }
    violations: list[Path] = []

    for source_path in app_root.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        if (
            source_path == approved_framework_request_adapter
            and _raw_request_parameter_function_names(tree) != _FRAMEWORK_REQUEST_ADAPTER_FUNCTIONS
        ):
            violations.append(source_path.relative_to(app_root))
            continue
        if _imports_unapproved_raw_request_type(
            tree,
            allow_direct_framework_connection=source_path in approved_raw_request_consumers,
        ):
            violations.append(source_path.relative_to(app_root))
            continue
        if _uses_unmodeled_raw_request_body(
            tree,
            allow_identity_session_cookie=source_path in approved_identity_cookie_consumers,
            allow_framework_request_presence=(source_path == approved_framework_request_adapter),
            allow_middleware_handoff=source_path == approved_middleware_handoff,
        ):
            violations.append(source_path.relative_to(app_root))

    return violations


def _framework_request_adapter_fixture(extra_source: str = "") -> str:
    return (
        "from fastapi import Request\n"
        "def get_framework_request_context(request: Request):\n"
        "    return request.state.request_id\n"
        "async def get_framework_request_presence(request: Request):\n"
        "    return bool(request.query_params), bool(await request.body())\n"
        "async def api_error_handler(request: Request, exc: Exception):\n"
        "    return request.state.request_id\n"
        "async def request_validation_error_handler(request: Request, exc: Exception):\n"
        "    return request.state.request_id\n"
        f"{extra_source}"
    )


def test_second_framework_request_consumer_is_rejected(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    adapter = app_root / "interfaces" / "http" / "framework_request.py"
    adapter.parent.mkdir(parents=True)
    adapter.write_text(
        _framework_request_adapter_fixture(),
        encoding="utf-8",
    )
    second_consumer = app_root / "interfaces" / "http" / "second_consumer.py"
    second_consumer.write_text(
        "from fastapi import Request\n"
        "def leak(request: Request):\n"
        "    return request.state.request_id\n",
        encoding="utf-8",
    )

    assert _raw_request_contract_violations(app_root) == [
        Path("interfaces/http/second_consumer.py")
    ]


def test_second_framework_request_consumer_in_adapter_file_is_rejected(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    adapter = app_root / "interfaces" / "http" / "framework_request.py"
    adapter.parent.mkdir(parents=True)
    adapter.write_text(
        _framework_request_adapter_fixture(
            "def second_consumer(request: Request):\n    return request.state.request_id\n"
        ),
        encoding="utf-8",
    )

    assert _raw_request_contract_violations(app_root) == [
        Path("interfaces/http/framework_request.py")
    ]


@pytest.mark.parametrize(
    "unsafe_return",
    (
        "payload = await request.body()\n    return payload",
        "return dict(request.query_params)",
    ),
)
def test_framework_request_presence_cannot_export_raw_input(unsafe_return: str) -> None:
    tree = ast.parse(
        "from fastapi import Request\n"
        "async def get_framework_request_presence(request: Request):\n"
        f"    {unsafe_return}\n"
    )

    assert _uses_unmodeled_raw_request_body(
        tree,
        allow_framework_request_presence=True,
    )


def test_production_routes_do_not_bypass_modeled_request_contracts() -> None:
    app_root = Path(__file__).resolve().parents[2] / "app"
    violations = _raw_request_contract_violations(app_root)

    assert violations == []


def test_actor_contexts_do_not_merge_staff_and_guardian_authority() -> None:
    staff = _issue_staff_context()
    guardian = _issue_guardian_context()

    assert not hasattr(staff, "guardian_relationship_id")
    assert not hasattr(guardian, "assignment_id")
    actors: tuple[ActorContext, ActorContext] = (staff, guardian)
    assert {type(actor) for actor in actors} == {StaffActorContext, GuardianActorContext}


def test_request_and_command_contexts_keep_one_idempotency_source() -> None:
    actor = _issue_staff_context()
    request = _issue_request_context(actor)
    command = _issue_command_context(actor)

    assert not hasattr(request, "idempotency_key")
    assert command.idempotency_key == "repair-create-20260805-0001"
    with pytest.raises(FrozenInstanceError):
        _attempt_attribute_mutation(command, "idempotency_key", "changed")


def test_execution_context_issuers_reject_non_exact_actor_types() -> None:
    forged_actor = cast(ActorContext, object())

    with pytest.raises(TypeError, match="必须是签发器生成的精确类型"):
        _issue_request_context(forged_actor)
    with pytest.raises(TypeError, match="必须是签发器生成的精确类型"):
        _issue_command_context(forged_actor)


def test_owner_contracts_only_expose_narrow_reader_or_resolver_methods() -> None:
    expected_methods = {
        CurrentSchoolReader: {"get_current_school"},
        PersonSnapshotResolver: {"resolve_person_snapshot"},
        CurrentAssignmentReader: {"get_current_assignment"},
        AssignmentSnapshotResolver: {"resolve_assignment_snapshot"},
        ResponsibilityGroupSnapshotResolver: {"resolve_group_snapshot"},
        CurrentLocationReader: {"get_current_location"},
        LocationSnapshotResolver: {"resolve_location_snapshot"},
        CurrentStudentRelationReader: {"get_current_student_relation"},
        StudentRelationSnapshotResolver: {"resolve_student_relation_snapshot"},
        WorkCalendarReader: {"get_published_snapshot"},
    }

    for contract, methods in expected_methods.items():
        public_methods = {
            name
            for name, value in vars(contract).items()
            if callable(value) and not name.startswith("_")
        }
        assert public_methods == methods

    assert not any("activate" in name for name in vars(WorkCalendarReader))
    assert not any("current_name" in name for name in vars(StudentRelationSnapshotResolver))
    assert not any("responsib" in name for name in vars(CurrentLocationReader))


def test_calendar_not_published_is_a_typed_frozen_result() -> None:
    missing = CalendarNotPublished(school_id=SCHOOL_ID, as_of=NOW)

    assert missing.school_id == SCHOOL_ID
    with pytest.raises(ValidationError):
        _attempt_attribute_mutation(missing, "as_of", LATER)


def test_calendar_not_published_rejects_naive_time_and_unknown_fields() -> None:
    assert issubclass(CalendarNotPublished, BaseModel)

    with pytest.raises(ValidationError):
        CalendarNotPublished(
            school_id=SCHOOL_ID,
            as_of=datetime(2026, 8, 5, 8, 0),
        )
    with pytest.raises(ValidationError):
        CalendarNotPublished.model_validate(
            {"school_id": SCHOOL_ID, "as_of": NOW, "activated": False}
        )


def _as_object_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    untrusted_mapping = cast(dict[object, object], value)
    assert all(isinstance(key, str) for key in untrusted_mapping)
    return cast(dict[str, object], untrusted_mapping)


def _as_object_list(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast(list[object], value)


_ARBITRARY_MAP_KEY = "<arbitrary-map-key>"


def _resolve_local_reference(reference: str, document: dict[str, object]) -> object:
    assert reference.startswith("#/")
    node: object = document
    for raw_segment in reference.removeprefix("#/").split("/"):
        segment = raw_segment.replace("~1", "/").replace("~0", "~")
        node = _as_object_dict(node)[segment]
    return node


def _collect_schema_field_names(
    schema: object,
    document: dict[str, object],
    *,
    visited_references: frozenset[str] = frozenset(),
) -> set[str]:
    if isinstance(schema, bool):
        return {_ARBITRARY_MAP_KEY} if schema else set()
    schema_object = _as_object_dict(schema)
    names: set[str] = set()
    reference = schema_object.get("$ref")
    if isinstance(reference, str):
        if reference not in visited_references:
            resolved = _resolve_local_reference(reference, document)
            if isinstance(resolved, (dict, bool)):
                names.update(
                    _collect_schema_field_names(
                        cast(object, resolved),
                        document,
                        visited_references=visited_references | {reference},
                    )
                )

    properties = schema_object.get("properties")
    if properties is not None:
        for name, child in _as_object_dict(properties).items():
            names.add(name)
            if isinstance(child, (dict, bool)):
                names.update(
                    _collect_schema_field_names(
                        cast(object, child),
                        document,
                        visited_references=visited_references,
                    )
                )

    schema_type = schema_object.get("type")
    schema_types = (
        _as_object_list(cast(object, schema_type))
        if isinstance(schema_type, list)
        else [schema_type]
    )
    type_is_declared = isinstance(schema_type, (str, list))
    type_allows_object = "object" in schema_types
    type_allows_array = "array" in schema_types
    object_keywords = {
        "properties",
        "additionalProperties",
        "unevaluatedProperties",
        "patternProperties",
        "propertyNames",
        "required",
        "dependentSchemas",
        "dependentRequired",
        "minProperties",
        "maxProperties",
    }
    is_object_schema = type_allows_object or bool(object_keywords.intersection(schema_object))
    if (
        is_object_schema
        and "additionalProperties" not in schema_object
        and "unevaluatedProperties" not in schema_object
    ):
        names.add(_ARBITRARY_MAP_KEY)

    metadata_keywords = {
        "$anchor",
        "$comment",
        "$id",
        "$schema",
        "default",
        "deprecated",
        "description",
        "discriminator",
        "example",
        "examples",
        "externalDocs",
        "format",
        "readOnly",
        "title",
        "writeOnly",
        "xml",
    }
    structural_keywords = set(schema_object).difference(metadata_keywords)
    composition_keywords = {"$ref", "allOf", "anyOf", "oneOf"}
    has_empty_all_of = schema_object.get("allOf") == []
    is_reference_or_composition_shell = bool(structural_keywords) and structural_keywords <= (
        composition_keywords
    )
    if (
        not type_is_declared
        and not is_object_schema
        and not (is_reference_or_composition_shell and not has_empty_all_of)
    ):
        names.add(_ARBITRARY_MAP_KEY)

    if type_allows_array:
        items = schema_object.get("items")
        has_unclosed_default_tail = (
            "items" not in schema_object and "unevaluatedItems" not in schema_object
        )
        has_unclosed_tuple_tail = (
            isinstance(items, list)
            and "additionalItems" not in schema_object
            and "unevaluatedItems" not in schema_object
        )
        if has_unclosed_default_tail or has_unclosed_tuple_tail or items is True:
            names.add(_ARBITRARY_MAP_KEY)

    for child_key in (
        "items",
        "additionalProperties",
        "unevaluatedProperties",
        "unevaluatedItems",
        "additionalItems",
        "contains",
        "if",
        "then",
        "else",
        "propertyNames",
    ):
        child = schema_object.get(child_key)
        if child_key in {"additionalProperties", "unevaluatedProperties"} and (
            child is True or isinstance(child, dict)
        ):
            names.add(_ARBITRARY_MAP_KEY)
        if isinstance(child, (dict, bool)):
            names.update(
                _collect_schema_field_names(
                    cast(object, child),
                    document,
                    visited_references=visited_references,
                )
            )
        elif isinstance(child, list):
            for child_schema in _as_object_list(cast(object, child)):
                if isinstance(child_schema, (dict, bool)):
                    names.update(
                        _collect_schema_field_names(
                            cast(object, child_schema),
                            document,
                            visited_references=visited_references,
                        )
                    )

    if "not" in schema_object:
        names.add(_ARBITRARY_MAP_KEY)

    dependent_schemas = schema_object.get("dependentSchemas")
    if isinstance(dependent_schemas, dict):
        for trigger_name, dependent_schema in _as_object_dict(
            cast(object, dependent_schemas)
        ).items():
            names.add(trigger_name)
            if isinstance(dependent_schema, (dict, bool)):
                names.update(
                    _collect_schema_field_names(
                        cast(object, dependent_schema),
                        document,
                        visited_references=visited_references,
                    )
                )

    required = schema_object.get("required")
    if isinstance(required, list):
        for required_name in _as_object_list(cast(object, required)):
            if isinstance(required_name, str):
                names.add(required_name)

    dependent_required = schema_object.get("dependentRequired")
    if isinstance(dependent_required, dict):
        for trigger_name, raw_required_names in _as_object_dict(
            cast(object, dependent_required)
        ).items():
            names.add(trigger_name)
            if isinstance(raw_required_names, list):
                for required_name in _as_object_list(cast(object, raw_required_names)):
                    if isinstance(required_name, str):
                        names.add(required_name)

    pattern_properties = schema_object.get("patternProperties")
    if isinstance(pattern_properties, dict):
        names.add(_ARBITRARY_MAP_KEY)
        for pattern_schema in _as_object_dict(cast(object, pattern_properties)).values():
            if isinstance(pattern_schema, (dict, bool)):
                names.update(
                    _collect_schema_field_names(
                        cast(object, pattern_schema),
                        document,
                        visited_references=visited_references,
                    )
                )

    for union_key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        variants = schema_object.get(union_key)
        if variants is not None:
            for variant in _as_object_list(variants):
                if isinstance(variant, (dict, bool)):
                    names.update(
                        _collect_schema_field_names(
                            cast(object, variant),
                            document,
                            visited_references=visited_references,
                        )
                    )
    return names


def _resolve_openapi_object(
    value: dict[str, object],
    document: dict[str, object],
    *,
    visited_references: frozenset[str] = frozenset(),
) -> dict[str, object]:
    reference = value.get("$ref")
    if not isinstance(reference, str):
        return value
    siblings = {key: child for key, child in value.items() if key != "$ref"}
    if reference in visited_references:
        return siblings
    resolved = _as_object_dict(_resolve_local_reference(reference, document))
    return {
        **_resolve_openapi_object(
            resolved,
            document,
            visited_references=visited_references | {reference},
        ),
        **siblings,
    }


def _collect_media_schema_field_names(content: object, document: dict[str, object]) -> set[str]:
    names: set[str] = set()
    for raw_media in _as_object_dict(content).values():
        media = _as_object_dict(raw_media)
        schema = media.get("schema")
        if schema is None:
            names.add(_ARBITRARY_MAP_KEY)
        else:
            names.update(_collect_schema_field_names(schema, document))
    return names


def _collect_parameter_field_names(parameters: object, document: dict[str, object]) -> set[str]:
    names: set[str] = set()
    for raw_parameter in _as_object_list(parameters):
        parameter = _resolve_openapi_object(_as_object_dict(raw_parameter), document)
        name = parameter.get("name")
        if isinstance(name, str):
            names.add(name)
        schema = parameter.get("schema")
        if schema is not None:
            names.update(_collect_schema_field_names(schema, document))
        content = parameter.get("content")
        if content is not None:
            names.update(_collect_media_schema_field_names(content, document))
    return names


def _collect_openapi_request_field_names(document: dict[str, object]) -> set[str]:
    request_fields: set[str] = set()
    paths = _as_object_dict(document["paths"])
    operation_names = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}

    for raw_path_item in paths.values():
        path_item = _resolve_openapi_object(_as_object_dict(raw_path_item), document)
        path_parameters = path_item.get("parameters")
        if path_parameters is not None:
            request_fields.update(_collect_parameter_field_names(path_parameters, document))

        for operation_name in operation_names:
            raw_operation = path_item.get(operation_name)
            if raw_operation is None:
                continue
            operation = _as_object_dict(raw_operation)
            operation_parameters = operation.get("parameters")
            if operation_parameters is not None:
                request_fields.update(
                    _collect_parameter_field_names(operation_parameters, document)
                )
            request_body = operation.get("requestBody")
            if request_body is None:
                continue
            request_body_object = _resolve_openapi_object(_as_object_dict(request_body), document)
            content = request_body_object.get("content")
            if content is None:
                continue
            request_fields.update(_collect_media_schema_field_names(content, document))
    return request_fields


def test_openapi_request_graph_finds_parameters_arrays_maps_and_reference_cycles() -> None:
    document: dict[str, object] = {
        "paths": {"/probe/{assignment_id}": {"$ref": "#/components/pathItems/ProbeAlias"}},
        "components": {
            "pathItems": {
                "ProbeAlias": {"$ref": "#/components/pathItems/Probe"},
                "Probe": {
                    "parameters": [{"$ref": "#/components/parameters/AssignmentAlias"}],
                    "post": {
                        "parameters": [{"$ref": "#/components/parameters/SchoolAlias"}],
                        "requestBody": {"$ref": "#/components/requestBodies/ProbeAlias"},
                    },
                },
            },
            "parameters": {
                "AssignmentAlias": {"$ref": "#/components/parameters/Assignment"},
                "Assignment": {
                    "name": "assignment_id",
                    "in": "path",
                    "schema": {"type": "string"},
                },
                "SchoolAlias": {"$ref": "#/components/parameters/School"},
                "School": {
                    "name": "school_id",
                    "in": "header",
                    "schema": {"type": "string"},
                },
            },
            "requestBodies": {
                "ProbeAlias": {"$ref": "#/components/requestBodies/Probe"},
                "Probe": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "array",
                                "items": {
                                    "allOf": [
                                        {
                                            "type": "object",
                                            "properties": {
                                                "guardian_relationship_id": {"type": "string"}
                                            },
                                        },
                                        {
                                            "anyOf": [
                                                {
                                                    "type": "object",
                                                    "properties": {
                                                        "nested": {
                                                            "type": "object",
                                                            "additionalProperties": {
                                                                "$ref": (
                                                                    "#/components/schemas/"
                                                                    "CyclicInput"
                                                                )
                                                            },
                                                        },
                                                        "freeform": {
                                                            "type": "object",
                                                            "additionalProperties": True,
                                                        },
                                                    },
                                                }
                                            ]
                                        },
                                        {
                                            "oneOf": [
                                                {
                                                    "type": "object",
                                                    "properties": {
                                                        "composition_marker": {"type": "string"}
                                                    },
                                                }
                                            ]
                                        },
                                    ]
                                },
                            }
                        }
                    }
                },
            },
            "schemas": {
                "CyclicInput": {
                    "type": "object",
                    "properties": {
                        "permission_granted": {"type": "boolean"},
                        "parent": {
                            "$ref": "#/components/schemas/CyclicInput",
                            "properties": {"is_authorized": {"type": "boolean"}},
                        },
                    },
                }
            },
        },
    }

    assert _collect_openapi_request_field_names(document) == {
        "assignment_id",
        "school_id",
        "guardian_relationship_id",
        "nested",
        "permission_granted",
        "parent",
        "composition_marker",
        "is_authorized",
        "freeform",
        _ARBITRARY_MAP_KEY,
    }


def test_openapi_request_graph_fails_closed_for_negation_and_dependent_applicators() -> None:
    document: dict[str, object] = {
        "paths": {
            "/probe": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "allOf": [
                                        {"not": False},
                                        {
                                            "type": "object",
                                            "properties": {"kind": {"type": "string"}},
                                            "dependentSchemas": {
                                                "kind": {
                                                    "type": "object",
                                                    "properties": {
                                                        "assignment_version": {"type": "integer"}
                                                    },
                                                    "unevaluatedProperties": False,
                                                }
                                            },
                                            "unevaluatedProperties": False,
                                        },
                                        {
                                            "type": "array",
                                            "unevaluatedItems": {
                                                "type": "object",
                                                "properties": {"server_proof": {"type": "string"}},
                                                "additionalProperties": False,
                                            },
                                        },
                                    ]
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    assert _collect_openapi_request_field_names(document) == {
        _ARBITRARY_MAP_KEY,
        "assignment_version",
        "kind",
        "server_proof",
    }


@pytest.mark.parametrize(
    "schema",
    [
        {"title": "payload"},
        {"if": False},
        {"allOf": []},
        {"const": {"school_id": "forged"}},
        {"type": "array"},
        {"type": "array", "contains": {"type": "string"}},
        {"type": "array", "prefixItems": [{"type": "string"}]},
    ],
)
def test_schema_collector_fails_closed_for_unbounded_objects_and_arrays(
    schema: dict[str, object],
) -> None:
    assert _ARBITRARY_MAP_KEY in _collect_schema_field_names(schema, {})


def test_schema_collector_keeps_closed_primitive_union_usable() -> None:
    schema: dict[str, object] = {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "title": "optional-text",
    }

    assert _ARBITRARY_MAP_KEY not in _collect_schema_field_names(schema, {})


def test_openapi_request_graph_fails_closed_when_media_schema_is_missing() -> None:
    document: dict[str, object] = {
        "paths": {"/probe": {"post": {"requestBody": {"content": {"application/json": {}}}}}}
    }

    assert _collect_openapi_request_field_names(document) == {_ARBITRARY_MAP_KEY}


def test_http_gate_preserves_unbounded_shape_marker_until_final_decision() -> None:
    document: dict[str, object] = {
        "paths": {
            "/probe": {
                "post": {
                    "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}}
                }
            }
        }
    }
    request_fields = _collect_openapi_request_field_names(document)

    assert _find_forbidden_http_request_fields(request_fields) == {_ARBITRARY_MAP_KEY}


def _canonical_request_field_snake_forms(exposed_name: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", exposed_name).strip()
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", normalized)
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", separated)
    separated = re.sub(r"[^0-9A-Za-z]+", "_", separated)
    canonical = re.sub(r"_+", "_", separated).strip("_").casefold()
    forms = {canonical}
    if canonical.startswith("x_"):
        forms.add(canonical.removeprefix("x_"))
    return {form for form in forms if form}


def _canonical_request_field_candidates(exposed_name: str) -> set[str]:
    snake_forms = _canonical_request_field_snake_forms(exposed_name)
    candidates = snake_forms | {form.replace("_", "") for form in snake_forms}
    flattened = {
        candidate.removeprefix("x") for candidate in candidates if candidate.startswith("x")
    }
    candidates.update(flattened)
    return {candidate for candidate in candidates if candidate}


@pytest.mark.parametrize(
    ("exposed_name", "canonical_name"),
    [
        ("schoolId", "school_id"),
        ("school-id", "school_id"),
        ("X-School-Id", "school_id"),
        ("XSchoolId", "school_id"),
        ("XSchool-Id", "school_id"),
        ("schoolid", "school_id"),
        ("ｓｃｈｏｏｌＩｄ", "school_id"),
        ("canManage", "can_manage"),
        ("Has-Permission", "has_permission"),
    ],
)
def test_http_field_name_canonicalization_closes_alias_and_header_variants(
    exposed_name: str, canonical_name: str
) -> None:
    assert not _canonical_request_field_candidates(exposed_name).isdisjoint(
        _canonical_request_field_candidates(canonical_name)
    )


@pytest.mark.parametrize("field_name", ["cancellation_reason", "candy_count"])
def test_http_authority_prefix_gate_keeps_word_boundaries(field_name: str) -> None:
    assert _find_forbidden_http_request_fields({field_name}) == set()


_FORBIDDEN_HTTP_REQUEST_FIELDS = {
    _ARBITRARY_MAP_KEY,
    "account_user_id",
    "actor",
    "actor_context",
    "actor_assignment_id",
    "actor_assignment_version",
    "actor_context_type",
    "actor_guardian_relationship_id",
    "actor_guardian_relationship_version",
    "actor_person_id",
    "assignment_id",
    "assignment_version",
    "acting_assignment_id",
    "authorization_version",
    "confirmation_result",
    "confirmed",
    "context_kind",
    "context_purpose",
    "guardian_relationship_version",
    "guardian_relationship_id",
    "idempotency_hash",
    "idempotency_key_hash",
    "idempotency_scope",
    "is_authorized",
    "issuance_provenance",
    "person_id",
    "proof_expires_at",
    "relationship_version",
    "request_id",
    "route_decision",
    "routing_decision",
    "rule_facts",
    "school_id",
    "security_version",
    "server_proof",
    "service_identity",
    "session_nonce_hash",
}
_FORBIDDEN_HTTP_REQUEST_PREFIXES = (
    "can_",
    "has_permission",
    "permission_granted",
)


def _input_alias_strings(alias: str | AliasChoices | AliasPath | None) -> set[str]:
    if isinstance(alias, str):
        return {alias}
    if isinstance(alias, AliasChoices):
        return {
            alias_name for choice in alias.choices for alias_name in _input_alias_strings(choice)
        }
    if isinstance(alias, AliasPath):
        return {segment for segment in alias.path if isinstance(segment, str)}
    return set()


def _model_types_in_core_schema(
    schema: object, seen_nodes: set[int] | None = None
) -> set[type[BaseModel]]:
    seen: set[int] = set() if seen_nodes is None else seen_nodes
    if id(schema) in seen:
        return set()
    seen.add(id(schema))

    model_types: set[type[BaseModel]] = set()
    if isinstance(schema, dict):
        schema_mapping = cast(dict[object, object], schema)
        model_class = schema_mapping.get("cls")
        if isinstance(model_class, type) and issubclass(model_class, BaseModel):
            model_types.add(model_class)
        for nested_schema in schema_mapping.values():
            model_types.update(_model_types_in_core_schema(nested_schema, seen))
    elif isinstance(schema, (list, tuple)):
        schema_sequence = cast(list[object] | tuple[object, ...], schema)
        for nested_schema in schema_sequence:
            model_types.update(_model_types_in_core_schema(nested_schema, seen))
    return model_types


def _model_types_in_annotation(
    annotation: object, seen_aliases: set[int] | None = None
) -> set[type[BaseModel]]:
    seen: set[int] = set() if seen_aliases is None else seen_aliases
    model_types: set[type[BaseModel]] = set()
    if isinstance(annotation, TypeAliasType):
        if id(annotation) in seen:
            return model_types
        seen.add(id(annotation))
        model_types.update(_model_types_in_core_schema(TypeAdapter(annotation).core_schema))
        model_types.update(_model_types_in_annotation(annotation.__value__, seen))
        return model_types
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        model_types.add(annotation)
    for argument in get_args(annotation):
        model_types.update(_model_types_in_annotation(argument, seen))
    return model_types


def _collect_model_input_field_names(
    model_type: type[BaseModel], seen_models: set[type[BaseModel]] | None = None
) -> set[str]:
    seen: set[type[BaseModel]] = set() if seen_models is None else seen_models
    if model_type in seen:
        return set()
    seen.add(model_type)

    names: set[str] = set()
    for field_name, field_info in model_type.model_fields.items():
        names.add(field_name)
        names.update(_input_alias_strings(field_info.alias))
        names.update(_input_alias_strings(field_info.validation_alias))
        for nested_model in _model_types_in_annotation(field_info.annotation):
            names.update(_collect_model_input_field_names(nested_model, seen))
    return names


def _iter_dependants(root: Dependant) -> list[Dependant]:
    dependants: list[Dependant] = []
    pending = [root]
    seen: set[int] = set()
    while pending:
        dependant = pending.pop()
        if id(dependant) in seen:
            continue
        seen.add(id(dependant))
        dependants.append(dependant)
        pending.extend(dependant.dependencies)
    return dependants


def _collect_effective_route_dependants(routes: list[BaseRoute]) -> list[Dependant]:
    dependants: list[Dependant] = []
    for route_context in iter_route_contexts(routes):
        effective_dependant = getattr(route_context, "dependant", None)
        if isinstance(effective_dependant, Dependant):
            dependants.append(effective_dependant)
        if isinstance(route_context.route, (Mount, Host)):
            dependants.extend(_collect_effective_route_dependants(route_context.route.routes))
    return dependants


def _collect_registered_request_input_field_names(target_app: FastAPI = app) -> set[str]:
    names: set[str] = set()
    for effective_dependant in _collect_effective_route_dependants(target_app.routes):
        for dependant in _iter_dependants(effective_dependant):
            fields = (
                *dependant.path_params,
                *dependant.query_params,
                *dependant.header_params,
                *dependant.cookie_params,
                *dependant.body_params,
            )
            for field in fields:
                names.add(field.name)
                names.add(field.alias)
                for model_type in _model_types_in_annotation(field.field_info.annotation):
                    names.update(_collect_model_input_field_names(model_type))
    return names


def _find_forbidden_http_request_fields(request_fields: set[str]) -> set[str]:
    violations: set[str] = set()
    if _ARBITRARY_MAP_KEY in request_fields:
        violations.add(_ARBITRARY_MAP_KEY)

    forbidden_candidates = {
        forbidden_field: _canonical_request_field_candidates(forbidden_field)
        for forbidden_field in _FORBIDDEN_HTTP_REQUEST_FIELDS - {_ARBITRARY_MAP_KEY}
    }
    for request_field in request_fields - {_ARBITRARY_MAP_KEY}:
        request_candidates = _canonical_request_field_candidates(request_field)
        for forbidden_field, candidates in forbidden_candidates.items():
            if not request_candidates.isdisjoint(candidates):
                violations.add(forbidden_field)
        snake_forms = _canonical_request_field_snake_forms(request_field)
        if any(
            (prefix.endswith("_") and snake_form.startswith(prefix))
            or (
                not prefix.endswith("_")
                and (snake_form == prefix or snake_form.startswith(f"{prefix}_"))
            )
            for snake_form in snake_forms
            for prefix in _FORBIDDEN_HTTP_REQUEST_PREFIXES
        ):
            violations.add(request_field)
    return violations


def test_http_forbidden_fields_cover_trusted_execution_context_contract() -> None:
    trusted_context_fields = {
        "account_user_id",
        "actor",
        "actor_context",
        "assignment_id",
        "assignment_version",
        "authorization_version",
        "guardian_relationship_id",
        "issuance_provenance",
        "person_id",
        "relationship_version",
        "request_id",
        "school_id",
    }
    database_envelope_fields = {
        "account_user_id",
        "assignment_id",
        "assignment_version",
        "authorization_version",
        "context_kind",
        "context_purpose",
        "guardian_relationship_id",
        "guardian_relationship_version",
        "idempotency_key_hash",
        "person_id",
        "proof_expires_at",
        "request_id",
        "school_id",
        "server_proof",
        "session_nonce_hash",
    }

    assert trusted_context_fields <= _FORBIDDEN_HTTP_REQUEST_FIELDS
    assert database_envelope_fields <= _FORBIDDEN_HTTP_REQUEST_FIELDS


def test_server_model_field_names_are_checked_even_when_alias_looks_harmless() -> None:
    class AliasBackedRequest(BaseModel):
        school_id: str = Field(alias="displayName")

    names = _collect_model_input_field_names(AliasBackedRequest)

    assert {"school_id", "displayName"} <= names
    assert _find_forbidden_http_request_fields(names) == {"school_id"}


def test_model_field_collector_unwraps_pep695_aliases() -> None:
    assert _AliasBackedAuthorityRequest in _model_types_in_annotation(_AliasBackedAuthorityPayload)
    assert "school_id" in _collect_model_input_field_names(_AliasWrappedAuthorityRequest)


def test_model_field_collector_resolves_quoted_pep695_aliases() -> None:
    assert _AliasBackedAuthorityRequest in _model_types_in_annotation(
        _ForwardAliasBackedAuthorityPayload
    )
    assert "school_id" in _collect_model_input_field_names(_ForwardAliasWrappedAuthorityRequest)


def test_registered_request_fields_include_router_context_dependencies() -> None:
    def read_hidden_authority(
        school_id: Annotated[str, Header(alias="display-name")],
    ) -> None:
        del school_id

    router = APIRouter()

    @router.get("/probe")
    def _probe() -> dict[str, bool]:
        return {"ok": True}

    assert callable(_probe)
    probe_app = FastAPI()
    probe_app.include_router(router, dependencies=[Depends(read_hidden_authority)])

    assert "school_id" in _collect_registered_request_input_field_names(probe_app)


def test_registered_request_fields_include_mounted_child_applications() -> None:
    child = FastAPI()

    @child.get("/probe")
    def _probe(
        school_id: Annotated[str, Header(alias="display-name")],
    ) -> dict[str, bool]:
        del school_id
        return {"ok": True}

    assert callable(_probe)
    parent = FastAPI()
    parent.mount("/child", child)

    assert "school_id" in _collect_registered_request_input_field_names(parent)


def test_http_request_graph_has_no_trusted_execution_or_decision_fields() -> None:
    document = cast(dict[str, object], app.openapi())
    request_fields = _collect_openapi_request_field_names(document)
    request_fields.update(_collect_registered_request_input_field_names())

    assert _find_forbidden_http_request_fields(request_fields) == set()
