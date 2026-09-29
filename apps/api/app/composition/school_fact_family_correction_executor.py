# pyright: strict
"""Fixed authenticated Writer port for family correction reports."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import AwareDatetime, Field, field_validator

from app.composition.school_fact_executor_client_settings import SchoolFactExecutorClientSettings
from app.domains.students.family_corrections import (
    FamilyCorrectionDenied,
    FamilyCorrectionNotFound,
    FamilyCorrectionReport,
)
from app.platform.contracts import CommandExecutionContext, FrozenContractModel
from app.platform.identity.executor_context_codec import (
    CommandContextTransportCodec,
    CommandContextTransportRejected,
)
from app.platform.school_facts.executor_contracts import EXECUTOR_AUTH_HEADER, Sha256Hex


class CorrectionReceipt(FrozenContractModel):
    report_id: UUID
    fact_kind: str | None = Field(default=None, min_length=1, max_length=64)
    status: Literal["submitted", "resolved", "reviewed"]
    field_name: str = Field(min_length=1, max_length=64)
    submitted_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    review_explanation: str | None = Field(default=None, max_length=1000)

    @classmethod
    def from_domain(cls, value: FamilyCorrectionReport) -> CorrectionReceipt:
        return cls(
            report_id=value.report_id,
            fact_kind=value.fact_kind,
            status=value.status,
            field_name=value.field_name,
            submitted_at=value.submitted_at,
            resolved_at=value.resolved_at,
            review_explanation=value.review_explanation,
        )


class CorrectionOption(FrozenContractModel):
    fact_kind: str | None = Field(
        default=None, min_length=1, max_length=64, exclude_if=lambda value: value is None
    )
    field_name: str
    label: str
    reference: str


class CorrectionOptions(FrozenContractModel):
    items: tuple[CorrectionOption, ...] = Field(max_length=100)


class StaffCorrectionReceipt(CorrectionReceipt):
    statement: str = Field(min_length=1, max_length=1000)

    @classmethod
    def from_domain(cls, value: FamilyCorrectionReport) -> StaffCorrectionReceipt:
        return cls(**CorrectionReceipt.from_domain(value).model_dump(), statement=value.statement)


class StaffCorrectionPage(FrozenContractModel):
    items: tuple[StaffCorrectionReceipt, ...] = Field(max_length=20)


class CorrectionSubmitRequest(FrozenContractModel):
    context_token: str
    session_binding: str = Field(min_length=1, max_length=255)
    target_reference: str = Field(min_length=1, max_length=512)
    field_name: str = Field(min_length=1, max_length=64)
    statement: str = Field(min_length=1, max_length=1000)


class CorrectionOwnRequest(FrozenContractModel):
    context_token: str
    session_binding: str = Field(min_length=1, max_length=255)
    report_id: UUID

    @field_validator("report_id", mode="before")
    @classmethod
    def parse_wire_report_id(cls, value: object) -> UUID:
        if isinstance(value, UUID):
            return value
        if type(value) is not str:
            raise ValueError("A UUID report reference is required.")
        return UUID(value)


class CorrectionOwnListRequest(FrozenContractModel):
    context_token: str
    session_binding: str = Field(min_length=1, max_length=255)
    limit: int = Field(default=20, ge=1, le=20)


class CorrectionByKeyRequest(FrozenContractModel):
    context_token: str
    session_binding: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=1, max_length=255)


class CorrectionStaffListRequest(FrozenContractModel):
    context_token: str
    session_binding: str = Field(min_length=1, max_length=255)
    fact_kind: str = Field(min_length=1, max_length=64)
    source_key: str = Field(min_length=1, max_length=255)
    field_name: str = Field(min_length=1, max_length=64)
    required_action: Literal["maintain", "end", "revoke"]
    limit: int = Field(default=20, ge=1, le=20)


class CorrectionResolveRequest(CorrectionOwnRequest):
    writer_receipt_ref: Sha256Hex


class CorrectionReviewPreparation(FrozenContractModel):
    kind: Literal["family_correction_review_preparation"] = "family_correction_review_preparation"
    report_id: UUID
    field_name: str
    explanation: str = Field(min_length=1, max_length=1000)
    target_version: int = Field(ge=1)
    expires_at: AwareDatetime
    confirmation_token: str = Field(min_length=16, max_length=8192)


class CorrectionReviewPrepareRequest(CorrectionOwnRequest):
    explanation: str = Field(min_length=1, max_length=1000)
    required_action: Literal["maintain", "end", "revoke"] = "maintain"


class CorrectionReviewResultRequest(CorrectionOwnRequest):
    confirmation_token: str = Field(min_length=16, max_length=8192)


class CorrectionReviewConfirmRequest(CorrectionReviewResultRequest):
    confirmed: Literal[True]


class CorrectionPage(FrozenContractModel):
    items: tuple[CorrectionReceipt, ...] = Field(max_length=20)


@runtime_checkable
class FamilyCorrectionExecutorKernel(Protocol):
    def prepare_review(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        report_id: UUID,
        explanation: str,
        required_action: Literal["maintain", "end", "revoke"],
    ) -> CorrectionReviewPreparation: ...
    def confirm_review(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        report_id: UUID,
        confirmation_token: str,
        confirmed: bool,
    ) -> FamilyCorrectionReport: ...
    def review_result(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        report_id: UUID,
        confirmation_token: str,
    ) -> FamilyCorrectionReport: ...
    def options(
        self, *, context: CommandExecutionContext, session_binding: str
    ) -> tuple[CorrectionOption, ...]: ...
    def list_own(
        self, *, context: CommandExecutionContext, session_binding: str, limit: int
    ) -> tuple[FamilyCorrectionReport, ...]: ...
    def own_by_key(
        self, *, context: CommandExecutionContext, session_binding: str, idempotency_key: str
    ) -> FamilyCorrectionReport: ...
    def submit(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        target_reference: str,
        field_name: str,
        statement: str,
    ) -> FamilyCorrectionReport: ...
    def own(
        self, *, context: CommandExecutionContext, session_binding: str, report_id: UUID
    ) -> FamilyCorrectionReport: ...
    def staff_list(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        fact_kind: str,
        source_key: str,
        field_name: str,
        required_action: Literal["maintain", "end", "revoke"],
        limit: int,
    ) -> tuple[FamilyCorrectionReport, ...]: ...
    def resolve(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        report_id: UUID,
        writer_receipt_ref: str,
    ) -> FamilyCorrectionReport: ...


def create_family_correction_executor_app(
    *,
    kernel: FamilyCorrectionExecutorKernel,
    context_codec: CommandContextTransportCodec,
    caller_auth_key: bytes | str,
) -> FastAPI:
    key = caller_auth_key.encode() if isinstance(caller_auth_key, str) else caller_auth_key
    if len(key) < 32:
        raise ValueError("Executor caller key is too short.")
    app = FastAPI(title="Family Correction Writer", docs_url=None, redoc_url=None, openapi_url=None)

    def auth(supplied: str | None = Header(default=None, alias=EXECUTOR_AUTH_HEADER)) -> None:
        if not hmac.compare_digest((supplied or "").encode(), key):
            raise HTTPException(401, "Writer Executor caller authentication failed.")

    def context(token: str) -> CommandExecutionContext:
        try:
            return context_codec.decode(token)
        except CommandContextTransportRejected as exc:
            raise HTTPException(401, "Trusted command context is invalid.") from exc

    def guarded(call: Callable[[], FamilyCorrectionReport]) -> CorrectionReceipt:
        try:
            return CorrectionReceipt.from_domain(call())
        except FamilyCorrectionNotFound as exc:
            raise HTTPException(404, "Correction report unavailable.") from exc
        except FamilyCorrectionDenied as exc:
            raise HTTPException(403, "Current correction authority changed.") from exc

    @app.post(
        "/internal/v1/school-facts/family-corrections/options",
        response_model=CorrectionOptions,
        dependencies=[Depends(auth)],
    )
    def options(r: CorrectionOwnListRequest) -> CorrectionOptions:  # pyright: ignore[reportUnusedFunction]
        try:
            items = kernel.options(
                context=context(r.context_token), session_binding=r.session_binding
            )
        except FamilyCorrectionDenied as exc:
            raise HTTPException(403, "Current correction authority changed.") from exc
        return CorrectionOptions(items=items)

    @app.post(
        "/internal/v1/school-facts/family-corrections/own-list",
        response_model=CorrectionPage,
        dependencies=[Depends(auth)],
    )
    def own_list(r: CorrectionOwnListRequest) -> CorrectionPage:  # pyright: ignore[reportUnusedFunction]
        try:
            rows = kernel.list_own(
                context=context(r.context_token), session_binding=r.session_binding, limit=r.limit
            )
        except FamilyCorrectionDenied as exc:
            raise HTTPException(403, "Current correction authority changed.") from exc
        return CorrectionPage(items=tuple(CorrectionReceipt.from_domain(x) for x in rows))

    @app.post(
        "/internal/v1/school-facts/family-corrections/by-key",
        response_model=CorrectionReceipt,
        dependencies=[Depends(auth)],
    )
    def by_key(r: CorrectionByKeyRequest) -> CorrectionReceipt:  # pyright: ignore[reportUnusedFunction]
        return guarded(
            lambda: kernel.own_by_key(
                context=context(r.context_token),
                session_binding=r.session_binding,
                idempotency_key=r.idempotency_key,
            )
        )

    @app.post(
        "/internal/v1/school-facts/family-corrections/submit",
        response_model=CorrectionReceipt,
        dependencies=[Depends(auth)],
    )
    def submit(r: CorrectionSubmitRequest) -> CorrectionReceipt:  # pyright: ignore[reportUnusedFunction]
        return guarded(
            lambda: kernel.submit(
                context=context(r.context_token),
                session_binding=r.session_binding,
                target_reference=r.target_reference,
                field_name=r.field_name,
                statement=r.statement,
            )
        )

    @app.post(
        "/internal/v1/school-facts/family-corrections/own",
        response_model=CorrectionReceipt,
        dependencies=[Depends(auth)],
    )
    def own(r: CorrectionOwnRequest) -> CorrectionReceipt:  # pyright: ignore[reportUnusedFunction]
        return guarded(
            lambda: kernel.own(
                context=context(r.context_token),
                session_binding=r.session_binding,
                report_id=r.report_id,
            )
        )

    @app.post(
        "/internal/v1/school-facts/family-corrections/staff-list",
        response_model=StaffCorrectionPage,
        dependencies=[Depends(auth)],
    )
    def staff_list(r: CorrectionStaffListRequest) -> StaffCorrectionPage:  # pyright: ignore[reportUnusedFunction]
        try:
            rows = kernel.staff_list(
                context=context(r.context_token),
                session_binding=r.session_binding,
                fact_kind=r.fact_kind,
                source_key=r.source_key,
                field_name=r.field_name,
                required_action=r.required_action,
                limit=r.limit,
            )
        except FamilyCorrectionDenied as exc:
            raise HTTPException(403, "Current correction authority changed.") from exc
        return StaffCorrectionPage(items=tuple(StaffCorrectionReceipt.from_domain(x) for x in rows))

    @app.post(
        "/internal/v1/school-facts/family-corrections/resolve",
        response_model=CorrectionReceipt,
        dependencies=[Depends(auth)],
    )
    def resolve(r: CorrectionResolveRequest) -> CorrectionReceipt:  # pyright: ignore[reportUnusedFunction]
        return guarded(
            lambda: kernel.resolve(
                context=context(r.context_token),
                session_binding=r.session_binding,
                report_id=r.report_id,
                writer_receipt_ref=r.writer_receipt_ref,
            )
        )

    @app.post(
        "/internal/v1/school-facts/family-corrections/prepare-review",
        response_model=CorrectionReviewPreparation,
        dependencies=[Depends(auth)],
    )
    def prepare_review(r: CorrectionReviewPrepareRequest) -> CorrectionReviewPreparation:
        try:
            return kernel.prepare_review(
                context=context(r.context_token),
                session_binding=r.session_binding,
                report_id=r.report_id,
                explanation=r.explanation,
                required_action=r.required_action,
            )
        except FamilyCorrectionDenied as exc:
            raise HTTPException(403, "Current review authority changed.") from exc

    @app.post(
        "/internal/v1/school-facts/family-corrections/confirm-review",
        response_model=CorrectionReceipt,
        dependencies=[Depends(auth)],
    )
    def confirm_review(r: CorrectionReviewConfirmRequest) -> CorrectionReceipt:
        return guarded(
            lambda: kernel.confirm_review(
                context=context(r.context_token),
                session_binding=r.session_binding,
                report_id=r.report_id,
                confirmation_token=r.confirmation_token,
                confirmed=r.confirmed,
            )
        )

    @app.post(
        "/internal/v1/school-facts/family-corrections/review-result",
        response_model=CorrectionReceipt,
        dependencies=[Depends(auth)],
    )
    def review_result(r: CorrectionReviewResultRequest) -> CorrectionReceipt:
        return guarded(
            lambda: kernel.review_result(
                context=context(r.context_token),
                session_binding=r.session_binding,
                report_id=r.report_id,
                confirmation_token=r.confirmation_token,
            )
        )

    return app


class FamilyCorrectionExecutorClient:
    def prepare_review(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        report_id: UUID,
        explanation: str,
        required_action: Literal["maintain", "end", "revoke"],
    ) -> CorrectionReviewPreparation:
        r = CorrectionReviewPrepareRequest(
            context_token=self._codec.encode(context),
            session_binding=session_binding,
            report_id=report_id,
            explanation=explanation,
            required_action=required_action,
        )
        result = self._post(
            "/internal/v1/school-facts/family-corrections/prepare-review",
            r,
            CorrectionReviewPreparation,
        )
        assert isinstance(result, CorrectionReviewPreparation)
        return result

    def confirm_review(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        report_id: UUID,
        confirmation_token: str,
        confirmed: bool,
    ) -> CorrectionReceipt:
        if confirmed is not True:
            raise FamilyCorrectionDenied("Explicit review confirmation required.")
        r = CorrectionReviewConfirmRequest(
            context_token=self._codec.encode(context),
            session_binding=session_binding,
            report_id=report_id,
            confirmation_token=confirmation_token,
            confirmed=True,
        )
        result = self._post(
            "/internal/v1/school-facts/family-corrections/confirm-review", r, CorrectionReceipt
        )
        assert isinstance(result, CorrectionReceipt)
        return result

    def review_result(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        report_id: UUID,
        confirmation_token: str,
    ) -> CorrectionReceipt:
        r = CorrectionReviewResultRequest(
            context_token=self._codec.encode(context),
            session_binding=session_binding,
            report_id=report_id,
            confirmation_token=confirmation_token,
        )
        result = self._post(
            "/internal/v1/school-facts/family-corrections/review-result", r, CorrectionReceipt
        )
        assert isinstance(result, CorrectionReceipt)
        return result

    def __init__(
        self,
        *,
        settings: SchoolFactExecutorClientSettings,
        context_codec: CommandContextTransportCodec | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._codec = context_codec or CommandContextTransportCodec(
            settings.context_transport_hmac_key
        )
        self._client = httpx.Client(
            base_url=settings.base_url,
            headers={EXECUTOR_AUTH_HEADER: settings.caller_auth_key.get_secret_value()},
            timeout=settings.request_timeout_seconds,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _post(
        self,
        path: str,
        request: FrozenContractModel,
        model: (
            type[CorrectionReceipt]
            | type[CorrectionPage]
            | type[CorrectionOptions]
            | type[StaffCorrectionPage]
            | type[CorrectionReviewPreparation]
        ),
    ) -> (
        CorrectionReceipt
        | CorrectionPage
        | CorrectionOptions
        | StaffCorrectionPage
        | CorrectionReviewPreparation
    ):
        try:
            response = self._client.post(path, json=request.model_dump(mode="json"))
            response.raise_for_status()
            return model.model_validate_json(response.content)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404 and path in {
                "/internal/v1/school-facts/family-corrections/own",
                "/internal/v1/school-facts/family-corrections/by-key",
                "/internal/v1/school-facts/family-corrections/review-result",
            }:
                try:
                    detail: object = exc.response.json()
                except ValueError:
                    detail = None
                if detail == {"detail": "Correction report unavailable."}:
                    raise FamilyCorrectionNotFound("Correction report unavailable.") from exc
            if exc.response.status_code in {403, 409}:
                raise FamilyCorrectionDenied("Current correction authority changed.") from exc
            raise RuntimeError("Family correction Writer unavailable.") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("Family correction Writer unavailable.") from exc

    def options(
        self, *, context: CommandExecutionContext, session_binding: str
    ) -> CorrectionOptions:
        r = CorrectionOwnListRequest(
            context_token=self._codec.encode(context), session_binding=session_binding
        )
        return self._post(
            "/internal/v1/school-facts/family-corrections/options", r, CorrectionOptions
        )  # type: ignore[return-value, arg-type]

    def own_list(
        self, *, context: CommandExecutionContext, session_binding: str, limit: int = 20
    ) -> CorrectionPage:
        r = CorrectionOwnListRequest(
            context_token=self._codec.encode(context), session_binding=session_binding, limit=limit
        )
        return self._post(
            "/internal/v1/school-facts/family-corrections/own-list", r, CorrectionPage
        )  # type: ignore[return-value]

    def by_key(
        self, *, context: CommandExecutionContext, session_binding: str, idempotency_key: str
    ) -> CorrectionReceipt | None:
        r = CorrectionByKeyRequest(
            context_token=self._codec.encode(context),
            session_binding=session_binding,
            idempotency_key=idempotency_key,
        )
        try:
            return self._post(
                "/internal/v1/school-facts/family-corrections/by-key", r, CorrectionReceipt
            )  # type: ignore[return-value]
        except FamilyCorrectionNotFound:
            return None

    def submit(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        target_reference: str,
        field_name: str,
        statement: str,
    ) -> CorrectionReceipt:
        r = CorrectionSubmitRequest(
            context_token=self._codec.encode(context),
            session_binding=session_binding,
            target_reference=target_reference,
            field_name=field_name,
            statement=statement,
        )
        return self._post(
            "/internal/v1/school-facts/family-corrections/submit", r, CorrectionReceipt
        )  # type: ignore[return-value]

    def own(
        self, *, context: CommandExecutionContext, session_binding: str, report_id: UUID
    ) -> CorrectionReceipt:
        r = CorrectionOwnRequest(
            context_token=self._codec.encode(context),
            session_binding=session_binding,
            report_id=report_id,
        )
        return self._post("/internal/v1/school-facts/family-corrections/own", r, CorrectionReceipt)  # type: ignore[return-value]

    def staff_list(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        fact_kind: str,
        source_key: str,
        field_name: str,
        required_action: Literal["maintain", "end", "revoke"],
        limit: int = 20,
    ) -> StaffCorrectionPage:
        r = CorrectionStaffListRequest(
            context_token=self._codec.encode(context),
            session_binding=session_binding,
            fact_kind=fact_kind,
            source_key=source_key,
            field_name=field_name,
            required_action=required_action,
            limit=limit,
        )
        return self._post(
            "/internal/v1/school-facts/family-corrections/staff-list", r, StaffCorrectionPage
        )  # type: ignore[return-value]

    def resolve(
        self,
        *,
        context: CommandExecutionContext,
        session_binding: str,
        report_id: UUID,
        writer_receipt_ref: str,
    ) -> CorrectionReceipt:
        r = CorrectionResolveRequest(
            context_token=self._codec.encode(context),
            session_binding=session_binding,
            report_id=report_id,
            writer_receipt_ref=writer_receipt_ref,
        )
        return self._post(
            "/internal/v1/school-facts/family-corrections/resolve", r, CorrectionReceipt
        )  # type: ignore[return-value]
