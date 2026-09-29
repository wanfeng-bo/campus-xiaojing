"""Finite school-fact joins over the existing authorized directory query port."""

from __future__ import annotations

from typing import Literal, Protocol, Self, cast

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from app.composition.school_fact_directory_product import (
    F2FactKind,
    FactQueryInput,
    Reference,
    Text,
)
from app.platform.assistant.contracts import JsonObject
from app.platform.authorization.secure_reader_contracts import SecureReadDenied
from app.platform.contracts import FrozenContractModel, StaffActorContext
from app.platform.school_facts.read_policy import SchoolFactAuthorizationUnavailable


class ServiceLookupTarget(FrozenContractModel):
    query_text: Text | None = Field(default=None, description="服务正式名称，精确匹配。")
    reference: Reference | None = Field(default=None, description="已返回的服务对象引用。")

    @model_validator(mode="after")
    def exact_selection(self) -> Self:
        if (self.query_text is None) == (self.reference is None):
            raise ValueError("每个服务目标须提供一个正式名称或已返回引用。")
        return self


class FactLookupInput(FrozenContractModel):
    subject_kind: Literal["location", "organization", "school_service", "person"]
    query_text: Text | None = Field(default=None, description="目标正式名称，精确匹配。")
    reference: Reference | None = Field(default=None, description="已返回的目标对象引用。")
    targets: tuple[ServiceLookupTarget, ...] | None = Field(
        default=None,
        min_length=1,
        max_length=4,
        description="比较多个指定服务时，可提供最多4个精确目标；仅school_service支持，与单目标名称/引用和all_authorized互斥。各目标单独核对，不因同名选择首项。",
    )
    campus_name: Text | None = Field(
        default=None, description="组织的正式校区名称约束；只依据组织与校区正式关系。"
    )
    include: tuple[
        Literal[
            "official_contact",
            "organization_profile",
            "organization_location",
            "organization_campus_relationship",
            "assignment",
        ],
        ...,
    ] = Field(
        min_length=1,
        max_length=3,
        description="所需事实：人员仅assignment，按姓名或引用定位后返回其获准正式任职，可合法多项；地点仅official_contact；组织支持official_contact、organization_profile职能、organization_location办公地点，正式校区自动返回；服务支持指定official_contact及提供组织organization_campus_relationship，其组织校区不等于服务覆盖范围。",
    )
    scope: Literal["selected", "all_authorized"] = Field(
        default="selected",
        description="仅学校服务允许显式获准短列表all_authorized；其它主体只允许具体目标selected。",
    )
    include_supporting_evidence: bool = Field(
        default=False,
        description=(
            "每条记录已随附source发布来源，普通来源追问直接使用，不需要重新查询或describe。"
            "仅用户要求逐字核对原始依据字段、文号或维护依据时开启；原值未分类为文件名、文号或追踪值，不据格式猜用途。"
            "此开关不返回路径或业务编码，完整依据也保留在页面展开证据中。"
        ),
    )
    include_routing_details: bool = Field(
        default=False,
        description="仅用户明确核对原记录路径或业务编码时开启；不用于普通来源说明，也不返回原始依据值。路径不作正式名称或关联证据。",
    )
    as_of: AwareDatetime | None = None

    @model_validator(mode="after")
    def selection(self) -> Self:
        if self.targets is not None:
            if (
                self.subject_kind != "school_service"
                or self.scope != "selected"
                or self.query_text is not None
                or self.reference is not None
            ):
                raise ValueError("多目标仅限指定服务，不可与单目标或获准列表混用。")
        elif self.scope == "all_authorized":
            if (
                self.subject_kind != "school_service"
                or self.query_text is not None
                or self.reference is not None
            ):
                raise ValueError("只有服务获准列表可以不指定目标，不能同时提供名称或引用。")
        elif (self.query_text is None) == (self.reference is None):
            raise ValueError("提供一个正式名称或已返回引用。")
        if self.subject_kind == "person" and (
            self.campus_name is not None or self.include != ("assignment",)
        ):
            raise ValueError("人员组合仅支持明确人员的正式任职；校区不用于猜测人员归属。")
        if self.subject_kind != "person" and "assignment" in self.include:
            raise ValueError("任职组合须以明确人员为目标。")
        if self.subject_kind == "school_service" and (
            self.campus_name is not None
            or any(
                kind not in {"official_contact", "organization_campus_relationship"}
                for kind in self.include
            )
        ):
            raise ValueError("服务只支持指定官方渠道、提供组织正式校区；不得按校区暗中过滤服务。")
        if (
            self.subject_kind == "organization"
            and "organization_campus_relationship" in self.include
        ):
            raise ValueError("组织正式校区已随定位返回，不作为新增请求事实。")
        if len(set(self.include)) != len(self.include):
            raise ValueError("查询事实不得重复。")
        if self.subject_kind == "location" and (
            self.campus_name is not None or any(kind != "official_contact" for kind in self.include)
        ):
            raise ValueError("地点查询只支持官方联系，校区约束仅用于组织。")
        return self


