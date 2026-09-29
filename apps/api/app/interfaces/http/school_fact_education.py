# pyright: reportUnusedFunction=false
"""Education routes delegating confirmation and receipts to the existing sole Writer."""

import json
from collections.abc import AsyncGenerator, Callable
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BeforeValidator

from app.composition.school_fact_assistant_gateway import ProductActor
from app.composition.school_fact_directory_product import (
    DirectoryFactProductService,
    FactProposal,
    FactReferenceChoicesInput,
    OwnReceipt,
    OwnReceiptPage,
)
from app.composition.school_fact_education_gateway import EducationQueryInput
from app.composition.school_fact_education_product import EDUCATION_LABELS, EducationBatchInput
from app.composition.school_fact_education_query import EducationQueryService
from app.composition.school_fact_education_reader import EducationCancellationRequiresCorrection
from app.composition.school_fact_education_views import (
    EducationMaintenanceViews,
    EducationRecordsInput,
)
from app.composition.school_fact_product_contacts import ContactReceipt
from app.domains.students.education_contracts import EDUCATION_FACT_KINDS, F4_EDUCATION_KINDS
from app.interfaces.http.errors import ApiError
from app.interfaces.http.framework_request import (
    FrameworkRequestContext,
    FrameworkRequestPresence,
    get_framework_request_context,
    get_framework_request_presence,
)
from app.interfaces.http.school_fact_product_contacts import (
    product_no_store,
    product_unavailable,
    require_empty_product_request,
    require_receipt_page_request,
)
from app.platform.assistant.contracts import JsonObject
from app.platform.contracts import StaffActorContext

EDUCATION_MAINTENANCE_KINDS = EDUCATION_FACT_KINDS | {"student_school_relationship"}


def _change_json(value: object) -> EducationBatchInput:
    return EducationBatchInput.model_validate_json(json.dumps(value))


def _query_json(value: object) -> EducationQueryInput:
    return EducationQueryInput.model_validate_json(json.dumps(value))


