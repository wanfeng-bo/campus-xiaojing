"""Authenticated F3 Staff family-fact product routes."""

import json
from collections.abc import AsyncGenerator, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BeforeValidator

from app.composition.school_fact_assistant_gateway import ProductActor
from app.composition.school_fact_directory_product import (
    DirectoryFactProductService,
    FactChangeInput,
    FactProposal,
    FactReferenceChoicesInput,
    OwnReceipt,
    OwnReceiptPage,
)
from app.composition.school_fact_family_staff_gateway import (
    FAMILY_FACT_LABELS,
    FamilyReadInput,
    FamilyStaffQueryPort,
)
from app.composition.school_fact_product_contacts import ContactReceipt
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
from app.platform.school_facts.f3_policy import F3_FACT_KINDS


def _change_json(value: object) -> FactChangeInput:
    return FactChangeInput.model_validate_json(json.dumps(value))


def create_family_staff_product_router(
    *,
    identity_dependency: Callable[..., ProductActor | AsyncGenerator[ProductActor]],
    query: FamilyStaffQueryPort,
    product: DirectoryFactProductService,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/school-fact-product/family-facts", tags=["school-fact-family-staff"]
    )

    def staff(identity: ProductActor) -> StaffActorContext:
        if not isinstance(identity.actor, StaffActorContext):
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
                    {"value": kind, "label": FAMILY_FACT_LABELS[kind]}
                    for kind in sorted(F3_FACT_KINDS)
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
            if fact_kind not in F3_FACT_KINDS:
                raise ValueError("unsupported F3 fact kind")
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
            if payload.fact_kind not in F3_FACT_KINDS:
                raise PermissionError("Unsupported maintenance reference owner")
            return query.reference_choices(
                actor=staff(identity), fact_kind=payload.fact_kind, field_name=payload.field_name
            )
        except Exception as exc:
            product_unavailable(exc)

    @router.post("/query")
    def read(
        payload: FamilyReadInput,
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
    ) -> JsonObject:
        product_no_store(response)
        try:
            if payload.fact_kind not in F3_FACT_KINDS:
                raise ValueError("unsupported F3 fact kind")
            return query.query(
                actor=staff(identity),
                fact_kind=payload.fact_kind,
                query_text=payload.query_text,
                student_number=payload.student_number,
                reference=payload.reference,
                related_field=payload.related_field,
                related_ref=payload.related_ref,
                as_of=payload.as_of,
                limit=payload.limit,
            )
        except Exception as exc:
            product_unavailable(exc)

    @router.post("/prepare", response_model=FactProposal)
    def prepare(
        payload: Annotated[FactChangeInput, BeforeValidator(_change_json)],
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
        context: Annotated[FrameworkRequestContext, Depends(get_framework_request_context)],
    ) -> FactProposal:
        product_no_store(response)
        try:
            if payload.fact_kind not in F3_FACT_KINDS:
                raise ValueError("F3 endpoint accepts only family facts")
            return product.prepare(
                actor=staff(identity),
                session_binding=identity.session_binding,
                payload=payload,
                request_id=context.request_id,
            )
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


__all__ = ["create_family_staff_product_router"]
