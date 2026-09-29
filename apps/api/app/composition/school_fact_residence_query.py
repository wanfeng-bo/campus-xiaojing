"""Fixed selected-child and exact staff residence queries with live evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from app.composition.school_fact_education_reader import aware, timestamp
from app.composition.school_fact_residence_time import unknown_time_document
from app.domains.students.family_read import FamilyReadEvidence
from app.domains.students.residence_read import (
    ResidenceQueryResult,
    family_rows,
    validate_quality,
    validate_source,
)
from app.platform.contracts import GuardianActorContext, StaffActorContext
from app.platform.school_facts.f7_coverage_basis import verify_coverage_basis


def family_residence_reference(actor: Any, student_id: Any, kind: Any, source_key: Any) -> Any:
    # An opaque locator, never an authority; the receiving owner must revalidate
    # the current relationship and its exact selected child before resolving it.
    body = f"{actor.school_id}:{actor.guardian_relationship_id}:{student_id}:{kind}:{source_key}"
    return "sf6_fam_" + hashlib.sha256(body.encode()).hexdigest()


def _verified_exact_residence_coverage(
    rows: Any,
    *,
    school_id: Any,
    fact_kind: str,
    source_key: str,
    version: int,
    factual_at: datetime,
    school_fact_version: int,
    visible_fields: set[str],
) -> list[dict[str, Any]]:
    """Only promote a published, exact target proof over a current authorized row."""
    if type(rows) is not list or len(rows) > 100:
        raise ValueError("寄宿完整覆盖依据不是有限正式集合。")
    verified: list[dict[str, Any]] = []
    for row in rows:
        if (
            type(row) is not dict
            or row.get("target_fact_kind") != fact_kind
            or (row.get("target_source_key") != source_key)
        ):
            raise ValueError("寄宿完整覆盖依据目标不符。")
        field = row.get("field_name")
        if field is not None and field not in visible_fields:
            continue
        if not verify_coverage_basis(
            row.get("coverage_basis"),
            school_id=school_id,
            owner_kind=row.get("owner_kind"),
            owner_source_key=row.get("owner_source_key"),
            target_fact_kind=fact_kind,
            target_source_key=source_key,
            field_name=field,
            purpose=None,
            contact_kind=None,
            factual_at=factual_at,
            school_fact_version=school_fact_version,
            members=((source_key, version),),
        ):
            continue
        source = row.get("source")
        validate_source(source)
        if source is None:
            continue
        verified.append(
            {
                "quality_kind": "coverage",
                "target_fact_kind": fact_kind,
                "field_name": field,
                "coverage_status": "complete",
                "valid_from": row.get("valid_from"),
                "valid_to": row.get("valid_to"),
                "version": row.get("quality_version"),
                "source": source,
            }
        )
    return verified


def _quality_for_answer(
    rows: list[dict[str, Any]], *, verified_complete: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Do not release a completeness conclusion without a verified mother set.

    The maintenance owner retains the original published declaration. This F6
    answer projection has only the selected child/page and cannot establish the
    full authorized set behind a legacy complete claim.
    """

    answer = []
    for row in rows:
        if type(row) is not dict:
            raise ValueError("Invalid residence quality projection")
        item = dict(row)
        if item.get("quality_kind") == "coverage" and item.get("coverage_status") == "complete":
            if verified_complete:
                continue
            item["coverage_status"] = "unknown"
        answer.append(item)
    if verified_complete:
        answer.extend(verified_complete)
    return answer


