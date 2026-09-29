# pyright: reportUnusedFunction=false
"""Explicit family correction UI actions; no direct database access or model execution."""

from collections.abc import Callable
from typing import Annotated, Literal, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import Field, field_validator

from app.composition.school_fact_directory_context import bind_directory_identity
from app.composition.school_fact_family_correction_executor import CorrectionReviewPreparation
from app.composition.school_fact_family_correction_product import (
    CorrectionField,
    FamilyCorrectionProduct,
    PublicCorrectionOptions,
    PublicCorrectionPage,
    PublicCorrectionReceipt,
    PublicStaffCorrectionPage,
)
from app.composition.school_fact_product_identity import ProductIdentity
from app.domains.students.family_corrections import FamilyCorrectionDenied
from app.interfaces.http.framework_request import (
    FrameworkRequestContext,
    get_framework_request_context,
)
from app.interfaces.http.school_fact_product_contacts import product_no_store
from app.platform.contracts import FrozenContractModel


class CorrectionSubmitInput(FrozenContractModel):
    reference: str = Field(min_length=8, max_length=1600)
    field_name: CorrectionField
    statement: str = Field(min_length=1, max_length=1000)
    idempotency_key: UUID

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def parse_wire_key(cls, value: object) -> UUID:
        if isinstance(value, UUID):
            return value
        if type(value) is not str:
            raise ValueError("A UUID idempotency key is required")
        return UUID(value)


class CorrectionResolveInput(FrozenContractModel):
    writer_receipt_ref: str = Field(pattern=r"^sf2receipt_[0-9a-f]{64}$")


class CorrectionReviewPrepareInput(FrozenContractModel):
    explanation: str = Field(min_length=1, max_length=1000)
    required_action: Literal["maintain", "end", "revoke"] = "maintain"


class CorrectionReviewResultInput(FrozenContractModel):
    confirmation_token: str = Field(min_length=16, max_length=8192)


class CorrectionReviewConfirmInput(CorrectionReviewResultInput):
    confirmed: Literal[True]


T = TypeVar("T")


