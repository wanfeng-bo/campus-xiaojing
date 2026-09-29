from __future__ import annotations

from collections.abc import Callable
from dataclasses import InitVar, dataclass
from typing import Annotated, NewType, Protocol, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

SchoolId = NewType("SchoolId", UUID)
PersonId = NewType("PersonId", UUID)
OrganizationUnitId = NewType("OrganizationUnitId", UUID)
AssignmentId = NewType("AssignmentId", UUID)
ResponsibilityTypeId = NewType("ResponsibilityTypeId", UUID)
ResponsibilityGroupId = NewType("ResponsibilityGroupId", UUID)
LocationId = NewType("LocationId", UUID)
StudentId = NewType("StudentId", UUID)
EnrollmentId = NewType("EnrollmentId", UUID)
AdministrativeClassId = NewType("AdministrativeClassId", UUID)
GuardianRelationshipId = NewType("GuardianRelationshipId", UUID)
CalendarVersionId = NewType("CalendarVersionId", UUID)

PositiveVersion = Annotated[int, Field(strict=True, ge=1)]
DisplayName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
DisplayPath = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
]
SemanticCode = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    ),
]
SnapshotBasis = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=120,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    ),
]
TimezoneName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]


class FrozenContractModel(BaseModel):
    """为内部强类型契约提供拒绝额外字段、不可变和严格校验的共同边界。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
    )


class EffectiveInterval(FrozenContractModel):
    """表示按 ``[valid_from, valid_to)`` 解释的非空有效区间。"""

    valid_from: AwareDatetime
    valid_to: AwareDatetime | None = None

    @model_validator(mode="after")
    def require_non_empty_half_open_range(self) -> Self:
        """拒绝空区间和反向区间，避免同一时点同时属于旧、新关系。"""

        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("有效期结束时间必须晚于开始时间。")
        return self


class HistoricalSnapshotContract(FrozenContractModel):
    """定义业务历史快照共有的学校、版本、采集时点和取值依据。"""

    school_id: SchoolId
    source_version: PositiveVersion
    captured_at: AwareDatetime
    effective_interval: EffectiveInterval
    snapshot_basis: SnapshotBasis


@dataclass(frozen=True, slots=True, kw_only=True)
class StaffActorContext:
    """表达服务端已核验的单一教职工任职上下文，不包含家长权限。"""

    account_user_id: int
    school_id: SchoolId
    person_id: PersonId
    assignment_id: AssignmentId
    assignment_version: int
    authorization_version: int
    issuance_provenance: InitVar[object]

    def __post_init__(self, issuance_provenance: object) -> None:
        """拒绝没有私有签发来源的直接构造和反序列化。"""

        _require_context_issuance_provenance(issuance_provenance)

    def __init_subclass__(cls, **_kwargs: object) -> None:
        """拒绝通过覆写 ``__post_init__`` 绕过签发来源校验。"""

        raise TypeError("可信执行上下文禁止继承。")


@dataclass(frozen=True, slots=True, kw_only=True)
class GuardianActorContext:
    """表达服务端已核验的单一家校关系上下文，不包含教师任职权限。"""

    account_user_id: int
    school_id: SchoolId
    person_id: PersonId
    guardian_relationship_id: GuardianRelationshipId
    relationship_version: int
    authorization_version: int
    issuance_provenance: InitVar[object]

    def __post_init__(self, issuance_provenance: object) -> None:
        """拒绝没有私有签发来源的直接构造和反序列化。"""

        _require_context_issuance_provenance(issuance_provenance)

    def __init_subclass__(cls, **_kwargs: object) -> None:
        """拒绝通过覆写 ``__post_init__`` 绕过签发来源校验。"""

        raise TypeError("可信执行上下文禁止继承。")


type ActorContext = StaffActorContext | GuardianActorContext


def _build_request_context_subclass_guard() -> tuple[Callable[[], None], Callable[[], None]]:
    """创建只允许从开放变为封闭、不能反向复位的 Request 继承门。"""

    sealed = False

    def require_open_subclassing() -> None:
        if sealed:
            raise TypeError("可信执行上下文禁止继承。")

    def seal_subclassing() -> None:
        nonlocal sealed
        sealed = True

    return require_open_subclassing, seal_subclassing


(
    _request_context_subclass_guard,
    _seal_request_context_subclassing,
) = _build_request_context_subclass_guard()


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestExecutionContext:
    """绑定只读请求使用的可信操作者和服务端请求标识。"""

    actor: ActorContext
    request_id: UUID
    issuance_provenance: InitVar[object]

    def __post_init__(self, issuance_provenance: object) -> None:
        """拒绝绕过应用入口拼装可信请求上下文。"""

        _require_context_issuance_provenance(issuance_provenance)

    def __init_subclass__(cls, **_kwargs: object) -> None:
        """只允许模块定义阶段建立唯一的 Command 子类型，随后封闭层次。"""

        _request_context_subclass_guard()
        super().__init_subclass__()


@dataclass(frozen=True, slots=True)
class CommandExecutionContext(RequestExecutionContext):
    """在只读上下文上增加本次写命令唯一的幂等键。"""

    idempotency_key: str

    def __init_subclass__(cls, **_kwargs: object) -> None:
        """禁止在写命令上下文之上建立可覆写校验的子类型。"""

        raise TypeError("可信执行上下文禁止继承。")


_seal_request_context_subclassing()


class _ContextProvenanceValidator(Protocol):
    def __call__(self, provenance: object) -> None: ...


class _StaffActorContextIssuer(Protocol):
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


class _GuardianActorContextIssuer(Protocol):
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


class _RequestExecutionContextIssuer(Protocol):
    def __call__(
        self,
        *,
        actor: ActorContext,
        request_id: UUID,
    ) -> RequestExecutionContext: ...


class _CommandExecutionContextIssuer(Protocol):
    def __call__(
        self,
        *,
        actor: ActorContext,
        request_id: UUID,
        idempotency_key: str,
    ) -> CommandExecutionContext: ...


def _build_context_issuance_boundary() -> tuple[
    _ContextProvenanceValidator,
    _StaffActorContextIssuer,
    _GuardianActorContextIssuer,
    _RequestExecutionContextIssuer,
    _CommandExecutionContextIssuer,
]:
    """把唯一 provenance 封在闭包内，只向身份边界提供受控签发函数。"""

    issuance_provenance = object()

    def require_provenance(provenance: object) -> None:
        if provenance is not issuance_provenance:
            raise ValueError("可信执行上下文只能由服务端身份解析器构造。")

    def require_exact_actor_context(actor: ActorContext) -> None:
        if type(actor) not in (StaffActorContext, GuardianActorContext):
            raise TypeError("ActorContext 必须是签发器生成的精确类型。")

    def issue_staff_actor_context(
        *,
        account_user_id: int,
        school_id: SchoolId,
        person_id: PersonId,
        assignment_id: AssignmentId,
        assignment_version: int,
        authorization_version: int,
    ) -> StaffActorContext:
        return StaffActorContext(
            account_user_id=account_user_id,
            school_id=school_id,
            person_id=person_id,
            assignment_id=assignment_id,
            assignment_version=assignment_version,
            authorization_version=authorization_version,
            issuance_provenance=issuance_provenance,
        )

    def issue_guardian_actor_context(
        *,
        account_user_id: int,
        school_id: SchoolId,
        person_id: PersonId,
        guardian_relationship_id: GuardianRelationshipId,
        relationship_version: int,
        authorization_version: int,
    ) -> GuardianActorContext:
        return GuardianActorContext(
            account_user_id=account_user_id,
            school_id=school_id,
            person_id=person_id,
            guardian_relationship_id=guardian_relationship_id,
            relationship_version=relationship_version,
            authorization_version=authorization_version,
            issuance_provenance=issuance_provenance,
        )

    def issue_request_execution_context(
        *,
        actor: ActorContext,
        request_id: UUID,
    ) -> RequestExecutionContext:
        require_exact_actor_context(actor)
        return RequestExecutionContext(
            actor=actor,
            request_id=request_id,
            issuance_provenance=issuance_provenance,
        )

    def issue_command_execution_context(
        *,
        actor: ActorContext,
        request_id: UUID,
        idempotency_key: str,
    ) -> CommandExecutionContext:
        require_exact_actor_context(actor)
        return CommandExecutionContext(
            actor=actor,
            request_id=request_id,
            idempotency_key=idempotency_key,
            issuance_provenance=issuance_provenance,
        )

    return (
        require_provenance,
        issue_staff_actor_context,
        issue_guardian_actor_context,
        issue_request_execution_context,
        issue_command_execution_context,
    )


# Task 2 先冻结签发边界；真实调用方在后续 identity/execution_context.py 落地。
(
    _require_context_issuance_provenance,
    _issue_staff_actor_context,
    _issue_guardian_actor_context,
    _issue_request_execution_context,
    _issue_command_execution_context,
) = _build_context_issuance_boundary()
