"""Minimal formal residence result; attendance and room rosters are separate facts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from app.domains.students.family_read import FamilyReadEvidence


@dataclass(frozen=True)
class ResidenceQueryResult:
    payload: dict[str, Any]
    evidence: FamilyReadEvidence | None = None
    family: bool = False

    def public(self) -> Any:
        return {
            "kind": "family_residence" if self.family else "residence_facts",
            **deepcopy(self.payload),
        }


SOURCE_FIELDS = frozenset({"kind", "label", "published_at"})
QUALITY_FIELDS = frozenset(
    {
        "quality_kind",
        "target_fact_kind",
        "field_name",
        "coverage_status",
        "negative_code",
        "dispute_status",
        "source_status",
        "verified_at",
        "review_due_at",
        "valid_from",
        "valid_to",
        "version",
        "source",
    }
)
FAMILY_ROW_FIELDS = frozenset(
    {
        "source_key",
        "fact_kind",
        "version",
        "student_name",
        "building_name",
        "room_name",
        "bed_name",
        "valid_from",
        "valid_to",
        "source",
        "quality",
        "limitations",
    }
)


def validate_source(source: dict[str, Any] | None) -> None:
    if source is None:
        return
    if (
        type(source) is not dict
        or set(source) != set(SOURCE_FIELDS)
        or source["kind"] != "formal_publication"
        or type(source["label"]) is not str
        or type(source["published_at"]) is not str
    ):
        raise ValueError("Invalid formal residence publication")


def validate_quality(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if type(rows) is not list or len(rows) > 100:
        raise ValueError("Invalid bounded residence quality")
    for row in rows:
        if type(row) is not dict or set(row) - QUALITY_FIELDS:
            raise ValueError("Overbroad residence quality")
        validate_source(row.get("source"))
    return deepcopy(rows)


def family_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if type(rows) is not list or len(rows) > 1:
        raise ValueError("Ambiguous selected-child residence")
    for row in rows:
        if (
            type(row) is not dict
            or set(row) - FAMILY_ROW_FIELDS
            or row.get("fact_kind") != "student_bed_residence"
            or type(row.get("source_key")) is not str
            or type(row.get("version")) is not int
        ):
            raise ValueError("Overbroad selected-child residence")
        validate_source(row.get("source"))
        validate_quality(row.get("quality", []))
    return deepcopy(rows)
