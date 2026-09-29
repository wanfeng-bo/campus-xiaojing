"""Direct controlled F5 reads use the same domain services as model tools."""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BeforeValidator

from app.composition.school_fact_directory_context import bind_directory_identity
from app.composition.school_fact_education_reader import EducationReadStale
from app.composition.school_fact_family_leadership_gateway import FamilyLeadershipInput
from app.composition.school_fact_family_teaching_gateway import FamilyTeachingInput
from app.composition.school_fact_product_identity import ProductIdentity
from app.composition.school_fact_teaching_gateway import TeachingQueryInput
from app.interfaces.http.school_fact_product_contacts import product_no_store
from app.platform.authorization.secure_reader_contracts import SecureReadDenied
from app.platform.contracts import GuardianActorContext, StaffActorContext
from app.platform.school_facts.application import SchoolFactChangeRejected


def _family_json(value):
    return FamilyTeachingInput.model_validate_json(json.dumps(value))


def _leadership_json(value):
    return FamilyLeadershipInput.model_validate_json(json.dumps(value))


def _staff_json(value):
    return TeachingQueryInput.model_validate_json(json.dumps(value))


def create_teaching_read_router(
    *, identity_dependency, resolve_identity, family_teaching, leadership, staff_teaching
):
    router = APIRouter(prefix="/api/v1/school-fact-product", tags=["school-fact-teaching"])

    def parent_result(query, payload, identity):
        if type(identity.actor) is not GuardianActorContext:
            raise HTTPException(status_code=403, detail="请先选择获准孩子。")
        with bind_directory_identity(identity.actor_context_ref):
            result = query.query(actor=identity.actor, **payload.model_dump())
            latest = resolve_identity(identity.actor_context_ref)
            if result.payload.get("status") in {"denied", "unavailable"}:
                raise HTTPException(
                    status_code=403 if result.payload["status"] == "denied" else 503,
                    detail="当前身份不可读取。"
                    if result.payload["status"] == "denied"
                    else "本次资料暂时不可读取。",
                )
            if (
                latest.actor != identity.actor
                or result.evidence is None
                or not result.evidence.valid(latest.actor)
            ):
                raise HTTPException(status_code=403, detail="身份或资料已变化，请重新查询。")
            return result.public()

    if family_teaching is not None:

        @router.post("/family/teaching")
        def family_read(
            payload: Annotated[FamilyTeachingInput, BeforeValidator(_family_json)],
            response: Response,
            identity: Annotated[ProductIdentity, Depends(identity_dependency)],
        ):
            product_no_store(response)
            return parent_result(family_teaching, payload, identity)

    if leadership is not None:

        @router.post("/family/leadership")
        def leadership_read(
            payload: Annotated[FamilyLeadershipInput, BeforeValidator(_leadership_json)],
            response: Response,
            identity: Annotated[ProductIdentity, Depends(identity_dependency)],
        ):
            product_no_store(response)
            return parent_result(leadership, payload, identity)

    if staff_teaching is not None:

        @router.post("/teaching/query")
        def staff_read(
            payload: Annotated[TeachingQueryInput, BeforeValidator(_staff_json)],
            response: Response,
            identity: Annotated[ProductIdentity, Depends(identity_dependency)],
        ):
            product_no_store(response)
            if type(identity.actor) is not StaffActorContext:
                raise HTTPException(status_code=403, detail="请先选择获权履职身份。")
            with bind_directory_identity(identity.actor_context_ref):
                try:
                    result = staff_teaching.query(identity.actor, **payload.model_dump())
                    latest = resolve_identity(identity.actor_context_ref)
                    if latest.actor != identity.actor:
                        raise PermissionError("Current identity changed")
                    result.private_proof.revalidate(actor=latest.actor)
                except (
                    PermissionError,
                    SchoolFactChangeRejected,
                    SecureReadDenied,
                    EducationReadStale,
                ):
                    raise HTTPException(
                        status_code=403, detail="当前履职身份无权读取这些资料。"
                    ) from None
                return result.public_result | {"evidence": list(result.evidence)}

    return router
