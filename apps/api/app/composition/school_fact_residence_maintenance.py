"""Append explicitly previewed accommodation effects to the original atomic proposal."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime

from app.composition.school_fact_directory_product import FactChangeSummary, ResolvedFactChange
from app.composition.school_fact_residence_product import at_time
from app.domains.students.education_versions import EducationSlice, revise_timeline
from app.domains.students.residence_changes import (
    BoardingEligibilitySlice,
    ResidenceFactSnapshot,
    plan_residence_eligibility_change,
)
from app.platform.school_facts.contracts import (
    CanonicalFactChangeSetV4,
    CanonicalFactContentV4,
    EducationReplacementSegment,
    safe_content_payload,
)
from app.platform.school_facts.f6_policy import F6_FACT_KINDS


def _revised_enrollment(timeline, change):
    original = tuple(
        EducationSlice(at_time(s["effective_at"]), at_time(s.get("valid_to")), s["content"])
        for s in timeline["segments"]
    )
    if change.replacement_segments is not None:
        return tuple(
            EducationSlice(s.effective_at, s.valid_to, safe_content_payload(s.content))
            for s in change.replacement_segments
        )
    if change.operation in {"end", "revoke"}:
        revised = tuple(
            EducationSlice(
                s.start,
                min(s.end, change.effective_at) if s.end else change.effective_at,
                s.payload,
            )
            for s in original
            if s.start < change.effective_at
        )
        if revised == original:
            raise ValueError("本次结束或撤销没有可裁切的在校期间，请核对原日期。")
        return revised
    return revise_timeline(
        original,
        operation=change.operation,
        at=change.effective_at,
        until=change.content.valid_to if change.content else None,
        payload=safe_content_payload(change.content),
        now=datetime.now(UTC),
    )


class ResidenceLifecycleCoordinator:
    def __init__(self, *, repository, adapter, family_repository):
        self.repository, self.adapter, self.family_repository = (
            repository,
            adapter,
            family_repository,
        )

    def expand(self, *, actor, resolved):
        roots = tuple(
            c for c in resolved.change_set.changes if c.fact_kind == "student_school_relationship"
        )
        additions = tuple(c for c in roots if c.expected_version == 0 and c.operation == "upsert")
        roots = tuple(c for c in roots if c not in additions)
        if not roots:
            # A new relationship cannot remove existing eligibility. Creating it
            # grants no read permission over the student's existing relationships.
            # The unchanged proposal still passes the unique Writer's full checks.
            return resolved
        base, groups = resolved.change_set.base_version, {}
        for root in roots:
            timeline = self.family_repository.read_education_timeline(
                actor=actor, source_key=root.source_key
            )
            if (
                timeline["school_fact_version"] != base
                or timeline["version"] != root.expected_version
            ):
                raise ValueError("在校与住宿资料已变化，请重新核对完整影响。")
            students = {s["content"]["student_source_key"] for s in timeline["segments"]}
            if root.content is not None:
                students.add(root.content.student_source_key)
            if root.replacement_segments is not None:
                students.update(s.content.student_source_key for s in root.replacement_segments)
            if len(students) != 1:
                raise ValueError("在校关系的学生归属无法唯一核实。")
            groups.setdefault(students.pop(), []).append((root, timeline))

        extras, summaries, proofs, enrollment_checks = [], [], [], []
        normalized, normalized_summaries = {}, {}
        for student, changes in groups.items():
            complete = self.family_repository.read_residence_eligibility(
                actor=actor, student_source_key=student, source_key=changes[0][0].source_key
            )
            if complete["school_fact_version"] != base:
                raise ValueError("完整在校关系已变化，请重新预检。")
            enrollment_checks.append(complete["revalidate"])
            timelines = {t["source_key"]: t for t in complete["timelines"]}
            slices = {
                key: tuple(
                    EducationSlice(
                        at_time(s["effective_at"]), at_time(s.get("valid_to")), s["content"]
                    )
                    for s in t["segments"]
                )
                for key, t in timelines.items()
            }
            for addition in additions:
                if addition.content.student_source_key != student:
                    continue
                if addition.source_key in timelines:
                    raise ValueError("新增在校关系已存在，请重新核对版本。")
                slices[addition.source_key] = _revised_enrollment({"segments": ()}, addition)
            earliest = min(
                [root.effective_at for root, _ in changes]
                + [s.start for rows in slices.values() for s in rows]
            )
            for root, _ in changes:
                original = timelines.get(root.source_key)
                if original is None or original["version"] != root.expected_version:
                    raise ValueError("原在校关系不在完整获准集合，请重新核对。")
                slices[root.source_key] = _revised_enrollment(original, root)
                if root.operation in {"end", "revoke"}:
                    revised = slices[root.source_key]
                    normalized[root.source_key] = root.model_copy(
                        update={
                            "operation": "correct",
                            "content": None,
                            "correction_reason": resolved.summary.reason
                            + (
                                "（原操作：结束）"
                                if root.operation == "end"
                                else "（原操作：撤销）"
                            ),
                            "replacement_segments": tuple(
                                EducationReplacementSegment(
                                    effective_at=s.start,
                                    valid_to=s.end,
                                    content=CanonicalFactContentV4.model_validate(dict(s.payload)),
                                )
                                for s in revised
                            ),
                        }
                    )
                    reference = self.family_repository.reference_for(
                        actor=actor, fact_kind=root.fact_kind, source_key=root.source_key
                    )

                    def public_segment(start, end, content):
                        return dict(
                            effective_at=start.isoformat(),
                            valid_to=end.isoformat() if end else None,
                            fields=self.family_repository.public_field_references(
                                actor=actor,
                                fact_kind="student_school_relationship",
                                fields=dict(content),
                            ),
                        )

                    before_segments = tuple(
                        public_segment(
                            at_time(s["effective_at"]), at_time(s.get("valid_to")), s["content"]
                        )
                        for s in original["segments"]
                    )
                    after_segments = tuple(
                        public_segment(s.start, s.end, s.payload) for s in revised
                    )
                    normalized_summaries[reference] = FactChangeSummary(
                        fact_kind=root.fact_kind,
                        reference=reference,
                        label="学生在校期间",
                        operation=root.operation,
                        before=None,
                        after=None,
                        effective_at=root.effective_at,
                        reason=resolved.summary.reason,
                        source_label="学校正式登记资料",
                        before_segments=before_segments,
                        after_segments=after_segments,
                    )
                    if reference == resolved.summary.reference:
                        normalized_summaries[reference] = resolved.summary.model_copy(
                            update={
                                "operation": root.operation,
                                "effective_at": root.effective_at,
                                "after": None,
                                "before_segments": before_segments,
                                "after_segments": after_segments,
                            }
                        )
                earliest = min([earliest] + [s.start for s in slices[root.source_key]])
            eligibility = tuple(
                BoardingEligibilitySlice(
                    s.start, s.end, s.payload["enrollment_status"], s.payload["residence_mode"]
                )
                for rows in slices.values()
                for s in rows
            )
            impacts = self.repository.read_student_impacts(
                actor=actor, student_source_key=student, as_of=earliest
            )
            if impacts.school_fact_version != base:
                raise ValueError("住宿影响清单已变化，请重新预检。")
            proofs.append(impacts.proof)
            snapshots = {
                row["source_key"]: ResidenceFactSnapshot(
                    row["source_key"],
                    row["version"],
                    student,
                    row["bed_source_key"],
                    at_time(row["valid_from"]),
                    at_time(row.get("valid_to")),
                    at_time(row.get("revoked_at")),
                )
                for row in impacts.records
            }
            original_snapshots = dict(snapshots)
            explicit = set()
            for unit in resolved.change_set.changes:
                if unit.fact_kind != "student_bed_residence":
                    continue
                if unit.source_key in snapshots:
                    old = snapshots[unit.source_key]
                    if unit.expected_version != old.version:
                        raise ValueError("指定住宿操作的版本已变化。")
                    if unit.operation == "revoke":
                        snapshots[unit.source_key] = replace(old, revoked_at=unit.effective_at)
                    elif (
                        unit.operation == "end"
                        and old.valid_from < unit.effective_at
                        and (old.valid_to is None or unit.effective_at < old.valid_to)
                    ):
                        snapshots[unit.source_key] = replace(old, valid_to=unit.effective_at)
                    else:
                        raise ValueError("指定住宿操作未形成明确有效的处理。")
                    explicit.add(unit.source_key)
                elif unit.content is not None and unit.content.student_source_key == student:
                    snapshots[unit.source_key] = ResidenceFactSnapshot(
                        unit.source_key,
                        0,
                        student,
                        unit.content.bed_source_key,
                        unit.effective_at,
                        unit.content.valid_to,
                    )
                    explicit.add(unit.source_key)
            planned = plan_residence_eligibility_change(
                student_source_key=student,
                snapshots=tuple(snapshots.values()),
                eligibility=eligibility,
            )
            if any(unit.source_key in explicit for unit in planned):
                raise ValueError("本批指定住宿操作仍与完整在校期间冲突，请明确处理后重新预检。")
            for change in planned:
                old = original_snapshots[change.source_key]
                names = self.repository.read(
                    actor=actor,
                    fact_kind="student_bed_residence",
                    source_keys=(change.source_key,),
                    fields=("student_name", "bed_name", "room_name"),
                    as_of=old.valid_from,
                )
                if names.school_fact_version != base or len(names.records) != 1:
                    raise ValueError("受影响学生与床位资料无法完整核对。")
                proofs.append(names.proof)
                labels = names.records[0].get("fields", names.records[0])
                before = self.adapter.public_fields(
                    actor, dict(student_source_key=student, bed_source_key=old.bed_source_key)
                ) | {name: labels.get(name) for name in ("student_name", "bed_name", "room_name")}
                before.update(
                    valid_from=old.valid_from.isoformat(),
                    valid_to=old.valid_to.isoformat() if old.valid_to else None,
                )
                after = (
                    dict(before, valid_to=change.effective_at.isoformat())
                    if change.operation == "end"
                    else None
                )
                segment = dict(
                    effective_at=old.valid_from.isoformat(),
                    valid_to=before["valid_to"],
                    fields=before,
                )
                summaries.append(
                    FactChangeSummary(
                        fact_kind=change.fact_kind,
                        label=f"{labels.get('student_name') or '学生'}的住宿安排",
                        reference=self.repository.reference_for(
                            actor=actor, fact_kind=change.fact_kind, source_key=change.source_key
                        ),
                        operation=change.operation,
                        before=before,
                        after=after,
                        effective_at=change.effective_at,
                        reason="完整在校或住校期间变化影响此住宿安排",
                        source_label="学校正式登记资料",
                        before_segments=(segment,),
                        after_segments=(dict(segment, valid_to=after["valid_to"], fields=after),)
                        if after
                        else (),
                    ).model_dump(mode="json")
                )
            extras.extend(planned)

        def revalidate(current):
            if resolved.summary_revalidate:
                resolved.summary_revalidate(current)
            for check in enrollment_checks:
                check(current)
            for proof in proofs:
                proof.revalidate(actor=current)

        revalidate(actor)
        if not extras and not normalized:
            return ResolvedFactChange(resolved.change_set, resolved.summary, revalidate)
        raw = resolved.change_set.model_dump(mode="json", exclude_unset=True)
        raw["schema_version"] = 4
        raw["changes"] = [
            {
                **normalized[c.source_key].model_dump(mode="json", exclude_unset=True),
                "schema_version": 4,
            }
            if c.source_key in normalized and c.fact_kind == "student_school_relationship"
            else {**value, "schema_version": 4}
            for c, value in zip(resolved.change_set.changes, raw["changes"], strict=True)
        ] + [c.model_dump(mode="json") for c in extras]
        raw["source_sha256"] = hashlib.sha256(
            json.dumps(raw["changes"], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        summary = normalized_summaries.pop(resolved.summary.reference, resolved.summary)
        impacts = [
            normalized_summaries.pop(row.get("reference"), None) or row
            for row in resolved.summary.impacts
        ]
        impacts.extend(normalized_summaries.values())
        impacts = [
            row.model_dump(mode="json") if isinstance(row, FactChangeSummary) else row
            for row in impacts
        ]
        return ResolvedFactChange(
            CanonicalFactChangeSetV4.model_validate_json(json.dumps(raw)),
            summary.model_copy(update={"impacts": (*impacts, *summaries)}),
            revalidate,
        )


def residence_dependency_changes(change_set):
    """Identify the retained F6 eligibility root and its explicit student effects.

    V4 also carries education completeness evidence. Its envelope alone is not
    an F6 purpose. Only the existing eligibility path and its causally linked
    membership/position changes use the separately approved F6 dependencies.
    """
    if getattr(change_set, "schema_version", 3) != 4:
        return ()
    roots = {
        c.source_key
        for c in change_set.changes
        if c.fact_kind == "student_school_relationship"
    }
    if not roots:
        return ()
    dependent_kinds = {
        "student_class_membership", "student_class_position", "teaching_group_member"
    }
    return tuple(
        c for c in change_set.changes
        if c.fact_kind == "student_school_relationship"
        or (
            c.fact_kind in dependent_kinds
            and getattr(c, "cause_source_key", None) in roots
        )
    )


def require_partitioned_maintenance(actor, change_set, *, residence, retained, dependency=None):
    local = tuple(c for c in change_set.changes if c.fact_kind in F6_FACT_KINDS)
    dependencies = residence_dependency_changes(change_set) if dependency is not None else ()
    old = tuple(
        c for c in change_set.changes
        if c.fact_kind not in F6_FACT_KINDS and c not in dependencies
    )
    if local:
        residence.require_maintenance(actor, change_set.model_copy(update={"changes": local}))
    if dependencies:
        dependency(actor, change_set.model_copy(update={"changes": dependencies}))
    if old:
        retained(actor, change_set.model_copy(update={"changes": old}))
