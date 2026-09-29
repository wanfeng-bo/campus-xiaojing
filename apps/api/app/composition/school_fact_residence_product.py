"""F6 maintenance semantics; persistence, confirmation and receipts stay shared."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic import Field, model_validator

from app.composition.school_fact_directory_product import (
    FactChangeInput,
    FactChangeSummary,
    MaintenanceFactKind,
    Reference,
    ResolvedFactChange,
)
from app.composition.school_fact_education_product import EducationChangeInput
from app.composition.school_fact_residence_time import unknown_time_document
from app.platform.school_facts.contracts import CanonicalFactChangeSetV4, fact_content_fields
from app.platform.school_facts.f6_policy import F6_FACT_KINDS
from app.platform.school_facts.school_scope_reference import school_scope_reference_coordinates

RESIDENCE_LABELS = {
    "dormitory_building_profile": "宿舍楼与寄宿区域",
    "dorm_room": "宿舍房间",
    "dorm_bed": "宿舍床位",
    "student_bed_residence": "学生住宿安排",
}
REFERENCE_KINDS = {
    "location_source_key": "location",
    "room_source_key": "location",
    "student_source_key": "student",
    "bed_source_key": "dorm_bed",
}
FIELD_LABELS = {
    "location_ref": "所在地点",
    "room_ref": "房间",
    "student_ref": "学生",
    "bed_ref": "床位",
    "usage_kind": "用途",
    "display_name": "名称",
    "description": "说明",
    "is_available": "是否启用",
    "valid_to": "结束时间",
    "authorized_bed_capacity": "批准床位数",
    "accommodation_zone": "住宿区域",
    "accessibility_tags": "无障碍设施",
    "business_code": "床位编号",
    "availability": "床位使用状态",
}
ENUMS = {
    "usage_kind": {"building": "宿舍楼", "residential_area": "寄宿区域"},
    "availability": {"available": "可使用", "unavailable": "暂停使用", "inactive": "已停用"},
}


def at_time(value: Any) -> Any:
    if value is None:
        return None
    result = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    if result.utcoffset() is None:
        raise ValueError("时间必须明确时区。")
    return result


def public_name(field: str) -> str:
    return field.removesuffix("_source_key") + "_ref" if field in REFERENCE_KINDS else field


class ResidenceBatchInput(FactChangeInput):
    additional_changes: tuple[FactChangeInput, ...] = Field(default=(), max_length=299)
    import_mode: Literal["incremental", "replace"] = Field(
        default="incremental",
        description="默认增量。replace仅完整替换明确选中的学生住宿安排对象集合，不表示整房或全校；从明确effective_at起持续生效。",
    )
    import_rows: tuple[FactChangeInput, ...] | None = Field(
        default=None,
        max_length=100,
        description="实际希望登记或保留的住宿行；缺行在增量时不改，replace空表会结束或撤销全部所选当前/未来安排。",
    )
    import_replacement_refs: tuple[Reference, ...] | None = Field(
        default=None,
        max_length=100,
        description="replace必须显式选择的现有住宿安排引用集合；每项须具备完整读取和维护权限。",
    )

    @model_validator(mode="after")
    def target(self) -> Any:
        if self.fact_kind == "student_school_relationship" and self.operation == "correct":
            EducationChangeInput.model_validate_json(
                self.model_dump_json(include=set(FactChangeInput.model_fields))
            )
            return self
        FactChangeInput.model_validate_json(
            self.model_dump_json(include=set(FactChangeInput.model_fields))
        )
        return self

    @model_validator(mode="after")
    def exact_batch(self) -> Any:
        if self.import_rows is None:
            if self.import_mode != "incremental" or self.import_replacement_refs is not None:
                raise ValueError("完整替换须明确提供实际住宿行与替换范围。")
        else:
            if self.fact_kind != "student_bed_residence" or self.additional_changes:
                raise ValueError("住宿名单导入只接受学生住宿安排，不能混入其他批次。")
            if self.import_mode == "replace":
                if not self.import_replacement_refs or self.effective_at is None:
                    raise ValueError("完整替换须显式选择非空安排集合与生效时点。")
            elif self.import_replacement_refs is not None or not self.import_rows:
                raise ValueError("增量导入不能附带完整替换范围或空行集合。")
            refs = self.import_replacement_refs or ()
            if len(set(refs)) != len(refs) or any(
                not ref.startswith("f6:student_bed_residence:")
                and school_scope_reference_coordinates(ref)
                != ("f6", "student_bed_residence", False)
                for ref in refs
            ):
                raise ValueError("完整替换范围须为互不重复的现有住宿引用。")
            row_refs: list[str | None] = []
            for row in self.import_rows:
                if row.fact_kind != "student_bed_residence" or row.operation not in {
                    "upsert",
                    "end",
                    "revoke",
                }:
                    raise ValueError("住宿导入行只支持登记、明确结束或撤销。")
                if self.import_mode == "replace" and row.target_ref and row.target_ref not in refs:
                    raise ValueError("已有导入对象须位于明确选择的替换集合内。")
                if (
                    self.import_mode == "replace"
                    and row.effective_at is not None
                    and row.effective_at != self.effective_at
                ):
                    raise ValueError("完整替换的每行须采用同一个明确生效时点。")
                row_refs.append(row.target_ref or row.creation_ref)
            if len(set(row_refs)) != len(row_refs):
                raise ValueError("导入行不能重复选择同一住宿安排。")
        items = (self, *self.additional_changes)
        if any(
            i.fact_kind not in F6_FACT_KINDS | {"fact_quality", "student_school_relationship"}
            for i in items
        ):
            raise ValueError("本批只能包含寄宿资料。")
        if any(i.fact_kind == "fact_quality" for i in items) and len(items) != 1:
            raise ValueError("请单独准备寄宿质量声明。")
        if any(i.fact_kind == "student_school_relationship" for i in items) and (
            len(items) != 1 or self.creation_ref is not None
        ):
            raise ValueError("请单独选择已有在校关系，由系统核对完整关联影响。")
        targets = [(i.fact_kind, i.target_ref or i.creation_ref) for i in items]
        if len(set(targets)) != len(targets):
            raise ValueError("同一批不能重复改动同一对象。")
        return self


def facility_timeline(rows: Any, *, operation: Any, at: Any, fields: Any, now: Any) -> Any:
    """Replace a selected interval, preserving unrelated past and future facts."""
    result: list[dict[str, Any]] = []
    matched = False
    for original in rows:
        row = deepcopy(original)
        start, end = at_time(row["effective_at"]), at_time(row.get("valid_to"))
        if operation == "cancel":
            if start == at:
                if at <= now:
                    raise ValueError("已生效安排不能作为未来安排撤销，请正式纠错。")
                matched = True
            else:
                result.append(row)
            continue
        if not (start <= at and (end is None or at < end)):
            result.append(row)
            continue
        if matched:
            raise ValueError("该时点存在冲突，不能选择其中一条进行维护。")
        matched = True
        if start < at:
            result.append({**row, "valid_to": at.isoformat()})
        if operation == "end":
            if fields:
                raise ValueError("结束安排不能附带新内容。")
            continue
        if operation not in {"upsert", "correct"}:
            raise ValueError("请选择更新、补正、结束或撤销未来安排。")
        content = {**row["content"], **fields}
        explicit_end = at_time(content.pop("valid_to", end))
        if explicit_end is not None and explicit_end <= at:
            raise ValueError("结束时间必须晚于生效时间。")
        if end is not None and (explicit_end is None or explicit_end > end):
            raise ValueError("不能越过后续正式安排延长本段资料。")
        result.append(
            dict(
                effective_at=at.isoformat(),
                valid_to=explicit_end.isoformat() if explicit_end else None,
                content=content,
            )
        )
        if (
            operation == "correct"
            and explicit_end is not None
            and (end is None or explicit_end < end)
        ):
            result.append({**row, "effective_at": explicit_end.isoformat()})
    if not matched:
        if operation != "correct":
            raise ValueError("所选时点没有可延续的原资料，请核对时点或明确补正。")
        content = dict(fields)
        end = at_time(content.pop("valid_to", None))
        result.append(
            dict(
                effective_at=at.isoformat(),
                valid_to=end.isoformat() if end else None,
                content=content,
            )
        )
    return sorted(result, key=lambda row: at_time(row["effective_at"]))


class ResidenceProductAdapter:
    def __init__(
        self,
        *,
        repository: Any,
        external: Any,
        clock: Any = lambda: datetime.now(UTC),
        time_context: Any = None,
    ) -> None:
        self.repository, self.external, self.clock = repository, external, clock
        self.time_context = time_context

    def attach_time_basis(self, *, actor: Any, resolved: Any) -> Any:
        """F6-only wrapper for its existing quality/eligibility dependency owners."""
        if self.time_context is None:
            return resolved
        proofs = [self.time_context.capture(actor)]

        def annotate(summary: Any) -> Any:
            proof = self.time_context.capture(actor, as_of=at_time(summary["effective_at"]))
            proofs.append(proof)
            return {
                **summary,
                **proof.document,
                "impacts": [annotate(item) for item in summary.get("impacts", ())],
            }

        summary = FactChangeSummary.model_validate_json(
            json.dumps(annotate(resolved.summary.model_dump(mode="json")), default=str)
        )
        original = resolved.summary_revalidate

        def revalidate(current: Any) -> None:
            for proof in proofs:
                proof.revalidate(actor=current)
            if original is not None:
                original(current)

        revalidate(actor)
        return replace(resolved, summary=summary, summary_revalidate=revalidate)

    def _source(self, actor: Any, kind: Any, reference: Any, creation: Any = False) -> Any:
        owner = self.repository if kind in F6_FACT_KINDS else self.external
        return owner.source_for_reference(
            actor=actor, fact_kind=kind, reference=reference, creation=creation
        )

    def _reference(self, actor: Any, kind: Any, key: Any) -> Any:
        owner = self.repository if kind in F6_FACT_KINDS else self.external
        return owner.reference_for(actor=actor, fact_kind=kind, source_key=key)

    def resolve_fields(self, actor: Any, kind: Any, fields: Any) -> Any:
        allowed, _ = fact_content_fields(kind, 4)
        public = {public_name(f): f for f in allowed}
        if set(fields) - public.keys():
            raise ValueError("维护字段不属于这类寄宿资料。")
        result: dict[str, Any] = {}
        for name, value in fields.items():
            field = public[name]
            if field in REFERENCE_KINDS and value is not None:
                if not isinstance(value, str):
                    raise ValueError("请选择已登记的关联对象。")
                value = self._source(actor, REFERENCE_KINDS[field], value)
            result[field] = value
        return result

    def public_fields(self, actor: Any, content: Any) -> Any:
        result: dict[str, Any] = {}
        for field, value in content.items():
            if field in REFERENCE_KINDS and value is not None:
                value = self._reference(actor, REFERENCE_KINDS[field], value)
            result[public_name(field)] = value
        return result

    def describe(self, *, actor: Any, fact_kind: Any) -> Any:
        if fact_kind not in F6_FACT_KINDS:
            raise ValueError("未接入这类寄宿资料。")
        allowed, required = fact_content_fields(fact_kind, 4)
        choices = self.repository.catalog_creation_references(actor, fact_kind)
        time_proof = self.time_context.capture(actor) if self.time_context else None
        if time_proof is not None:
            time_proof.revalidate(actor=actor)
        return {
            **(time_proof.document if time_proof else unknown_time_document()),
            "kind": "fact_fields",
            "fact_kind": fact_kind,
            "label": RESIDENCE_LABELS[fact_kind],
            "creation_choices": [
                {"creation_ref": v["reference"], "label": v["label"]} for v in choices
            ],
            "fields": [
                {
                    "name": public_name(f),
                    "label": FIELD_LABELS.get(public_name(f), f),
                    "required": f in required,
                    "type": "reference"
                    if f in REFERENCE_KINDS
                    else "boolean"
                    if f == "is_available"
                    else "number"
                    if f == "authorized_bed_capacity"
                    else "datetime"
                    if f == "valid_to"
                    else "select"
                    if f in ENUMS
                    else "lines"
                    if f == "accessibility_tags"
                    else "text",
                    "reference_kind": REFERENCE_KINDS.get(f),
                    "options": [
                        {"value": v, "label": label} for v, label in ENUMS.get(f, {}).items()
                    ],
                }
                for f in sorted(allowed)
            ],
            "notice": (
                "住宿纠错使用明确的旧记录撤销与新记录登记；"
                "换床使用同批结束旧床和登记新床。不会自动恢复已结束安排。"
            ),
        }

    def resolve_change(
        self, *, actor: Any, payload: Any, request_id: Any, effective_at: Any
    ) -> Any:
        if getattr(payload, "import_rows", None) is not None:
            return self.resolve_import(
                actor=actor, payload=payload, request_id=request_id, effective_at=effective_at
            )
        items = (payload, *getattr(payload, "additional_changes", ()))
        units: list[dict[str, Any]] = []
        summaries: list[FactChangeSummary] = []
        proofs: list[Any] = []
        # User-local dates are interpreted from the current school context;
        # individual fact dates below also retain their own school snapshot.
        if self.time_context is not None:
            proofs.append(self.time_context.capture(actor))
        versions: set[int] = set()
        for index, item in enumerate(items, 1):
            kind = str(item.fact_kind)
            if kind not in F6_FACT_KINDS:
                raise ValueError("本批包含未获支持的资料类型。")
            at = item.effective_at or effective_at
            time_proof = self.time_context.capture(actor, as_of=at) if self.time_context else None
            if time_proof is not None:
                proofs.append(time_proof)
            source = self._source(
                actor,
                kind,
                item.target_ref or item.creation_ref,
                creation=item.creation_ref is not None,
            )
            read = self.repository.read_for_maintenance(
                actor=actor,
                fact_kind=kind,
                source_key=source,
                creation=item.creation_ref is not None,
            )
            if len(read.records) != 1:
                raise ValueError("正式维护对象无法唯一核实。")
            row = read.records[0]
            version = row["version"]
            if (version == 0) != (item.creation_ref is not None):
                raise ValueError("对象已改变，请重新选择。")
            versions.add(read.school_fact_version)
            proofs.append(read.proof)
            fields = self.resolve_fields(actor, kind, item.fields)
            segments = deepcopy(list(row.get("segments", ())))
            matching = [
                s
                for s in segments
                if at_time(s["effective_at"]) <= at
                and (at_time(s.get("valid_to")) is None or at < at_time(s["valid_to"]))
            ]
            if len(matching) > 1:
                raise ValueError("该时点有冲突，不能取第一条修改。")
            before: dict[str, Any] = matching[0]["content"] if matching else {}
            unit = dict(
                schema_version=4,
                fact_kind=kind,
                source_key=source,
                expected_version=version,
                effective_at=at.isoformat(),
                operation=item.operation,
                content=None,
                origin={"sheet_code": "residence_maintenance", "row_number": index},
            )
            after = None
            after_segments = []
            if kind == "student_bed_residence":
                if version == 0 and item.operation == "upsert":
                    unit["content"] = fields
                    after = fields
                elif version and item.operation in {"end", "revoke", "cancel"} and not fields:
                    if item.operation == "end":
                        after_segments = [
                            {
                                **s,
                                "valid_to": min(at_time(s["valid_to"]), at).isoformat()
                                if s.get("valid_to")
                                else at.isoformat(),
                            }
                            for s in segments
                            if at_time(s["effective_at"]) < at
                        ]
                    if item.operation == "cancel":
                        if not segments or any(
                            at_time(s["effective_at"]) <= self.clock() for s in segments
                        ):
                            raise ValueError("只能撤销尚未生效的住宿安排。")
                        unit["operation"] = "revoke"
                else:
                    raise ValueError(
                        "住宿学生和床位不能直接改写；请在同一预览中结束或撤销原记录，再登记正确记录。"
                    )
            elif not version:
                if item.operation != "upsert":
                    raise ValueError("新设施必须明确登记完整内容。")
                unit["content"] = fields
                after = fields
            else:
                after_segments = facility_timeline(
                    segments, operation=item.operation, at=at, fields=fields, now=self.clock()
                )
                unit.update(
                    operation="correct",
                    content=None,
                    correction_reason=item.reason,
                    replacement_segments=after_segments,
                )
                visible = [
                    s
                    for s in after_segments
                    if at_time(s["effective_at"]) <= at
                    and (s.get("valid_to") is None or at < at_time(s["valid_to"]))
                ]
                after = visible[0]["content"] if len(visible) == 1 else None
            units.append(unit)

            def public_segments(values: Any) -> Any:
                return tuple(
                    {
                        "effective_at": s["effective_at"],
                        "valid_to": s.get("valid_to"),
                        "fields": self.public_fields(actor, s["content"]),
                    }
                    for s in values
                )

            summaries.append(
                FactChangeSummary(
                    **(time_proof.document if time_proof else unknown_time_document()),
                    fact_kind=cast(MaintenanceFactKind, kind),
                    label=str(before.get("display_name") or RESIDENCE_LABELS[kind]),
                    reference=item.target_ref or item.creation_ref,
                    operation=item.operation,
                    before=self.public_fields(actor, before) if before else None,
                    after=self.public_fields(actor, after) if after is not None else None,
                    effective_at=at,
                    reason=item.reason,
                    source_label="学校正式登记资料",
                    before_segments=public_segments(segments),
                    after_segments=public_segments(after_segments),
                )
            )
        if len(versions) != 1:
            raise ValueError("准备期间正式资料已变化，请重新核对整批影响。")
        material = {"request": str(request_id), "units": units}
        changes = CanonicalFactChangeSetV4.model_validate_json(
            json.dumps(
                dict(
                    schema_version=4,
                    source_sha256=hashlib.sha256(
                        json.dumps(material, sort_keys=True, default=str).encode()
                    ).hexdigest(),
                    base_version=versions.pop(),
                    changes=units,
                ),
                default=str,
            )
        )

        def revalidate(current: Any) -> Any:
            for proof in proofs:
                proof.revalidate(actor=current)

        revalidate(actor)
        summary = summaries[0].model_copy(
            update={"impacts": tuple(s.model_dump(mode="json") for s in summaries[1:])}
        )
        return ResolvedFactChange(changes, summary, revalidate)

    def resolve_import(
        self, *, actor: Any, payload: Any, request_id: Any, effective_at: Any
    ) -> Any:
        """A finite explicit arrangement set; never infer a room/student roster."""
        kind = "student_bed_residence"
        at = payload.effective_at or effective_at
        references = set(payload.import_replacement_refs or ()) | {
            row.target_ref or row.creation_ref for row in payload.import_rows
        }
        snapshots: dict[str, Any] = {}
        proofs: list[Any] = []
        versions: set[Any] = set()
        source_refs: dict[str, str] = {}
        # The global formal version and natural boundaries cover additions even
        # when the explicitly selected set currently contains no active rows.
        context = self.repository.read_page_context(actor)
        proofs.append(context.proof)
        for reference in sorted(references):
            creation = reference.startswith("f6create:") or school_scope_reference_coordinates(
                reference
            ) == ("f6", kind, True)
            key = self._source(actor, kind, reference, creation=creation)
            if key in source_refs:
                raise ValueError("同一住宿安排不能使用多个导入引用。")
            source_refs[key] = reference
            read = self.repository.read_for_maintenance(
                actor=actor,
                fact_kind=kind,
                source_key=key,
                creation=creation,
                required_fields=None
                if creation
                else ("student_source_key", "bed_source_key", "effective_at", "valid_to"),
                required_maintenance_fields=None if creation else ("effective_at", "valid_to"),
            )
            if len(read.records) != 1 or (read.records[0]["version"] == 0) != creation:
                raise ValueError("住宿导入缺少完整、唯一的正式快照。")
            snapshots[reference] = read.records[0]
            proofs.append(read.proof)
            versions.add(read.school_fact_version)
        if len(versions) != 1:
            raise ValueError("完整导入集合在核对期间已变化。")
        actions: list[FactChangeInput] = []
        seen: set[str] = set()
        for row in payload.import_rows:
            reference = row.target_ref or row.creation_ref
            seen.add(reference)
            snapshot = snapshots[reference]
            if snapshot["version"] and row.operation == "upsert":
                segments = snapshot["segments"]
                if len(segments) != 1:
                    raise ValueError("已撤销或不唯一的住宿安排不能通过导入自动恢复。")
                old = {**segments[0]["content"], "valid_to": segments[0].get("valid_to")}
                fields = self.resolve_fields(actor, kind, row.fields)
                for name, value in fields.items():
                    same = (
                        at_time(value) == at_time(old.get(name))
                        if name == "valid_to"
                        else value == old.get(name)
                    )
                    if not same:
                        raise ValueError(
                            "住宿学生、床位或日期改变须明确结束旧记录并登记新记录，不能改写原安排。"
                        )
                continue  # Explicit retained row; permission and timeline proof still required.
            actions.append(row.model_copy(update={"effective_at": row.effective_at or at}))
        if payload.import_mode == "replace":
            for reference in payload.import_replacement_refs:
                if reference in seen:
                    continue
                segments = snapshots[reference]["segments"]
                if len(segments) > 1:
                    raise ValueError("住宿安排不唯一，不能部分替换。")
                if not segments:
                    continue
                row = segments[0]
                start, end = at_time(row["effective_at"]), at_time(row.get("valid_to"))
                if end is not None and end <= at:
                    continue  # History outside the selected replacement interval is preserved.
                actions.append(
                    FactChangeInput(
                        fact_kind=kind,
                        target_ref=reference,
                        operation="revoke" if start >= at else "end",
                        fields={},
                        effective_at=at,
                        reason=payload.reason,
                    )
                )
        if not actions:
            raise ValueError("所选住宿集合无变化，无需生成正式操作。")
        material = actions[0].model_dump(mode="json")
        material["additional_changes"] = [row.model_dump(mode="json") for row in actions[1:]]
        result = self.resolve_change(
            actor=actor,
            payload=ResidenceBatchInput.model_validate_json(json.dumps(material)),
            request_id=request_id,
            effective_at=at,
        )
        if result.change_set.base_version != next(iter(versions)):
            raise ValueError("住宿完整替换在准备期间已变化。")
        original = result.summary_revalidate

        def revalidate(current: Any) -> None:
            for proof in proofs:
                proof.revalidate(actor=current)
            if original is not None:
                original(current)

        revalidate(actor)
        notice = (
            (
                "仅完整替换明确选中的住宿安排，自"
                + at.isoformat()
                + "起持续生效；所选范围以外不变，历史保留。"
                + ("空表：全部所选当前安排结束、未来安排撤销。" if not payload.import_rows else "")
            )
            if payload.import_mode == "replace"
            else "默认增量，缺行不结束安排。"
        )
        return replace(
            result,
            summary=result.summary.model_copy(
                update={"reason": payload.reason, "source_label": "学校正式登记资料；" + notice}
            ),
            summary_revalidate=revalidate,
        )
