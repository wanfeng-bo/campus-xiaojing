"""Authenticated F2 product routes; no ORM or fact business rule in HTTP."""

import json
from collections.abc import AsyncGenerator, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import AwareDatetime, BeforeValidator, Field

from app.composition.school_fact_assistant_gateway import ProductActor
from app.composition.school_fact_directory_adapter import FACT_LABELS, DirectoryProductAdapter
from app.composition.school_fact_directory_product import (
    DirectoryFactProductService,
    F2FactKind,
    FactChangeInput,
    FactProposal,
    FactQueryInput,
    OwnReceipt,
    OwnReceiptPage,
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
from app.platform.contracts import FrozenContractModel


def _query_json(value: object) -> FactQueryInput:
    return FactQueryInput.model_validate_json(json.dumps(value))


def _change_json(value: object) -> FactChangeInput:
    return FactChangeInput.model_validate_json(json.dumps(value))


class _OwnReceiptQuery(FrozenContractModel):
    limit: int = Field(default=5, strict=False, ge=1, le=20)


def create_directory_product_router(
    *,
    identity_dependency: Callable[..., ProductActor | AsyncGenerator[ProductActor]],
    query: DirectoryProductAdapter,
    product: DirectoryFactProductService,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/school-fact-product/facts", tags=["school-fact-directory"])

    @router.get("/catalog")
    def catalog(  # pyright: ignore[reportUnusedFunction]
        response: Response, identity: Annotated[ProductActor, Depends(identity_dependency)]
    ) -> JsonObject:
        product_no_store(response)
        return {
            "families": [{"value": kind, "label": label} for kind, label in FACT_LABELS.items()]
        }

    @router.get("/access")
    def access(  # pyright: ignore[reportUnusedFunction]
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
        fact_kind: F2FactKind | None = None,
    ) -> JsonObject:
        product_no_store(response)
        try:
            return query.access(actor=identity.actor, fact_kind=fact_kind)
        except Exception as exc:
            product_unavailable(exc)

    @router.get("/describe/{fact_kind}")
    def describe(  # pyright: ignore[reportUnusedFunction]
        fact_kind: F2FactKind,
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
    ) -> JsonObject:
        product_no_store(response)
        try:
            return query.describe(actor=identity.actor, fact_kind=fact_kind)
        except Exception as exc:
            product_unavailable(exc)

    @router.post("/query")
    def read(  # pyright: ignore[reportUnusedFunction]
        payload: Annotated[FactQueryInput, BeforeValidator(_query_json)],
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
    ) -> JsonObject:
        product_no_store(response)
        try:
            return query.query(actor=identity.actor, payload=payload)
        except Exception as exc:
            product_unavailable(exc)

    @router.get("/original/{reference}")
    def original(  # pyright: ignore[reportUnusedFunction]
        reference: str,
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
        as_of: Annotated[AwareDatetime | None, Query()] = None,
    ) -> JsonObject:
        product_no_store(response)
        try:
            return query.original(
                actor=identity.actor, reference=reference, offset=0, limit=100000, as_of=as_of
            )
        except Exception as exc:
            product_unavailable(exc)

    @router.post("/prepare", response_model=FactProposal)
    def prepare(  # pyright: ignore[reportUnusedFunction]
        payload: Annotated[FactChangeInput, BeforeValidator(_change_json)],
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
        context: Annotated[FrameworkRequestContext, Depends(get_framework_request_context)],
    ) -> FactProposal:
        product_no_store(response)
        try:
            return product.prepare(
                actor=identity.actor,
                session_binding=identity.session_binding,
                payload=payload,
                request_id=context.request_id,
            )
        except Exception as exc:
            product_unavailable(exc)

    @router.get("/proposals/{proposal_ref}", response_model=FactProposal)
    def proposal(  # pyright: ignore[reportUnusedFunction]
        proposal_ref: str,
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
    ) -> FactProposal:
        product_no_store(response)
        require_empty_product_request(presence)
        try:
            return product.proposal(
                actor=identity.actor,
                session_binding=identity.session_binding,
                proposal_ref=proposal_ref,
            )
        except Exception as exc:
            product_unavailable(exc)

    @router.post("/proposals/{proposal_ref}/confirm", response_model=ContactReceipt)
    def confirm(  # pyright: ignore[reportUnusedFunction]
        proposal_ref: str,
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
    ) -> ContactReceipt:
        product_no_store(response)
        require_empty_product_request(presence)
        try:
            return product.confirm(
                actor=identity.actor,
                session_binding=identity.session_binding,
                proposal_ref=proposal_ref,
                request_id=presence.context.request_id,
            )
        except Exception as exc:
            product_unavailable(exc)

    @router.get("/history-receipts", response_model=OwnReceiptPage)
    def own_receipts(  # pyright: ignore[reportUnusedFunction]
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
        selection: Annotated[_OwnReceiptQuery, Query()],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
    ) -> OwnReceiptPage:
        product_no_store(response)
        try:
            if presence.has_body:
                raise ValueError("own receipt list does not accept a body")
            return product.own_receipts(
                actor=identity.actor,
                session_binding=identity.session_binding,
                request_id=presence.context.request_id,
                limit=selection.limit,
            )
        except Exception as exc:
            product_unavailable(exc)

    @router.get("/history-receipts/{receipt_ref}", response_model=OwnReceipt)
    def own_receipt(  # pyright: ignore[reportUnusedFunction]
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
                actor=identity.actor,
                session_binding=identity.session_binding,
                receipt_ref=receipt_ref,
                request_id=presence.context.request_id,
                target_offset=target_offset,
                target_limit=target_limit,
            )
        except Exception as exc:
            product_unavailable(exc)

    @router.get("/receipts/{proposal_ref}", response_model=ContactReceipt)
    def receipt(  # pyright: ignore[reportUnusedFunction]
        proposal_ref: str,
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
    ) -> ContactReceipt:
        product_no_store(response)
        require_empty_product_request(presence)
        try:
            return product.receipt(
                actor=identity.actor,
                session_binding=identity.session_binding,
                proposal_ref=proposal_ref,
                request_id=presence.context.request_id,
            )
        except Exception as exc:
            product_unavailable(exc)

    return router