class FamilyResidenceQueryService:
    def __init__(
        self,
        *,
        repository: Any,
        current_actor: Any,
        reference_key: bytes,
        clock: Any = lambda: datetime.now(UTC),
        time_context: Any = None,
    ) -> None:
        self.repository, self.current_actor, self.clock = repository, current_actor, clock
        self.time_context = time_context
        if type(reference_key) is not bytes or len(reference_key) < 16:
            raise ValueError("Private residence reference key required")

    def _basis(self, actor: Any, at: Any) -> Any:
        now = self.clock()
        if type(actor) is not GuardianActorContext or not self.current_actor(actor):
            return None
        basis = self.repository.authority(actor)
        if (
            basis is None
            or basis.relationship_kind != "verified_guardian"
            or not basis.permits(actor, now=now, factual_at=now)
            or at < basis.valid_from
            or basis.valid_to is not None
            and at >= basis.valid_to
        ):
            return None
        return basis

    def query(self, *, actor: Any, as_of: Any = None) -> Any:
        queried_at = self.clock()
        at = aware(as_of or queried_at)

        def failed(status: Any) -> Any:
            return ResidenceQueryResult({"status": status}, family=True)

        try:
            basis = self._basis(actor, at)
            if basis is None:
                return failed("denied")
            time_proof = (
                self.time_context.capture(actor, as_of=as_of, current_at=queried_at)
                if self.time_context
                else None
            )
            raw: dict[str, Any] = self.repository.residence_read(actor, as_of=as_of)
            if self._basis(actor, at) != basis:
                return failed("denied")
            allowed: set[Any] = {
                "status",
                "records",
                "school_fact_version",
                "student_id",
                "student_version",
                "relationship_version",
                "as_of",
                "next_boundary",
                "residence_mode",
                "enrollment_status",
                "quality",
            }
            if (
                type(raw) is not dict
                or set(raw) - allowed
                or raw.get("student_id") != str(basis.student_id)
                or raw.get("student_version") != basis.student_version
                or raw.get("relationship_version") != basis.relationship_version
                or raw.get("status")
                not in {"available", "no_current_records", "conflict", "partial"}
                or type(raw.get("school_fact_version")) is not int
                or raw.get("residence_mode") not in {None, "day", "boarding"}
            ):
                return failed("unavailable")
            rows = family_rows(raw["records"])
            exact_quality = getattr(self.repository, "exact_coverage", None)
            for row in rows:
                verified: list[dict[str, Any]] = []
                if exact_quality is not None:
                    proof_rows = exact_quality(
                        actor,
                        student_id=basis.student_id,
                        fact_kind=row["fact_kind"],
                        source_key=row["source_key"],
                        as_of=at,
                    )
                    verified = _verified_exact_residence_coverage(
                        proof_rows,
                        school_id=actor.school_id,
                        fact_kind=row["fact_kind"],
                        source_key=row["source_key"],
                        version=row["version"],
                        factual_at=at,
                        school_fact_version=raw["school_fact_version"],
                        visible_fields=set(row) - {"quality", "source", "source_key"},
                    )
                row["quality"] = _quality_for_answer(row["quality"], verified_complete=verified)
                row["reference"] = family_residence_reference(
                    actor, basis.student_id, row["fact_kind"], row.pop("source_key")
                )
            qualities = _quality_for_answer(validate_quality(raw.get("quality", [])))
            status = raw["status"]
            if rows and raw.get("residence_mode") != "boarding":
                status = "conflict" if raw.get("residence_mode") == "day" else "partial"
            limits: list[Any] = []
            if not rows:
                limits.append("absence_is_not_proof_of_never_boarding")
            if rows and any(not r.get("building_name") for r in rows):
                limits.append("building_use_not_recorded")
            if rows:
                limits.append("formal_arrangement_not_actual_presence")
            coverage = "unknown"
            all_quality: list[Any] = [
                *qualities,
                *(q for row in rows for q in row.get("quality", [])),
            ]
            # A verified exact claim covers only its one residence record. The
            # selected-child result can claim complete coverage only from an
            # independently verified result-level mother set.
            for quality in qualities:
                if quality.get("quality_kind") == "coverage" and quality.get("field_name") is None:
                    coverage = quality.get("coverage_status") or "unknown"
            for quality in all_quality:
                if quality.get("dispute_status") == "open":
                    status = "conflict"
                if quality.get("source_status") in {"unavailable", "unreachable", "stale"}:
                    if status != "conflict":
                        status = "partial"
                    limits.append("source_quality_unavailable")
                if (
                    quality.get("review_due_at")
                    and timestamp(quality["review_due_at"]) <= queried_at
                ):
                    if status != "conflict":
                        status = "partial"
                    limits.append("quality_review_overdue")
            public: dict[str, Any] = {
                **(time_proof.document if time_proof else unknown_time_document()),
                "status": status,
                "records": rows,
                "as_of": at.isoformat(),
                "queried_at": queried_at.isoformat(),
                "residence_mode": raw.get("residence_mode"),
                "enrollment_status": raw.get("enrollment_status"),
                "quality": qualities,
                "coverage": {"status": coverage},
                "limitations": list(dict.fromkeys(limits)),
            }
            if at > queried_at:
                public["future_basis"] = "按目前已登记的安排"
            version = raw["school_fact_version"]
            boundary = timestamp(raw.get("next_boundary")) if as_of is None else None
            if basis.valid_to is not None:
                boundary = min(boundary, basis.valid_to) if boundary else basis.valid_to
            if time_proof is not None and time_proof.next_boundary is not None:
                boundary = (
                    min(boundary, time_proof.next_boundary)
                    if boundary
                    else time_proof.next_boundary
                )

            def current(candidate: Any) -> Any:
                now = self.clock()
                return (
                    candidate == actor
                    and queried_at <= now
                    and (boundary is None or now < boundary)
                    and self._basis(candidate, at if as_of is not None else now) == basis
                    and (time_proof is None or time_proof.revalidate(actor=candidate) is None)
                    and self.repository.residence_stamp(candidate) == version
                    and self._basis(candidate, at if as_of is not None else self.clock()) == basis
                )

            evidence = FamilyReadEvidence(current, (self.clock, queried_at, boundary))
            if not evidence.valid(actor):
                return failed("unavailable")
            return ResidenceQueryResult(public, evidence, family=True)
        except Exception:
            return failed("unavailable")