def create_education_product_router(
    *,
    identity_dependency: Callable[..., ProductActor | AsyncGenerator[ProductActor]],
    query: EducationMaintenanceViews,
    product: DirectoryFactProductService,
    education_query: EducationQueryService,
    teaching_enabled: bool = False,
) -> APIRouter:
    enabled_kinds = (
        EDUCATION_MAINTENANCE_KINDS
        if teaching_enabled
        else F4_EDUCATION_KINDS | {"student_school_relationship"}
    )
    # F4-owned quality keeps the existing education dependency resolver and
    # final Writer guard; this does not open the policy-scope maintenance route.
    maintain_kinds = enabled_kinds | {"fact_quality"}
    router = APIRouter(
        prefix="/api/v1/school-fact-product/education-facts", tags=["school-fact-education"]
    )

    def staff(identity: ProductActor) -> StaffActorContext:
        if type(cast(object, identity.actor)) is not StaffActorContext:
            raise PermissionError("Staff identity required")
        return identity.actor

    @router.get("/catalog")
    def catalog(
        response: Response, identity: Annotated[ProductActor, Depends(identity_dependency)]
    ) -> JsonObject:
        product_no_store(response)
        try:
            staff(identity)
            return {
                "families": [
                    {
                        "value": kind,
                        "label": (
                            EDUCATION_LABELS | {"student_school_relationship": "学生在校关系"}
                        )[kind],
                    }
                    for kind in sorted(enabled_kinds)
                ]
            }
        except Exception as exc:
            product_unavailable(exc)

    @router.get("/describe/{fact_kind}")
    def describe(
        fact_kind: str,
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
    ) -> JsonObject:
        product_no_store(response)
        try:
            if fact_kind not in maintain_kinds:
                raise ValueError("unsupported education fact kind")
            return query.describe(actor=staff(identity), fact_kind=fact_kind)
        except Exception as exc:
            product_unavailable(exc)

    @router.post("/choices")
    def reference_choices(
        payload: FactReferenceChoicesInput,
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
    ) -> JsonObject:
        product_no_store(response)
        try:
            if payload.fact_kind not in maintain_kinds:
                raise PermissionError("Unsupported maintenance reference owner")
            return query.reference_choices(
                actor=staff(identity), fact_kind=payload.fact_kind, field_name=payload.field_name
            )
        except Exception as exc:
            product_unavailable(exc)

    @router.post("/query")
    def read(
        payload: EducationRecordsInput,
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
    ) -> JsonObject:
        product_no_store(response)
        try:
            # F4 already exposes these two finite dependency projections.
            if payload.fact_kind not in enabled_kinds | {"fact_quality", "policy_scope"}:
                raise PermissionError("Unsupported or disabled education query kind")
            return query.query(
                actor=staff(identity),
                fact_kind=payload.fact_kind,
                query_text=payload.query_text,
                reference=payload.reference,
                related_field=payload.related_field,
                related_ref=payload.related_ref,
                as_of=payload.as_of,
                academic_year=payload.academic_year,
                campus_ref=payload.campus_ref,
                position_ref=payload.position_ref,
                limit=payload.limit,
            )
        except Exception as exc:
            product_unavailable(exc)

    @router.post("/lookup")
    def lookup(
        payload: Annotated[EducationQueryInput, BeforeValidator(_query_json)],
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
    ) -> JsonObject:
        product_no_store(response)
        try:
            result = education_query.query(staff(identity), **payload.model_dump())
            result.private_proof.revalidate(actor=staff(identity))
            return result.public_result
        except Exception as exc:
            product_unavailable(exc)

    @router.post("/prepare", response_model=FactProposal)
    def prepare(
        payload: Annotated[EducationBatchInput, BeforeValidator(_change_json)],
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
        context: Annotated[FrameworkRequestContext, Depends(get_framework_request_context)],
    ) -> FactProposal:
        product_no_store(response)
        try:
            if payload.fact_kind not in maintain_kinds:
                raise ValueError("Education endpoint accepts only approved education changes")
            return product.prepare(
                actor=staff(identity),
                session_binding=identity.session_binding,
                payload=payload,
                request_id=context.request_id,
            )
        except EducationCancellationRequiresCorrection:
            raise ApiError(
                status_code=409,
                code=EducationCancellationRequiresCorrection.code,
                message=EducationCancellationRequiresCorrection.message,
            ) from None
        except Exception as exc:
            product_unavailable(exc)

    @router.get("/proposals/{proposal_ref}", response_model=FactProposal)
    def proposal(
        proposal_ref: str,
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
    ) -> FactProposal:
        product_no_store(response)
        require_empty_product_request(presence)
        try:
            return product.proposal(
                actor=staff(identity),
                session_binding=identity.session_binding,
                proposal_ref=proposal_ref,
            )
        except EducationCancellationRequiresCorrection:
            raise ApiError(
                status_code=409,
                code=EducationCancellationRequiresCorrection.code,
                message=EducationCancellationRequiresCorrection.message,
            ) from None
        except Exception as exc:
            product_unavailable(exc)

    @router.post("/proposals/{proposal_ref}/confirm", response_model=ContactReceipt)
    def confirm(
        proposal_ref: str,
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
    ) -> ContactReceipt:
        product_no_store(response)
        require_empty_product_request(presence)
        try:
            return product.confirm(
                actor=staff(identity),
                session_binding=identity.session_binding,
                proposal_ref=proposal_ref,
                request_id=presence.context.request_id,
            )
        except Exception as exc:
            product_unavailable(exc)

    @router.get("/history-receipts", response_model=OwnReceiptPage)
    def own_receipts(
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
        limit: int = 5,
    ) -> OwnReceiptPage:
        product_no_store(response)
        try:
            if presence.has_body or not 1 <= limit <= 20:
                raise ValueError("invalid own receipt request")
            return product.own_receipts(
                actor=staff(identity),
                session_binding=identity.session_binding,
                request_id=presence.context.request_id,
                limit=limit,
            )
        except Exception as exc:
            product_unavailable(exc)

    @router.get("/history-receipts/{receipt_ref}", response_model=OwnReceipt)
    def own_receipt(
        receipt_ref: str,
        response: Response,
        request: Request,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
        target_offset: int = Query(default=0, ge=0, le=50_000),
        target_limit: int = Query(default=20, ge=1, le=20),
    ) -> OwnReceipt:
        product_no_store(response)
        require_receipt_page_request(presence, set(request.query_params))
        try:
            return product.own_receipt(
                actor=staff(identity),
                session_binding=identity.session_binding,
                receipt_ref=receipt_ref,
                request_id=presence.context.request_id,
                target_offset=target_offset,
                target_limit=target_limit,
            )
        except Exception as exc:
            product_unavailable(exc)

    @router.get("/receipts/{proposal_ref}", response_model=ContactReceipt)
    def receipt(
        proposal_ref: str,
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
    ) -> ContactReceipt:
        product_no_store(response)
        require_empty_product_request(presence)
        try:
            return product.receipt(
                actor=staff(identity),
                session_binding=identity.session_binding,
                proposal_ref=proposal_ref,
                request_id=presence.context.request_id,
            )
        except Exception as exc:
            product_unavailable(exc)

    return router


__all__ = ["create_education_product_router"]
