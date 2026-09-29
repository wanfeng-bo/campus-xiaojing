"""Current F2 school leadership directory, independent of child teaching membership."""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import text

from app.composition.school_fact_family_query import SqlFamilyReadRepository
from app.domains.students.family_read import FamilyReadEvidence
from app.platform.contracts import ActorContext, GuardianActorContext
from app.platform.school_facts.family_leadership_policy import FamilyLeadershipPolicy

_READ = text("""SELECT app.school_fact_family_leadership(
 :school,:adult,:relation,:version,CAST(:school_codes AS text[]),
 CAST(:campus_codes AS text[]),:contacts)""")


class FamilyLeadershipRepository(SqlFamilyReadRepository):
    def __init__(self, session_factory, policy: Callable[[], FamilyLeadershipPolicy]):
        super().__init__(session_factory)
        self.policy = policy

    def leadership_read(self, actor, *, role_kind, include_official_contacts):
        policy = self.policy()
        if policy.school_id != actor.school_id:
            raise ValueError("Leadership vocabulary belongs to another school")
        school_codes, campus_codes = policy.codes(role_kind)
        with self.sessions() as session:
            raw = session.execute(
                _READ,
                self._parameters(actor)
                | {
                    "school_codes": list(school_codes),
                    "campus_codes": list(campus_codes),
                    "contacts": include_official_contacts,
                },
            ).scalar_one()
            if type(raw) is not dict:
                raise ValueError("Invalid leadership projection")
            return raw | {"configuration": policy.fingerprint}


@dataclass(frozen=True)
class FamilyLeadershipResult:
    payload: dict[str, Any]
    evidence: FamilyReadEvidence | None = None
    correction_targets: tuple[dict[str, Any], ...] = ()

    def public(self):
        return {"kind": "family_school_leadership", **deepcopy(self.payload)}


def _fingerprint(value):
    if isinstance(value, dict):
        return {k: _fingerprint(v) for k, v in value.items() if k != "as_of"}
    if isinstance(value, list):
        return [_fingerprint(v) for v in value]
    return value


def _public(raw, contacts):
    leaders = raw["leaders"]
    if type(leaders) is not list or len(leaders) > 32:
        raise ValueError("Leadership bound exceeded")
    allowed = {
        "display_name",
        "position_title",
        "assignment_kind",
        "role_kind",
        "organization",
        "campuses",
        "official_contacts",
        "source",
    }
    contact_fields = {
        "owner_kind",
        "owner_name",
        "contact_kind",
        "contact_value",
        "service_hours",
        "source",
    }

    def source_valid(source):
        return type(source) is dict and set(source) <= {"publication_reference", "published_at"}

    for leader in leaders:
        if type(leader) is not dict or set(leader) - allowed:
            raise ValueError("Overbroad leader projection")
        if not source_valid(leader.get("source", {})):
            raise ValueError("Overbroad leader source")
        for contact in leader.get("official_contacts", []):
            if not contacts or type(contact) is not dict or set(contact) - contact_fields:
                raise ValueError("Unrequested or overbroad contact projection")
            if not source_valid(contact.get("source", {})):
                raise ValueError("Overbroad contact source")
    quality = raw.get("quality", [])
    quality_fields = {
        "source_key",
        "version",
        "quality_kind",
        "coverage_status",
        "dispute_status",
        "source_status",
        "negative_code",
        "verified_at",
        "review_due_at",
        "target_fact_kind",
        "field_name",
        "freshness",
        "applies_to",
        "source",
    }
    if (
        type(quality) is not list
        or len(quality) > 100
        or any(
            type(q) is not dict or set(q) - quality_fields or not source_valid(q.get("source", {}))
            for q in quality
        )
    ):
        raise ValueError("Overbroad quality projection")
    return {
        "kind": "family_school_leadership",
        "owner": "school_facts.f2",
        "status": raw["status"],
        "leaders": deepcopy(leaders),
        "quality": [{k: deepcopy(v) for k, v in q.items() if k != "source_key"} for q in quality],
        "as_of": raw.get("as_of"),
        "channel_meaning": "组织或办公地点的公开渠道，不是个人联系方式；未承诺转接或受理",
    }


