# pyright: strict

from __future__ import annotations

from app.composition.school_fact_writer_client import SchoolFactWriterExecutorClient
from app.platform.contracts import CommandExecutionContext, SchoolId
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    ConfirmationReceiptV1,
    PreflightReportV1,
)
from app.platform.school_facts.ports import (
    OwnOriginalReadOptions,
    SchoolFactApplyResult,
    SchoolFactChangeProvenance,
    SchoolFactOwnResult,
    SchoolFactOwnResultPage,
    SchoolFactOwnTargetPage,
    SchoolFactResultLookup,
)


class SchoolFactExecutorChangeBoundary:
    """把正式 router 高层端口机械映射到固定 Executor 四操作。"""

    def __init__(self, client: SchoolFactWriterExecutorClient) -> None:
        self._client = client

    def list_own_results(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        limit: int = 20,
    ) -> SchoolFactOwnResultPage:
        return self._client.list_own_results(
            context=context,
            session_binding=session_binding,
            limit=limit,
        )

    def check_own_result_by_locator(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        locator_sha256: str,
        original_fields: dict[str, tuple[str, ...]] | None = None,
        target_offset: int = 0,
        target_limit: int = 3,
    ) -> SchoolFactOwnResult | None:
        options: OwnOriginalReadOptions = (
            {
                "original_fields": original_fields,
                "target_offset": target_offset,
                "target_limit": target_limit,
            }
            if original_fields is not None
            else {}
        )
        return self._client.check_own_result_by_locator(
            context=context,
            session_binding=session_binding,
            locator_sha256=locator_sha256,
            **options,
        )

    def read_own_target_page(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        locator_sha256: str,
        offset: int = 0,
        limit: int = 20,
    ) -> SchoolFactOwnTargetPage | None:
        return self._client.read_own_target_page(
            context=context,
            session_binding=session_binding,
            locator_sha256=locator_sha256,
            offset=offset,
            limit=limit,
        )

    def preflight(
        self,
        *,
        school_id: SchoolId,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> PreflightReportV1:
        return self._client.preflight(
            school_id=school_id,
            change_set=change_set,
            context=context,
            session_binding=session_binding,
        )

    def prepare_confirmation(
        self,
        *,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_binding: str,
        provenance: SchoolFactChangeProvenance | None = None,
    ) -> ConfirmationReceiptV1:
        return self._client.prepare(
            change_set=change_set,
            context=context,
            session_binding=session_binding,
            provenance=provenance,
        )

    def confirm(
        self,
        *,
        credential: str,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> SchoolFactApplyResult:
        return self._client.confirm(
            credential=credential,
            change_set=change_set,
            context=context,
            session_binding=session_binding,
        )

    def check_result_by_digest(
        self,
        *,
        school_id: SchoolId,
        source_sha256: str,
        plan_sha256: str,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> SchoolFactApplyResult | None:
        return self._client.result_by_digest(
            school_id=school_id,
            source_sha256=source_sha256,
            plan_sha256=plan_sha256,
            context=context,
            session_binding=session_binding,
        ).result

    def check_result_by_locator(
        self,
        *,
        school_id: SchoolId,
        actor_user_id: int,
        session_binding: str,
        locator_sha256: str,
        context: CommandExecutionContext,
    ) -> SchoolFactResultLookup:
        return self._client.result_by_locator(
            school_id=school_id,
            actor_user_id=actor_user_id,
            session_binding=session_binding,
            locator_sha256=locator_sha256,
            context=context,
        )