class ResidencePageCursor:
    """F6 page context, bound to the actor and the complete finite selection."""

    def __init__(self, reference_key: bytes) -> None:
        if type(reference_key) is not bytes or len(reference_key) < 16:
            raise ValueError("Private residence cursor key required")
        self._key = hmac.digest(reference_key, b"f6-cursor-v1", "sha256")

    @staticmethod
    def fingerprint(selection: Any) -> str:
        return hashlib.sha256(json.dumps(selection, sort_keys=True).encode()).hexdigest()

    def decode(
        self,
        *,
        actor: Any,
        cursor: Any,
        selection: Any,
        version: Any,
        now: Any,
        school_coverage: bool = False,
    ) -> dict[str, Any]:
        if cursor is None:
            return {"offset": 0}
        if type(cursor) is not str or len(cursor) > 4000:
            raise ValueError("Invalid page context")
        try:
            data, mac = cursor.rsplit(".", 1)
            if not hmac.compare_digest(
                mac, hmac.digest(self._key, f"{actor}:{data}".encode(), "sha256").hex()
            ):
                raise ValueError("Invalid page context")
            page: dict[str, Any] = json.loads(bytes.fromhex(data))
            if (
                type(page) is not dict
                or page.get("query") != self.fingerprint(selection)
                or page.get("version") != version
                or type(page.get("offset")) is not int
                or page["offset"] < 0
                or not school_coverage
                and page["offset"] > 10000
            ):
                raise ValueError("Changed page selection or facts")
            if page.get("expires") and now >= timestamp(page["expires"]):
                raise ValueError("Changed page time")
            return page
        except (KeyError, TypeError, UnicodeError) as exc:
            raise ValueError("Invalid page context") from exc

    def encode(
        self, *, actor: Any, selection: Any, offset: Any, version: Any, scope: Any, expires: Any
    ) -> str:
        data = (
            json.dumps(
                {
                    "query": self.fingerprint(selection),
                    "offset": offset,
                    "version": version,
                    "scope": scope,
                    "expires": expires.isoformat() if expires else None,
                },
                sort_keys=True,
            )
            .encode()
            .hex()
        )
        return data + "." + hmac.digest(self._key, f"{actor}:{data}".encode(), "sha256").hex()


