"""Deterministic digest of an exact, bounded school-fact mother set.

The digest is evidence of *which* rows were checked, not proof that the
school's real-world population is complete. The publishing maintainer remains
responsible for that declaration. A caller-supplied digest is never trusted.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from pydantic import ValidationError

from app.platform.school_facts.contracts import CoverageBasisV1, CoverageBasisV2


def mother_set_digest(members: Iterable[tuple[str, int]]) -> tuple[int, str]:
    ordered = sorted(members)
    if len(ordered) > 50_000 or len({key for key, _version in ordered}) != len(ordered):
        raise ValueError("Mother set is unbounded or has duplicate source keys.")
    if any(not key or type(version) is not int or version < 1 for key, version in ordered):
        raise ValueError("Mother set has an invalid member or version.")
    encoded = json.dumps(ordered, ensure_ascii=False, separators=(",", ":")).encode()
    return len(ordered), hashlib.sha256(encoded).hexdigest()


def verify_grade_group_collection_basis(
    raw: object,
    *,
    school_id: UUID,
    owner_source_key: str,
    campus_source_key: str,
    academic_year: int,
    field_name: str,
    factual_at: datetime,
    school_fact_version: int,
    members: Iterable[tuple[str, int]],
) -> bool:
    """Release one exact F4 set only in the publication's unchanged school revision.

    A new record outside the reader's exact object grants cannot be seen by the
    reader. Any later school write therefore makes this collection claim
    unknown until it is explicitly republished; no hidden member count leaks.
    """
    if not isinstance(raw, dict) or factual_at.utcoffset() is None:
        return False
    try:
        nullable = {
            "owner_source_key", "target_source_key", "field_name", "purpose",
            "contact_kind", "valid_to",
        }
        basis = CoverageBasisV2.model_validate_json(
            json.dumps({**dict.fromkeys(nullable), **raw}, default=str)
        )
        selected = tuple(members)
        count, digest = mother_set_digest(selected)
    except (ValidationError, TypeError, ValueError):
        return False
    return (
        basis.school_id == school_id
        and basis.owner_source_key == owner_source_key
        and basis.campus_source_key == campus_source_key
        and basis.academic_year == academic_year
        and basis.field_name == field_name
        and basis.valid_from <= factual_at
        and (basis.valid_to is None or factual_at < basis.valid_to)
        and school_fact_version == basis.school_fact_version + 1
        and basis.authorized_source_keys == tuple(sorted(key for key, _ in selected))
        and basis.member_count == count
        and basis.member_sha256 == digest
    )


def verified_exact_student_basis(
    raw: object,
    *,
    school_id: UUID,
    owner_source_key: str,
    target_source_key: str,
    target_version: int,
    field_name: str | None,
    factual_at: datetime,
    school_fact_version: int,
) -> bool:
    """Only the exact authorized student member may promote a F7 claim.

    The current formal Reader rechecks this exact target and its version. An
    unrelated later school write must not invalidate the target's proof. This
    does not certify real-world coverage.
    """
    return verify_coverage_basis(
        raw,
        school_id=school_id,
        owner_kind="person",
        owner_source_key=owner_source_key,
        target_fact_kind="student",
        target_source_key=target_source_key,
        field_name=field_name,
        purpose=None,
        contact_kind=None,
        factual_at=factual_at,
        school_fact_version=school_fact_version,
        members=((target_source_key, target_version),),
    )


def verify_coverage_basis(
    raw: object,
    *,
    school_id: UUID,
    owner_kind: str,
    owner_source_key: str | None,
    target_fact_kind: str,
    target_source_key: str | None,
    field_name: str | None,
    purpose: str | None,
    contact_kind: str | None,
    factual_at: datetime,
    school_fact_version: int,
    members: Iterable[tuple[str, int]],
) -> bool:
    """Compare a stored proof with a freshly authorized *whole* domain set.

    The caller must obtain `members` through its current bounded domain Reader,
    prove it is not a page/subset, and revalidate the exact authority at result
    use. This function only checks the typed coordinate and exact set digest.
    """
    if not isinstance(raw, dict) or factual_at.utcoffset() is None:
        return False
    try:
        # The canonical Writer stores JSONB with null coordinates omitted. Their
        # absence means only null, never a wildcard or an inferred dimension.
        nullable = {
            "owner_source_key", "target_source_key", "field_name",
            "purpose", "contact_kind", "valid_to",
        }
        basis = CoverageBasisV1.model_validate_json(
            json.dumps({**dict.fromkeys(nullable), **raw})
        )
    except (ValidationError, TypeError, ValueError):
        return False
    try:
        count, digest = mother_set_digest(members)
    except ValueError:
        return False
    return (
        basis.school_id == school_id
        and basis.owner_kind == owner_kind
        and basis.owner_source_key == owner_source_key
        and basis.target_fact_kind == target_fact_kind
        and basis.target_source_key == target_source_key
        and basis.field_name == field_name
        and basis.purpose == purpose
        and basis.contact_kind == contact_kind
        and basis.valid_from <= factual_at
        and (basis.valid_to is None or factual_at < basis.valid_to)
        # The broad school revision proves the claim was published already;
        # freshness comes from the freshly authorized exact member digest.
        # Requiring the immediately next revision would invalidate every
        # unchanged mother set after any unrelated formal school write.
        and basis.school_fact_version < school_fact_version
        and basis.member_count == count
        and basis.member_sha256 == digest
    )
