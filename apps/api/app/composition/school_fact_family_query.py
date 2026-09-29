"""Fixed Reader-role SQL assembly for the current selected child only."""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import cast

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domains.students.family_read import FamilyQueryService, ParentReadBasis
from app.platform.contracts import ActorContext, GuardianActorContext

_AUTHORITY = text("""
SELECT school_id, adult_person_id, relationship_id, relationship_version,
       student_id, student_version, relationship_kind, valid_from, valid_to
FROM app.school_fact_family_authority(:school, :adult, :relation, :version)
""")
_PROFILE = text("""
SELECT app.school_fact_family_profile(
    :school, :adult, :relation, :version, :factual_at
) AS payload
""")
_QUALITY = text("""
SELECT app.school_fact_f7_family_profile_quality(
    :school, :adult, :relation, :version, :factual_at, :fields
) AS payload
""")


class SqlFamilyReadRepository:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.sessions = session_factory

    @staticmethod
    def _parameters(actor: GuardianActorContext) -> dict[str, object]:
        if type(actor) is not GuardianActorContext:
            raise ValueError("Trusted guardian context required")
        return {
            "school": actor.school_id,
            "adult": actor.person_id,
            "relation": actor.guardian_relationship_id,
            "version": actor.relationship_version,
        }

    def authority(self, actor: GuardianActorContext) -> ParentReadBasis | None:
        with self.sessions() as session:
            row = session.execute(_AUTHORITY, self._parameters(actor)).mappings().one_or_none()
            return None if row is None else ParentReadBasis(**dict(row))

    def read_profile(
        self, actor: GuardianActorContext, *, factual_at: datetime | None
    ) -> Mapping[str, object]:
        parameters = self._parameters(actor) | {"factual_at": factual_at}
        # One statement returns one fixed projection. No connection spans a model wait.
        with self.sessions() as session:
            raw = session.execute(_PROFILE, parameters).scalar_one()
            if type(raw) is not dict:
                raise ValueError("Invalid family Reader payload")
            return cast(dict[str, object], raw)

    def read_quality(
        self,
        actor: GuardianActorContext,
        *,
        factual_at: datetime | None,
        fields: tuple[str, ...],
    ) -> Mapping[str, object]:
        if not fields or len(fields) != len(set(fields)):
            raise ValueError("Exact family quality fields required")
        parameters = self._parameters(actor) | {
            "factual_at": factual_at, "fields": list(fields),
        }
        with self.sessions() as session:
            raw = session.execute(_QUALITY, parameters).scalar_one()
            if type(raw) is not dict:
                raise ValueError("Invalid family quality Reader payload")
            return cast(dict[str, object], raw)


def create_family_query_service(
    *,
    session_factory: Callable[[], Session],
    current_actor: Callable[[ActorContext], bool],
    enable_quality: bool = False,
) -> FamilyQueryService:
    return FamilyQueryService(
        repository=SqlFamilyReadRepository(session_factory), current_actor=current_actor,
        quality_enabled=enable_quality,
    )
