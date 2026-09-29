# pyright: strict
"""Pure interpretation of already authorized, public F2 quality statements.

This consumer cannot authorize a read. Its caller supplies the current Reader's
projection, never canonical source keys. Statements describe only their explicit
owner, target, dimensions and half-open factual window. Collection completeness
is never inferred from the number of visible rows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import JsonValue


@dataclass(frozen=True, slots=True)
class QualityFact:
    reference: str
    fact_kind: str
    fields: Mapping[str, object]
    valid_from: datetime
    valid_to: datetime | None


def _time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None
    return None


def _owner(statement: QualityFact) -> tuple[str, str | None] | None:
    kind = statement.fields.get("owner_kind")
    if kind == "school":
        return "school", None
    if kind not in {"organization", "person", "location"}:
        return None
    reference = statement.fields.get(str(kind) + "_ref")
    if not isinstance(reference, str):
        return None
    return str(kind), reference


def _owned(record: QualityFact, owner: tuple[str, str | None]) -> bool:
    kind, reference = owner
    if kind == "school":
        # Every input row has already been restricted to the actor's school.
        return True
    if record.fact_kind == kind:
        return record.reference == reference
    field = kind + "_ref"
    if record.fact_kind == "policy_version" and kind == "organization":
        field = "publishing_organization_ref"
    return record.fields.get(field) == reference


def _dimensions(statement: QualityFact, record: QualityFact) -> bool:
    return all(
        statement.fields.get(field) is None or statement.fields[field] == record.fields.get(field)
        for field in ("purpose", "contact_kind")
    )


def _one(values: set[str]) -> str:
    return (
        next(iter(values))
        if len(values) == 1
        else ("conflicting_statements" if values else "unknown")
    )


def evaluate_quality(
    *,
    fact_kind: str,
    reference: str | None,
    related_ref: str | None,
    name: str | None,
    factual_at: datetime,
    records: Sequence[QualityFact],
    statements: Sequence[QualityFact],
    query_status: str,
    evidence_status: str,
    evidence_truncated: bool,
    records_truncated: bool = False,
    verified_complete_claim_refs: frozenset[str] | None = None,
) -> dict[str, JsonValue]:
    """Return composable evidence, without promoting a scoped claim to all facts."""
    usable_query = query_status in {"known", "no_current_records"}
    result: dict[str, JsonValue] = {
        "existence": "known" if usable_query and records else "unknown",
        "coverage": "unknown",
        "source_availability": "unknown",
        "freshness": "unknown",
        "conflict": "unknown",
        "evidence_status": evidence_status,
        "evidence_truncated": evidence_truncated,
        "statements": [],
        "notice": "质量结论仅针对各声明列明的对象、用途及请求时点；不证明未查询或未获权集合完整。",
    }
    if not usable_query or evidence_status not in {"known", "no_current_records"}:
        return result
    visible = tuple(
        row
        for row in records
        if row.fact_kind == fact_kind
        and row.valid_from <= factual_at
        and (row.valid_to is None or factual_at < row.valid_to)
    )
    claims: list[JsonValue] = []
    sources: set[str] = set()
    freshness: set[str] = set()
    disputes: set[str] = set()
    coverage: set[str] = set()
    contradicts = False
    whole_negative = False
    for statement in statements:
        fields = statement.fields
        owner = _owner(statement)
        if (
            statement.fact_kind != "fact_quality"
            or fields.get("target_fact_kind") != fact_kind
            or owner is None
            or statement.valid_from > factual_at
            or statement.valid_to is not None
            and factual_at >= statement.valid_to
        ):
            continue
        verified = _time(fields.get("verified_at"))
        if verified is None or verified > factual_at:
            continue
        target = fields.get("target_ref")
        matches = tuple(
            row
            for row in visible
            if (target is None or target == row.reference)
            and _owned(row, owner)
            and _dimensions(statement, row)
        )
        owner_selected = related_ref is not None and (
            related_ref == owner[1]
            or owner[0] == "school"
            and related_ref.startswith(("f2:school:", "schoolv1:f2:school:o:"))
        )
        # A null target can explicitly describe an empty owner's collection.
        # Named searches cannot establish that the full owner collection was queried.
        owner_claim = target is None and owner_selected and name is None
        if not matches and not owner_claim:
            continue
        if target is not None and not matches:
            continue
        kind = fields.get("quality_kind")
        if kind not in {"source", "coverage", "negative", "dispute", "review", "correction"}:
            continue
        narrow = fields.get("purpose") is not None or fields.get("contact_kind") is not None
        exact_query = bool(
            len(visible) == 1
            and matches
            and (reference == visible[0].reference or name is not None)
        )
        # A null-target claim is retained verbatim, even when it applies to only
        # one dimension of the selected owner's collection.
        applies_to_query = exact_query or (owner_claim and not narrow)
        # A singleton response with no further page may summarize that one
        # returned record, even if the caller selected its owner rather than
        # its exact reference. This never certifies the owner's full collection.
        summarizes_returned = applies_to_query or bool(
            len(visible) == 1 and len(matches) == 1 and not records_truncated
        )
        claim: dict[str, JsonValue] = {
            "reference": statement.reference,
            "kind": str(kind),
            "target_fact_kind": fact_kind,
            "target_ref": target if isinstance(target, str) else None,
            "owner_kind": owner[0],
            "owner_ref": related_ref if owner[0] == "school" and owner_selected else owner[1],
            "applies_to_query": applies_to_query,
            "applies_to_records": [row.reference for row in matches],
            "valid_from": statement.valid_from.isoformat(),
            "valid_to": statement.valid_to.isoformat() if statement.valid_to else None,
        }
        for field in (
            "purpose",
            "contact_kind",
            "basis_reference",
            "responsibility",
            "verified_at",
            "evidence_reference",
            "coverage_status",
            "negative_code",
            "dispute_status",
            "source_status",
            "last_success_at",
            "review_due_at",
            "replacement_ref",
        ):
            value = fields.get(field)
            if isinstance(value, str):
                claim[field] = value
            elif isinstance(value, datetime):
                claim[field] = value.isoformat()
        claims.append(claim)
        # A claim about one returned row remains visible as evidence, but must not
        # become the summary of a larger authorized collection.
        if (
            summarizes_returned
            and kind == "source"
            and fields.get("source_status") in {"available", "unavailable"}
        ):
            sources.add(str(fields["source_status"]))
        due = _time(fields.get("review_due_at"))
        if summarizes_returned and due is not None:
            freshness.add("review_overdue" if due <= factual_at else "within_review_period")
        if (
            summarizes_returned
            and kind == "dispute"
            and fields.get("dispute_status") in {"open", "resolved"}
        ):
            disputes.add(str(fields["dispute_status"]))
        if kind == "coverage":
            status = fields.get("coverage_status")
            if status == "partial" and target is not None and exact_query:
                coverage.add("partial")
            elif status == "complete" and not records_truncated:
                legacy_exact = (
                    verified_complete_claim_refs is None and target is not None and exact_query
                )
                current_verified = (
                    verified_complete_claim_refs is not None
                    and statement.reference in verified_complete_claim_refs
                    and (exact_query or owner_claim and not narrow)
                )
                if legacy_exact or current_verified:
                    coverage.add("complete")
        if kind == "negative":
            contradicts = contradicts or bool(matches)
            whole_negative = whole_negative or applies_to_query
    result.update(
        statements=claims,
        source_availability=_one(sources),
        freshness=_one(freshness),
        conflict="contradictory_evidence" if contradicts else _one(disputes),
        coverage="unknown" if evidence_truncated else _one(coverage),
    )
    if whole_negative and not visible:
        result["existence"] = "formal_negative"
    return result
