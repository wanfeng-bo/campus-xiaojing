"""Current verified family relationship authority and its minimal student read contract.

The repository owns a fixed SQL projection. This domain never accepts a student,
field list or permission supplied by the browser/model and never issues a Grant.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol, cast
from uuid import UUID

from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.platform.contracts import ActorContext, GuardianActorContext

FamilyStatus = Literal[
    "available", "no_current_records", "conflict", "denied", "source_unavailable"
]


@dataclass(frozen=True, slots=True, repr=False)
class ParentReadBasis:
    school_id: UUID
    adult_person_id: UUID
    relationship_id: UUID
    relationship_version: int
    student_id: UUID
    student_version: int
    relationship_kind: str
    valid_from: datetime
    valid_to: datetime | None

    def permits(self, actor: ActorContext, *, now: datetime, factual_at: datetime) -> bool:
        return (
            type(actor) is GuardianActorContext
            and self.school_id == actor.school_id
            and self.adult_person_id == actor.person_id
            and self.relationship_id == actor.guardian_relationship_id
            and self.relationship_version == actor.relationship_version
            and self.student_version >= 1
            and self.valid_from <= now
            and (self.valid_to is None or now < self.valid_to)
            and self.valid_from <= factual_at <= now
            and (self.valid_to is None or factual_at < self.valid_to)
            and self.relationship_kind in {"verified_guardian", "recognized_caregiver"}
            and (self.relationship_kind == "verified_guardian" or factual_at == now)
        )


class FamilyReadRepository(Protocol):
    def authority(self, actor: GuardianActorContext) -> ParentReadBasis | None: ...

    def read_profile(
        self, actor: GuardianActorContext, *, factual_at: datetime | None
    ) -> Mapping[str, object]: ...

    def read_quality(
        self,
        actor: GuardianActorContext,
        *,
        factual_at: datetime | None,
        fields: tuple[str, ...],
    ) -> Mapping[str, object]: ...


class ParentReadAuthorizationAdapter:
    def __init__(
        self, repository: FamilyReadRepository, current_actor: Callable[[ActorContext], bool]
    ) -> None:
        self.repository, self.current_actor = repository, current_actor

    def decide(
        self, actor: ActorContext, *, checked_at: datetime, factual_at: datetime
    ) -> ParentReadBasis | None:
        if type(actor) is not GuardianActorContext or not self.current_actor(actor):
            return None
        basis = self.repository.authority(actor)
        if basis is None or not basis.permits(actor, now=checked_at, factual_at=factual_at):
            return None
        return basis

    def revalidate(
        self,
        basis: ParentReadBasis,
        actor: GuardianActorContext,
        *,
        checked_at: datetime,
        factual_at: datetime,
    ) -> bool:
        return self.decide(actor, checked_at=checked_at, factual_at=factual_at) == basis


@dataclass(frozen=True, slots=True, repr=False)
class FamilyReadEvidence:
    _valid: Callable[[ActorContext], bool]
    time_window: tuple[Callable[[], datetime], datetime, datetime | None] | None = None

    def valid(self, actor: ActorContext) -> bool:
        try:
            if self._valid(actor) is not True:
                return False
            if self.time_window is not None:
                clock, observed_at, valid_before = self.time_window
                now = clock()
                return observed_at <= now and (valid_before is None or now < valid_before)
            return True
        except Exception:
            return False


@dataclass(frozen=True, slots=True, repr=False)
class FamilyQueryResult:
    status: FamilyStatus
    profile: dict[str, object]
    sources: tuple[dict[str, object], ...]
    missing: tuple[str, ...]
    as_of: datetime
    queried_at: datetime
    relationship_kind: str | None = None
    conflicting_fields: tuple[str, ...] = ()
    evidence: FamilyReadEvidence | None = field(default=None, repr=False)
    quality: dict[str, object] | None = None
    quality_version: int | None = field(default=None, repr=False)

    def public(self) -> dict[str, object]:
        public = {
            "kind": "family_student_profile",
            "status": self.status,
            "query_scope": "本次目标孩子及本次获准字段",
            "relationship_kind": self.relationship_kind,
            "profile": deepcopy(self.profile),
            "sources": deepcopy(list(self.sources)),
            "authorized_fields": (
                [
                    "name",
                    "enrollment_status",
                    "campus",
                    "student_number",
                    "residence_mode",
                    "region",
                ]
                if self.relationship_kind == "verified_guardian"
                else ["name", "enrollment_status", "campus"]
                if self.relationship_kind == "recognized_caregiver"
                else []
            ),
            "field_restrictions": (
                {
                    "student_number": "denied",
                    "residence_mode": "denied",
                    "region": "denied",
                    "history": "denied",
                }
                if self.relationship_kind == "recognized_caregiver"
                else {}
            ),
            "missing": list(self.missing),
            "conflicting_fields": list(self.conflicting_fields),
            "as_of": self.as_of.isoformat(),
            "queried_at": self.queried_at.isoformat(),
            "coverage": {"status": "unknown", "meaning": "未声明学校资料已完整覆盖"},
        }
        if self.quality is not None:
            public["quality"] = deepcopy(self.quality)
        return public


def _digest(result: FamilyQueryResult) -> str:
    public = result.public()
    public.pop("queried_at")
    public.pop("as_of")
    public["quality_version"] = result.quality_version
    return hashlib.sha256(
        json.dumps(public, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


_FAMILY_FIELDS = (
    "name", "enrollment_status", "campus", "student_number", "residence_mode", "region"
)
_QUALITY_KINDS = {"source", "coverage", "dispute", "review", "correction"}


def _quality_payload(
    raw: Mapping[str, object], *, fields: tuple[str, ...], factual_at: datetime
) -> tuple[dict[str, object], int]:
    if set(raw) != {"school_fact_version", "claims"}:
        raise ValueError("Invalid family quality projection")
    version, claims = raw["school_fact_version"], raw["claims"]
    if type(version) is not int or version < 0 or type(claims) is not list or len(claims) > 100:
        raise ValueError("Invalid family quality version or count")
    quality: dict[str, object] = {
        field: {"status": "no_current_claims", "risks": []} for field in fields
    }
    for item in claims:
        if type(item) is not dict or not set(item) <= {
            "field_name", "quality_kind", "source_status", "coverage_status",
            "dispute_status", "review_due_at",
        }:
            raise ValueError("Invalid family quality claim")
        claim = cast(dict[str, object], item)
        name, kind = claim.get("field_name"), claim.get("quality_kind")
        if name not in fields or kind not in _QUALITY_KINDS:
            raise ValueError("Family quality exceeded selected field")
        status_keys = {
            "source": ("source_status", {"available", "unavailable"}),
            "coverage": ("coverage_status", {"partial", "complete"}),
            "dispute": ("dispute_status", {"open", "resolved"}),
        }
        if kind in status_keys:
            state_name, values = status_keys[cast(str, kind)]
            if claim.get(state_name) not in values:
                raise ValueError("Invalid family quality state")
        elif kind == "review":
            due = claim.get("review_due_at")
            if type(due) is not str:
                raise ValueError("Invalid family quality review time")
            parsed = datetime.fromisoformat(due)
            if parsed.utcoffset() is None:
                raise ValueError("Invalid family quality review timezone")
        state_field = (
            status_keys[cast(str, kind)][0]
            if kind in status_keys else "review_due_at" if kind == "review" else None
        )
        if any(
            claim.get(key) is not None
            for key in {"source_status", "coverage_status", "dispute_status", "review_due_at"}
            - ({state_field} if state_field is not None else set())
        ):
            raise ValueError("Mixed family quality claim states")
        entry = cast(dict[str, object], quality[cast(str, name)])
        entry["status"] = "claims"
        risks = cast(list[str], entry["risks"])
        risk = None
        if kind == "source" and claim.get("source_status") == "unavailable":
            risk = "这项资料的来源目前无法核实"
        elif kind == "coverage" and claim.get("coverage_status") == "partial":
            risk = "学校声明这项资料登记未覆盖完整范围"
        elif kind == "dispute" and claim.get("dispute_status") == "open":
            risk = "学校登记了这项资料的争议"
        elif kind == "review" and datetime.fromisoformat(
            cast(str, claim["review_due_at"])
        ) <= factual_at:
            risk = "这项资料已超过登记的复核时间"
        if risk is not None and risk not in risks:
            risks.append(risk)
    return quality, cast(int, version)


def _payload(
    raw: Mapping[str, object], basis: ParentReadBasis
) -> tuple[
    FamilyStatus, dict[str, object], tuple[dict[str, object], ...], tuple[str, ...], tuple[str, ...]
]:
    if set(raw) != {"status", "profile", "sources", "missing", "conflicting_fields"}:
        raise ValueError("Invalid family projection")
    status = raw["status"]
    if status not in {"available", "no_current_records", "conflict"}:
        raise ValueError("Invalid family projection status")
    if (
        type(raw["profile"]) is not dict
        or type(raw["sources"]) is not list
        or type(raw["missing"]) is not list
        or type(raw["conflicting_fields"]) is not list
    ):
        raise ValueError("Invalid family projection shape")
    profile = cast(dict[str, object], raw["profile"])
    raw_sources = cast(list[object], raw["sources"])
    raw_missing = cast(list[object], raw["missing"])
    raw_conflicts = cast(list[object], raw["conflicting_fields"])
    fields = {"name", "enrollment_status", "campus"}
    if basis.relationship_kind == "verified_guardian":
        fields |= {"student_number", "residence_mode", "region"}
    if set(profile) - fields or len(raw_sources) > 16:
        raise ValueError("Overbroad family projection")
    if any(type(v) is not str or v not in fields for v in raw_missing):
        raise ValueError("Invalid missing-field projection")
    if any(type(v) is not str or v not in fields for v in raw_conflicts) or (
        set(cast(list[str], raw_conflicts)) & set(cast(list[str], raw_missing))
    ):
        raise ValueError("Conflicting fields cannot be missing")
    conflicts = set(cast(list[str], raw_conflicts))
    missing = set(cast(list[str], raw_missing))
    if len(conflicts) != len(raw_conflicts) or len(missing) != len(raw_missing):
        raise ValueError("Repeated family field state")
    if (status == "conflict") != bool(conflicts):
        raise ValueError("Family conflict status does not match its fields")
    if any(profile.get(key) is not None for key in conflicts | missing):
        raise ValueError("An uncertain family field cannot publish a chosen value")
    if status == "no_current_records" and any(value is not None for value in profile.values()):
        raise ValueError("Empty family status contains a fact")
    for key, value in profile.items():
        if key == "region" and value is not None:
            if type(value) is not dict:
                raise ValueError("Invalid district projection")
            district = cast(dict[str, object], value)
            if set(district) != {"district_code", "district_name"} or any(
                type(v) is not str or not v or len(v) > 120 for v in district.values()
            ):
                raise ValueError("Invalid district projection")
        elif value is not None and (type(value) is not str or len(value) > 200):
            raise ValueError("Invalid family field")
    sources: list[dict[str, object]] = []
    for raw_source in raw_sources:
        if type(raw_source) is not dict:
            raise ValueError("Invalid family source")
        source = cast(dict[str, object], raw_source)
        if set(source) != {
            "fact",
            "reference",
            "published_at",
            "channel",
            "source_name",
            "source_trace_reference",
        }:
            raise ValueError("Invalid family source")
        for key, value in source.items():
            if key in {"source_name", "source_trace_reference"} and value is None:
                continue
            if type(value) is not str or len(value) > (
                1000 if key in {"source_name", "source_trace_reference"} else 150
            ):
                raise ValueError("Invalid family source")
        sources.append(source)
    # Reject a corrupt/overbroad SQL projection; do not silently post-filter it.
    return (
        cast(FamilyStatus, status),
        deepcopy(profile),
        tuple(deepcopy(sources)),
        tuple(cast(list[str], raw_missing)),
        tuple(cast(list[str], raw_conflicts)),
    )


class FamilyQueryService:
    def __init__(
        self,
        *,
        repository: FamilyReadRepository,
        current_actor: Callable[[ActorContext], bool],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        quality_enabled: bool = False,
    ) -> None:
        self.repository, self.clock = repository, clock
        self.quality_enabled = quality_enabled
        self.authorization = ParentReadAuthorizationAdapter(repository, current_actor)

    def query(
        self,
        *,
        actor: ActorContext,
        as_of: datetime | None = None,
        quality_fields: tuple[str, ...] | None = None,
        _with_evidence: bool = True,
    ) -> FamilyQueryResult:
        now = self.clock()
        at = now if as_of is None else as_of

        def denied(status: FamilyStatus) -> FamilyQueryResult:
            return FamilyQueryResult(status, {}, (), (), at, now)

        if type(actor) is not GuardianActorContext or at.utcoffset() is None:
            return denied("denied")
        try:
            basis = self.authorization.decide(actor, checked_at=now, factual_at=at)
            if basis is None or (
                basis.relationship_kind == "recognized_caregiver" and as_of is not None
            ):
                return denied("denied")
            raw = self.repository.read_profile(actor, factual_at=None if as_of is None else at)
            status, profile, sources, missing, conflicts = _payload(raw, basis)
            quality: dict[str, object] | None = None
            quality_version: int | None = None
            if self.quality_enabled:
                if quality_fields is not None and (
                    len(quality_fields) != len(set(quality_fields))
                    or not set(quality_fields) <= set(_FAMILY_FIELDS)
                ):
                    raise ValueError("Invalid selected family quality fields")
                allowed = (
                    _FAMILY_FIELDS if basis.relationship_kind == "verified_guardian"
                    else _FAMILY_FIELDS[:3]
                )
                selected = tuple(
                    field for field in (quality_fields or allowed)
                    if field in allowed and field in profile
                )
                if selected:
                    quality, quality_version = _quality_payload(
                        self.repository.read_quality(
                            actor, factual_at=None if as_of is None else at, fields=selected
                        ),
                        fields=selected,
                        factual_at=at,
                    )
            # Neither formal read nor independent quality may cross the current
            # relationship boundary. This check runs after both Reader calls.
            after = self.clock()
            recheck_at = after if as_of is None else at
            if not self.authorization.revalidate(
                basis, actor, checked_at=after, factual_at=recheck_at
            ):
                return denied("denied")
            result = FamilyQueryResult(
                status, profile, sources, missing, at, now, basis.relationship_kind,
                conflicts, quality=quality, quality_version=quality_version,
            )
            if _with_evidence:
                digest = _digest(result)

                def current(candidate: ActorContext) -> bool:
                    if candidate != actor:
                        return False
                    fresh = self.query(
                        actor=candidate, as_of=as_of, quality_fields=quality_fields,
                        _with_evidence=False,
                    )
                    return (
                        fresh.status not in {"denied", "source_unavailable"}
                        and _digest(fresh) == digest
                    )

                object.__setattr__(
                    result,
                    "evidence",
                    FamilyReadEvidence(current, (self.clock, now, basis.valid_to)),
                )
            return result
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "28000":
                return denied("denied")
            return denied("source_unavailable")
        except (SQLAlchemyError, ValueError, TypeError, KeyError):
            return denied("source_unavailable")
