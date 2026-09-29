"""Authorized maintenance views; raw references stay outside ordinary answers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import AwareDatetime, Field, model_validator

from app.composition.school_fact_education_product import (
    EDUCATION_LABELS,
    REFERENCE_KINDS,
    EducationProductAdapter,
    public_name,
)
from app.composition.school_fact_education_query import (
    EducationQueryProof,
    EducationQueryResult,
    active_segment,
)
from app.domains.students.education_contracts import EDUCATION_CONTENT_FIELDS
from app.platform.contracts import FrozenContractModel, StaffActorContext
from app.platform.school_facts.f7_coverage_basis import verify_grade_group_collection_basis


def maintenance_publication_source(row, segment):
    """Map the actual segment publication, without borrowing a newer root's date."""
    source = segment.get("source", {})
    reference = source.get("publication_reference") or source.get("reference")
    published_at = source.get("published_at")
    if published_at is None and reference and reference == row.get("publication_reference"):
        published_at = row.get("source_published_at")
    return {
        "kind": "formal_publication",
        "label": source.get("summary") or source.get("label") or "学校正式登记资料",
        "reference": reference,
        "published_at": published_at,
        "recorded_at": segment.get("recorded_at"),
    }


class EducationRecordsInput(FrozenContractModel):
    fact_kind: str = Field(min_length=1, max_length=80)
    query_text: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="指定事实类型的正式名称；班级口语中的年级、班序、校区使用综合query的独立条件。",
    )
    reference: str | None = Field(default=None, min_length=8, max_length=1600)
    related_field: str | None = None
    related_ref: str | None = None
    academic_year: int | None = Field(
        default=None,
        strict=True,
        ge=1,
        le=9999,
        description="教育质量或制度依据的适用学年起始年筛选；不是事实查询日期，也不是届别或年级。",
    )
    campus_ref: str | None = Field(default=None, min_length=8, max_length=1600)
    position_ref: str | None = Field(default=None, min_length=8, max_length=1600)
    as_of: AwareDatetime | None = Field(
        default=None, description="事实适用时点，含时区；省略为现在，历史问法使用此项。"
    )
    limit: int = Field(default=20, strict=True, ge=1, le=100)

    @model_validator(mode="after")
    def exact(self):
        if sum(v is not None for v in (self.query_text, self.reference, self.related_ref)) > 1:
            raise ValueError("请使用名称、对象或明确关系中的一种选择。")
        if (self.related_field is None) != (self.related_ref is None):
            raise ValueError("关联查询需要同时选择关系和对象。")
        return self


