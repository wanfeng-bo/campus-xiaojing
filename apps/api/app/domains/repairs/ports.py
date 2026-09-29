from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domains.repairs.contracts import (
    RepairCardEditCandidate,
    RepairCardEditSource,
    RepairDraftContinuation,
    RepairEntryInterpretationCandidate,
    RepairPresentationRelationSeed,
    RepairPresentationTicketProjection,
    RepairScopeBindingFact,
    RepairScopeKind,
    RepairScopePurpose,
    RepairSubmissionProjection,
)
from app.domains.repairs.models import RepairTicket
from app.platform.contracts import LocationId, SchoolId, StaffActorContext


class RepairCommitOutcomeUnknown(RuntimeError):
    """表示正式提交应答丢失，Handler 必须用新的只读事务探测。"""


class RepairPersistenceError(RuntimeError):
    """只携带报修错误映射需要的 SQLSTATE，不暴露数据库异常内容。"""

    def __init__(self, *, sqlstate: str | None) -> None:
        super().__init__("repair persistence operation failed")
        self.sqlstate = sqlstate


class RepairDraftModelUnavailableError(RuntimeError):
    """用固定安全文案表示本次非正式文字整理不可用。"""

    def __init__(self) -> None:
        super().__init__("报修文字整理暂时不可用，请直接填写地点和问题。")


class RepairCardEditModelInvalidOutputError(RuntimeError):
    """表示供应商返回值无法构造严格的单字段改卡候选。"""


class RepairCardEditModelPort(Protocol):
    """只解释一次未提交卡片的单字段修改，不拥有草稿或正式事实。"""

    def interpret_card_edit(self, source: RepairCardEditSource) -> RepairCardEditCandidate: ...


class RepairDraftModelPort(Protocol):
    """定义报修领域消费的唯一模型能力，不暴露供应商语义。"""

    def interpret(
        self,
        *,
        raw_text: str,
        continuation: RepairDraftContinuation | None,
    ) -> RepairEntryInterpretationCandidate: ...


class RepairPresentationFactReader(Protocol):
    """先读无敏感关系种子，再按已授权工单读取最小呈现字段。"""

    def list_relation_seeds(
        self,
        actor: StaffActorContext,
    ) -> tuple[RepairPresentationRelationSeed, ...]: ...

    def get_ticket_projections(
        self,
        school_id: SchoolId,
        ticket_ids: tuple[UUID, ...],
    ) -> tuple[RepairPresentationTicketProjection, ...]: ...


class RepairScopeBindingReader(Protocol):
    """只读取报修模块拥有的当前范围绑定。"""

    def list_current_scope_bindings(
        self,
        school_id: SchoolId,
        *,
        purpose: RepairScopePurpose,
        scope_kind: RepairScopeKind,
        location_id: LocationId | None,
        as_of: datetime,
    ) -> tuple[RepairScopeBindingFact, ...]: ...


class RepairScopeBindingLocker(Protocol):
    """按稳定 ID 锁定已解析的范围绑定，供提交私有重验使用。"""

    def lock_scope_bindings(
        self,
        school_id: SchoolId,
        binding_ids: tuple[UUID, ...],
        *,
        as_of: datetime,
    ) -> tuple[RepairScopeBindingFact, ...]: ...


class RepairTicketWriter(Protocol):
    """只在调用方事务中追加一张已经重验的正式报修工单。"""

    def add_ticket(self, ticket: RepairTicket) -> None: ...


class RepairSubmissionProbeReader(Protocol):
    """按同校内部工单 ID 读取无电话、无正文的提交结果。"""

    def get_submission_projection(
        self,
        school_id: SchoolId,
        ticket_id: UUID,
    ) -> RepairSubmissionProjection | None: ...


class RepairSubmissionRepository(
    RepairScopeBindingReader,
    RepairScopeBindingLocker,
    RepairTicketWriter,
    RepairSubmissionProbeReader,
    Protocol,
):
    """组合一次正式提交实际使用的报修领域持久化边界。"""

    pass
