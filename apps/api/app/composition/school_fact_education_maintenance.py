"""Finite education impact expansion before the existing confirmation boundary."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.composition.school_fact_directory_product import FactChangeSummary, ResolvedFactChange
from app.composition.school_fact_education_product import (
    EDUCATION_LABELS,
    REFERENCE_KINDS,
    EducationBatchInput,
    EducationChangeInput,
    EducationProductAdapter,
    _at,
)
from app.domains.students.education_change_planner import (
    EducationChangeCommand,
    EducationFactSnapshot,
    EducationTimelineSegment,
    EndProvenance,
    plan_education_change,
)
from app.domains.students.education_contracts import EDUCATION_CONTENT_FIELDS
from app.platform.contracts import StaffActorContext
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1
from app.platform.school_facts.school_scope_reference import school_scope_reference_coordinates


def _creation_reference(reference: str) -> bool:
    coordinates = school_scope_reference_coordinates(reference)
    return reference.startswith("f4create:") or coordinates is not None and coordinates[2]


def _education_reference_kind(reference: str) -> str:
    coordinates = school_scope_reference_coordinates(reference)
    if coordinates is not None:
        if coordinates[0] not in {"f4", "f5"} or coordinates[2]:
            raise ValueError("请选择正式教育对象引用。")
        return coordinates[1]
    parts = reference.split(":")
    if len(parts) != 3 or parts[0] not in {"f4", "f5"}:
        raise ValueError("请选择正式教育对象引用。")
    return parts[1]


def snapshot_at(row: dict[str, Any], *, kind: str, at: datetime) -> EducationFactSnapshot | None:
    raw_segments = tuple(row.get("segments", ()))
    if not raw_segments:
        return None
    segments = []
    for segment in raw_segments:
        provenance = None
        if segment.get("cause_source_key"):
            prior = [
                p
                for p in row.get("prior_segments", ())
                if _at(p["effective_at"]) == _at(segment["effective_at"])
            ]
            if "causal_prior" in segment:
                prior = (
                    [segment["causal_prior"]] if isinstance(segment["causal_prior"], dict) else []
                )
            if len(prior) == 1 and (
                "causal_prior" in segment
                or prior[0].get("revision", row["version"] - 1) == row["version"] - 1
            ):
                provenance = EndProvenance(
                    cause_source_key=segment["cause_source_key"],
                    cause_version=segment["cause_version"],
                    ended_target_version=prior[0].get("ended_target_version", row["version"]),
                    prior_effective_at=_at(prior[0]["effective_at"]),
                    prior_content={**prior[0]["content"], "valid_to": prior[0].get("valid_to")},
                )
        segments.append(
            EducationTimelineSegment(
                effective_at=_at(segment["effective_at"]),
                valid_to=_at(segment.get("valid_to")),
                content={k: v for k, v in segment["content"].items() if k != "valid_to"},
                cause_source_key=segment.get("cause_source_key"),
                cause_version=segment.get("cause_version"),
                provenance=provenance,
            )
        )
    segments.sort(key=lambda s: s.effective_at)
    current = [
        s for s in segments if s.effective_at <= at and (s.valid_to is None or at < s.valid_to)
    ]
    if len(current) > 1:
        raise ValueError("Conflicting current education timeline")
    future = [s for s in segments if s.effective_at > at]
    segment = current[0] if current else future[0] if future else segments[-1]
    return EducationFactSnapshot(
        fact_kind=kind,
        source_key=row["source_key"],
        version=row["version"],
        effective_at=segment.effective_at,
        content={**segment.content, "valid_to": segment.valid_to},
        provenance=segment.provenance,
        cancelled=row.get("status") == "cancelled",
        segments=tuple(segments),
        prior_segments=tuple(
            EducationTimelineSegment(
                effective_at=_at(s["effective_at"]),
                valid_to=_at(s.get("valid_to")),
                content={k: v for k, v in s["content"].items() if k != "valid_to"},
            )
            for s in row.get("prior_segments", ())
        ),
        latest_operation=row.get("latest_operation"),
    )


def _merge_teaching_restorations(
    changes: list[dict[str, Any]], snapshot: EducationFactSnapshot, *, reason: str
) -> dict[str, Any]:
    """Compose disjoint proven restorations against one unchanged root version."""
    if len(changes) == 1:
        return changes[0]
    original = {s.effective_at: s for s in snapshot.segments}
    restored: dict[datetime, dict[str, Any]] = {}
    for change in changes:
        rows = change["replacement_segments"]
        if len(rows) != len(original) or {s["effective_at"] for s in rows} != original.keys():
            raise ValueError("同批取消的关联区间不一致，须重新核对完整影响。")
        for row in rows:
            start = row["effective_at"]
            before = original[start]
            if row["valid_to"] == before.valid_to and row["content"] == before.content:
                continue
            previous = restored.get(start)
            if previous is not None and (
                row["valid_to"] != previous["valid_to"] or row["content"] != previous["content"]
            ):
                raise ValueError("同批取消对同一关联区间给出不同恢复，须明确处置。")
            # The two primary cancellations retain their own causal lineage in this
            # atomic batch. A combined correction must not claim one shared cause.
            restored[start] = {**row, "cause_source_key": None, "cause_version": None}
    return {
        **changes[0],
        "effective_at": min(c["effective_at"] for c in changes),
        "correction_reason": reason,
        "cause_source_key": None,
        "cause_version": None,
        "replacement_segments": [
            restored.get(s.effective_at)
            or dict(
                effective_at=s.effective_at,
                valid_to=s.valid_to,
                content=dict(s.content),
                cause_source_key=s.cause_source_key,
                cause_version=s.cause_version,
            )
            for s in snapshot.segments
        ],
    }


def resolve_family_with_education(
    *, maintenance, fallback, actor, payload, request_id, effective_at
):
    """Keep the F3 entry on the shared preview/causal/confirmation boundary.

    The complete ResolvedFactChange is returned intact, including every teaching
    impact and the proof callback. The coordinator still calls the raw F3 adapter
    internally; the composition closure must not replace that adapter itself.
    """
    if (
        maintenance is not None
        and payload.fact_kind == "student_school_relationship"
        and payload.target_ref
        and (
            payload.operation in {"upsert", "correct"}
            or (
                getattr(maintenance, "canonical_version", 3) == 4
                and payload.operation in {"end", "revoke"}
            )
        )
    ):
        return maintenance.resolve_change(
            actor=actor,
            payload=EducationBatchInput.model_validate(payload.model_dump()),
            request_id=request_id,
            effective_at=effective_at,
        )
    return fallback(actor=actor, payload=payload, request_id=request_id, effective_at=effective_at)


def _school_relationship_impacts(primary, snapshots, *, read, reason, now):
    """F6's original F3 owner closes exact F4/F5 dependencies before residence.

    Read closures remain the education repository's complete, authorized filters.
    Ending one school relationship cannot end a different relationship's members.
    """
    from app.domains.students.education_versions import EducationSlice, revise_timeline

    def timeline(snapshot):
        return tuple(
            EducationSlice(s.effective_at, s.valid_to, s.content) for s in snapshot.segments
        )

    def after(change, original):
        if change.get("replacement_segments") is not None:
            return tuple(
                EducationSlice(_at(s["effective_at"]), _at(s.get("valid_to")), s["content"])
                for s in change["replacement_segments"]
            )
        content = change.get("content")
        return revise_timeline(
            original,
            operation=change["operation"],
            at=_at(change["effective_at"]),
            until=_at((content or {}).get("valid_to")),
            payload=None
            if content is None
            else {k: v for k, v in content.items() if k != "valid_to"},
            now=now,
        )

    def merge(periods):
        result = []
        for start, end in sorted(periods, key=lambda p: p[0]):
            if result and (result[-1][1] is None or start <= result[-1][1]):
                prior_start, prior_end = result[-1]
                result[-1] = (
                    prior_start,
                    None if prior_end is None or end is None else max(prior_end, end),
                )
            else:
                result.append((start, end))
        return tuple(result)

    def eligible(rows):
        return merge(
            (s.start, s.end)
            for s in rows
            if s.payload["enrollment_status"] in {"enrolled", "suspended"}
        )

    roots, periods = {}, {}
    for change in primary:
        if change["fact_kind"] != "student_school_relationship":
            continue
        key = change["source_key"]
        before = timeline(snapshots[key])
        revised = after(change, before)
        if eligible(before) == eligible(revised):
            continue
        students = {s.payload["student_source_key"] for s in (*before, *revised)}
        if len(students) != 1:
            raise ValueError("在校关系的学生归属无法唯一核实。")
        roots[key] = change
        periods[key] = eligible(revised)
        for kind in ("student_class_membership", "student_class_position", "teaching_group_member"):
            read(kind, filters={"student_source_key": next(iter(students))})
        if len(snapshots) > 300:
            raise ValueError("在校关系完整影响超过单批容量，不能拆分原子变更。")
    if not roots:
        return ()

    proposed = {c["source_key"]: c for c in primary}
    revised_by_key = {
        k: after(proposed[k], timeline(s)) if k in proposed else timeline(s)
        for k, s in snapshots.items()
    }
    extras = []

    def clip(row, coverage):
        # Retain only the continuous authorized prefix. A later eligibility period
        # never silently revives a dependent arrangement after an uncovered gap.
        for start, end in coverage:
            if start <= row.start and (end is None or row.start < end):
                return EducationSlice(
                    row.start,
                    row.end if end is None else end if row.end is None else min(row.end, end),
                    row.payload,
                )
        return None

    def save(snapshot, revised, cause):
        original = revised_by_key[snapshot.source_key]
        revised = tuple(s for s in revised if s is not None)
        if revised == original:
            return
        if snapshot.source_key in proposed:
            raise ValueError("同批明确安排仍超出在校或归班期间，请核对完整差异。")
        revised_by_key[snapshot.source_key] = revised
        prior = {s.effective_at: s for s in snapshot.segments}

        def segment(row):
            old = prior[row.start]
            unchanged = old.valid_to == row.end and old.content == row.payload
            return dict(
                effective_at=row.start,
                valid_to=row.end,
                content=dict(row.payload),
                cause_source_key=old.cause_source_key if unchanged else cause["source_key"],
                cause_version=old.cause_version if unchanged else cause["expected_version"] + 1,
            )

        extras.append(
            dict(
                schema_version=4,
                fact_kind=snapshot.fact_kind,
                source_key=snapshot.source_key,
                expected_version=snapshot.version,
                operation="correct",
                effective_at=cause["effective_at"],
                content=None,
                correction_reason=reason,
                cause_source_key=cause["source_key"],
                cause_version=cause["expected_version"] + 1,
                replacement_segments=[segment(s) for s in revised],
                origin={"sheet_code": "school_relationship_impact", "row_number": len(extras) + 1},
            )
        )

    changed_classes = {}
    for snapshot in tuple(snapshots.values()):
        if snapshot.fact_kind not in {"student_class_membership", "teaching_group_member"}:
            continue
        revised, causes = [], []
        for row in revised_by_key[snapshot.source_key]:
            key = row.payload["school_relationship_source_key"]
            cut = clip(row, periods[key]) if key in roots else row
            revised.append(cut)
            if cut != row:
                causes.append(roots[key])
                if snapshot.fact_kind == "student_class_membership":
                    changed_classes[
                        row.payload["student_source_key"], row.payload["class_source_key"]
                    ] = roots[key]
        if causes:
            save(snapshot, revised, min(causes, key=lambda c: c["effective_at"]))
    for snapshot in tuple(snapshots.values()):
        if snapshot.fact_kind != "student_class_position":
            continue
        revised, causes = [], []
        for row in revised_by_key[snapshot.source_key]:
            coordinate = row.payload["student_source_key"], row.payload["class_source_key"]
            if coordinate not in changed_classes:
                revised.append(row)
                continue
            coverage = merge(
                (member.start, member.end)
                for key, value in snapshots.items()
                if value.fact_kind == "student_class_membership"
                for member in revised_by_key[key]
                if (member.payload["student_source_key"], member.payload["class_source_key"])
                == coordinate
                and member.payload["membership_status"] == "assigned"
            )
            revised.append(clip(row, coverage))
            causes.append(changed_classes[coordinate])
        if causes:
            save(snapshot, revised, min(causes, key=lambda c: c["effective_at"]))
    return tuple(extras)


class EducationMaintenanceCoordinator:
    def __init__(
        self,
        adapter: EducationProductAdapter,
        *,
        family: Any,
        dependencies: Any = None,
        canonical_version: int = 3,
    ) -> None:
        self.adapter, self.repository, self.family = adapter, adapter.repository, family
        self.dependencies = dependencies
        if canonical_version not in {3, 4}:
            raise ValueError("Unsupported education maintenance contract.")
        self.canonical_version = canonical_version

    def _school_relation(
        self, actor: StaffActorContext, item: EducationChangeInput, request_id: UUID, at: datetime
    ) -> ResolvedFactChange:
        operations = {"upsert", "correct"} | (
            {"end", "revoke"} if self.canonical_version == 4 else set()
        )
        if item.creation_ref or item.operation not in operations:
            raise ValueError("本教育入口只更新或补正已正式登记的在校关系。")
        if item.operation in {"end", "revoke"} and item.fields:
            raise ValueError("结束或撤销在校期间不能附带修改字段，请使用明确补正。")
        kind, source = self.family.repository.source_for_reference(actor, item.target_ref)
        if kind != "student_school_relationship":
            raise ValueError("必须选择原学生在校关系。")
        # The F4-only projection independently checks the complete F3 read scope.
        timeline = self.family.repository.read_education_timeline(actor=actor, source_key=source)
        original = snapshot_at(timeline, kind=kind, at=at)
        if original is None:
            raise ValueError("已清空在校关系须明确登记新安排。")
        fields = self.family.repository.resolve_field_references(
            actor=actor, fact_kind=kind, fields=item.fields
        )
        content = {**original.content, **fields}
        from app.domains.students.education_versions import EducationSlice, revise_timeline

        terminal = item.operation in {"end", "revoke"}
        if terminal:
            revised = tuple(
                EducationSlice(s.effective_at, min(s.valid_to, at) if s.valid_to else at, s.content)
                for s in original.segments
                if s.effective_at < at
            )
            if revised == tuple(
                EducationSlice(s.effective_at, s.valid_to, s.content) for s in original.segments
            ):
                raise ValueError("本次结束或撤销没有可裁切的在校期间，请核对原日期。")
        else:
            revised = revise_timeline(
                tuple(
                    EducationSlice(s.effective_at, s.valid_to, s.content) for s in original.segments
                ),
                operation=item.operation,
                at=at,
                until=_at(content.get("valid_to")),
                payload={k: v for k, v in content.items() if k != "valid_to"},
                now=datetime.now(UTC),
            )
        data = {
            "schema_version": self.canonical_version,
            "source_sha256": hashlib.sha256(str(request_id).encode()).hexdigest(),
            "base_version": timeline["school_fact_version"],
            "changes": [
                {
                    "schema_version": self.canonical_version,
                    "fact_kind": kind,
                    "source_key": source,
                    "expected_version": timeline["version"],
                    "operation": "correct" if terminal else item.operation,
                    "effective_at": at.isoformat(),
                    "content": None if terminal else content,
                    "correction_reason": (
                        item.reason
                        + ("（原操作：结束）" if item.operation == "end" else "（原操作：撤销）")
                    )
                    if terminal
                    else item.reason
                    if item.operation == "correct"
                    else None,
                    **(
                        {
                            "replacement_segments": [
                                dict(effective_at=s.start, valid_to=s.end, content=dict(s.payload))
                                for s in revised
                            ]
                        }
                        if terminal
                        else {}
                    ),
                    "origin": {"sheet_code": "education_maintenance", "row_number": 1},
                }
            ],
        }
        cs = CanonicalFactChangeSetV1.model_validate_json(json.dumps(data, default=str))
        public = self.family.repository.public_field_references

        def public_slice(start, end, value):
            result = {
                "effective_at": start.isoformat(),
                "valid_to": None if end is None else end.isoformat(),
                "fields": public(actor=actor, fact_kind=kind, fields=dict(value)),
            }
            matching = [
                s
                for s in timeline["segments"]
                if _at(s["effective_at"]) <= start
                and (s.get("valid_to") is None or start < _at(s["valid_to"]))
                and {k: v for k, v in s["content"].items() if k != "valid_to"} == dict(value)
            ]
            if len(matching) == 1:
                result["source"] = {
                    k: v
                    for k, v in (matching[0].get("source") or {}).items()
                    if k
                    in {
                        "source_kind",
                        "source_ref",
                        "batch_id",
                        "published_at",
                        "publication_reference",
                    }
                }
            return result

        observed_version = (timeline["version"], timeline["school_fact_version"])

        def revalidate(current):
            latest = self.family.repository.read_education_timeline(
                actor=current, source_key=source
            )
            if (latest["version"], latest["school_fact_version"]) != observed_version:
                raise ValueError("在校关系及完整影响已变化，请重新预览。")

        return ResolvedFactChange(
            cs,
            FactChangeSummary(
                fact_kind=kind,
                label="学生在校关系",
                reference=item.target_ref,
                operation=item.operation,
                before=public(
                    actor=actor,
                    fact_kind=kind,
                    fields={k: v for k, v in original.content.items() if k != "valid_to"},
                ),
                after=None
                if terminal
                else {
                    **public(
                        actor=actor,
                        fact_kind=kind,
                        fields={k: v for k, v in content.items() if k != "valid_to"},
                    ),
                    "valid_to": None if content["valid_to"] is None else str(content["valid_to"]),
                },
                effective_at=at,
                reason=item.reason,
                source_label="学校正式在校关系",
                before_segments=tuple(
                    public_slice(s.effective_at, s.valid_to, s.content) for s in original.segments
                ),
                after_segments=tuple(public_slice(s.start, s.end, s.payload) for s in revised),
            ),
            revalidate,
        )

    def resolve_import(self, *, actor, payload, request_id, effective_at):
        from app.domains.students.teaching_contracts import TEACHING_KINDS
        from app.domains.students.teaching_excel import plan_teaching_import

        if payload.fact_kind not in TEACHING_KINDS or payload.additional_changes:
            raise ValueError("教学导入须选择唯一教学类别，不能混入其他批次。")
        references = set(payload.import_target_refs) | set(payload.import_replacement_refs or ())
        references.add(payload.target_ref or payload.creation_ref)
        by_key, snapshots, proofs = {}, [], []
        versions = set()
        for reference in sorted(references):
            key = self.adapter._source(actor, payload.fact_kind, reference)
            if key in by_key:
                raise ValueError("同一导入来源不能具有多个选择引用。")
            by_key[key] = reference
            result = self.repository.read_records(
                actor,
                payload.fact_kind,
                fields=tuple(sorted(EDUCATION_CONTENT_FIELDS[payload.fact_kind])),
                source_keys=(key,),
                as_of=effective_at,
                creation=_creation_reference(reference),
                action="maintain" if _creation_reference(reference) else "read",
            )
            proofs.append(result.proof)
            versions.add(result.school_fact_version)
            if len(result.records) != 1:
                raise ValueError("导入对象未得到完整当前快照。")
            if result.records[0]["version"]:
                snapshot = snapshot_at(result.records[0], kind=payload.fact_kind, at=effective_at)
                if snapshot is None:
                    raise ValueError("已取消对象不能通过增量导入自动恢复。")
                snapshots.append(snapshot)
        if len(versions) != 1:
            raise ValueError("导入准备期间学校资料已变化。")
        import_rows = []
        for raw in payload.import_rows:
            row = dict(raw)
            if "reference" in row:
                reference = row.pop("reference")
                if reference not in references or "source_key" in row:
                    raise ValueError("表格对象引用未经本次明确选择。")
                row["source_key"] = self.adapter._source(actor, payload.fact_kind, reference)
            for field, kind in REFERENCE_KINDS.items():
                if field.endswith("_source_keys"):
                    continue
                value = row.get(field)
                if value not in (None, "", "__CLEAR__"):
                    row[field] = self.adapter._source(actor, kind, value)
            for field, kind in {
                "grade_source_keys": "education_grade",
                "stage_source_keys": "education_stage",
                "subject_source_keys": "teaching_subject",
            }.items():
                value = row.get(field)
                if value not in (None, "", "__CLEAR__"):
                    values = json.loads(value) if isinstance(value, str) else value
                    if not isinstance(values, (list, tuple)):
                        raise ValueError("表格引用数组须为正式对象引用列表。")
                    row[field] = [self.adapter._source(actor, kind, ref) for ref in values]
            import_rows.append(row)
        if any(str(row.get("source_key", "")) not in by_key for row in import_rows):
            raise ValueError("导入稳定来源未选择已核实的对象或创建项。")
        replacement_keys = (
            None
            if payload.import_replacement_refs is None
            else frozenset(
                self.adapter._source(actor, payload.fact_kind, ref)
                for ref in payload.import_replacement_refs
            )
        )
        plan = plan_teaching_import(
            payload.fact_kind,
            import_rows,
            tuple(snapshots),
            effective_at=effective_at,
            mode=payload.import_mode,
            replacement_keys=replacement_keys,
        )
        if not plan.changes:
            raise ValueError("内容无变化，无需生成正式操作。")
        changes = []
        for change in plan.changes:
            reference = by_key[change["source_key"]]
            changes.append(
                dict(
                    fact_kind=payload.fact_kind,
                    operation=change["operation"],
                    creation_ref=reference if _creation_reference(reference) else None,
                    target_ref=None if _creation_reference(reference) else reference,
                    fields=self.adapter.public_fields(actor, change.get("content") or {}),
                    effective_at=change["effective_at"],
                    reason=payload.reason,
                )
            )
        request = EducationBatchInput.model_validate_json(
            json.dumps(
                {**changes[0], "additional_changes": changes[1:], "purpose": "record"}, default=str
            )
        )
        resolved = self.resolve_change(
            actor=actor, payload=request, request_id=request_id, effective_at=effective_at
        )
        if resolved.change_set.base_version != next(iter(versions)):
            raise ValueError("导入完整影响已变化，须重新预览。")
        original = resolved.summary_revalidate

        def revalidate(current):
            for proof in proofs:
                proof.revalidate(actor=current)
            if original:
                original(current)

        revalidate(actor)
        return ResolvedFactChange(resolved.change_set, resolved.summary, revalidate)

    def resolve_change(
        self,
        *,
        actor: StaffActorContext,
        payload: EducationBatchInput,
        request_id: UUID,
        effective_at: datetime,
    ) -> ResolvedFactChange:
        from app.domains.students.teaching_contracts import TEACHING_KINDS

        cancel_inputs = tuple(
            c
            for c in (payload, *payload.additional_changes)
            if c.fact_kind in TEACHING_KINDS and c.operation == "cancel"
        )
        teaching_cancel = bool(cancel_inputs)
        if teaching_cancel:
            if any(c.cancel_from is None for c in cancel_inputs):
                raise ValueError("请选择要取消的未来安排的准确开始时点。")
            effective_at = getattr(self.repository, "clock", lambda: datetime.now(UTC))()
            if any(c.cancel_from <= effective_at for c in cancel_inputs):
                raise ValueError("只能取消尚未生效的教学安排。")
        if payload.import_rows is not None:
            return self.resolve_import(
                actor=actor, payload=payload, request_id=request_id, effective_at=effective_at
            )
        items = (
            EducationChangeInput.model_validate_json(
                payload.model_dump_json(
                    exclude={
                        "additional_changes",
                        "purpose",
                        "corrected_purpose",
                        "student_ref",
                        "old_class_ref",
                        "replace_refs",
                        "reschedule_from",
                        "cancel_from",
                        "import_rows",
                        "import_mode",
                        "import_target_refs",
                        "import_replacement_refs",
                    }
                )
            ),
            *payload.additional_changes,
        )
        creation_references = {}
        for item in items:
            if item.creation_ref and item.fact_kind in EDUCATION_CONTENT_FIELDS:
                source = self.adapter._source(actor, item.fact_kind, item.creation_ref)
                creation_references[(item.fact_kind, source)] = item.creation_ref
        resolved = []
        for item in items:
            method = (
                self._school_relation if item.fact_kind == "student_school_relationship" else None
            )
            at = (
                effective_at
                if item.fact_kind in TEACHING_KINDS and item.operation == "cancel"
                else item.effective_at or effective_at
            )
            if item.fact_kind in {"fact_quality", "policy_scope"}:
                if self.dependencies is None or payload.purpose != "record":
                    raise ValueError(
                        "Dependency maintenance requires its own finite record adapter"
                    )
                resolved.append(
                    self.dependencies.resolve_change(
                        actor=actor, payload=item, request_id=request_id, effective_at=at
                    )
                )
                continue
            result = (
                method(actor, item, request_id, at)
                if method
                else self.adapter.resolve_change(
                    actor=actor,
                    payload=item,
                    request_id=request_id,
                    effective_at=at,
                    creation_references=creation_references,
                )
            )
            resolved.append(result)
        versions = {r.change_set.base_version for r in resolved}
        if len(versions) != 1:
            raise ValueError("学校资料在预览期间已变化，请重新核对整批。")
        base = next(iter(versions))
        primary = [
            c.model_dump(mode="json", exclude_unset=True)
            for r in resolved
            for c in r.change_set.changes
        ]
        for change in primary:
            change["effective_at"] = _at(change["effective_at"])
            change.setdefault("content", None)
        if len({c["source_key"] for c in primary}) != len(primary):
            raise ValueError("同一稳定来源在本批重复出现，请明确合并后的唯一变更。")
        cancel_boundaries = {
            self.adapter._source(actor, c.fact_kind, c.target_ref): c.cancel_from
            for c in cancel_inputs
        }
        cancellation_proofs = {}
        snapshots: dict[str, EducationFactSnapshot] = {}
        proofs = []
        records_by_key: dict[str, dict[str, Any]] = {}

        def read(
            kind: str, *, filters: dict[str, str] | None = None, keys: tuple[str, ...] | None = None
        ):
            if keys is not None:
                keys = tuple(k for k in keys if (kind, k) not in creation_references)
                if not keys:
                    return  # These exact roots were checked as absent by their creation reads.
            result = self.repository.read_records(
                actor,
                kind,
                fields=tuple(sorted(EDUCATION_CONTENT_FIELDS[kind])),
                source_keys=keys,
                filter=filters,
                as_of=effective_at,
            )
            if result.school_fact_version != base:
                raise ValueError("联动影响已变化，请重新核对整批。")
            proofs.append(result.proof)
            for row in result.records:
                if row["version"] < 1:
                    continue
                records_by_key[row["source_key"]] = row
                snapshot = snapshot_at(row, kind=kind, at=effective_at)
                if snapshot is not None:
                    snapshots[row["source_key"]] = snapshot

        # Exact primary coordinates are always part of the concurrency snapshot.
        for change in primary:
            kind, key = change["fact_kind"], change["source_key"]
            if kind in EDUCATION_CONTENT_FIELDS and change["expected_version"]:
                read(kind, keys=(key,))
                if change["operation"] == "cancel" and key in snapshots:
                    # Canonical cancellation identifies the future arrangement's boundary;
                    # planner.current decision time remains the preview's effective_at.
                    future = [s for s in snapshots[key].segments if s.effective_at > effective_at]
                    if not future:
                        raise ValueError("没有尚未生效的安排可撤销。")
                    from app.domains.students.teaching_contracts import TEACHING_KINDS

                    if kind in TEACHING_KINDS:
                        if key not in cancel_boundaries:
                            raise ValueError("每次取消须明确选择一个未来教学切片。")
                        selected = [s for s in future if s.effective_at == cancel_boundaries[key]]
                        if len(selected) == 1:
                            change["effective_at"] = selected[0].effective_at
                        else:
                            raise ValueError("所选未来教学切片不存在或已生效。")
                        continue
                    change["effective_at"] = (
                        future[-1].effective_at
                        if snapshots[key].latest_operation == "upsert"
                        else future[0].effective_at
                    )
            elif kind == "student_school_relationship":
                row = self.family.repository.read_education_timeline(actor=actor, source_key=key)
                if (
                    row["version"] != change["expected_version"]
                    or row["school_fact_version"] != base
                ):
                    raise ValueError("在校关系已变化，请重新预览。")
                records_by_key[key] = row
                snapshot = snapshot_at(row, kind=kind, at=effective_at)
                if snapshot is None:
                    raise ValueError("在校关系时间轴为空。")
                snapshots[key] = snapshot

        from app.domains.students.teaching_change_planner import (
            plan_teaching_change,
            reschedule_teaching_future,
            teaching_change_notices,
            teaching_impact_requests,
        )
        from app.domains.students.teaching_contracts import TEACHING_KINDS

        if payload.reschedule_from is not None:
            key = primary[0]["source_key"]
            if key not in snapshots:
                raise ValueError("未来改期缺少原正式安排快照。")
            primary[0] = reschedule_teaching_future(
                primary[0], snapshots[key], from_at=payload.reschedule_from, now=effective_at
            )
        if (
            payload.purpose == "replace_assignment"
            and primary[0]["fact_kind"] == "teaching_assignment"
        ):
            for reference in payload.replace_refs:
                key = self.adapter._source(actor, "teaching_assignment", reference)
                if any(c["source_key"] == key for c in primary):
                    raise ValueError("任教交接的原安排不能与同批修改重复。")
                read("teaching_assignment", keys=(key,))
                if key not in snapshots:
                    raise ValueError("任教交接缺少明确原安排。")
                primary.append(
                    dict(
                        schema_version=3,
                        fact_kind="teaching_assignment",
                        source_key=key,
                        expected_version=snapshots[key].version,
                        operation="end",
                        content=None,
                        effective_at=primary[0]["effective_at"],
                        cause_source_key=primary[0]["source_key"],
                        cause_version=primary[0]["expected_version"] + 1,
                        origin={"sheet_code": "teaching_handoff", "row_number": len(primary) + 1},
                    )
                )
        if teaching_cancel:
            from app.domains.students.teaching_change_planner import cancel_teaching_future

            for index, change in enumerate(primary):
                key = change["source_key"]
                if key not in cancel_boundaries:
                    continue
                evidence = self.repository.read_records(
                    actor,
                    change["fact_kind"],
                    source_keys=(key,),
                    fields=tuple(sorted(EDUCATION_CONTENT_FIELDS[change["fact_kind"]])),
                    as_of=effective_at,
                    cancellation_start=cancel_boundaries[key],
                    expected_version=change["expected_version"],
                )
                if evidence.school_fact_version != base or len(evidence.records) != 1:
                    raise ValueError("取消的正式前态或完整影响已变化，请重新预览。")
                proofs.append(evidence.proof)
                cancellation_proofs[key] = evidence.records[0]
                primary[index] = cancel_teaching_future(
                    change,
                    snapshots[key],
                    proof=evidence.records[0],
                    now=effective_at,
                )
        relationship_impacts = ()
        if self.canonical_version == 4:
            relationship_impacts = _school_relationship_impacts(
                primary, snapshots, read=read, reason=payload.reason, now=effective_at
            )
        teaching_primary = [
            c
            for c in primary
            if not (self.canonical_version == 4 and c["fact_kind"] == "student_school_relationship")
        ]
        teaching_scope = any(
            c["fact_kind"]
            in TEACHING_KINDS
            | {"student_school_relationship", "education_class", "student_class_membership"}
            for c in teaching_primary
        )
        if teaching_scope:
            seen_requests = set()
            while True:
                pending = (
                    set(teaching_impact_requests(teaching_primary, tuple(snapshots.values())))
                    - seen_requests
                )
                if not pending:
                    break
                for kind, field, key in sorted(pending):
                    read(kind, keys=(key,)) if field == "source_key" else read(
                        kind, filters={field: key}
                    )
                seen_requests.update(pending)
                if len(snapshots) > 300:
                    raise ValueError("教学完整影响超过单批容量，不能拆分原子变更。")

        if payload.reschedule_from is not None:
            from app.domains.students.teaching_change_planner import reschedule_teaching_dependents

            primary.extend(
                reschedule_teaching_dependents(
                    primary[0],
                    tuple(snapshots.values()),
                    from_at=payload.reschedule_from,
                    explicit_keys=frozenset(c["source_key"] for c in primary),
                )
            )
        if teaching_cancel:
            from app.domains.students.teaching_change_planner import cancel_teaching_dependents

            explicit_keys = frozenset(c["source_key"] for c in primary)
            restorations: dict[str, list[dict[str, Any]]] = {}
            for change in tuple(primary):
                if change["source_key"] in cancellation_proofs:
                    for restored in cancel_teaching_dependents(
                        change,
                        tuple(snapshots.values()),
                        proof=cancellation_proofs[change["source_key"]],
                        explicit_keys=explicit_keys,
                    ):
                        restorations.setdefault(restored["source_key"], []).append(restored)
            primary.extend(
                _merge_teaching_restorations(changes, snapshots[key], reason=payload.reason)
                for key, changes in restorations.items()
            )
        commands = []
        for index, change in enumerate(primary):
            kind, key = change["fact_kind"], change["source_key"]
            if self.canonical_version == 4 and kind == "student_school_relationship":
                continue  # Complete, exact relationship dependencies were handled above.
            content = (
                change.get("content") or snapshots.get(key).content
                if key in snapshots
                else change.get("content") or {}
            )
            operation = change["operation"]
            if operation == "cancel" and key in snapshots:
                selected = [
                    s
                    for s in snapshots[key].segments
                    if s.effective_at == _at(change["effective_at"])
                ]
                if len(selected) != 1:
                    raise ValueError("撤销必须明确唯一未来安排。")
                content = selected[0].content
            if kind in TEACHING_KINDS:
                continue  # Teaching endings and causal changes are expanded below.
            purpose = payload.purpose if index == 0 else "record"
            if (
                index > 0
                and payload.purpose == "historical_correction"
                and operation == "correct"
                and kind == primary[0]["fact_kind"]
            ):
                purpose = "historical_correction"
            student = content.get("student_source_key")
            class_key = content.get("class_source_key")
            if (
                kind == "student_school_relationship"
                and content.get("enrollment_status")
                in {"graduated", "transferred_out", "withdrawn"}
                and purpose != "historical_correction"
            ):
                purpose = "graduation"
            elif kind == "education_class" and operation == "end":
                purpose, class_key = "close_class", key
            elif kind in EDUCATION_CONTENT_FIELDS and operation == "end":
                purpose = "replace_assignment"
            elif operation == "cancel":
                purpose = "cancel_future"
            elif kind == "student_class_membership" and purpose == "record":
                purpose = "transfer"
            if payload.student_ref and index == 0:
                student = self.adapter._source(actor, "student", payload.student_ref)
            if payload.old_class_ref and index == 0:
                class_key = self.adapter._source(actor, "education_class", payload.old_class_ref)
            if purpose == "record":
                continue
            if student:
                for affected in ("student_class_membership", "student_class_position"):
                    read(affected, filters={"student_source_key": student})
            elif (class_key or kind == "education_class") and purpose != "replace_assignment":
                class_key = class_key or key
                for affected in (
                    "student_class_membership",
                    "student_class_position",
                    "class_teacher_assignment",
                    "class_location",
                    "class_placement",
                    "class_position_definition",
                ):
                    read(affected, filters={"class_source_key": class_key})
            if (
                payload.corrected_purpose if purpose == "historical_correction" else purpose
            ) == "transfer":
                # Find the OLD formal class, never replace the user's destination with it.
                old = [
                    s
                    for s in snapshots.values()
                    if s.fact_kind == "student_class_membership"
                    and (
                        s.source_key != key
                        or s.content.get("class_source_key") != content.get("class_source_key")
                    )
                    and s.content.get("student_source_key") == student
                    and s.effective_at < _at(change["effective_at"])
                    and (
                        _at(s.content.get("valid_to")) is None
                        or _at(change["effective_at"]) < _at(s.content["valid_to"])
                    )
                ]
                if not old:
                    if operation == "end" and key in snapshots:
                        class_key = snapshots[key].content.get("class_source_key")
                    else:
                        continue  # Initial/return placement; Writer still checks all intervals.
                elif len(old) == 1:
                    class_key = old[0].content.get("class_source_key")
                else:
                    raise ValueError("原归班关系存在冲突，须在本批明确纠正。")
            replacements = (
                frozenset(
                    self.adapter._source(actor, _education_reference_kind(ref), ref)
                    for ref in payload.replace_refs
                )
                if index == 0
                else frozenset()
            )
            if replacements:
                for ref in payload.replace_refs:
                    read(
                        _education_reference_kind(ref),
                        keys=(self.adapter._source(actor, _education_reference_kind(ref), ref),),
                    )
            elif purpose == "replace_assignment" and operation == "end":
                replacements = frozenset({key})
            commands.append((purpose, change, student, class_key, replacements))

        proposed = {c["source_key"]: c for c in (*primary, *relationship_impacts)}
        for purpose, change, student, class_key, replacements in commands:
            command = EducationChangeCommand(
                purpose=purpose,
                corrected_purpose=payload.corrected_purpose
                if purpose == "historical_correction"
                else None,
                effective_at=effective_at
                if purpose == "cancel_future"
                else _at(change["effective_at"]),
                cause_source_key=change["source_key"],
                cause_version=change["expected_version"] + (0 if purpose == "cancel_future" else 1),
                primary_changes=tuple(proposed.values()),
                student_source_key=student,
                class_source_key=class_key,
                replaced_source_keys=replacements,
                correction_reason=payload.reason,
            )
            plan = plan_education_change(
                command,
                tuple(snapshots.values()),
                authorized_keys=frozenset(proposed) | frozenset(snapshots),
                complete_snapshot=True,
            )
            if plan.issues:
                raise ValueError(
                    "本批关联影响尚未核清，需明确冲突、后续安排或缺少的维护对象后重新预检。"
                )
            proposed = {c["source_key"]: c for c in plan.changes}
        teaching_notes = teaching_change_notices(primary, tuple(snapshots.values()))
        teaching_plan = None
        if (
            teaching_scope
            and any(s.fact_kind in TEACHING_KINDS for s in snapshots.values())
            or any(c["fact_kind"] in TEACHING_KINDS for c in primary)
        ):
            retained_relationships = {
                k: c
                for k, c in proposed.items()
                if self.canonical_version == 4 and c["fact_kind"] == "student_school_relationship"
            }
            teaching_plan = plan_teaching_change(
                tuple(c for k, c in proposed.items() if k not in retained_relationships),
                tuple(snapshots.values()),
                now=effective_at,
                authorized_keys=frozenset(proposed) | frozenset(snapshots),
                complete_snapshot=True,
            )
            if teaching_plan.issues:
                raise ValueError(
                    "教学完整影响未核清，须处理未来安排、独立结束或批次容量后重新预览。"
                )
            proposed = {
                **retained_relationships,
                **{c["source_key"]: c for c in teaching_plan.changes},
            }
        if len(proposed) > 300:
            raise ValueError("完整联动超过单批容量，不能暗中拆分提交。")
        for proof in proofs:
            proof.revalidate(actor=actor)
        # A domain adapter can require a newer typed contract (for example,
        # complete coverage's server-derived mother-set basis). Preserve that
        # validated requirement when composing the atomic batch. This is local
        # to this batch: it does not enable additional lifecycle operations or
        # mutate this coordinator's configured maintenance policy.
        batch_version = max(
            self.canonical_version, *(item.change_set.schema_version for item in resolved)
        )
        changes = tuple(
            {**value, "schema_version": batch_version} for value in proposed.values()
        )
        digest = hashlib.sha256(
            json.dumps(changes, sort_keys=True, default=str).encode()
        ).hexdigest()
        cs = CanonicalFactChangeSetV1.model_validate_json(
            json.dumps(
                {
                    "schema_version": batch_version,
                    "source_sha256": digest,
                    "base_version": base,
                    "changes": changes,
                },
                default=str,
            )
        )
        explicit = {c.change_set.changes[0].source_key: c.summary for c in resolved}
        impacts = []
        label_proofs = []
        for change in cs.changes:
            key, kind = str(change.source_key), str(change.fact_kind)
            if key in explicit:
                summary = explicit[key]
                if kind == "education_class" and summary.effective_at != change.effective_at:
                    from app.composition.school_fact_education_labels import class_labels

                    names = {key: str((summary.before or {}).get("display_name") or "班级")}
                    labels = class_labels(
                        repository=self.repository,
                        external=self.adapter.external,
                        actor=actor,
                        names=names,
                        as_of=change.effective_at,
                        school_fact_version=base,
                    )
                    labels.revalidate(actor=actor)
                    label_proofs.append(labels)
                    summary = summary.model_copy(update={"label": labels.labels[key]})
                if kind in EDUCATION_CONTENT_FIELDS:
                    summary = summary.model_copy(
                        update={
                            "operation": str(change.operation),
                            "effective_at": change.effective_at,
                            "after": self.adapter.public_fields(
                                actor,
                                change.content.model_dump(mode="json", exclude_unset=True),
                                creation_references=creation_references,
                            )
                            if change.content is not None
                            else None,
                        }
                    )
            else:
                snapshot = snapshots[key]
                summary = FactChangeSummary(
                    fact_kind=kind,
                    label=EDUCATION_LABELS[kind],
                    reference=self.adapter._reference(actor, kind, key),
                    operation=str(change.operation),
                    before=self.adapter.public_fields(
                        actor,
                        {
                            k: v.isoformat() if isinstance(v, datetime) else v
                            for k, v in snapshot.content.items()
                        },
                    ),
                    after=self.adapter.public_fields(
                        actor,
                        change.content.model_dump(mode="json", exclude_unset=True),
                        creation_references=creation_references,
                    )
                    if change.content is not None
                    else None,
                    effective_at=change.effective_at,
                    reason=payload.reason,
                    source_label="学校正式登记资料",
                )
            if kind in EDUCATION_CONTENT_FIELDS:
                from app.domains.students.education_versions import EducationSlice, revise_timeline

                original = snapshots.get(key)
                old_segments = () if original is None else original.segments

                def public_segment(start, end, content, key=key):
                    value = {
                        "effective_at": start.isoformat(),
                        "valid_to": None if end is None else end.isoformat(),
                        "fields": self.adapter.public_fields(
                            actor, dict(content), creation_references=creation_references
                        ),
                    }
                    matching = [
                        s
                        for s in records_by_key.get(key, {}).get("segments", ())
                        if _at(s["effective_at"]) <= start
                        and (s.get("valid_to") is None or start < _at(s["valid_to"]))
                        and {k: v for k, v in s["content"].items() if k != "valid_to"}
                        == dict(content)
                    ]
                    if len(matching) == 1:
                        source = matching[0].get("source") or {}
                        value["source"] = {
                            k: v
                            for k, v in source.items()
                            if k
                            in {
                                "source_kind",
                                "source_ref",
                                "batch_id",
                                "publication_reference",
                                "published_at",
                            }
                        }
                        cause = matching[0].get("cause_source_key")
                        if cause in records_by_key:
                            cause_kind = records_by_key[cause]["fact_kind"]
                            value["cause_reference"] = self.adapter._reference(
                                actor, cause_kind, cause
                            )
                            value["cause_version"] = matching[0].get("cause_version")
                    return value

                before_segments = tuple(
                    public_segment(s.effective_at, s.valid_to, s.content) for s in old_segments
                )
                if change.replacement_segments is not None:
                    after_segments = tuple(
                        public_segment(
                            s.effective_at,
                            s.valid_to,
                            s.content.model_dump(mode="json", exclude_unset=True),
                        )
                        for s in change.replacement_segments
                    )
                elif (
                    change.operation == "cancel"
                    and original
                    and original.prior_segments
                    and original.latest_operation == "upsert"
                    and any(s.effective_at < change.effective_at for s in old_segments)
                ):
                    after_segments = tuple(
                        public_segment(s.effective_at, s.valid_to, s.content)
                        for s in original.prior_segments
                    )
                else:
                    content = (
                        None
                        if change.content is None
                        else change.content.model_dump(mode="json", exclude_unset=True)
                    )
                    timeline = revise_timeline(
                        tuple(
                            EducationSlice(s.effective_at, s.valid_to, s.content)
                            for s in old_segments
                        ),
                        operation=str(change.operation),
                        at=change.effective_at,
                        until=None if content is None else _at(content.get("valid_to")),
                        payload=None
                        if content is None
                        else {k: v for k, v in content.items() if k != "valid_to"},
                        now=effective_at,
                    )
                    after_segments = tuple(
                        public_segment(s.start, s.end, s.payload) for s in timeline
                    )
                summary = summary.model_copy(
                    update={"before_segments": before_segments, "after_segments": after_segments}
                )
            if kind == "teaching_assignment":
                from app.composition.school_fact_education_labels import teaching_preview_labels

                names = teaching_preview_labels(
                    adapter=self.adapter,
                    actor=actor,
                    summary=summary,
                    as_of=effective_at,
                    school_fact_version=base,
                )
                label_proofs.append(names)
                summary = summary.model_copy(
                    update={
                        "reference_labels": names.labels,
                        "reference_labels_as_of": effective_at,
                    }
                )
            impacts.append(summary.model_dump(mode="json", exclude={"impacts"}))
        from app.composition.school_fact_education_regroup import summarize_regroup

        regroup = summarize_regroup(impacts)
        if teaching_notes or teaching_plan is not None:
            regroup = {
                **(
                    regroup
                    or {"scope": "batch_only", "notice": "本次教学安排完整影响", "periods": []}
                ),
                "teaching_notices": list(teaching_notes),
                "teaching_cancellations": [
                    {
                        "reference": self.adapter._reference(actor, snapshots[k].fact_kind, k),
                        "effective_at": cancel_boundaries[k].isoformat(),
                        "publication_reference": evidence["publication_reference"],
                    }
                    for k, evidence in cancellation_proofs.items()
                ],
                "teaching_impacts": [
                    {
                        "reference": self.adapter._reference(actor, snapshots[k].fact_kind, k),
                        "label": EDUCATION_LABELS.get(snapshots[k].fact_kind, "关联资料"),
                        "changed": k in proposed,
                    }
                    for k in (teaching_plan.impact_keys if teaching_plan else ())
                    if k in snapshots
                ],
            }
        primary_key = str(resolved[0].change_set.changes[0].source_key)
        first_impact = next(
            (
                i
                for i, c in zip(impacts, cs.changes, strict=True)
                if str(c.source_key) == primary_key
            ),
            None,
        )
        first = (
            FactChangeSummary.model_validate_json(json.dumps(first_impact))
            if first_impact is not None
            else resolved[0].summary
        ).model_copy(update={"impacts": tuple(impacts), "regroup": regroup})

        if teaching_notes:
            first = first.model_copy(
                update={"source_label": first.source_label + "；" + "；".join(teaching_notes)}
            )

        def revalidate_summary(current: StaffActorContext) -> None:
            for proof in proofs:
                proof.revalidate(actor=current)
            for labels in label_proofs:
                labels.revalidate(actor=current)
            for item in resolved:
                if item.summary_revalidate is not None:
                    item.summary_revalidate(current)

        return ResolvedFactChange(cs, first, revalidate_summary)
