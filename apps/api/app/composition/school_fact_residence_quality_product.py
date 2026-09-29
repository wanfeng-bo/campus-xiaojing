"""F6 quality maintenance over the existing F2 quality facts and sole Writer."""

import hashlib
import json
from typing import Any

from app.composition.school_fact_directory_product import FactChangeSummary, ResolvedFactChange
from app.composition.school_fact_residence_product import RESIDENCE_LABELS, at_time
from app.platform.school_facts.contracts import CanonicalFactChangeSetV4, fact_content_fields
from app.platform.school_facts.f2_policy import F2FactScope
from app.platform.school_facts.f6_policy import F6_FACT_FIELDS, F6_FACT_KINDS
from app.platform.school_facts.f7_coverage_basis import mother_set_digest
from app.platform.school_facts.school_scope_reference import school_scope_reference_coordinates

LABEL = "寄宿事实依据与质量"
OWNER_FIELDS = {
    "person_ref": ("person_source_key", "person"),
    "location_ref": ("location_source_key", "location"),
}


class ResidenceQualityProductAdapter:
    def __init__(
        self,
        *,
        bridge: Any,
        directory: Any,
        residence: Any,
        external: Any,
        cursor_codec: Any = None,
    ) -> None:
        self.bridge, self.directory, self.residence, self.external = (
            bridge,
            directory,
            residence,
            external,
        )
        self.cursor_codec = cursor_codec

    def reference_for(self, actor: Any, key: Any, creation: Any = False) -> Any:
        return self.directory.repository.reference_for(
            actor=actor, fact_kind="fact_quality", source_key=key, creation=creation
        )

    def source_for(self, actor: Any, reference: Any, creation: Any = False) -> Any:
        return self.directory.repository.source_for(
            actor=actor, fact_kind="fact_quality", reference=reference, creation=creation
        )

    def public_fields(self, actor: Any, content: Any) -> Any:
        result = dict(content)
        result.pop("coverage_basis", None)
        kind = content.get("target_fact_kind")
        for field in ("target_source_key", "replacement_source_key"):
            value = result.pop(field, None)
            if value is not None:
                result[field.replace("_source_key", "_ref")] = self.residence.reference_for(
                    actor=actor, fact_kind=kind, source_key=value
                )
        for public, (field, owner) in OWNER_FIELDS.items():
            value = result.pop(field, None)
            if value is not None:
                result[public] = self.external.reference_for(
                    actor=actor, fact_kind=owner, source_key=value
                )
        return result

    def resolve_fields(self, actor: Any, fields: Any, previous: Any) -> Any:
        if "coverage_basis" in fields:
            raise ValueError("完整覆盖的受控母集依据不能由维护请求填写。")
        allowed, _ = fact_content_fields("fact_quality", 4)
        mapping = {public: field for public, (field, _) in OWNER_FIELDS.items()}
        mapping.update(target_ref="target_source_key", replacement_ref="replacement_source_key")
        permitted = (
            allowed
            - {
                "education_target_source_key",
                "education_target_kind",
                "academic_year",
                "position_source_key",
                "teaching_role",
                "organization_source_key",
            }
            - set(mapping.values())
        ) | mapping.keys()
        if set(fields) - permitted:
            raise ValueError("这不是已接入的寄宿依据字段。")
        merged = dict(previous)
        merged.update({key: value for key, value in fields.items() if key not in mapping})
        kind = merged.get("target_fact_kind")
        if kind not in F6_FACT_KINDS:
            raise ValueError("必须明确依据对应的寄宿资料类型。")
        for public, field in mapping.items():
            if public not in fields:
                continue
            value = fields[public]
            if value is not None:
                if public in OWNER_FIELDS:
                    value = self.external.source_for_reference(
                        actor=actor, fact_kind=OWNER_FIELDS[public][1], reference=value
                    )
                else:
                    value = self.residence.source_for_reference(
                        actor=actor, fact_kind=kind, reference=value
                    )
            merged[field] = value
        return merged

    def describe(self, *, actor: Any, fact_kind: Any = "fact_quality") -> Any:
        if fact_kind != "fact_quality":
            raise ValueError("不是寄宿质量资料。")
        original = self.directory.describe(actor=actor, fact_kind="fact_quality")
        fields = [
            dict(value)
            for value in original["fields"]
            if value["name"] not in {"organization_ref", "purpose", "contact_kind"}
        ]
        options = {
            "target_fact_kind": [
                {"value": kind, "label": label} for kind, label in RESIDENCE_LABELS.items()
            ],
            "owner_kind": [
                {"value": "person", "label": "对应学生"},
                {"value": "location", "label": "对应地点"},
                {"value": "school", "label": "本校"},
            ],
            "negative_code": [
                {"value": "no_residence", "label": "学校明确没有住宿安排"},
                {"value": "no_dormitory", "label": "学校明确没有寄宿用途"},
            ],
        }
        for field in fields:
            if field["name"] in options:
                field["options"] = options[field["name"]]
            if field["name"] in {"target_ref", "replacement_ref"}:
                field["reference_kind"] = None
        fields.append(
            {
                "name": "field_name",
                "label": "适用字段（可不填）",
                "type": "text",
                "required": False,
                "reference_kind": None,
                "options": [],
            }
        )
        return {
            **original,
            "label": LABEL,
            "fields": fields,
            "notice": (
                "质量声明不代替正式住宿登记；必须明确对象、期间和发布依据。"
                "没有记录不能自动登记为不存在。"
            ),
        }

    def resolve_change(
        self, *, actor: Any, payload: Any, request_id: Any, effective_at: Any
    ) -> Any:
        if payload.fact_kind != "fact_quality" or getattr(payload, "additional_changes", ()):
            raise ValueError("请单独准备这项寄宿质量声明。")
        creation = payload.creation_ref is not None
        key = self.source_for(actor, payload.creation_ref or payload.target_ref, creation)
        old = None if creation else self.bridge.read_content(actor, key)
        before: dict[str, Any] = {} if old is None else old["content"]
        version = 0 if old is None else old["version"]
        school_version = self.bridge.projection.current_version(actor.school_id)
        if payload.operation not in {"upsert", "end", "revoke"}:
            raise ValueError("依据使用原有登记、结束或撤销操作。")
        if payload.operation != "upsert" and (creation or payload.fields):
            raise ValueError("结束和撤销须选择原记录，不能同时提供新内容。")
        content = (
            self.resolve_fields(actor, payload.fields, before)
            if payload.operation == "upsert"
            else None
        )
        target_proof = None
        if content is not None:
            complete = (
                content.get("quality_kind") == "coverage"
                and content.get("coverage_status") == "complete"
            )
            if complete:
                kind, target_key = content.get("target_fact_kind"), content.get("target_source_key")
                if kind not in F6_FACT_KINDS or not isinstance(target_key, str) or not target_key:
                    raise ValueError("寄宿完整覆盖须选定精确正式目标。")
                target_repository = getattr(self.residence, "repository", None)
                if target_repository is None:
                    raise PermissionError("寄宿目标的正式读取链尚未接通。")
                permit = target_repository.authority.require_scope(
                    actor, fact_kind=kind, action="read", checked_at=target_repository.clock()
                )
                claim_field = content.get("field_name")
                if claim_field is not None and claim_field not in F6_FACT_FIELDS[kind]:
                    raise ValueError("质量声明字段不属于该寄宿事实。")
                selected = (
                    (claim_field,)
                    if claim_field is not None
                    else tuple(sorted(F6_FACT_FIELDS[kind]))
                )
                if not set(selected) <= set(permit.scope.field_names):
                    raise PermissionError("没有获准读取质量声明覆盖的目标字段。")
                target = target_repository.read(
                    actor,
                    kind,
                    source_keys=(target_key,),
                    fields=selected,
                    as_of=effective_at,
                    limit=1,
                )
                if (
                    target.school_fact_version != school_version
                    or len(target.records) != 1
                    or target.has_more
                ):
                    raise PermissionError("完整覆盖目标已变化或不可完整读取，请重新预检。")
                row = target.records[0]
                if (
                    row.get("source_key") != target_key
                    or row.get("fact_kind") != kind
                    or type(row.get("version")) is not int
                    or at_time(row.get("valid_from")) > effective_at
                    or row.get("valid_to") is not None
                    and effective_at >= at_time(row["valid_to"])
                ):
                    raise ValueError("该时点的寄宿目标事实不可核实。")
                target.proof.revalidate(actor=actor)
                target_proof = target.proof
                count, digest = mother_set_digest(((target_key, row["version"]),))
                owner_kind = content.get("owner_kind")
                owner_key = {
                    "school": None,
                    "person": content.get("person_source_key"),
                    "location": content.get("location_source_key"),
                }.get(owner_kind)
                if owner_kind not in {"school", "person", "location"} or (
                    owner_kind != "school" and not owner_key
                ):
                    raise ValueError("寄宿完整覆盖缺少精确归属对象。")
                content["coverage_basis"] = {
                    "schema_version": 1,
                    "school_id": str(actor.school_id),
                    "owner_kind": owner_kind,
                    "owner_source_key": owner_key,
                    "target_fact_kind": kind,
                    "target_source_key": target_key,
                    "field_name": content.get("field_name"),
                    "purpose": content.get("purpose"),
                    "contact_kind": content.get("contact_kind"),
                    "valid_from": effective_at.isoformat(),
                    "valid_to": content.get("valid_to"),
                    "school_fact_version": school_version,
                    "member_count": count,
                    "member_sha256": digest,
                }
            else:
                content.pop("coverage_basis", None)
        material = {
            "schema_version": 4,
            "base_version": school_version,
            "source_sha256": hashlib.sha256(str(request_id).encode()).hexdigest(),
            "changes": [
                {
                    "schema_version": 4,
                    "fact_kind": "fact_quality",
                    "source_key": key,
                    "expected_version": version,
                    "operation": payload.operation,
                    "effective_at": effective_at.isoformat(),
                    "content": content,
                    "origin": {"sheet_code": "residence_quality", "row_number": 1},
                }
            ],
        }
        changes = CanonicalFactChangeSetV4.model_validate_json(json.dumps(material, default=str))
        proof = self.bridge.require_change_set(actor, changes)

        def current(selected: Any) -> Any:
            if selected != actor:
                raise PermissionError("当前维护身份已变化。")
            if old is not None:
                old["proof"].revalidate()
            proof.revalidate()
            if target_proof is not None:
                target_proof.revalidate(actor=selected)

        current(actor)
        return ResolvedFactChange(
            changes,
            FactChangeSummary(
                fact_kind="fact_quality",
                label=LABEL,
                reference=payload.target_ref or payload.creation_ref,
                operation=payload.operation,
                before=self.public_fields(actor, before) if before else None,
                after=self.public_fields(actor, content) if content is not None else None,
                effective_at=effective_at,
                reason=payload.reason,
                source_label="学校正式登记的寄宿依据",
            ),
            current,
        )

    def query(
        self,
        *,
        actor: Any,
        reference: Any = None,
        related_ref: Any = None,
        as_of: Any = None,
        limit: Any = 100,
        cursor: Any = None,
        **unused: Any,
    ) -> Any:
        if unused.get("query_text"):
            raise ValueError("请按正式对象查询寄宿依据。")
        if self.cursor_codec is None or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid quality page context")
        now = self.residence.clock()
        at = at_time(as_of) if as_of is not None else now
        permit = self.bridge.f2_guard.require_current(actor, "fact_quality", purpose="read")
        scope = F2FactScope.from_reference(permit.scope_plan.reference)
        if scope.school_id != actor.school_id or scope.fact_kind != "fact_quality":
            raise PermissionError("依据范围与当前学校不符。")
        version = self.bridge.projection.current_version(actor.school_id)
        context = self.residence.read_page_context(actor, as_of=as_of)
        scope_hash = hashlib.sha256(
            f"{scope.document_sha256}:{context.scope_hash}".encode()
        ).hexdigest()
        selection = {
            "kind": "fact_quality",
            "reference": reference,
            "related_ref": related_ref,
            "at": at.isoformat() if as_of is not None else None,
            "limit": limit,
        }
        page = self.cursor_codec.decode(
            actor=actor,
            cursor=cursor,
            selection=selection,
            version=version,
            now=now,
            school_coverage=scope.is_school_wide,
        )
        if cursor is not None and page.get("scope") != scope_hash:
            raise ValueError("资料或权限已变化，请重新查询。")
        if reference:
            keys = (self.source_for(actor, reference),)
        elif scope.is_school_wide:
            keys = self._school_quality_keys(actor, scope, permit, version)
        else:
            keys = tuple(scope.object_keys)
        if any(not scope.allows_key(key) for key in keys):
            raise PermissionError("依据不在当前读取范围。")
        target_kind, target_key = None, None
        if related_ref:
            parts = related_ref.split(":", 2)
            coordinate = school_scope_reference_coordinates(related_ref)
            if coordinate and coordinate[0] == "f6" and not coordinate[2]:
                parts = ["f6", coordinate[1], related_ref]
            if len(parts) != 3 or parts[0] != "f6" or parts[1] not in F6_FACT_KINDS:
                raise ValueError("请选择当前获准的寄宿对象。")
            target_kind = parts[1]
            target_key = self.residence.source_for_reference(
                actor=actor, fact_kind=target_kind, reference=related_ref
            )
        offset = page["offset"]
        selected = keys[offset : offset + limit]
        rows: list[dict[str, Any]] = []
        proofs: list[Any] = [context.proof]
        for key in selected:
            coordinates, _ = self.bridge.coordinates(actor, key)
            if coordinates["content"].get("target_fact_kind") not in F6_FACT_KINDS:
                continue
            if related_ref:
                if (
                    coordinates["content"]["target_fact_kind"] != target_kind
                    or coordinates["content"].get("target_source_key") != target_key
                ):
                    continue
            value = self.bridge.read_content(actor, key)
            proofs.append(value["proof"])
            start, end = at_time(value["valid_from"]), at_time(value.get("valid_to"))
            if not start <= at or end is not None and at >= end:
                continue
            rows.append(
                {
                    "reference": self.reference_for(actor, key),
                    "fact_kind": "fact_quality",
                    "label": LABEL,
                    "fields": self.public_fields(actor, value["content"]),
                    "version": value["version"],
                    "valid_from": start.isoformat(),
                    "valid_to": end.isoformat() if end else None,
                    "source": value.get("source") or {"label": "学校正式登记资料"},
                }
            )
        for proof in proofs:
            proof.revalidate()
        self.bridge.f2_guard.revalidate(permit, actor)
        if self.bridge.projection.current_version(actor.school_id) != version:
            raise PermissionError("寄宿依据在查询期间已变化。")
        more = offset + len(selected) < len(keys)
        boundary = context.expires
        until = getattr(
            getattr(getattr(permit, "basis", None), "governance", None), "valid_until", None
        )
        if until is not None:
            boundary = min(boundary, until) if boundary else until
        return {
            "kind": "directory_facts",
            "family_owner": "school_facts.residence",
            "fact_kind": "fact_quality",
            "status": "known" if rows else "no_current_records",
            "records": rows,
            "returned_count": len(rows),
            "matched_count": None,
            "truncated": more,
            "next_cursor": self.cursor_codec.encode(
                actor=actor,
                selection=selection,
                offset=offset + len(selected),
                version=version,
                scope=scope_hash,
                expires=boundary,
            )
            if more
            else None,
            "queried_at": now.isoformat(),
            "factual_at": at.isoformat(),
            "coverage": {
                "status": "unknown",
                "description": "仅当前获准的寄宿依据；不表示全校资料完整。",
            },
        }

    def _school_quality_keys(self, actor: Any, scope: Any, permit: Any, version: int):
        keys: list[str] = []
        after = None
        while True:
            self.bridge.f2_guard.revalidate(permit, actor)
            page = self.bridge.projection.scope_keys(
                school_id=actor.school_id,
                family="f2",
                fact_kind="fact_quality",
                after_key=after,
                limit=100,
            )
            if (
                type(page) is not dict
                or type(page.get("keys")) is not list
                or len(page["keys"]) > 100
                or type(page.get("has_more")) is not bool
                or page.get("school_fact_version") != version
            ):
                raise PermissionError("寄宿依据枚举期间已变化。")
            selected = page["keys"]
            if (
                any(not scope.allows_key(key) for key in selected)
                or selected != sorted(set(selected))
                or selected
                and after is not None
                and selected[0] <= after
            ):
                raise ValueError("Invalid quality source ordering")
            more = page["has_more"]
            next_after = selected[-1] if more and selected else None
            if page.get("next_after") != next_after or more and not selected:
                raise ValueError("Invalid quality source continuation")
            keys.extend(selected)
            if not more:
                break
            after = next_after
        self.bridge.f2_guard.revalidate(permit, actor)
        if self.bridge.projection.current_version(actor.school_id) != version:
            raise PermissionError("寄宿依据枚举期间已变化。")
        return tuple(keys)
