# pyright: strict

from __future__ import annotations

from typing import Any, Literal, TypeVar

import httpx
from pydantic import BaseModel

from app.composition.school_fact_executor_client_settings import SchoolFactExecutorClientSettings
from app.platform.contracts import CommandExecutionContext, SchoolId
from app.platform.identity.executor_context_codec import CommandContextTransportCodec
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV1,
    ConfirmationReceiptV1,
    PreflightReportV1,
    canonical_plan_sha256,
)
from app.platform.school_facts.executor_contracts import (
    EXECUTOR_AUTH_HEADER,
    SchoolFactExecutorApplyResponse,
    SchoolFactExecutorConfirmRequest,
    SchoolFactExecutorDigestResultRequest,
    SchoolFactExecutorLocatorResultRequest,
    SchoolFactExecutorOwnResultListRequest,
    SchoolFactExecutorOwnResultLocatorRequest,
    SchoolFactExecutorOwnResultLookupResponse,
    SchoolFactExecutorOwnResultPage,
    SchoolFactExecutorPreflightRequest,
    SchoolFactExecutorPrepareRequest,
    SchoolFactExecutorProvenance,
    SchoolFactExecutorResultResponse,
)
from app.platform.school_facts.f7_receipt_contracts import (
    SchoolFactExecutorOwnTargetPageRequest,
    SchoolFactExecutorOwnTargetPageResponse,
)
from app.platform.school_facts.ports import (
    SchoolFactApplyResult,
    SchoolFactChangeProvenance,
    SchoolFactOwnResult,
    SchoolFactOwnResultPage,
    SchoolFactOwnTargetPage,
    SchoolFactResultLookup,
)

_ResponseModel = TypeVar("_ResponseModel", bound=BaseModel)
_ExecutorPath = Literal[
    "/internal/v1/school-facts/preflight",
    "/internal/v1/school-facts/prepare",
    "/internal/v1/school-facts/confirm",
    "/internal/v1/school-facts/result",
    "/internal/v1/school-facts/own-results/list",
    "/internal/v1/school-facts/own-results/lookup",
    "/internal/v1/school-facts/own-results/target-page",
]
_OWN_RESULT_READ_PATHS: frozenset[_ExecutorPath] = frozenset({
    "/internal/v1/school-facts/own-results/list",
    "/internal/v1/school-facts/own-results/lookup",
    "/internal/v1/school-facts/own-results/target-page",
})


def _restore_required_nulls(value: object, payload: object) -> None:
    """Preserve required or explicit canonical nulls, never absent optional fields."""
    if isinstance(value, BaseModel) and isinstance(payload, dict):
        for name, field in type(value).model_fields.items():
            child = getattr(value, name)
            if child is None and (field.is_required() or name in value.model_fields_set):
                payload[name] = None
            elif name in payload:
                _restore_required_nulls(child, payload[name])
    elif isinstance(value, (list, tuple)) and isinstance(payload, list):
        for child, item in zip(value, payload, strict=True):
            _restore_required_nulls(child, item)


class SchoolFactWriterExecutorUnavailable(RuntimeError):
    """表示固定 Executor 调用失败或返回了不可信响应。"""


