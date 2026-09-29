"""Authenticated selected-child routes; all family rules belong to the domain."""

from collections.abc import Callable
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.composition.school_fact_directory_context import bind_directory_identity
from app.composition.school_fact_family_education_query import FamilyEducationQueryService
from app.composition.school_fact_family_gateway import FamilyProfileInput
from app.composition.school_fact_product_identity import ProductIdentity
from app.domains.students.family_read import FamilyQueryService
from app.interfaces.http.school_fact_product_contacts import product_no_store
from app.platform.assistant.contracts import JsonObject
from app.platform.contracts import GuardianActorContext


class FamilyEducationHttpInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: list[
        Literal[
            "class", "grade", "teachers", "classroom", "positions", "cohorts", "official_contacts"
        ]
    ] = Field(min_length=1, max_length=7)
    as_of: AwareDatetime | None = None


def create_family_profile_router(
    *,
    identity_dependency: Callable[..., ProductIdentity],
    query: FamilyQueryService,
    education: FamilyEducationQueryService | None = None,
    resolve_identity: Callable[[str], ProductIdentity] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/school-fact-product/family", tags=["school-fact-family"])

    @router.post("/profile")
    def profile(
        payload: FamilyProfileInput,
        response: Response,
        identity: Annotated[ProductIdentity, Depends(identity_dependency)],
    ) -> JsonObject:
        product_no_store(response)
        with bind_directory_identity(identity.actor_context_ref):
            return cast(JsonObject, query.query(actor=identity.actor, as_of=payload.as_of).public())

    if education is not None:
        if resolve_identity is None:
            raise ValueError("Education HTTP requires current identity revalidation")

        @router.post("/education")
        def education_lookup(
            payload: FamilyEducationHttpInput,
            response: Response,
            identity: Annotated[ProductIdentity, Depends(identity_dependency)],
        ) -> JsonObject:
            product_no_store(response)
            if type(identity.actor) is not GuardianActorContext:
                raise HTTPException(status_code=403, detail="Selected child required")
            with bind_directory_identity(identity.actor_context_ref):
                result = education.query(
                    actor=identity.actor, fields=tuple(payload.fields), as_of=payload.as_of
                )
                latest = resolve_identity(identity.actor_context_ref)
                if (
                    latest.actor != identity.actor
                    or result.evidence is None
                    or not result.evidence.valid(latest.actor)
                ):
                    raise HTTPException(
                        status_code=403, detail="Current child authorization required"
                    )
                return cast(JsonObject, result.public())

    return router