class DirectoryLookupPort(Protocol):
    def query(self, *, actor: StaffActorContext, payload: FactQueryInput) -> JsonObject: ...


def _records(view: JsonObject) -> list[JsonObject]:
    rows = view.get("records")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _incomplete(view: JsonObject, *, combination_limit: int | None = 8) -> bool:
    return (
        bool(view.get("truncated"))
        or bool(view.get("has_more"))
        or bool(view.get("next_cursor"))
        or combination_limit is not None
        and len(_records(view)) > combination_limit
    )


def _quality_incomplete(view: JsonObject) -> bool:
    quality = view.get("quality")
    return isinstance(quality, dict) and (
        quality.get("evidence_status") in {"source_unavailable", "forbidden", "denied"}
        or bool(quality.get("evidence_truncated"))
        or quality.get("source_availability") == "source_unavailable"
    )


def lookup(
    adapter: DirectoryLookupPort, *, actor: StaffActorContext, payload: FactLookupInput
) -> JsonObject:
    """Resolve authorized subjects and only their finite direct fact relationships.

    Every dependency travels through query, so the caller's existing read-capture
    context also captures empty selections, relations, names and source failures.
    No state survives this call; a changed target is resolved afresh.
    """
    results: list[JsonObject] = []
    cache: dict[tuple[str, str | None, str | None, str | None, str], JsonObject] = {}
    service_reads: dict[str, int] = {}
    service_rows: dict[str, int] = {}

    def query(
        kind: F2FactKind,
        *,
        name: str | None = None,
        reference: str | None = None,
        related: str | None = None,
        scope: Literal["selected", "all_authorized"] = "selected",
    ) -> JsonObject:
        key = kind, name, reference, related, scope
        if key in cache:
            return cache[key]
        request = FactQueryInput(
            fact_kind=kind,
            query_text=name,
            reference=reference,
            related_ref=related,
            as_of=payload.as_of,
            scope=scope,
        )
        bounded_service = payload.subject_kind == "school_service"
        if bounded_service and len(results) >= 32:
            # The existing display contract accepts at most 32 query views.
            # Stop before IO; preserve every checked view and mark only the
            # unperformed dependency in its service group, never discard a read.
            limited: JsonObject = {
                "kind": "directory_facts",
                "fact_kind": kind,
                "status": "partial",
                "records": [],
                "truncated": True,
                "query": request.model_dump(mode="json"),
                "notice": "本次有限组合读取已到总依赖上限，尚未读取这一部分。",
            }
            cache[key] = limited
            return limited
        if bounded_service and (service_reads.get(kind, 0) >= 8 or service_rows.get(kind, 0) >= 8):
            result: JsonObject = {
                "kind": "directory_facts",
                "fact_kind": kind,
                "status": "partial",
                "records": [],
                "truncated": True,
                "query": request.model_dump(mode="json"),
                "notice": "本次有限组合读取已到该类依赖上限，尚未读取这一部分。",
            }
            cache[key] = result
            results.append(result)
            return result
        try:
            result: JsonObject = adapter.query(actor=actor, payload=request)
        except (PermissionError, SecureReadDenied):
            result = {
                "kind": "directory_facts",
                "fact_kind": kind,
                "status": "denied",
                "records": [],
                "query": request.model_dump(mode="json"),
            }
        except SchoolFactAuthorizationUnavailable:
            result = {
                "kind": "directory_facts",
                "fact_kind": kind,
                "status": "source_unavailable",
                "records": [],
                "query": request.model_dump(mode="json"),
            }
        if bounded_service:
            service_reads[kind] = service_reads.get(kind, 0) + 1
            rows = _records(result)
            remaining = 8 - service_rows.get(kind, 0)
            if len(rows) > remaining:
                result = {
                    **result,
                    "records": cast(list[JsonValue], rows[:remaining]),
                    "truncated": True,
                    "returned_count": remaining,
                }
            service_rows[kind] = service_rows.get(kind, 0) + min(len(rows), remaining)
        cache[key] = result
        results.append(result)
        return result

    def bundle(
        status: str,
        *,
        subject: JsonObject | None = None,
        candidates: list[JsonObject] | None = None,
    ) -> JsonObject:
        result: JsonObject = {
            "kind": "directory_bundle",
            "status": status,
            "subject_label": subject.get("label") if subject else payload.query_text,
            "results": cast(list[JsonValue], results),
            "requested_fact_kinds": list(payload.include),
            "query_completion": (
                "subject_unresolved"
                if subject is None
                else "completed"
                if status in {"known", "no_current_records", "formal_negative"}
                else "incomplete"
            ),
        }
        if subject:
            result["subject_reference"] = subject.get("reference")
        if candidates is not None:
            # Person joins keep the authorized Reader page and its continuation;
            # the older multi-service expansion cap does not truncate candidates.
            result["candidates"] = cast(
                list[JsonValue], candidates if payload.subject_kind == "person" else candidates[:8]
            )
        return result

    def service_bundle(target: JsonObject, candidates: list[JsonObject]) -> JsonObject:
        # These are typed positive paths, not service coverage or an exclusion proof.
        # Every referenced object still passes through its own authorized query.
        if target.get("status") not in {
            "known",
            "ambiguous",
            "no_current_records",
            "formal_negative",
        }:
            result = bundle(str(target.get("status", "partial")))
            result["service_groups"] = []
            if payload.scope == "all_authorized":
                result["query_completion"] = "incomplete"
            return result
        if payload.scope == "selected" and (_incomplete(target) or len(candidates) > 1):
            return bundle("partial" if _incomplete(target) else "ambiguous", candidates=candidates)
        if not candidates:
            result = bundle(str(target.get("status", "partial")))
            result["service_groups"] = []
            if payload.scope == "all_authorized":
                result["query_completion"] = (
                    "incomplete"
                    if _incomplete(target) or _quality_incomplete(target)
                    else "completed"
                )
            return result

        groups: list[JsonValue] = []
        complete_states = {"known", "no_current_records", "formal_negative"}
        incomplete = _incomplete(target) or _quality_incomplete(target)
        process_incomplete = incomplete

        def linked(kind: F2FactKind, reference: JsonValue) -> tuple[JsonObject, JsonObject | None]:
            nonlocal process_incomplete
            if not isinstance(reference, str):
                return {"status": "reference_not_provided"}, None
            part = query(kind, reference=reference)
            state = str(part.get("status", "partial"))
            rows = _records(part)
            if state == "known" and (len(rows) != 1 or rows[0].get("reference") != reference):
                state = "partial"
            if _incomplete(part) or _quality_incomplete(part):
                state = "partial" if state in complete_states else state
            if state not in complete_states:
                process_incomplete = True
            return {"reference": reference, "status": state}, part

        for service in candidates[:8]:
            ref = service.get("reference")
            fields = service.get("fields")
            if not isinstance(ref, str) or not isinstance(fields, dict):
                incomplete = True
                process_incomplete = True
                continue
            group: JsonObject = {"service_reference": ref}
            group_incomplete = False
            if "official_contact" in payload.include:
                contact, _ = linked("official_contact", fields.get("official_contact_ref"))
                group["official_contact"] = contact
                group_incomplete = contact["status"] not in complete_states
            if "organization_campus_relationship" in payload.include:
                group["relationship_scope"] = "provider_affiliation_only"
                provider, _ = linked("organization", fields.get("organization_ref"))
                group["provider"] = provider
                provider_ref = fields.get("organization_ref")
                campuses: JsonObject = {
                    "status": "not_queried",
                    "relation_references": [],
                    "campuses": [],
                }
                group["provider_campuses"] = campuses
                if provider["status"] == "known" and isinstance(provider_ref, str):
                    relations = query("organization_campus_relationship", related=provider_ref)
                    state = str(relations.get("status", "partial"))
                    if _incomplete(relations) or _quality_incomplete(relations):
                        state = "partial" if state in complete_states else state
                    campuses["status"] = state
                    if state not in complete_states:
                        process_incomplete = True
                    relationship_refs: list[JsonValue] = []
                    campus_links: list[JsonValue] = []
                    seen: set[str] = set()
                    for relation in _records(relations)[:8]:
                        relation_fields = relation.get("fields")
                        relation_ref = relation.get("reference")
                        if (
                            not isinstance(relation_fields, dict)
                            or relation_fields.get("organization_ref") != provider_ref
                            or not isinstance(relation_ref, str)
                        ):
                            campuses["status"] = "partial"
                            process_incomplete = True
                            continue
                        relationship_refs.append(relation_ref)
                        campus_ref = relation_fields.get("campus_ref")
                        if not isinstance(campus_ref, str):
                            process_incomplete = True
                        if isinstance(campus_ref, str) and campus_ref in seen:
                            continue
                        if isinstance(campus_ref, str):
                            seen.add(campus_ref)
                        campus, campus_view = linked("location", campus_ref)
                        if campus["status"] == "known" and campus_view is not None:
                            campus_fields = _records(campus_view)[0].get("fields")
                            if (
                                not isinstance(campus_fields, dict)
                                or campus_fields.get("kind") != "campus"
                            ):
                                campus["status"] = "partial"
                                process_incomplete = True
                        campus_links.append(campus)
                        if campus["status"] != "known":
                            campuses["status"] = "partial"
                    campuses["relation_references"] = relationship_refs
                    campuses["campuses"] = campus_links
                group_incomplete = (
                    group_incomplete
                    or provider["status"] != "known"
                    or campuses["status"] not in complete_states
                )
            group["status"] = "partial" if group_incomplete else "known"
            incomplete = incomplete or group_incomplete
            groups.append(group)
        result = bundle(
            "partial" if incomplete else "known",
            subject=candidates[0] if payload.scope == "selected" else None,
        )
        result["service_groups"] = groups
        result["query_completion"] = "incomplete" if process_incomplete else "completed"
        result["selection_scope"] = payload.scope
        return result

    if payload.targets is not None:
        target_results: list[JsonValue] = []
        groups: list[JsonValue] = []
        grouped: set[str] = set()
        completed = True
        fully_known = True
        for selection in payload.targets:
            target = query(
                "school_service", name=selection.query_text, reference=selection.reference
            )
            candidates = _records(target)
            part = service_bundle(target, candidates)
            resolution = str(target.get("status", "partial"))
            if len(candidates) > 1:
                resolution = "partial" if _incomplete(target) else "ambiguous"
            elif _incomplete(target):
                resolution = "partial"
            complete_empty = (
                resolution in {"no_current_records", "formal_negative"}
                and not _incomplete(target)
                and not _quality_incomplete(target)
            )
            target_result: JsonObject = {
                "selection": selection.model_dump(mode="json", exclude_none=True),
                "status": resolution,
                "component_status": part["status"],
                "query_completion": "completed" if complete_empty else part["query_completion"],
                "service_references": [],
            }
            if resolution == "ambiguous":
                target_result["candidate_references"] = [
                    row["reference"]
                    for row in candidates[:8]
                    if isinstance(row.get("reference"), str)
                ]
            selected_refs: list[JsonValue] = []
            for group in cast(list[JsonValue], part.get("service_groups", [])):
                if not isinstance(group, dict):
                    continue
                ref = group.get("service_reference")
                if isinstance(ref, str):
                    selected_refs.append(ref)
                    if ref not in grouped:
                        grouped.add(ref)
                        groups.append(group)
            target_result["service_references"] = selected_refs
            target_results.append(target_result)
            completed = completed and target_result["query_completion"] == "completed"
            fully_known = fully_known and resolution == "known" and part["status"] == "known"
        result = bundle("known" if fully_known else "partial")
        result["service_groups"] = groups
        result["target_results"] = target_results
        result["query_completion"] = "completed" if completed else "incomplete"
        result["selection_scope"] = "selected_targets"
        return result

    target = query(
        payload.subject_kind,
        name=payload.query_text,
        reference=payload.reference,
        scope=payload.scope,
    )
    candidates = _records(target)
    if payload.subject_kind == "school_service":
        return service_bundle(target, candidates)

    if target.get("status") not in {"known", "ambiguous"}:
        return bundle(str(target.get("status", "partial")))
    page_limit = None if payload.subject_kind == "person" else 8
    if _incomplete(target, combination_limit=page_limit):
        return bundle("partial", candidates=candidates)
    if not candidates:
        return bundle("partial")

    if (
        len(candidates) > 1
        and payload.campus_name is None
        and payload.subject_kind != "organization"
    ):
        return bundle("ambiguous", candidates=candidates)

    campus: JsonObject | None = None
    if payload.campus_name is not None:
        campus_view = query("location", name=payload.campus_name)
        campuses = _records(campus_view)
        if campus_view.get("status") not in {"known", "ambiguous"}:
            return bundle(str(campus_view.get("status", "partial")))
        if _incomplete(campus_view):
            return bundle("partial", candidates=campuses)
        if len(campuses) != 1:
            return bundle("ambiguous", candidates=campuses)
        campus = campuses[0]
        fields = campus.get("fields")
        if not isinstance(fields, dict) or fields.get("kind") != "campus":
            return bundle("partial")

    matched: list[JsonObject] = []
    unresolved = False
    if payload.subject_kind == "organization":
        for candidate in candidates:
            ref = candidate.get("reference")
            if not isinstance(ref, str):
                return bundle("partial")
            relation_view = query("organization_campus_relationship", related=ref)
            if relation_view.get("status") not in {
                "known",
                "no_current_records",
                "formal_negative",
            }:
                return bundle(str(relation_view.get("status", "partial")))
            if _incomplete(relation_view):
                return bundle("partial", candidates=candidates)
            linked: set[str] = set()
            campus_labels: list[JsonObject] = []
            for relation in _records(relation_view):
                fields = relation.get("fields")
                if not isinstance(fields, dict) or fields.get("organization_ref") != ref:
                    unresolved = True
                    continue
                campus_ref = fields.get("campus_ref")
                if not isinstance(campus_ref, str):
                    unresolved = True
                    continue
                if campus is not None and campus.get("reference") != campus_ref:
                    continue
                # A relation never grants permission to its linked object's label.
                location = campus if campus and campus.get("reference") == campus_ref else None
                if location is None:
                    location_view = query("location", reference=campus_ref)
                    locations = _records(location_view)
                    if (
                        location_view.get("status") != "known"
                        or len(locations) != 1
                        or _incomplete(location_view)
                    ):
                        unresolved = True
                        continue
                    location = locations[0]
                location_fields = location.get("fields")
                if not isinstance(location_fields, dict) or location_fields.get("kind") != "campus":
                    unresolved = True
                    continue
                linked.add(campus_ref)
                campus_labels.append({"reference": campus_ref, "label": location.get("label")})
            if campus is None or campus.get("reference") in linked:
                matched.append({**candidate, "campuses": cast(list[JsonValue], campus_labels)})
        if unresolved:
            return bundle("partial", candidates=candidates)
        candidates = matched

    if not candidates:
        return bundle("no_current_records")
    if len(candidates) != 1:
        return bundle("ambiguous", candidates=candidates)
    subject = candidates[0]
    ref = subject.get("reference")
    if not isinstance(ref, str):
        return bundle("partial")
    requested: list[JsonObject] = []
    for kind in payload.include:
        fact_view = query(kind, related=ref)
        requested.append(fact_view)
        if kind == "organization_location" and fact_view.get("status") == "known":
            if _incomplete(fact_view):
                return bundle("partial", subject=subject)
            for relation in _records(fact_view):
                fields = relation.get("fields")
                location_ref = fields.get("location_ref") if isinstance(fields, dict) else None
                if isinstance(location_ref, str):
                    query("location", reference=location_ref)
                else:
                    unresolved = True
    statuses = {str(view.get("status")) for view in results}
    if "source_unavailable" in statuses:
        status = "source_unavailable"
    elif "denied" in statuses:
        status = "denied"
    elif unresolved or any(
        _incomplete(view, combination_limit=page_limit) or _quality_incomplete(view)
        for view in results
    ):
        status = "partial"
    elif statuses - {"known", "ambiguous", "no_current_records", "formal_negative"}:
        status = "partial"
    else:
        # Completing a bounded query does not require a positive value. Preserve
        # each requested part, including explicit negatives and current absence.
        requested_statuses = {str(view.get("status")) for view in requested}
        status = next(iter(requested_statuses)) if len(requested_statuses) == 1 else "known"
    return bundle(status, subject=subject)
