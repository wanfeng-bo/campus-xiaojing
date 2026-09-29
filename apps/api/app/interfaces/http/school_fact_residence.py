"""F6 HTTP adapters, with the existing identity, confirmation and receipt controls."""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import AwareDatetime, BeforeValidator

from app.composition.school_fact_directory_context import bind_directory_identity
from app.composition.school_fact_directory_product import (
    FactProposal,
    FactReferenceChoicesInput,
    OwnReceipt,
    OwnReceiptPage,
)
from app.composition.school_fact_product_contacts import ContactReceipt
from app.composition.school_fact_product_identity import ProductIdentity
from app.composition.school_fact_residence_product import RESIDENCE_LABELS, ResidenceBatchInput
from app.composition.school_fact_residence_views import ResidenceRecordsInput
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
from app.platform.contracts import GuardianActorContext, StaffActorContext


def change_json(value: Any) -> Any:
    return ResidenceBatchInput.model_validate_json(json.dumps(value))


def records_json(value: Any) -> Any:
    return ResidenceRecordsInput.model_validate_json(json.dumps(value))


def create_residence_product_router(*, identity_dependency: Any, views: Any, product: Any) -> Any:
    router = APIRouter(
        prefix="/api/v1/school-fact-product/residence-facts", tags=["school-fact-residence"]
    )

    def staff(identity: Any) -> Any:
        if type(identity.actor) is not StaffActorContext:
            raise PermissionError("当前不是获权教职工身份。")
        return identity.actor

    @router.get("/catalog")
    def catalog(
        response: Response, identity: Annotated[ProductIdentity, Depends(identity_dependency)]
    ) -> Any:
        product_no_store(response)
        try:
            with bind_directory_identity(identity.actor_context_ref):
                staff(identity)
                labels = (
                    {**RESIDENCE_LABELS, "fact_quality": "寄宿事实依据与质量"}
                    if views.quality is not None
                    else RESIDENCE_LABELS
                )
                if views.eligibility is not None:
                    labels = {**labels, "student_school_relationship": "在校与寄宿方式"}
                return {
                    "families": [{"value": kind, "label": label} for kind, label in labels.items()]
                }
        except Exception as exc:
            product_unavailable(exc)

    @router.get("/describe/{fact_kind}")
    def describe(
        fact_kind: str,
        response: Response,
        identity: Annotated[ProductIdentity, Depends(identity_dependency)],
    ) -> Any:
        product_no_store(response)
        try:
            with bind_directory_identity(identity.actor_context_ref):
                return views.describe(actor=staff(identity), fact_kind=fact_kind)
        except Exception as exc:
            product_unavailable(exc)

    @router.post("/choices")
    def choices(
        payload: FactReferenceChoicesInput,
        response: Response,
        identity: Annotated[ProductIdentity, Depends(identity_dependency)],
    ) -> Any:
        product_no_store(response)
        try:
            with bind_directory_identity(identity.actor_context_ref):
                return views.reference_choices(
                    actor=staff(identity),
                    fact_kind=payload.fact_kind,
                    field_name=payload.field_name,
                )
        except Exception as exc:
            product_unavailable(exc)

    @router.post("/query")
    def read(
        payload: Annotated[ResidenceRecordsInput, BeforeValidator(records_json)],
        response: Response,
        identity: Annotated[ProductIdentity, Depends(identity_dependency)],
    ) -> Any:
        product_no_store(response)
        try:
            with bind_directory_identity(identity.actor_context_ref):
                return views.query(actor=staff(identity), **payload.model_dump())
        except Exception as exc:
            product_unavailable(exc)

    @router.post("/prepare", response_model=FactProposal)
    def prepare(
        payload: Annotated[ResidenceBatchInput, BeforeValidator(change_json)],
        response: Response,
        identity: Annotated[ProductIdentity, Depends(identity_dependency)],
        context: Annotated[FrameworkRequestContext, Depends(get_framework_request_context)],
    ) -> Any:
        product_no_store(response)
        try:
            with bind_directory_identity(identity.actor_context_ref):
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
        identity: Annotated[ProductIdentity, Depends(identity_dependency)],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
    ) -> Any:
        product_no_store(response)
        require_empty_product_request(presence)
        try:
            with bind_directory_identity(identity.actor_context_ref):
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
        identity: Annotated[ProductIdentity, Depends(identity_dependency)],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
    ) -> Any:
        product_no_store(response)
        require_empty_product_request(presence)
        try:
            with bind_directory_identity(identity.actor_context_ref):
                return product.confirm(
                    actor=staff(identity),
                    session_binding=identity.session_binding,
                    proposal_ref=proposal_ref,
                    request_id=presence.context.request_id,
                )
        except Exception as exc:
            product_unavailable(exc)

    @router.get("/receipts/{proposal_ref}", response_model=ContactReceipt)
    def receipt(
        proposal_ref: str,
        response: Response,
        identity: Annotated[ProductIdentity, Depends(identity_dependency)],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
    ) -> Any:
        product_no_store(response)
        require_empty_product_request(presence)
        try:
            with bind_directory_identity(identity.actor_context_ref):
                return product.receipt(
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
        identity: Annotated[ProductIdentity, Depends(identity_dependency)],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
        limit: Annotated[int, Query(ge=1, le=20)] = 5,
    ) -> Any:
        product_no_store(response)
        try:
            with bind_directory_identity(identity.actor_context_ref):
                if presence.has_body:
                    raise ValueError("回执查询不接受请求正文。")
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
        identity: Annotated[ProductIdentity, Depends(identity_dependency)],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
        target_offset: int = Query(default=0, ge=0, le=50_000),
        target_limit: int = Query(default=20, ge=1, le=20),
    ) -> Any:
        product_no_store(response)
        require_receipt_page_request(presence, set(request.query_params))
        try:
            with bind_directory_identity(identity.actor_context_ref):
                return product.own_receipt(
                    actor=staff(identity),
                    session_binding=identity.session_binding,
                    request_id=presence.context.request_id,
                    receipt_ref=receipt_ref,
                    target_offset=target_offset,
                    target_limit=target_limit,
                )
        except Exception as exc:
            product_unavailable(exc)

    return router


def create_family_residence_router(
    *, identity_dependency: Any, query: Any, resolve_identity: Any
) -> Any:
    router = APIRouter(
        prefix="/api/v1/school-fact-product/family", tags=["school-fact-family-residence"]
    )

    @router.get("/residence")
    def residence(
        response: Response,
        identity: Annotated[ProductIdentity, Depends(identity_dependency)],
        as_of: Annotated[AwareDatetime | None, Query()] = None,
    ) -> Any:
        product_no_store(response)
        try:
            current = resolve_identity(identity.actor_context_ref)
            if type(current.actor) is not GuardianActorContext or current.actor != identity.actor:
                raise PermissionError("请重新选择获准孩子。")
            with bind_directory_identity(identity.actor_context_ref):
                result = query.query(actor=current.actor, as_of=as_of)
                public = result.public()
                if public.get("status") in {"denied", "unavailable"}:
                    raise HTTPException(
                        status_code=403 if public["status"] == "denied" else 503,
                        detail="当前身份不可读取。"
                        if public["status"] == "denied"
                        else "本次资料暂时不可读取。",
                    )
                latest = resolve_identity(identity.actor_context_ref)
                if (
                    latest.actor != current.actor
                    or result.evidence is None
                    or not result.evidence.valid(latest.actor)
                ):
                    raise PermissionError("当前身份或孩子资料已变化。")
                return public
        except HTTPException:
            raise
        except Exception as exc:
            product_unavailable(exc)

    return router