class FamilyLeadershipQueryService:
    def __init__(self, *, repository, current_actor, clock=lambda: datetime.now(UTC)):
        self.repository, self.current_actor, self.clock = repository, current_actor, clock

    def _basis(self, actor):
        if type(actor) is not GuardianActorContext or not self.current_actor(actor):
            return None
        basis = self.repository.authority(actor)
        now = self.clock()
        if (
            basis is None
            or basis.relationship_kind != "verified_guardian"
            or not basis.permits(actor, now=now, factual_at=now)
        ):
            return None
        return basis

    def current_read_eligibility(
        self, actor: ActorContext,
    ) -> Literal["available", "denied", "unavailable"]:
        """Check this directory's current basis without reading directory content.

        Composition may use any eligible relationship in the same family scope;
        this decision is neither a grant nor a reusable authorization snapshot.
        The actual read and every released result still recheck their own basis.
        """
        try:
            return "available" if self._basis(actor) is not None else "denied"
        except Exception:
            return "unavailable"

    def query(self, *, actor: ActorContext, role_kind="all", include_official_contacts=False):
        observed_at = self.clock()
        if (
            role_kind not in {"all", "school_leader", "campus_leader"}
            or type(include_official_contacts) is not bool
        ):
            raise ValueError("Invalid leadership request")
        try:
            basis = self._basis(actor)
            if basis is None:
                return FamilyLeadershipResult({"status": "denied"})

            def read():
                return self.repository.leadership_read(
                    actor, role_kind=role_kind, include_official_contacts=include_official_contacts
                )

            raw = read()
            if self._basis(actor) != basis:
                return FamilyLeadershipResult({"status": "denied"})
            if (
                raw.get("student_id") != str(basis.student_id)
                or raw.get("student_version") != basis.student_version
                or raw.get("relationship_version") != basis.relationship_version
            ):
                raise ValueError("Guardian support snapshot changed")
            if raw.get("status") not in {"available", "no_current_records"}:
                return FamilyLeadershipResult({"status": "unavailable"})
            public = _public(raw, include_official_contacts)
            fingerprint = _fingerprint(raw)

            def valid(current):
                return (
                    current == actor
                    and self._basis(current) == basis
                    and _fingerprint(read()) == fingerprint
                    and self._basis(current) == basis
                )

            targets = raw.get("targets", [])
            allowed_targets = {
                "person": {"display_name"},
                "assignment": {"position_title", "person_source_key", "organization_source_key"},
                "official_contact": {"contact_value", "contact_kind", "service_hours"},
            }
            if (
                type(targets) is not list
                or len(targets) > 320
                or any(
                    type(t) is not dict
                    or set(t)
                    != {"fact_kind", "fact_source_key", "field_name", "target_version", "label"}
                    or t["field_name"] not in allowed_targets.get(t["fact_kind"], set())
                    or (t["fact_kind"] == "official_contact" and not include_official_contacts)
                    for t in targets
                )
            ):
                raise ValueError("Overbroad correction targets")
            boundaries = [basis.valid_to] if basis.valid_to is not None else []
            boundaries.extend(
                deadline
                for quality in raw.get("quality", [])
                if quality.get("review_due_at")
                and (deadline := datetime.fromisoformat(quality["review_due_at"])) > observed_at
            )
            return FamilyLeadershipResult(
                public,
                FamilyReadEvidence(valid, (self.clock, observed_at, min(boundaries, default=None))),
                tuple(deepcopy(targets)),
            )
        except Exception:
            return FamilyLeadershipResult({"status": "unavailable"})


def create_family_leadership_query_service(
    *, session_factory, current_actor, policy: Callable[[], FamilyLeadershipPolicy]
):
    return FamilyLeadershipQueryService(
        repository=FamilyLeadershipRepository(session_factory, policy), current_actor=current_actor
    )
