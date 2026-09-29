"""Maintenance projections reuse the same exact Reader, never a second fact store."""

from datetime import UTC, datetime
from typing import Any

from pydantic import AwareDatetime, Field

from app.composition.school_fact_residence_product import (
    REFERENCE_KINDS,
    RESIDENCE_LABELS,
    public_name,
)
from app.composition.school_fact_residence_time import unknown_time_document
from app.platform.contracts import FrozenContractModel
from app.platform.school_facts.contracts import fact_content_fields
from app.platform.school_facts.f6_policy import F6_FACT_KINDS


class ResidenceRecordsInput(FrozenContractModel):
    fact_kind: str
    query_text: str | None = Field(default=None, min_length=1, max_length=200)
    student_number: str | None = Field(default=None, min_length=1, max_length=1000)
    reference: str | None = Field(default=None, min_length=8, max_length=1600)
    related_ref: str | None = Field(default=None, min_length=8, max_length=1600)
    related_field: str | None = None
    as_of: AwareDatetime | None = None
    limit: int = Field(default=100, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=4000)


class ResidenceMaintenanceViews:
    def __init__(
        self,
        *,
        repository: Any,
        adapter: Any,
        external_choices: Any,
        query: Any,
        quality: Any = None,
        eligibility: Any = None,
    ) -> None:
        self.repository, self.adapter, self.external_choices = repository, adapter, external_choices
        self.quality = quality
        self.eligibility = eligibility
        self.query_service = query

    def describe(self, **kwargs: Any) -> Any:
        if (
            kwargs.get("fact_kind") == "student_school_relationship"
            and self.eligibility is not None
        ):
            return self.with_time(kwargs["actor"], self.eligibility.describe(**kwargs))
        if kwargs.get("fact_kind") == "fact_quality" and self.quality is not None:
            return self.with_time(kwargs["actor"], self.quality.describe(**kwargs))
        return self.adapter.describe(**kwargs)

    def with_time(self, actor: Any, view: Any, *, as_of: Any = None) -> Any:
        if view.get("status") in {"forbidden", "denied", "unavailable", "unsupported_selection"}:
            return view
        service = getattr(self.adapter, "time_context", None)
        observed = view.get("factual_at") or view.get("as_of")
        current_at = datetime.fromisoformat(observed) if isinstance(observed, str) else None
        proof = (
            service.capture(actor, as_of=as_of, current_at=current_at)
            if service is not None
            else None
        )
        if proof is not None:
            proof.revalidate(actor=actor)
        return {**view, **(proof.document if proof is not None else unknown_time_document())}

    def query(
        self,
        *,
        actor: Any,
        fact_kind: Any,
        query_text: Any = None,
        student_number: Any = None,
        reference: Any = None,
        related_ref: Any = None,
        related_field: Any = None,
        as_of: Any = None,
        limit: Any = 100,
        cursor: Any = None,
    ) -> Any:
        if fact_kind == "student_school_relationship" and self.eligibility is not None:
            return self.with_time(
                actor,
                self.eligibility.query(
                    actor=actor,
                    fact_kind=fact_kind,
                    query_text=query_text,
                    student_number=student_number,
                    reference=reference,
                    related_ref=related_ref,
                    related_field=related_field,
                    as_of=as_of,
                    limit=limit,
                    cursor=cursor,
                ),
                as_of=as_of,
            )
        if student_number is not None:
            raise ValueError("学号条件仅用于学生在校关系。")
        if fact_kind == "fact_quality" and self.quality is not None:
            return self.with_time(
                actor,
                self.quality.query(
                    actor=actor,
                    reference=reference,
                    related_ref=related_ref,
                    as_of=as_of,
                    limit=limit,
                    cursor=cursor,
                    query_text=query_text,
                ),
                as_of=as_of,
            )
        if fact_kind not in F6_FACT_KINDS:
            raise ValueError("未接入该类寄宿资料。")
        if related_field is not None and related_field not in {
            public_name(f) for f in REFERENCE_KINDS
        }:
            raise ValueError("未接入的寄宿关联条件。")
        result, next_cursor = self.query_service.read_page(
            actor=actor,
            fact_kind=fact_kind,
            as_of=as_of,
            reference=reference,
            name=query_text,
            related_ref=related_ref,
            limit=limit,
            cursor=cursor,
        )
        rows: list[Any] = []
        for row in result.records:
            source: dict[str, Any] = {**(row.get("source") or {}), "reference": None}
            source.setdefault("label", "学校正式登记资料")
            source.setdefault("published_at", None)
            fields = self.adapter.public_fields(actor, row["fields"])
            rows.append(
                dict(
                    reference=self.repository._reference(
                        actor, fact_kind, row["source_key"], False
                    ),
                    fact_kind=fact_kind,
                    label=row.get("student_name")
                    or row.get("display_name")
                    or row.get("room_name")
                    or RESIDENCE_LABELS[fact_kind],
                    fields=fields,
                    version=row["version"],
                    valid_from=row["valid_from"],
                    valid_to=row.get("valid_to"),
                    source=source,
                )
            )
        result.proof.revalidate(actor=actor)
        return dict(
            **(
                result.proof.time_proof.document
                if getattr(result.proof, "time_proof", None)
                else unknown_time_document()
            ),
            kind="directory_facts",
            family_owner="school_facts.residence",
            fact_kind=fact_kind,
            status="known" if result.status == "available" else result.status,
            records=rows,
            matched_count=None,
            returned_count=len(rows),
            truncated=result.has_more,
            next_cursor=next_cursor,
            queried_at=self.repository.clock().isoformat(),
            factual_at=result.as_of.isoformat(),
            coverage={
                "status": "unknown",
                "description": "仅本次获准对象和字段，不能据此推断全校覆盖。",
            },
        )

    def reference_choices(self, *, actor: Any, fact_kind: Any, field_name: Any) -> Any:
        if fact_kind == "student_school_relationship" and self.eligibility is not None:
            return self.eligibility.reference_choices(
                actor=actor, fact_kind=fact_kind, field_name=field_name
            )
        if fact_kind == "fact_quality" and self.quality is not None:
            if field_name in {"person_ref", "location_ref"}:
                return self.external_choices(
                    actor=actor, fact_kind=field_name.removesuffix("_ref"), limit=100
                )
            if field_name not in {"target_ref", "replacement_ref"}:
                raise ValueError("未接入该依据关联字段。")
            rows: list[Any] = []
            for kind in sorted(F6_FACT_KINDS):
                try:
                    result = self.query(actor=actor, fact_kind=kind, limit=100)
                except PermissionError:
                    continue
                if result["truncated"]:
                    raise ValueError("关联对象超出完整选择范围，请先缩小明确授权范围。")
                rows.extend(result["records"])
            return {
                "kind": "directory_facts",
                "family_owner": "school_facts.residence",
                "fact_kind": "fact_quality",
                "status": "known" if rows else "no_current_records",
                "records": rows,
                "returned_count": len(rows),
                "matched_count": None,
                "truncated": False,
                "next_cursor": None,
                "queried_at": datetime.now(UTC).isoformat(),
                "coverage": {"status": "unknown", "description": "仅当前获准选择。"},
            }
        if fact_kind not in F6_FACT_KINDS:
            raise ValueError("未接入该维护类型。")
        field = next((key for key in REFERENCE_KINDS if public_name(key) == field_name), None)
        allowed, _ = fact_content_fields(fact_kind, 4)
        if field is None or field not in allowed:
            raise ValueError("未接入该关联字段。")
        target = REFERENCE_KINDS[field]
        if target in F6_FACT_KINDS:
            return self.query(actor=actor, fact_kind=target, limit=100)
        return self.external_choices(actor=actor, fact_kind=target, limit=100)
