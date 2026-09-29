"""Finite, authorized class labels with retained release proofs."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.composition.school_fact_education_reader import EducationReadStale, timestamp
from app.platform.contracts import StaffActorContext
from app.platform.school_facts.school_scope_reference import school_scope_reference_coordinates


@dataclass(frozen=True)
class ClassLabels:
    labels: dict[str, str]
    education: tuple[Any, ...] = ()
    external: tuple[Any, ...] = ()

    def revalidate(self, *, actor: StaffActorContext) -> None:
        for proof in (*self.education, *self.external):
            proof.revalidate(actor=actor)


def class_labels(
    *,
    repository: Any,
    external: Any,
    actor: StaffActorContext,
    names: dict[str, str],
    as_of: datetime,
    school_fact_version: int,
) -> ClassLabels:
    if not names or external is None:
        return ClassLabels(dict(names))
    proofs: list[Any] = []
    try:
        read = repository.read_records(
            actor=actor,
            kind="class_placement",
            fields=("class_source_key", "campus_source_key", "class_number"),
            filter={"class_source_key": sorted(names)},
            as_of=as_of,
        )
        proofs.append(read.proof)
        if (
            read.school_fact_version != school_fact_version
            or read.proof.school_fact_version != school_fact_version
        ):
            raise EducationReadStale("班级标签资料版本已变化，请重新查询。")
        placements: dict[str, list[dict[str, Any]]] = {}
        for row in read.records:
            for segment in row.get("segments", ()):
                end = timestamp(segment.get("valid_to"))
                start = timestamp(segment["effective_at"])
                if start is None:
                    raise ValueError("班级安排缺少事实时间。")
                if start <= as_of and (end is None or as_of < end):
                    content = segment["content"]
                    key = content.get("class_source_key")
                    if key not in names:
                        raise PermissionError("班级标签超出已选范围。")
                    placements.setdefault(key, []).append(content)
        selected = {key: rows[0] for key, rows in placements.items() if len(rows) == 1}
        campuses = tuple(
            sorted(
                {
                    row["campus_source_key"]
                    for row in selected.values()
                    if row.get("campus_source_key")
                }
            )
        )
        ext = ()
        campus_names: dict[str, str | None] = {}
        if campuses:
            response = external.read_records(
                actor, "location", source_keys=campuses, fields=("display_name",), as_of=as_of
            )
            proof = response.get("proof")
            if proof is None:
                raise PermissionError("校区标签缺少读取依据。")
            if proof.school_fact_version != school_fact_version:
                raise EducationReadStale("班级标签资料版本已变化，请重新查询。")
            ext = (proof,)
            for row in response.get("records", ()):
                if row.get("source_key") not in campuses or not set(row.get("content", {})) <= {
                    "display_name"
                }:
                    raise PermissionError("校区标签超出已选范围。")
                campus_names[row["source_key"]] = row.get("content", {}).get("display_name")
        labels: dict[str, str] = {}
        for key, name in names.items():
            placement = selected.get(key, {})
            campus = campus_names.get(str(placement.get("campus_source_key", "")))
            parts = [name, campus or "校区尚未核实"]
            if placement.get("class_number") is not None:
                parts.append(str(placement["class_number"]) + "班")
            labels[key] = " · ".join(parts)
        return ClassLabels(labels, tuple(proofs), ext)
    except PermissionError:
        return ClassLabels(
            {key: name + "（校区信息未获准）" for key, name in names.items()}, tuple(proofs)
        )


@dataclass(frozen=True)
class PreviewLabels:
    labels: dict[str, str]
    actor: StaffActorContext
    school_fact_version: int
    education: tuple[Any, ...] = ()
    external: tuple[Any, ...] = ()

    def revalidate(self, *, actor: StaffActorContext) -> None:
        if actor != self.actor:
            raise PermissionError("名称核验身份已变化。")
        for proof in self.education:
            if proof.school_fact_version != self.school_fact_version:
                raise EducationReadStale("名称资料版本已变化。")
            proof.revalidate(actor=actor)
        for proof in self.external:
            if proof.school_fact_version != self.school_fact_version:
                raise EducationReadStale("名称资料版本已变化。")
            proof.revalidate()


def teaching_preview_labels(*, adapter, actor, summary, as_of, school_fact_version):
    """Current authorized names annotate immutable references, never replace them."""
    from app.composition.school_fact_education_external import EducationRelatedReadIncomplete

    fields = [summary.before or {}, summary.after or {}]
    fields.extend(s["fields"] for s in (*summary.before_segments, *summary.after_segments))
    choices = set()
    kinds = {
        "assignment_ref": "assignment",
        "offering_ref": "teaching_offering",
        "subject_refs": "teaching_subject",
    }
    for content in fields:
        for field, kind in kinds.items():
            value = content.get(field)
            for ref in value if isinstance(value, list) else [value]:
                coordinate = (
                    school_scope_reference_coordinates(ref) if isinstance(ref, str) else None
                )
                if isinstance(ref, str) and not (
                    ref.startswith("f4create:") or coordinate is not None and coordinate[2]
                ):
                    choices.add((kind, ref))
    labels, education, external = {}, [], []

    def external_content(kind, key, requested):
        if adapter.external is None:
            raise PermissionError("名称读取不可用。")
        result = adapter.external.read_records(
            actor, kind, source_keys=(key,), fields=requested, as_of=as_of, include_boundaries=False
        )
        if not isinstance(result, dict) or result.get("proof") is None:
            raise PermissionError("名称缺少读取依据。")
        proof = result["proof"]
        if proof.school_fact_version != school_fact_version:
            raise EducationReadStale("名称资料版本已变化。")
        rows = result.get("records", ())
        if (
            len(rows) != 1
            or rows[0].get("source_key") != key
            or set(rows[0].get("content", {})) != set(requested)
        ):
            raise PermissionError("名称读取超出明确对象或字段。")
        external.append(proof)
        return rows[0]["content"]

    for kind, ref in sorted(choices):
        try:
            key = adapter._source(actor, kind, ref)
            if kind == "assignment":
                owner = external_content(kind, key, ("person_source_key",))
                content = external_content("person", owner["person_source_key"], ("display_name",))
                name = content.get("display_name")
            else:
                result = adapter.repository.read_records(
                    actor, kind, source_keys=(key,), fields=("display_name",), as_of=as_of
                )
                if (
                    result.school_fact_version != school_fact_version
                    or result.proof.school_fact_version != school_fact_version
                ):
                    raise EducationReadStale("名称资料版本已变化。")
                education.append(result.proof)
                if len(result.records) != 1 or result.records[0].get("source_key") != key:
                    continue
                segments = [
                    s
                    for s in result.records[0].get("segments", ())
                    if timestamp(s["effective_at"]) <= as_of
                    and (s.get("valid_to") is None or as_of < timestamp(s["valid_to"]))
                ]
                if len(segments) != 1 or set(segments[0].get("content", {})) != {"display_name"}:
                    continue
                name = segments[0]["content"].get("display_name")
            if isinstance(name, str) and name.strip():
                labels[ref] = name
        except EducationReadStale:
            raise
        except (PermissionError, EducationRelatedReadIncomplete, RuntimeError):
            # A label is optional; the signed reference and complete diff remain authoritative.
            continue
    result = PreviewLabels(labels, actor, school_fact_version, tuple(education), tuple(external))
    result.revalidate(actor=actor)
    return result