class EducationMaintenanceViews:
    def __init__(
        self,
        adapter: EducationProductAdapter,
        *,
        directory: Any,
        family: Any,
        dependencies: Any = None,
    ) -> None:
        self.dependencies = dependencies
        self.adapter, self.repository, self.directory, self.family = (
            adapter,
            adapter.repository,
            directory,
            family,
        )

    def describe(self, *, actor: StaffActorContext, fact_kind: str) -> dict[str, Any]:
        if fact_kind in {"fact_quality", "policy_scope"} and self.dependencies is not None:
            return self.dependencies.describe(actor=actor, fact_kind=fact_kind)
        if fact_kind == "student_school_relationship":
            return self.family.query.describe(actor=actor, fact_kind=fact_kind)
        result: dict[str, Any] = dict(self.adapter.describe(actor=actor, fact_kind=fact_kind))
        related = [
            {"field": public_name(field), "kind": target, "label": public_name(field)}
            for field, target in REFERENCE_KINDS.items()
            if field in EDUCATION_CONTENT_FIELDS[fact_kind] and field != "grade_source_keys"
        ]
        result["query_support"] = {
            "name_fields": ["display_name"]
            if "display_name" in EDUCATION_CONTENT_FIELDS[fact_kind]
            else [],
            "reference": True,
            "all_authorized": True,
            "related_fields": related,
            "related_kinds": sorted({v["kind"] for v in related}),
            "name_match": "exact",
            "related_match": "direct",
            "guidance": "按正式名称精确查询，或选择已返回对象及明确的直接关系。",
        }
        return result

    def query_with_proof(
        self,
        *,
        actor: StaffActorContext,
        fact_kind: str,
        query_text: str | None = None,
        reference: str | None = None,
        related_field: str | None = None,
        related_ref: str | None = None,
        as_of: datetime | None = None,
        limit: int = 20,
        academic_year: int | None = None,
        campus_ref: str | None = None,
        position_ref: str | None = None,
    ) -> EducationQueryResult:
        if fact_kind in {"fact_quality", "policy_scope"} and self.dependencies is not None:
            return self.dependencies.query_with_proof(
                actor=actor,
                fact_kind=fact_kind,
                query_text=query_text,
                reference=reference,
                related_field=related_field,
                related_ref=related_ref,
                as_of=as_of,
                limit=limit,
                academic_year=academic_year,
                campus_ref=campus_ref,
                position_ref=position_ref,
            )
        grade_group_collection = (
            fact_kind == "education_grade_group"
            and academic_year is not None
            and campus_ref is not None
            and position_ref is None
            and query_text is None
            and reference is None
            and related_ref is None
        )
        if not grade_group_collection and any(
            v is not None for v in (academic_year, campus_ref, position_ref)
        ):
            raise ValueError("学年、校区、岗位条件仅用于教育依据查询。")
        EducationRecordsInput(
            fact_kind=fact_kind,
            query_text=query_text,
            reference=reference,
            related_field=related_field,
            related_ref=related_ref,
            as_of=as_of,
            limit=limit,
        )
        if fact_kind not in EDUCATION_CONTENT_FIELDS:
            raise ValueError("该教育工具不读取其他领域资料。")
        filters = {}
        if query_text is not None:
            if "display_name" not in EDUCATION_CONTENT_FIELDS[fact_kind]:
                raise ValueError("这类资料需按已返回对象或直接关系查询，不能按名称搜索。")
            filters["display_name"] = query_text
        if related_ref:
            canonical = next(
                (
                    f
                    for f in EDUCATION_CONTENT_FIELDS[fact_kind]
                    if public_name(f) == related_field and f in REFERENCE_KINDS
                ),
                None,
            )
            if canonical is None or canonical == "grade_source_keys":
                raise ValueError("该资料不支持所选关系。")
            filters[canonical] = self.adapter.resolve_reference(
                actor, REFERENCE_KINDS[canonical], related_ref
            )
        campus_key = None
        if grade_group_collection:
            campus_key = self.adapter.resolve_reference(actor, "location", campus_ref)
            filters.update(campus_source_key=campus_key, academic_year=academic_year)
        keys = (
            (self.repository.source_for_reference(actor, fact_kind, reference),)
            if reference
            else None
        )
        result = self.repository.read_records(
            actor, fact_kind, source_keys=keys, fields=None, filter=filters, as_of=as_of
        )
        at = result.as_of
        records: list[dict[str, Any]] = []
        states: list[str] = []
        record_keys: list[str] = []
        current_references = dict(result.references)
        for row in result.records:
            current = active_segment(row, at)
            if current is None:
                states.append(row.get("status", "no_current_records"))
                continue
            record_keys.append(row["source_key"])
            records.append(
                {
                    "fact_kind": fact_kind,
                    "reference": current_references[row["source_key"]],
                    "label": current["content"].get("display_name", EDUCATION_LABELS[fact_kind]),
                    "fields": self.adapter.public_fields(actor, current["content"]),
                    "version": row["version"],
                    "valid_from": current["effective_at"],
                    "valid_to": current.get("valid_to"),
                    "source": maintenance_publication_source(row, current),
                }
            )
        proof = EducationQueryProof(result.proof)
        if fact_kind == "education_class" and records and self.adapter.external is not None:
            from app.composition.school_fact_education_labels import class_labels
            from app.composition.school_fact_education_reader import EducationReadProof

            labels = class_labels(
                repository=self.repository,
                external=self.adapter.external,
                actor=actor,
                names={
                    key: row["label"]
                    for key, row in zip(record_keys[:limit], records[:limit], strict=True)
                },
                as_of=at,
                school_fact_version=result.school_fact_version,
            )
            for key, row in zip(record_keys[:limit], records[:limit], strict=True):
                row["label"] = labels.labels[key]
            proof = EducationQueryProof(
                EducationReadProof.combine((result.proof, *labels.education)), labels.external
            )

        collection_complete = False
        quality_reader = getattr(self.adapter.external, "quality_for", None)
        if grade_group_collection and quality_reader is not None and len(records) <= limit:
            try:
                calendar = self.repository.authority.require_scope(
                    actor, fact_kind="education_calendar_rule", action="read",
                    checked_at=self.repository.clock(),
                )
                calendar_keys = tuple(calendar.scope.object_keys)
            except PermissionError:
                calendar_keys = ()
            if calendar_keys:
                from app.composition.school_fact_education_quality import (
                    EducationDependencyDenied,
                )

                try:
                    quality = quality_reader(
                        actor,
                        target_kind="education_grade_group",
                        target_source_keys=tuple(record_keys),
                        field_names=("grade_source_key",),
                        owner_kind="education_calendar_rule",
                        owner_source_keys=calendar_keys,
                        campus_source_keys=(campus_key,),
                        academic_year=academic_year,
                        as_of=at,
                    )
                except EducationDependencyDenied:
                    quality = None
                if quality is not None:
                    quality_proof = quality.get("proof")
                    if (
                        quality_proof is None
                        or quality.get("school_fact_version") != result.school_fact_version
                    ):
                        raise PermissionError("年级组质量缺少本次读取依据。")
                    proof = EducationQueryProof(proof.education, proof.external + (quality_proof,))
                    members = tuple((row["source_key"], row["version"]) for row in result.records
                                    if active_segment(row, at) is not None)
                    collection_complete = any(
                        statement.get("content", {}).get("quality_kind") == "coverage"
                        and statement["content"].get("coverage_status") == "complete"
                        and statement["content"].get("education_target_kind")
                        == "education_calendar_rule"
                        and statement["content"].get("target_fact_kind")
                        == "education_grade_group"
                        and statement["content"].get("field_name") == "grade_source_key"
                        and statement["content"].get("academic_year") == academic_year
                        and verify_grade_group_collection_basis(
                            statement["content"].get("coverage_basis"),
                            school_id=actor.school_id,
                            owner_source_key=statement["content"].get(
                                "education_target_source_key", ""
                            ),
                            campus_source_key=campus_key,
                            academic_year=academic_year,
                            field_name="grade_source_key",
                            factual_at=at,
                            school_fact_version=result.school_fact_version,
                            members=members,
                        )
                        for statement in quality.get("statements", ())
                    )

        proof.revalidate(actor=actor)
        doc: dict[str, Any] = {
            "kind": "directory_facts",
            "fact_kind": fact_kind,
            "status": "known"
            if records
            else "not_recorded"
            if states and set(states) == {"not_recorded"}
            else "no_current_records",
            "records": records[:limit],
            "matched_count": len(records),
            "returned_count": min(len(records), limit),
            "next_cursor": None,
            "truncated": len(records) > limit,
            "has_more": len(records) > limit,
            "coverage": {
                "status": "complete" if collection_complete else "unknown",
                "description": "本校区本学年年级组正式集合已核对。"
                if collection_complete else "本次获准范围的完整覆盖尚未声明。",
            },
            "queried_at": self.repository.clock().isoformat(),
            "factual_at": at.isoformat(),
            "family_owner": "school_facts.education",
            "query_scope": "本次身份获准的明确对象及所问时点",
            "selection": {
                "scope": "selected",
                "query_text": query_text,
                "reference": reference,
                "related_ref": related_ref,
                "academic_year": academic_year,
                "campus_ref": campus_ref,
                "as_of": as_of.isoformat() if as_of else None,
            },
        }
        return EducationQueryResult(doc, (), proof)

    def reference_choices(self, *, actor, fact_kind: str, field_name: str):
        from app.composition.school_fact_directory_product import reference_choice_kind

        kind = reference_choice_kind(self.describe(actor=actor, fact_kind=fact_kind), field_name)
        if kind in EDUCATION_CONTENT_FIELDS or kind in {"student", "student_school_relationship"}:
            return self.query(actor=actor, fact_kind=kind)
        backend = self.directory.evidence
        if backend is None:
            raise PermissionError("关联候选缺少当前读取依据。")
        with backend.capture(actor=actor, current_time=True) as capture:
            result = self.query(actor=actor, fact_kind=kind)
            proof = capture.seal()
        # Use the same owner dispatch as public_fields() and proposal summaries.
        # Directory aliases may be valid inputs but are not canonical form values.
        for row in result.get("records", ()):
            source = self.adapter.external.source_for_reference(actor, kind, row["reference"])
            row["reference"] = self.adapter.external.reference_for(
                actor=actor, fact_kind=kind, source_key=source
            )
        names = None
        if kind == "assignment" and result.get("records"):
            by_reference = {}
            for row in result["records"]:
                reference = row.get("fields", {}).get("person_ref")
                if reference:
                    by_reference[reference] = self.adapter.external.source_for_reference(
                        actor, "person", reference
                    )
            if by_reference:
                names = self.adapter.external.read_records(
                    actor,
                    "person",
                    source_keys=tuple(dict.fromkeys(by_reference.values())),
                    fields=("display_name",),
                    as_of=None,
                )
                labels = {
                    row["source_key"]: row["content"].get("display_name")
                    for row in names["records"]
                }
                for row in result["records"]:
                    key = by_reference.get(row.get("fields", {}).get("person_ref"))
                    if labels.get(key):
                        row["label"] = f"{labels[key]} · {row['label']}"
        if names is not None:
            names["proof"].revalidate()
        if proof is None or proof.check(actor) is not True:
            raise PermissionError("关联候选读取依据已变化。")
        return result

    def query(self, **kwargs: Any) -> dict[str, Any]:
        kind = kwargs["fact_kind"]
        if kind in {"fact_quality", "policy_scope"} and self.dependencies is not None:
            return self.dependencies.query(**kwargs)
        if kind in EDUCATION_CONTENT_FIELDS:
            return self.query_with_proof(**kwargs).public_result
        # Existing reference choices retain their existing Reader and projection.
        actor = kwargs["actor"]
        if kind in {"student", "student_school_relationship"}:
            return self.family.query.query(**kwargs)
        from app.composition.school_fact_directory_product import FactQueryInput

        return self.directory.query(
            actor=actor,
            payload=FactQueryInput(
                fact_kind=kind,
                page_size=100,
                query_text=kwargs.get("query_text"),
                reference=kwargs.get("reference"),
                related_ref=kwargs.get("related_ref"),
                as_of=kwargs.get("as_of"),
                scope="all_authorized"
                if not any(kwargs.get(k) for k in ("query_text", "reference", "related_ref"))
                else "selected",
            ),
            bounded_pages=False,
        )
