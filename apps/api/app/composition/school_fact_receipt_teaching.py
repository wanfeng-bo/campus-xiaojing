"""Name an authorized original teaching revision through exact historical Readers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from app.composition.school_fact_education_external import EducationRelatedReadIncomplete
from app.composition.school_fact_education_reader import EducationReadStale
from app.composition.school_fact_read_evidence import register_release_time_window, validation_batch
from app.platform.assistant.contracts import JsonObject
from app.platform.contracts import StaffActorContext
from app.platform.school_facts.ports import SchoolFactOwnOriginalSegment


@dataclass(frozen=True, repr=False)
class OriginalTeachingNameProof:
    actor: StaffActorContext
    proofs: tuple[Any, ...]
    clock: Callable[[], datetime]
    observed_at: datetime

    def revalidate(self, *, actor: StaffActorContext | None = None) -> None:
        if actor is not None and actor != self.actor:
            raise PermissionError("Original teaching name identity changed")
        with validation_batch():
            for proof in self.proofs:
                proof.revalidate()
            # Factual dates stay fixed at the original segment. Only current
            # permission/identity clocks advance; do not expire an ended record.
            register_release_time_window(
                self.clock, observed_at=self.observed_at, valid_before=None
            )


@dataclass(frozen=True)
class OriginalTeachingProjection:
    material: JsonObject
    proof: OriginalTeachingNameProof


def project_original_teaching(
    actor: StaffActorContext,
    segments: tuple[SchoolFactOwnOriginalSegment, ...],
    *,
    external_reader: Any,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> OriginalTeachingProjection:
    """The caller owns original-revision authority; names retain independent evidence."""
    if type(actor) is not StaffActorContext or len(segments) > 128:
        raise ValueError("Invalid original teaching projection")
    observed_at = clock()
    if observed_at.utcoffset() is None:
        raise ValueError("Original teaching observation requires a timezone")
    proofs: list[Any] = []
    items: list[Any] = []

    def read_name_field(kind: str, key: str, field: str, at: datetime) -> str | None:
        result = external_reader.read_records(
            actor, kind, source_keys=(key,), fields=(field,), as_of=at, include_boundaries=False
        )
        rows = result["records"]
        if type(rows) is not list:
            raise ValueError("Original teaching association is not exact")
        rows = cast(list[Any], rows)
        if len(rows) > 1:
            raise ValueError("Original teaching association is not exact")
        proof = result["proof"]
        proof.revalidate()
        proofs.append(proof)
        if result.get("boundary_proof") is not None:
            boundary_proof = result["boundary_proof"]
            boundary_proof.revalidate()
            proofs.append(boundary_proof)
        if not rows:
            return None
        row = rows[0]
        if row["source_key"] != key or set(row["content"]) != {field}:
            raise ValueError("Original teaching association exceeds its selection")
        value = row["content"][field]
        if not isinstance(value, str) or not value:
            raise ValueError("Original teaching association value is unavailable")
        return value

    with validation_batch():
        for segment in segments:
            start, end, content = segment.effective_at, segment.valid_to, segment.content
            if (
                start.utcoffset() is None
                or end is not None
                and (end.utcoffset() is None or end <= start)
                or set(content) != {"assignment_source_key", "offering_source_key", "teaching_role"}
                or any(type(value) is not str or not value for value in content.values())
                or content["teaching_role"] not in {"lead", "co_teacher"}
            ):
                raise ValueError("Invalid original teaching segment")
            name = None
            unavailable_reason = "future_not_queried"
            if start <= observed_at:
                try:
                    person = read_name_field(
                        "assignment", content["assignment_source_key"], "person_source_key", start
                    )
                    if person is not None:
                        name = read_name_field("person", person, "display_name", start)
                    unavailable_reason = "historical_read_empty"
                except EducationRelatedReadIncomplete as error:
                    name = None
                    unavailable_reason = (
                        "historical_read_empty" if error.status == "not_recorded" else "conflict"
                    )
                except EducationReadStale:
                    name = None
                    unavailable_reason = "stale"
                except PermissionError:
                    name = None
                    unavailable_reason = "permission_denied"
                except Exception:
                    # Missing/denied names cannot erase or replace the independently
                    # authorized original assignment, role, or effective interval.
                    name = None
                    unavailable_reason = "source_unavailable"
            items.append(
                {
                    "teacher_name": name,
                    "teaching_role": {"lead": "主讲", "co_teacher": "共同任教"}[
                        content["teaching_role"]
                    ],
                    "effective_at": start.isoformat(),
                    "valid_to": None if end is None else end.isoformat(),
                    "name_status": "verified" if name is not None else "unverified",
                    "name_as_of": start.isoformat(),
                    **({"name_unavailable_reason": unavailable_reason} if name is None else {}),
                }
            )
        proof = OriginalTeachingNameProof(actor, tuple(proofs), clock, observed_at)
        proof.revalidate(actor=actor)
    return OriginalTeachingProjection(
        {
            "items": items,
            "name_basis": (
                "姓名按原生效时点、在当前权限下核对关联历史资料；未核实姓名不代表原任教未登记。"
            ),
        },
        proof,
    )
