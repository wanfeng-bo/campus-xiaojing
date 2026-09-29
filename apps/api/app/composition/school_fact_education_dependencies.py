"""Finite product adapter for the retained F2 education dependencies."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from app.composition.school_fact_directory_product import FactChangeSummary, ResolvedFactChange
from app.composition.school_fact_education_product import EDUCATION_LABELS, FIELD_LABELS
from app.composition.school_fact_education_quality import (
    EDUCATION_DEPENDENCY_FIELDS,
    EDUCATION_DEPENDENCY_KINDS,
    EducationDependencyReferenceAdapter,
    EducationQualityBridge,
)
from app.composition.school_fact_education_query import (
    EducationQueryProof,
    EducationQueryResult,
    active_segment,
)
from app.composition.school_fact_education_reader import EducationReadProof, EducationReadStale
from app.platform.contracts import StaffActorContext
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.contracts import CanonicalFactChangeSetV1
from app.platform.school_facts.f7_coverage_basis import mother_set_digest
from app.platform.school_facts.school_scope_reference import school_scope_reference_coordinates

DEPENDENCY_KINDS = frozenset({"fact_quality", "policy_scope"})
DEPENDENCY_LABELS = {"fact_quality": "教育事实质量依据", "policy_scope": "教育制度适用范围"}


class EducationDependencyProductAdapter:
    def __init__(
        self,
        *,
        bridge: EducationQualityBridge,
        references: EducationDependencyReferenceAdapter,
        directory_adapter: Any,
        repository: Any,
    ) -> None:
        self.bridge, self.references, self.directory_adapter, self.repository = (
            bridge,
            references,
            directory_adapter,
            repository,
        )
        bridge.target_content_resolver = self.content_for_source

    def _policy_keys(self, actor: StaffActorContext, permit: Any, scope: Any):
        if not scope.is_school_wide:
            return tuple(scope.object_keys), None
        version = self.bridge.projection.current_version(actor.school_id)
        keys: list[str] = []
        after = None
        for _ in range(100):
            self.bridge.f2_guard.revalidate(permit, actor)
            page = self.bridge.projection.scope_keys(
                school_id=actor.school_id, kind="policy", after_key=after, limit=100
            )
            if (
                type(page) is not dict
                or type(page.get("keys")) is not list
                or len(page["keys"]) > 100
                or type(page.get("has_more")) is not bool
                or page.get("school_fact_version") != version
            ):
                raise EducationReadStale("制度范围枚举期间已变化。")
            selected = tuple(page["keys"])
            if any(type(key) is not str or not scope.allows_key(key) for key in selected):
                raise PermissionError("制度来源超出获准范围。")
            if selected != tuple(sorted(set(selected), key=lambda key: key.encode("utf-8"))) or (
                after is not None
                and selected
                and selected[0].encode("utf-8") <= after.encode("utf-8")
            ):
                raise ValueError("Invalid policy source ordering")
            more = page["has_more"]
            next_after = selected[-1] if more and selected else None
            if page.get("next_after") != next_after or more and not selected:
                raise ValueError("Invalid policy continuation")
            keys.extend(selected)
            if not more:
                self.bridge.f2_guard.revalidate(permit, actor)
                if self.bridge.projection.current_version(actor.school_id) != version:
                    raise EducationReadStale("制度范围枚举期间已变化。")
                return tuple(keys), version
            after = next_after
        raise EducationReadStale("制度范围过大，请缩小查询条件。")

    def describe(self, *, actor: StaffActorContext, fact_kind: str) -> dict[str, Any]:
        if fact_kind not in DEPENDENCY_KINDS:
            raise ValueError("不支持的教育依赖类型。")
        original = self.directory_adapter.describe(actor=actor, fact_kind=fact_kind)
        fields = [
            dict(f)
            for f in original["fields"]
            if f["name"] not in {"organization_ref", "person_ref", "location_ref", "field_name"}
        ]
        options = [
            {"value": k, "label": EDUCATION_LABELS[k]} for k in sorted(EDUCATION_DEPENDENCY_KINDS)
        ]
        for field in fields:
            if field["name"] == "target_fact_kind":
                field["options"] = options
            if field["name"] == "owner_kind":
                field["options"] = [{"value": "education", "label": "教育对象"}]
            if field["name"] == "negative_code":
                field["options"] = [
                    {"value": "no_class_teacher", "label": "明确未安排班主任"},
                    {"value": "no_fixed_location", "label": "明确没有固定教室"},
                    {"value": "vacant_position", "label": "明确岗位空缺"},
                    {"value": "no_teaching_assignment", "label": "明确未安排指定任教角色"},
                ]

        def descriptor(
            name: str,
            label: str,
            kind: str,
            required: bool = False,
            reference_kind: str | None = None,
            choices: list[Any] | None = None,
        ) -> dict[str, Any]:
            return {
                "name": name,
                "label": label,
                "type": kind,
                "required": required,
                "reference_kind": reference_kind,
                "options": choices or [],
            }

        fields.extend(
            [
                descriptor(
                    "education_target_kind", "适用教育对象类型", "select", True, choices=options
                ),
                descriptor("education_target_ref", "适用教育对象", "reference", True),
                descriptor("academic_year", "适用学年（可不填）", "number"),
            ]
        )
        if fact_kind == "fact_quality":
            choices = [
                {"value": name, "label": FIELD_LABELS.get(name, "学校登记字段：" + name)}
                for name in sorted(
                    {f for values in EDUCATION_DEPENDENCY_FIELDS.values() for f in values}
                )
            ]
            fields.extend(
                [
                    descriptor(
                        "campus_ref",
                        "完整年级组集合的校区",
                        "reference",
                        reference_kind="location",
                    ),
                    descriptor("field_name", "明确适用字段（可不填）", "select", choices=choices),
                    descriptor(
                        "teaching_role",
                        "适用任教角色",
                        "select",
                        choices=[
                            {"value": "lead", "label": "主讲"},
                            {"value": "co_teacher", "label": "协同任课"},
                        ],
                    ),
                    descriptor(
                        "position_ref",
                        "适用班内岗位（可不填）",
                        "reference",
                        reference_kind="class_position_definition",
                    ),
                ]
            )
        try:
            permit, scope = self.bridge.require_dependency_scope(actor, fact_kind, "maintain")
            creation_keys = scope.creation_keys
        except (PermissionError, SchoolFactChangeRejected):
            permit, _ = self.bridge.require_dependency_scope(actor, fact_kind, "read")
            creation_keys = ()
        self.bridge.f2_guard.revalidate(permit, actor)
        return {
            "kind": "fact_fields",
            "fact_kind": fact_kind,
            "label": DEPENDENCY_LABELS[fact_kind],
            "fields": fields,
            "creation_choices": [
                {
                    "creation_ref": self.references.reference_for(
                        actor, fact_kind, key, creation=True
                    ),
                    "label": DEPENDENCY_LABELS[fact_kind] + "新登记项",
                }
                for key in creation_keys
            ],
            "query_support": {
                "name_fields": [],
                "name_match": "exact",
                "reference": True,
                "all_authorized": False,
                "related_match": "direct",
                "related_kinds": sorted(EDUCATION_DEPENDENCY_KINDS),
                "related_fields": [
                    {
                        "field": "education_target_ref",
                        "kind": k,
                        "label": "适用于" + EDUCATION_LABELS[k],
                    }
                    for k in sorted(EDUCATION_DEPENDENCY_KINDS)
                ],
                "guidance": "先选定正式教育对象，再查询其质量依据或制度适用范围。",
            },
        }

    def _owner(self, actor: StaffActorContext, reference: str) -> tuple[str, str]:
        coordinate = school_scope_reference_coordinates(reference)
        if coordinate is not None:
            if coordinate[0] not in {"f4", "f5"} or coordinate[2]:
                raise ValueError("请选择正式教育对象引用。")
            kind = coordinate[1]
            if kind not in EDUCATION_DEPENDENCY_KINDS:
                raise ValueError("请选择正式教育对象引用。")
            return kind, self.references.source_for(actor, kind, reference)
        parts = reference.split(":")
        if (
            len(parts) != 3
            or parts[0] not in {"f4", "f5"}
            or parts[1] not in EDUCATION_DEPENDENCY_KINDS
        ):
            raise ValueError("请选择正式教育对象引用。")
        kind = parts[1]
        return kind, self.references.source_for(actor, kind, reference)

    def _empty(self, kind: str, at: datetime, status: str, notice: str) -> dict[str, Any]:
        return {
            "kind": "directory_facts",
            "fact_kind": kind,
            "status": status,
            "records": [],
            "matched_count": None,
            "returned_count": 0,
            "truncated": False,
            "next_cursor": None,
            "queried_at": at.isoformat(),
            "factual_at": at.isoformat(),
            "coverage": {"status": "unknown", "description": notice},
            "family_owner": "school_facts.education",
        }

    def _coordinate_basis(self, actor, kind, key, as_of):
        """Read only one granted field to obtain the normal temporal/version proof.

        Existing SQL target Readers require a nonempty projected field set. The
        selected field value is never copied into the quality result.
        """
        authority = self.bridge.education_authority
        permit = authority.require_scope(
            actor, fact_kind=kind, action="read", checked_at=self.bridge.clock()
        )
        authority.require_object(permit, key, is_creation=False)
        fields = tuple(sorted(permit.scope.field_names))[:1]
        authority.require_fields(permit, fields)
        return self.repository.read_records(
            actor, kind, fields=fields, source_keys=(key,), as_of=as_of
        )

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
        if fact_kind not in DEPENDENCY_KINDS or not 1 <= limit <= 100 or query_text:
            raise ValueError("教育质量和适用范围须按正式教育对象查询。")
        at = self.bridge.clock() if as_of is None else as_of
        campus_keys = (
            (self.references.source_for(actor, "location", campus_ref),) if campus_ref else ()
        )
        position_keys = (
            (self.references.source_for(actor, "class_position_definition", position_ref),)
            if position_ref
            else ()
        )
        source = None
        hint = None
        coordinate_proofs: list[Any] = []
        if reference:
            source = self.references.source_for(actor, fact_kind, reference)
            resolved = self.bridge.read_for_source(actor, fact_kind, source, as_of)
            coordinate_proofs.append(resolved["proof"])
            content = resolved["content"]
            owner_kind, owner_key = (
                content["education_target_kind"],
                content["education_target_source_key"],
            )
            hint = {
                "owner_kind": owner_kind,
                "owner_key": owner_key,
                "target_kind": content.get("target_fact_kind", owner_kind),
                "selection": {
                    "academic_year": content.get("academic_year"),
                    "campus_source_keys": (content["campus_source_key"],)
                    if content.get("campus_source_key")
                    else (),
                    "position_source_keys": (content["position_source_key"],)
                    if content.get("position_source_key")
                    else (),
                },
            }
        else:
            if related_field != "education_target_ref" or related_ref is None:
                raise ValueError("请先选择教育对象，再查询适用依据。")
            owner_kind, owner_key = self._owner(actor, related_ref)
        owner_read = self._coordinate_basis(actor, owner_kind, owner_key, as_of)
        ownpermit = self.bridge.education_authority.require_scope(
            actor, fact_kind=owner_kind, action="read", checked_at=self.bridge.clock()
        )
        self.bridge.education_authority.require_object(ownpermit, owner_key, is_creation=False)
        targets = (
            [hint["target_kind"]]
            if hint
            else sorted(EDUCATION_DEPENDENCY_KINDS)
            if fact_kind == "fact_quality"
            else [owner_kind]
        )
        rows: list[dict[str, Any]] = []
        proofs: list[Any] = list(coordinate_proofs)
        partial = False
        for target_kind in targets:
            try:
                target = self.bridge.education_authority.require_scope(
                    actor, fact_kind=target_kind, action="read", checked_at=self.bridge.clock()
                )
            except PermissionError:
                partial = True
                continue
            if fact_kind == "policy_scope":
                policy_permit, policies = self.bridge.require_dependency_scope(
                    actor, "policy", "read"
                )
                policy_keys, policy_version = self._policy_keys(actor, policy_permit, policies)
                if not policy_keys:
                    continue
            else:
                policy_keys = ()
                policy_version = None
            if target.scope.is_school_wide:
                target_keys, target_version = self.repository.authorized_source_keys(
                    actor, target_kind, tuple(target.scope.field_names)
                )
            else:
                target_keys = tuple(target.scope.object_keys)
                target_version = None
            selection: dict[str, Any] = {
                "kind": fact_kind,
                "owner_kind": owner_kind,
                "owner_source_keys": (owner_key,),
                "target_kind": target_kind,
                "teaching_roles": ("lead", "co_teacher")
                if target_kind == "teaching_assignment"
                else (),
                "target_source_keys": (),
                "field_names": tuple(target.scope.field_names),
                "source_keys": (source,) if source else None,
                "policy_source_keys": policy_keys,
                "as_of": as_of,
                "academic_year": academic_year
                if academic_year is not None
                else (hint["selection"].get("academic_year") if hint else None),
                "campus_source_keys": campus_keys
                or (hint["selection"].get("campus_source_keys", ()) if hint else ()),
                "position_source_keys": position_keys
                or (hint["selection"].get("position_source_keys", ()) if hint else ()),
            }
            policy_pages = (
                [policy_keys[start : start + 100] for start in range(0, len(policy_keys), 100)]
                if policy_keys else [()]
            )
            target_pages = [
                target_keys[start : start + 100]
                for start in range(0, max(len(target_keys), 1), 100)
            ]
            if len(target_pages) * len(policy_pages) > 1000:
                raise EducationReadStale("关联范围过大，请缩小查询条件。")
            for target_page in target_pages:
                for policy_page in policy_pages:
                    selection["target_source_keys"] = target_page
                    selection["policy_source_keys"] = policy_page
                    result = self.bridge.read_dependency(actor, **selection)
                    if (
                        target_version is not None
                        and result["school_fact_version"] != target_version
                    ):
                        raise EducationReadStale("学校资料枚举期间已变化。")
                    if (
                        policy_version is not None
                        and result["school_fact_version"] != policy_version
                    ):
                        raise EducationReadStale("制度范围枚举期间已变化。")
                    proofs.append(result["proof"])
                    rows.extend(result["records"])
            if (
                target_version is not None
                and self.repository.projection.current_version(actor.school_id) != target_version
            ):
                raise EducationReadStale("学校资料枚举期间已变化。")
        proof = EducationQueryProof(EducationReadProof.combine((owner_read.proof,)), tuple(proofs))
        proof.revalidate(actor=actor)
        seen: set[str] = set()
        public: list[dict[str, Any]] = []
        for row in rows:
            if row["source_key"] in seen:
                continue
            seen.add(row["source_key"])
            public.append(
                {
                    "fact_kind": fact_kind,
                    "reference": self.references.reference_for(actor, fact_kind, row["source_key"]),
                    "label": DEPENDENCY_LABELS[fact_kind],
                    "fields": self.references.public_fields(actor, row["content"]),
                    "version": row["version"],
                    "valid_from": row["effective_at"],
                    "valid_to": row.get("valid_to"),
                    "source": {
                        "label": row.get("source", {}).get("label", "学校正式登记依据"),
                        "reference": row.get("source", {}).get("source_ref"),
                        "published_at": row.get("source", {}).get("published_at"),
                    },
                }
            )
        document = self._empty(
            fact_kind,
            at,
            "partial" if partial else "known" if public else "no_current_records",
            "仅明确教育对象及当前获准目标字段；未取得声明不等于已确认不存在。",
        )
        document.update(
            records=public[:limit],
            returned_count=min(len(public), limit),
            matched_count=None if partial else len(public),
            truncated=partial or len(public) > limit,
        )
        return EducationQueryResult(document, tuple(rows), proof)

    def query(self, **kwargs: Any) -> dict[str, Any]:
        return self.query_with_proof(**kwargs).public_result

    def content_for_source(
        self, actor: StaffActorContext, kind: str, key: str, effective_at: datetime | None = None
    ) -> dict[str, Any]:
        return self.bridge.read_for_source(actor, kind, key, effective_at)

    def resolve_change(
        self, *, actor: StaffActorContext, payload: Any, request_id: UUID, effective_at: datetime
    ) -> ResolvedFactChange:
        kind = payload.fact_kind
        if kind not in DEPENDENCY_KINDS:
            raise ValueError("不是教育依赖资料。")
        if "coverage_basis" in payload.fields:
            raise ValueError("完整覆盖的受控母集依据不能由维护请求填写。")
        creation = payload.creation_ref is not None
        source = self.references.source_for(
            actor, kind, payload.creation_ref if creation else payload.target_ref, creation=creation
        )
        if creation:
            coordinates = self.bridge.creation_coordinates(actor, kind, source)
            before: dict[str, Any] = {}
            version = 0
            school_version = coordinates["school_fact_version"]
        else:
            resolved = self.content_for_source(actor, kind, source, effective_at)
            before = resolved["content"]
            version = resolved["row"]["version"]
            school_version = resolved["proof"].school_fact_version
        merged = {
            **before,
            **self.references.resolve_fields(actor, payload.fields, existing=before),
        }
        if payload.operation not in {"upsert", "end", "revoke"}:
            raise ValueError("教育质量和制度适用范围使用其原事实操作。")
        if payload.operation != "upsert" and payload.fields:
            raise ValueError("结束或撤销不能携带替换字段。")
        complete = (
            kind == "fact_quality"
            and payload.operation == "upsert"
            and merged.get("quality_kind") == "coverage"
            and merged.get("coverage_status") == "complete"
        )
        target_proof = None
        if complete:
            target_kind = merged.get("target_fact_kind")
            target_key = merged.get("target_source_key")
            owner_key = merged.get("education_target_source_key")
            collection = (
                target_kind == "education_grade_group"
                and target_key is None
                and merged.get("field_name") == "grade_source_key"
                and merged.get("education_target_kind") == "education_calendar_rule"
                and isinstance(merged.get("campus_source_key"), str)
                and type(merged.get("academic_year")) is int
            )
            if (
                target_kind not in EDUCATION_DEPENDENCY_KINDS
                or not collection
                and (not isinstance(target_key, str) or not target_key)
                or not isinstance(owner_key, str)
                or not owner_key
            ):
                raise ValueError("教育完整覆盖须选定精确归属和事实目标。")
            if collection:
                target = self.repository.read_records(
                    actor,
                    target_kind,
                    fields=("grade_source_key", "campus_source_key", "academic_year"),
                    source_keys=None,
                    filter={
                        "campus_source_key": merged["campus_source_key"],
                        "academic_year": merged["academic_year"],
                    },
                    as_of=effective_at,
                )
                if target.school_fact_version != school_version:
                    raise PermissionError("目标资料已变化，请重新预检。")
                members = []
                for row in target.records:
                    segment = active_segment(row, effective_at)
                    if segment is None:
                        continue
                    item = segment["content"]
                    if (
                        row.get("fact_kind") != target_kind
                        or type(row.get("version")) is not int
                        or item.get("campus_source_key") != merged["campus_source_key"]
                        or item.get("academic_year") != merged["academic_year"]
                        or not item.get("grade_source_key")
                    ):
                        raise ValueError("年级组母集包含不符的正式记录。")
                    members.append((row["source_key"], row["version"]))
                members.sort()
                if len(members) > 100:
                    raise ValueError("这组对象超出当前精确授权的有界核查范围。")
                count, digest = mother_set_digest(members)
                merged["coverage_basis"] = {
                    "schema_version": 2,
                    "school_id": str(actor.school_id),
                    "owner_kind": "education",
                    "owner_source_key": owner_key,
                    "target_fact_kind": target_kind,
                    "target_source_key": None,
                    "field_name": "grade_source_key",
                    "purpose": None,
                    "contact_kind": None,
                    "campus_source_key": merged["campus_source_key"],
                    "academic_year": merged["academic_year"],
                    "authorized_source_keys": [key for key, _version in members],
                    "valid_from": effective_at.isoformat(),
                    "valid_to": merged.get("valid_to"),
                    "school_fact_version": school_version,
                    "member_count": count,
                    "member_sha256": digest,
                }
            else:
                target = self._coordinate_basis(actor, target_kind, target_key, effective_at)
                if target.school_fact_version != school_version or len(target.records) != 1:
                    raise PermissionError("目标资料已变化，请重新预检。")
                row = target.records[0]
                segments = row.get("segments", ())
                if (
                    row.get("source_key") != target_key
                    or row.get("fact_kind") != target_kind
                    or type(row.get("version")) is not int
                    or len(segments) != 1
                    or datetime.fromisoformat(segments[0]["effective_at"]) > effective_at
                    or segments[0].get("valid_to") is not None
                    and effective_at >= datetime.fromisoformat(segments[0]["valid_to"])
                ):
                    raise ValueError("该时点的完整目标事实不可核实。")
            target.proof.revalidate()
            target_proof = target.proof
            if not collection:
                count, digest = mother_set_digest(((target_key, row["version"]),))
                merged["coverage_basis"] = {
                    "schema_version": 1,
                    "school_id": str(actor.school_id),
                    "owner_kind": "education",
                    "owner_source_key": owner_key,
                    "target_fact_kind": target_kind,
                    "target_source_key": target_key,
                    "field_name": merged.get("field_name"),
                    "purpose": None,
                    "contact_kind": None,
                    "valid_from": effective_at.isoformat(),
                    "valid_to": merged.get("valid_to"),
                    "school_fact_version": school_version,
                    "member_count": count,
                    "member_sha256": digest,
                }
        else:
            merged.pop("coverage_basis", None)
        material = {
            "request_id": str(request_id),
            "payload": payload.model_dump(mode="json"),
            "effective_at": effective_at.isoformat(),
            "version": version,
        }
        plan = CanonicalFactChangeSetV1.model_validate_json(
            json.dumps(
                {
                    "schema_version": 4 if complete else 3,
                    "source_sha256": hashlib.sha256(
                        json.dumps(material, sort_keys=True).encode()
                    ).hexdigest(),
                    "base_version": school_version,
                    "changes": [
                        {
                            "schema_version": 4 if complete else 3,
                            "fact_kind": kind,
                            "operation": payload.operation,
                            "source_key": source,
                            "expected_version": version,
                            "effective_at": effective_at.isoformat(),
                            "content": merged if payload.operation == "upsert" else None,
                            "origin": {"sheet_code": "education_dependencies", "row_number": 1},
                        }
                    ],
                }
            )
        )
        authorization_proof = self.bridge.require_change_set(actor, plan)
        authorization_proof.revalidate()

        def revalidate(current: StaffActorContext) -> None:
            if current != actor:
                raise PermissionError("身份已变化，请重新预检。")
            authorization_proof.revalidate()
            if target_proof is not None:
                target_proof.revalidate()

        return ResolvedFactChange(
            change_set=plan,
            summary=FactChangeSummary(
                fact_kind=kind,
                label=DEPENDENCY_LABELS[kind],
                reference=payload.target_ref or payload.creation_ref,
                operation=payload.operation,
                before=self.references.public_fields(
                    actor, {key: value for key, value in before.items() if key != "coverage_basis"}
                )
                if before
                else None,
                after=self.references.public_fields(
                    actor, {key: value for key, value in merged.items() if key != "coverage_basis"}
                )
                if payload.operation == "upsert"
                else None,
                effective_at=effective_at,
                reason=payload.reason,
                source_label="学校明确登记的教育依据",
            ),
            summary_revalidate=revalidate,
        )
