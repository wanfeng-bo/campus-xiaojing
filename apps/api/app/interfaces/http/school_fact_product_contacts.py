from collections.abc import Callable
from typing import Annotated, Never

from fastapi import APIRouter, Depends, Response

from app.composition.school_fact_assistant_gateway import (
    ContactQueryInput,
    ProductActor,
    QueryRepository,
)
from app.composition.school_fact_directory_product import (
    DirectoryFactProductService,
    FactPreflightRejected,
)
from app.composition.school_fact_product_contacts import (
    ContactChangeInput,
    ContactProductUnavailable,
    ContactProposal,
    ContactQueryView,
    ContactReceipt,
    OfficialContactProductService,
    contact_query_view,
)
from app.interfaces.http.errors import ApiError
from app.interfaces.http.framework_request import (
    FrameworkRequestContext,
    FrameworkRequestPresence,
    get_framework_request_context,
    get_framework_request_presence,
)
from app.platform.authorization.secure_reader_contracts import SecureReadDenied


def product_unavailable(exc: Exception) -> Never:
    if isinstance(exc, (PermissionError, SecureReadDenied)):
        raise ApiError(
            status_code=403, code="school_facts.forbidden", message="当前履职身份未获准此操作。"
        ) from None
    if isinstance(exc, FactPreflightRejected):
        raise ApiError(
            status_code=409, code="school_facts.preflight_rejected", message=exc.reason
        ) from None
    if isinstance(exc, ContactProductUnavailable):
        raise ApiError(
            status_code=409,
            code="school_facts.proposalproduct_unavailable",
            message="候选已失效、事实发生变化或当前身份不能访问，请查询事实或核对原回执。",
        ) from None
    if isinstance(exc, ValueError):
        raise ApiError(
            status_code=422,
            code="school_facts.request_invalid",
            message="请求内容不符合学校事实接口。",
        ) from None
    raise ApiError(
        status_code=503,
        code="school_facts.sourceproduct_unavailable",
        message="学校事实服务暂时不可用。",
    ) from None


def product_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def require_empty_product_request(presence: FrameworkRequestPresence) -> None:
    if presence.has_body or presence.has_query_parameters:
        raise ApiError(
            status_code=422, code="school_facts.request_invalid", message="此操作不接受附加参数。"
        )


def require_receipt_page_request(
    presence: FrameworkRequestPresence, query_keys: set[str]
) -> None:
    """Accept only the fixed pagination selectors on an owner receipt read."""

    if presence.has_body or query_keys - {"target_offset", "target_limit"}:
        raise ApiError(
            status_code=422, code="school_facts.request_invalid", message="此操作不接受附加参数。"
        )


def create_school_fact_product_contact_router(
    *,
    identity_dependency: Callable[..., ProductActor],
    query: QueryRepository,
    contacts: OfficialContactProductService,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/school-fact-product", tags=["school-fact-product"])

    @router.post("/contacts/query", response_model=ContactQueryView)
    def _query_contacts(  # pyright: ignore[reportUnusedFunction]
        payload: ContactQueryInput,
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
    ) -> ContactQueryView:
        product_no_store(response)
        try:
            return contact_query_view(
                query.read_official_contacts(
                    actor=identity.actor,
                    campus_name=payload.campus_name,
                    campus_ref=payload.campus_ref,
                    subject=payload.subject,
                    scope=payload.scope,
                )
            )
        except Exception as exc:
            product_unavailable(exc)

    @router.post("/contacts/prepare", response_model=ContactProposal)
    def _prepare(  # pyright: ignore[reportUnusedFunction]
        payload: ContactChangeInput,
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
        context: Annotated[FrameworkRequestContext, Depends(get_framework_request_context)],
    ) -> ContactProposal:
        product_no_store(response)
        try:
            return contacts.prepare(
                actor=identity.actor,
                session_binding=identity.session_binding,
                payload=payload,
                request_id=context.request_id,
            )
        except Exception as exc:
            product_unavailable(exc)

    @router.post("/contacts/proposals/{proposal_ref}/confirm", response_model=ContactReceipt)
    def _confirm(  # pyright: ignore[reportUnusedFunction]
        proposal_ref: str,
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
    ) -> ContactReceipt:
        product_no_store(response)
        require_empty_product_request(presence)
        try:
            return contacts.confirm(
                actor=identity.actor,
                session_binding=identity.session_binding,
                proposal_ref=proposal_ref,
                request_id=presence.context.request_id,
            )
        except Exception as exc:
            product_unavailable(exc)

    router.include_router(
        create_school_fact_product_receipt_router(
            identity_dependency=identity_dependency, contacts=contacts, prefix=""
        )
    )
    return router


def create_school_fact_product_receipt_router(
    *,
    identity_dependency: Callable[..., ProductActor],
    contacts: OfficialContactProductService | DirectoryFactProductService,
    prefix: str = "/api/v1/school-fact-product",
) -> APIRouter:
    """Preserve receipt URLs while the composition chooses their current authority."""
    router = APIRouter(prefix=prefix, tags=["school-fact-product"])

    @router.get("/receipts/{proposal_ref}", response_model=ContactReceipt)
    def _receipt(  # pyright: ignore[reportUnusedFunction]
        proposal_ref: str,
        response: Response,
        identity: Annotated[ProductActor, Depends(identity_dependency)],
        presence: Annotated[FrameworkRequestPresence, Depends(get_framework_request_presence)],
    ) -> ContactReceipt:
        product_no_store(response)
        require_empty_product_request(presence)
        try:
            return contacts.receipt(
                actor=identity.actor,
                session_binding=identity.session_binding,
                proposal_ref=proposal_ref,
                request_id=presence.context.request_id,
            )
        except Exception as exc:
            product_unavailable(exc)

    return router