class SchoolFactWriterExecutorClient:
    """API 侧固定代理；没有数据库配置，也没有任意请求/命令入口。"""

    def __init__(
        self,
        *,
        settings: SchoolFactExecutorClientSettings,
        context_codec: CommandContextTransportCodec | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        caller_key = settings.caller_auth_key.get_secret_value()
        self._context_codec = context_codec or CommandContextTransportCodec(
            settings.context_transport_hmac_key
        )
        self._client = httpx.Client(
            base_url=settings.base_url,
            headers={EXECUTOR_AUTH_HEADER: caller_key},
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SchoolFactWriterExecutorClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def preflight(
        self,
        *,
        school_id: SchoolId,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> PreflightReportV1:
        request = SchoolFactExecutorPreflightRequest(
            school_id=school_id,
            change_set=change_set,
            context_token=self._context_codec.encode(context),
            session_binding=session_binding,
        )
        return self._post(
            "/internal/v1/school-facts/preflight",
            request,
            PreflightReportV1,
        )

    def prepare(
        self,
        *,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_binding: str,
        provenance: SchoolFactChangeProvenance | None = None,
    ) -> ConfirmationReceiptV1:
        request = SchoolFactExecutorPrepareRequest(
            plan_sha256=canonical_plan_sha256(change_set),
            change_set=change_set,
            context_token=self._context_codec.encode(context),
            session_binding=session_binding,
            provenance=(
                SchoolFactExecutorProvenance.from_domain(provenance)
                if provenance is not None
                else None
            ),
        )
        return self._post(
            "/internal/v1/school-facts/prepare",
            request,
            ConfirmationReceiptV1,
        )

    def confirm(
        self,
        *,
        credential: str,
        change_set: CanonicalFactChangeSetV1,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> SchoolFactApplyResult:
        request = SchoolFactExecutorConfirmRequest(
            plan_sha256=canonical_plan_sha256(change_set),
            credential=credential,
            change_set=change_set,
            context_token=self._context_codec.encode(context),
            session_binding=session_binding,
        )
        response = self._post(
            "/internal/v1/school-facts/confirm",
            request,
            SchoolFactExecutorApplyResponse,
        )
        return self._apply_result(response)

    def result_by_digest(
        self,
        *,
        school_id: SchoolId,
        source_sha256: str,
        plan_sha256: str,
        context: CommandExecutionContext,
        session_binding: str,
    ) -> SchoolFactResultLookup:
        request = SchoolFactExecutorDigestResultRequest(
            lookup_kind="digest",
            school_id=school_id,
            source_sha256=source_sha256,
            plan_sha256=plan_sha256,
            context_token=self._context_codec.encode(context),
            session_binding=session_binding,
        )
        return self._lookup_result(request)

    def result_by_locator(
        self,
        *,
        school_id: SchoolId,
        actor_user_id: int,
        session_binding: str,
        locator_sha256: str,
        context: CommandExecutionContext,
    ) -> SchoolFactResultLookup:
        request = SchoolFactExecutorLocatorResultRequest(
            lookup_kind="locator",
            school_id=school_id,
            actor_user_id=actor_user_id,
            context_token=self._context_codec.encode(context),
            session_binding=session_binding,
            locator_sha256=locator_sha256,
        )
        return self._lookup_result(request)

    def list_own_results(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        limit: int = 20,
    ) -> SchoolFactOwnResultPage:
        request = SchoolFactExecutorOwnResultListRequest(
            context_token=self._context_codec.encode(context),
            session_binding=session_binding,
            limit=limit,
        )
        response = self._post(
            "/internal/v1/school-facts/own-results/list",
            request,
            SchoolFactExecutorOwnResultPage,
        )
        if len(response.items) > limit or len(
            {item.locator_sha256 for item in response.items}
        ) != len(response.items):
            raise SchoolFactWriterExecutorUnavailable("Own receipt list is inconsistent.")
        return response.to_domain()

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
        request = SchoolFactExecutorOwnResultLocatorRequest(
            context_token=self._context_codec.encode(context),
            session_binding=session_binding,
            locator_sha256=locator_sha256,
            original_fields=original_fields,
            target_offset=target_offset,
            target_limit=target_limit,
        )
        response = self._post(
            "/internal/v1/school-facts/own-results/lookup",
            request,
            SchoolFactExecutorOwnResultLookupResponse,
        )
        if response.item is not None and response.item.locator_sha256 != locator_sha256:
            raise SchoolFactWriterExecutorUnavailable("Own receipt locator does not match.")
        if response.item is not None:
            for index, target in enumerate(response.item.targets):
                requested = (
                    original_fields is not None
                    and target_offset <= index < target_offset + target_limit
                )
                original = target.original_result
                if (original is not None) != requested:
                    raise SchoolFactWriterExecutorUnavailable(
                        "Original receipt page does not match."
                    )
                if (
                    original is not None
                    and original.status == "available"
                    and (
                        original_fields is None
                        or target.fact_kind != "teaching_assignment"
                        or tuple(original.field_names) != original_fields.get(target.fact_kind)
                    )
                ):
                    raise SchoolFactWriterExecutorUnavailable(
                        "Original receipt fields do not match."
                    )
        return response.item.to_domain() if response.item is not None else None

    def read_own_target_page(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        locator_sha256: str,
        offset: int = 0,
        limit: int = 20,
    ) -> SchoolFactOwnTargetPage | None:
        request = SchoolFactExecutorOwnTargetPageRequest(
            context_token=self._context_codec.encode(context),
            session_binding=session_binding,
            locator_sha256=locator_sha256,
            offset=offset,
            limit=limit,
        )
        response = self._post(
            "/internal/v1/school-facts/own-results/target-page",
            request,
            SchoolFactExecutorOwnTargetPageResponse,
        )
        if response.page is None:
            return None
        page = response.page
        if (
            page.terminal.locator_sha256 != locator_sha256
            or page.offset != offset
            or page.next_offset > offset + limit
            or len(page.items) > limit
        ):
            raise SchoolFactWriterExecutorUnavailable("Own receipt target page does not match.")
        return page.to_domain()

    def _lookup_result(
        self,
        request: SchoolFactExecutorDigestResultRequest | SchoolFactExecutorLocatorResultRequest,
    ) -> SchoolFactResultLookup:
        response = self._post(
            "/internal/v1/school-facts/result",
            request,
            SchoolFactExecutorResultResponse,
        )
        return SchoolFactResultLookup(
            found=response.found,
            result=(self._apply_result(response.result) if response.result is not None else None),
        )

    def _post(
        self,
        path: _ExecutorPath,
        request: BaseModel,
        response_model: type[_ResponseModel],
    ) -> _ResponseModel:
        payload: dict[str, Any] = request.model_dump(mode="json", exclude_none=True)
        if isinstance(
            request,
            (
                SchoolFactExecutorPreflightRequest,
                SchoolFactExecutorPrepareRequest,
                SchoolFactExecutorConfirmRequest,
            ),
        ):
            # Canonical contracts contain required and explicitly supplied
            # nullable fields. Their presence is part of the package digest;
            # optional omissions and custom serializers must remain intact.
            _restore_required_nulls(request.change_set, payload["change_set"])
        try:
            response = self._client.post(path, json=payload)
            response.raise_for_status()
            return response_model.model_validate_json(response.content)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409 or (
                exc.response.status_code == 403 and path in _OWN_RESULT_READ_PATHS
            ):
                # A denied detail read may be followed by the separately
                # authorized own-terminal read. Unknown/transport failures and
                # refusals on execution paths must never masquerade as that case.
                raise SchoolFactChangeRejected(
                    "School Fact change was rejected by Writer Executor."
                ) from exc
            raise SchoolFactWriterExecutorUnavailable(
                "School Facts Writer Executor call failed."
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise SchoolFactWriterExecutorUnavailable(
                "School Facts Writer Executor 调用失败。"
            ) from exc

    @staticmethod
    def _apply_result(value: SchoolFactExecutorApplyResponse) -> SchoolFactApplyResult:
        return SchoolFactApplyResult(
            batch_id=value.batch_id,
            target_version=value.target_version,
            changed_count=value.changed_count,
            replayed=value.replayed,
        )