def create_family_correction_router(
    *,
    identity_dependency: Callable[..., ProductIdentity],
    product: FamilyCorrectionProduct,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/school-fact-product", tags=["school-fact-family-corrections"]
    )

    def call(response: Response, identity: ProductIdentity, work: Callable[[], T]) -> T:
        product_no_store(response)
        try:
            with bind_directory_identity(identity.actor_context_ref):
                return work()
        except FamilyCorrectionDenied:
            raise HTTPException(403, "当前身份不能提交或查看这项报错，请重新核对。") from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "暂时无法核对报错结果，请查询原回执。") from None

    @router.get("/family/corrections/options", response_model=PublicCorrectionOptions)
    def options(
        response: Response,
        identity: Annotated[ProductIdentity, Depends(identity_dependency)],
        request: Annotated[FrameworkRequestContext, Depends(get_framework_request_context)],
    ) -> PublicCorrectionOptions:
        return call(
            response,
            identity,
            lambda: PublicCorrectionOptions(
                items=product.options(identity, request.request_id),
            ),
        )

    @router.post("/family/corrections", response_model=PublicCorrectionReceipt)
    def submit(
        payload: CorrectionSubmitInput,
        response: Response,
        identity: Annotated[ProductIdentity, Depends(identity_dependency)],
        request: Annotated[FrameworkRequestContext, Depends(get_framework_request_context)],
    ) -> PublicCorrectionReceipt:
        return call(
            response,
            identity,
            lambda: product.submit(identity, request.request_id, **payload.model_dump()),
        )

    @router.get("/family/corrections", response_model=PublicCorrectionPage)
    def own_list(
        response: Response,
        identity: Annotated[ProductIdentity, Depends(identity_dependency)],
        request: Annotated[FrameworkRequestContext, Depends(get_framework_request_context)],
        limit: Annotated[int, Query(ge=1, le=20)] = 20,
    ) -> PublicCorrectionPage:
        return call(
            response, identity, lambda: product.own_list(identity, request.request_id, limit=limit)
        )

    @router.get("/family/corrections/by-key/{key}", response_model=PublicCorrectionReceipt)
    def by_key(
        key: UUID,
        response: Response,
        identity: Annotated[ProductIdentity, Depends(identity_dependency)],
        request: Annotated[FrameworkRequestContext, Depends(get_framework_request_context)],
    ) -> PublicCorrectionReceipt:
        result = call(
            response, identity, lambda: product.by_key(identity, request.request_id, key=key)
        )
        if result is None:
            # This is a point-in-time lookup, not permission to resubmit an uncertain command.
            raise HTTPException(404, "尚未查到本次报错回执，请稍后继续查询原记录。")
        return result

    @router.get("/family-facts/corrections", response_model=PublicStaffCorrectionPage)
    def staff_list(
        response: Response,
        target_reference: str,
        field_name: CorrectionField,
        required_action: Literal["maintain", "end", "revoke"],
        identity: Annotated[ProductIdentity, Depends(identity_dependency)],
        request: Annotated[FrameworkRequestContext, Depends(get_framework_request_context)],
        limit: Annotated[int, Query(ge=1, le=20)] = 20,
    ) -> PublicStaffCorrectionPage:
        return call(
            response,
            identity,
            lambda: product.staff_list(
                identity,
                request.request_id,
                target_reference=target_reference,
                field_name=field_name,
                required_action=required_action,
                limit=limit,
            ),
        )

    @router.post(
        "/family-facts/corrections/{report_id}/resolve", response_model=PublicCorrectionReceipt
    )
    def resolve(
        report_id: UUID,
        payload: CorrectionResolveInput,
        response: Response,
        identity: Annotated[ProductIdentity, Depends(identity_dependency)],
        request: Annotated[FrameworkRequestContext, Depends(get_framework_request_context)],
    ) -> PublicCorrectionReceipt:
        return call(
            response,
            identity,
            lambda: product.resolve(
                identity,
                request.request_id,
                report_id=report_id,
                writer_receipt_ref=payload.writer_receipt_ref,
            ),
        )

    @router.post(
        "/family-facts/corrections/{report_id}/prepare-review",
        response_model=CorrectionReviewPreparation,
    )
    def prepare_review(
        report_id: UUID,
        payload: CorrectionReviewPrepareInput,
        response: Response,
        identity: Annotated[ProductIdentity, Depends(identity_dependency)],
        request: Annotated[FrameworkRequestContext, Depends(get_framework_request_context)],
    ) -> CorrectionReviewPreparation:
        return call(
            response,
            identity,
            lambda: product.prepare_review(
                identity, request.request_id, report_id=report_id, **payload.model_dump()
            ),
        )

    @router.post(
        "/family-facts/corrections/{report_id}/confirm-review",
        response_model=PublicCorrectionReceipt,
    )
    def confirm_review(
        report_id: UUID,
        payload: CorrectionReviewConfirmInput,
        response: Response,
        identity: Annotated[ProductIdentity, Depends(identity_dependency)],
        request: Annotated[FrameworkRequestContext, Depends(get_framework_request_context)],
    ) -> PublicCorrectionReceipt:
        return call(
            response,
            identity,
            lambda: product.confirm_review(
                identity, request.request_id, report_id=report_id, **payload.model_dump()
            ),
        )

    @router.post(
        "/family-facts/corrections/{report_id}/review-result",
        response_model=PublicCorrectionReceipt,
    )
    def review_result(
        report_id: UUID,
        payload: CorrectionReviewResultInput,
        response: Response,
        identity: Annotated[ProductIdentity, Depends(identity_dependency)],
        request: Annotated[FrameworkRequestContext, Depends(get_framework_request_context)],
    ) -> PublicCorrectionReceipt:
        return call(
            response,
            identity,
            lambda: product.review_result(
                identity, request.request_id, report_id=report_id, **payload.model_dump()
            ),
        )

    return router