class ResidenceQueryService:
    def __init__(
        self, *, repository: Any, current_actor: Any, reference_key: bytes, time_context: Any = None
    ) -> None:
        self.repository, self.current_actor = repository, current_actor
        self.time_context = time_context
        self.cursor_codec = ResidencePageCursor(reference_key)

    def read_page(
        self,
        *,
        actor: Any,
        fact_kind: Any,
        reference: Any = None,
        name: Any = None,
        related_ref: Any = None,
        as_of: Any = None,
        cursor: Any = None,
        limit: Any = 25,
    ) -> Any:
        """Trusted adapter port; raw records remain inside their field owner."""
        if type(actor) is not StaffActorContext or not self.current_actor(actor):
            raise PermissionError("当前身份已变化。")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid residence page size")
        selection = {
            "kind": fact_kind,
            "reference": reference,
            "name": name,
            "related_ref": related_ref,
            "at": as_of.isoformat() if as_of else None,
            "limit": limit,
        }
        page = self.cursor_codec.decode(
            actor=actor,
            cursor=cursor,
            selection=selection,
            version=self.repository.current_version(actor.school_id)
            if cursor is not None
            else None,
            now=self.repository.clock(),
            school_coverage=bool(
                cursor is not None
                and self.repository.authority.require_scope(
                    actor, fact_kind=fact_kind, action="read", checked_at=self.repository.clock()
                ).scope.is_school_wide
            ),
        )
        offset = page["offset"]
        keys = (
            None
            if reference is None
            else (self.repository.source_for_reference(actor, fact_kind, reference),)
        )
        result = self.repository.read(
            actor,
            fact_kind,
            as_of=as_of,
            source_keys=keys,
            name=name,
            related_ref=related_ref,
            offset=offset,
            limit=limit,
        )
        if self.time_context is not None:
            time_proof = self.time_context.capture(actor, as_of=as_of, current_at=result.as_of)
            boundary = result.proof.next_boundary
            if time_proof.next_boundary is not None:
                boundary = (
                    min(boundary, time_proof.next_boundary)
                    if boundary
                    else time_proof.next_boundary
                )
            result = replace(
                result, proof=replace(result.proof, time_proof=time_proof, next_boundary=boundary)
            )
        scope_hash = result.proof.permit.scope.document_sha256
        if cursor is not None and page.get("scope") != scope_hash:
            raise ValueError("Changed page authority")
        result.proof.revalidate(actor=actor)
        next_cursor = None
        if result.has_more:
            if not result.records:
                raise ValueError("Incomplete residence page")
            boundary = result.proof.next_boundary
            for permit in (result.proof.permit, *result.proof.additional_permits):
                governance = getattr(getattr(permit, "basis", None), "governance", None)
                until = getattr(governance, "valid_until", None)
                if until is not None:
                    boundary = min(boundary, until) if boundary else until
            next_cursor = self.cursor_codec.encode(
                actor=actor,
                selection=selection,
                offset=offset + len(result.records),
                version=result.school_fact_version,
                scope=scope_hash,
                expires=boundary,
            )
        return result, next_cursor

    def query(
        self,
        *,
        actor: Any,
        fact_kind: Any = "student_bed_residence",
        reference: Any = None,
        name: Any = None,
        related_ref: Any = None,
        as_of: Any = None,
        cursor: Any = None,
        limit: Any = 25,
    ) -> Any:
        if type(actor) is not StaffActorContext or not self.current_actor(actor):
            return ResidenceQueryResult({"status": "denied"})
        try:
            result, next_cursor = self.read_page(
                actor=actor,
                fact_kind=fact_kind,
                as_of=as_of,
                reference=reference,
                name=name,
                related_ref=related_ref,
                limit=limit,
                cursor=cursor,
            )
            records: list[Any] = []
            exact_quality = getattr(self.repository, "exact_coverage", None)
            for original in result.records:
                row = dict(original)
                if "quality" in row:
                    verified = []
                    if exact_quality is not None:
                        proof_rows = exact_quality(
                            actor,
                            fact_kind=fact_kind,
                            source_key=row["source_key"],
                            as_of=result.as_of,
                            read_proof=result.proof,
                        )
                        verified = _verified_exact_residence_coverage(
                            proof_rows,
                            school_id=actor.school_id,
                            fact_kind=fact_kind,
                            source_key=row["source_key"],
                            version=row["version"],
                            factual_at=result.as_of,
                            school_fact_version=result.school_fact_version,
                            visible_fields=set(row.get("fields", {})),
                        )
                    row["quality"] = _quality_for_answer(row["quality"], verified_complete=verified)
                row["reference"] = self.repository._reference(
                    actor, fact_kind, row.pop("source_key"), False
                )
                row.pop("fields", None)
                # Stable source keys are owner-only coordinates. A related ref
                # requires that related owner's independent current read scope.
                for field in tuple(row):
                    if not field.endswith("_source_key"):
                        continue
                    key = row.pop(field)
                    kind = (
                        "dorm_bed"
                        if field == "bed_source_key"
                        else "student"
                        if field == "student_source_key"
                        else "location"
                    )
                    if key is not None:
                        try:
                            ref = self.repository.related_reference_for(actor, kind, key)
                        except PermissionError:
                            continue
                        if ref:
                            row[field.removesuffix("_source_key") + "_ref"] = ref
                records.append(row)
            public: dict[str, Any] = {
                **(
                    result.proof.time_proof.document
                    if getattr(result.proof, "time_proof", None)
                    else unknown_time_document()
                ),
                "status": result.status,
                "records": records,
                "as_of": result.as_of.isoformat(),
                "queried_at": self.repository.clock().isoformat(),
                "coverage": {"status": "authorized_selection_only"},
                "has_more": result.has_more,
                "next_cursor": next_cursor,
                "limitations": ["formal_arrangement_not_actual_presence"]
                if fact_kind == "student_bed_residence"
                else [],
            }
            proof = result.proof

            def valid(candidate: Any) -> Any:
                if candidate != actor or not self.current_actor(candidate):
                    return False
                proof.revalidate(actor=candidate)
                return True

            return ResidenceQueryResult(
                public,
                FamilyReadEvidence(
                    valid, (self.repository.clock, proof.observed_at, proof.next_boundary)
                ),
            )
        except PermissionError:
            return ResidenceQueryResult({"status": "denied"})
        except Exception:
            return ResidenceQueryResult({"status": "unavailable"})
