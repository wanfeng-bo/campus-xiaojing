# pyright: strict

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.platform.contracts import CommandExecutionContext, SchoolId
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    ConfirmationReceiptV1,
    PreflightReportV1,
)
from app.platform.school_facts.ports import (
    SchoolFactApplyResult,
    SchoolFactChangeProvenance,
    SchoolFactOwnResult,
    SchoolFactOwnResultPage,
    SchoolFactResultLookup,
)


class SchoolFactChangeBoundary(Protocol):
    """正式 HTTP 确认链所需的最窄高层端口，可由本地 kernel 或固定 Executor 代理实现。"""

    def preflight(
        self,
        *,
        school_id: SchoolId,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> PreflightReportV1: ...

    def prepare_confirmation(
        self,
        *,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_binding: str,
        provenance: SchoolFactChangeProvenance | None = None,
    ) -> ConfirmationReceiptV1: ...

    def confirm(
        self,
        *,
        credential: str,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> SchoolFactApplyResult: ...

    def check_result_by_digest(
        self,
        *,
        school_id: SchoolId,
        source_sha256: str,
        plan_sha256: str,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> SchoolFactApplyResult | None: ...

    def check_result_by_locator(
        self,
        *,
        school_id: SchoolId,
        actor_user_id: int,
        session_binding: str,
        locator_sha256: str,
        context: CommandExecutionContext,
    ) -> SchoolFactResultLookup: ...


@runtime_checkable
class SchoolFactOwnResultBoundary(Protocol):
    """Independent current-identity read; never reuses a confirmation session."""

    def list_own_results(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        limit: int = 20,
    ) -> SchoolFactOwnResultPage: ...

    def check_own_result_by_locator(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        locator_sha256: str,
        original_fields: dict[str, tuple[str, ...]] | None = None,
        target_offset: int = 0,
        target_limit: int = 3,
    ) -> SchoolFactOwnResult | None: ...
