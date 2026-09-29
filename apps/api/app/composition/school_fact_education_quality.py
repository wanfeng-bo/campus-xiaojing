"""Finite F2 quality/policy facts intersected with real F4 owners and fields."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from app.composition.school_fact_education_reader import EducationReadStale, aware, timestamp
from app.platform.contracts import StaffActorContext
from app.platform.governance.contracts import GovernanceAuthorizationBasis
from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.f2_policy import F2FactScope
from app.platform.school_facts.f4_policy import F4_FACT_FIELDS, F4_FACT_KINDS
from app.platform.school_facts.f5_policy import F5_FACT_FIELDS, F5_FACT_KINDS

EDUCATION_DEPENDENCY_KINDS = F4_FACT_KINDS | F5_FACT_KINDS
EDUCATION_DEPENDENCY_FIELDS = F4_FACT_FIELDS | F5_FACT_FIELDS


class EducationDependencyDenied(PermissionError):
    """An explicit missing F2 dependency grant, before any factual projection."""


@dataclass(frozen=True)
class EducationDependencyProof:
    bridge: Any
    actor: StaffActorContext
    f2_permits: tuple[Any, ...]
    education_permits: tuple[Any, ...]
    school_fact_version: int
    next_boundary: datetime | None = None

    def revalidate(self) -> None:
        from app.composition.school_fact_read_evidence import (
            register_release_time_window,
            validation_batch,
        )

        with validation_batch() as release:
            observed_at = self.bridge.clock()
            for p in self.f2_permits:
                self.bridge.f2_guard.revalidate(p, self.actor)
                governance = getattr(getattr(p, "basis", None), "governance", None)
                if isinstance(governance, GovernanceAuthorizationBasis):
                    register_release_time_window(
                        self.bridge.clock,
                        observed_at=governance.issued_as_of,
                        valid_before=governance.valid_until,
                    )
            batch = getattr(self.bridge.education_authority, "revalidate_many", None)
            if batch is not None:
                batch(self.education_permits, actor=self.actor, checked_at=self.bridge.clock())
            else:
                for p in self.education_permits:
                    self.bridge.education_authority.revalidate(
                        p, actor=self.actor, checked_at=self.bridge.clock()
                    )

            def content_current() -> None:
                if (
                    self.bridge.projection.current_version(self.actor.school_id)
                    != self.school_fact_version
                ):
                    raise EducationReadStale("质量或制度依据已变化。")
                if self.next_boundary is not None and self.bridge.clock() >= self.next_boundary:
                    raise EducationReadStale("质量或制度适用期间已变化。")

            release.check_content(
                (id(self.bridge), self.actor, self.school_fact_version, self.next_boundary),
                content_current,
            )
            register_release_time_window(
                self.bridge.clock,
                observed_at=observed_at,
                valid_before=self.next_boundary,
            )


@dataclass(frozen=True)
class EducationQualityReadProof:
    proofs: tuple[Any, ...]
    school_fact_version: int

    def revalidate(self) -> None:
        for proof in self.proofs:
            proof.revalidate()


class EducationQualityBridge:
    def __init__(
        self,
        *,
        f2_guard: Any,
        education_authority: Any,
        projection: Any,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        target_content_resolver: Any = None,
    ) -> None:
        self.target_content_resolver = target_content_resolver or self.read_for_source
        self.known_dependencies: set[tuple[str, str, str]] = set()
        self.f2_guard, self.education_authority, self.projection, self.clock = (
            f2_guard,
            education_authority,
            projection,
            clock,
        )

    def require_dependency_scope(
        self, actor: StaffActorContext, kind: str, purpose: str
    ) -> tuple[Any, F2FactScope]:
        try:
            permit = self.f2_guard.require_current(actor, kind, purpose=purpose)
        except SchoolFactChangeRejected as exc:
            if (kind, purpose) != ("fact_quality", "read"):
                raise
            raise EducationDependencyDenied("当前身份没有这项关联资料的精确权限。") from exc
        scope = F2FactScope.from_reference(permit.scope_plan.reference)
        if scope.school_id != actor.school_id or scope.fact_kind != kind:
            raise PermissionError("不能借用其他学校或类型的质量权限。")
        return permit, scope

    def _education(
        self,
        actor: StaffActorContext,
        kind: str,
        keys: tuple[str, ...],
        fields: tuple[str, ...],
        action: str = "read",
    ) -> Any:
        if (
            kind not in EDUCATION_DEPENDENCY_KINDS
            or not set(fields) <= EDUCATION_DEPENDENCY_FIELDS[kind]
        ):
            raise ValueError("须明确教育事实和字段。")
        p = self.education_authority.require_scope(
            actor, fact_kind=kind, action=action, checked_at=self.clock()
        )
        if fields:
            self.education_authority.require_fields(p, fields)
        for key in keys:
            self.education_authority.require_object(p, key, is_creation=False)
        return p

    def read_for_source(
        self, actor: StaffActorContext, kind: str, key: str, at: datetime | None = None
    ) -> dict[str, Any]:
        factual = aware(at or self.clock())
        permits: list[Any] = []

        def before() -> None:
            permit, scope = self.require_dependency_scope(actor, kind, "read")
            if not scope.allows_key(key):
                raise PermissionError("原依据没有精确读取权限。")
            permits.append(permit)

        def after(raw: dict[str, Any]) -> None:
            for permit in permits:
                self.f2_guard.revalidate(permit, actor)
            if raw.get("school_fact_version") != self.projection.current_version(actor.school_id):
                raise EducationReadStale("依据坐标已变化。")

        raw = self.projection.selection(
            school_id=actor.school_id,
            kind=kind,
            source_key=key,
            as_of=factual,
            before_read=before,
            after_read=after,
        )
        rows = raw.get("records", [])
        allowed = {
            "source_key",
            "version",
            "owner_kind",
            "owner_key",
            "target_kind",
            "target_key",
            "replacement_key",
            "field_name",
            "academic_year",
            "position_key",
            "campus_key",
            "policy_key",
            "teaching_role",
        }
        if len(rows) != 1 or rows[0].get("source_key") != key or set(rows[0]) - allowed:
            raise PermissionError("原教育依据坐标不能唯一确认。")
        row = rows[0]
        target_kind = row["target_kind"]
        if target_kind not in EDUCATION_DEPENDENCY_KINDS:
            raise PermissionError("原依据不是教育事实。")
        fields = (
            (row["field_name"],)
            if row.get("field_name")
            else tuple(sorted(EDUCATION_DEPENDENCY_FIELDS[target_kind]))
        )
        target_keys = tuple(
            dict.fromkeys(row[name] for name in ("target_key", "replacement_key") if row.get(name))
        )
        result = self.read_dependency(
            actor,
            kind=kind,
            source_keys=(key,),
            owner_kind=row["owner_kind"],
            owner_source_keys=(row["owner_key"],),
            target_kind=target_kind,
            target_source_keys=target_keys,
            field_names=fields,
            as_of=at,
            academic_year=row.get("academic_year"),
            policy_source_keys=(row["policy_key"],) if row.get("policy_key") else (),
            campus_source_keys=(row["campus_key"],) if row.get("campus_key") else (),
            position_source_keys=(row["position_key"],) if row.get("position_key") else (),
            teaching_roles=(row["teaching_role"],) if row.get("teaching_role") else (),
        )
        if (
            result["proof"].school_fact_version != raw["school_fact_version"]
            or len(result["records"]) != 1
            or result["records"][0].get("version") != row["version"]
        ):
            raise EducationReadStale("原依据读取期间发生变化。")
        return {
            "content": result["records"][0]["content"],
            "row": result["records"][0],
            "proof": result["proof"],
        }

    def recognizes_change(self, actor: StaffActorContext, change: Any) -> bool:
        if change.fact_kind not in {"fact_quality", "policy_scope"}:
            return False
        if change.content is not None:
            return getattr(
                change.content, "education_target_kind", None
            ) in EDUCATION_DEPENDENCY_KINDS and bool(
                getattr(change.content, "education_target_source_key", None)
            )
        resolved = self.read_for_source(
            actor, change.fact_kind, change.source_key, change.effective_at
        )
        return resolved["content"].get("education_target_kind") in EDUCATION_DEPENDENCY_KINDS

    def require_change_set(
        self, actor: StaffActorContext, change_set: Any
    ) -> EducationDependencyProof:
        fp: list[Any] = []
        ep: list[Any] = []
        relevant = [
            c for c in change_set.changes if c.fact_kind in {"fact_quality", "policy_scope"}
        ]
        if not relevant:
            raise ValueError("没有教育质量或制度适用范围修改。")
        for change in relevant:
            kind = change.fact_kind
            p, scope = self.require_dependency_scope(actor, kind, "maintain")
            if not scope.allows_key(
                change.source_key, is_creation=change.expected_version == 0
            ) or (change.expected_version == 0 and (change.operation != "upsert")):
                raise PermissionError("该F2教育专用对象没有取得精确维护权限。")
            fp.append(p)
            if change.content is None:
                if self.target_content_resolver is None:
                    raise PermissionError("终止前须另有读取权限以核验原教育关联。")
                resolved = self.target_content_resolver(
                    actor, kind, change.source_key, change.effective_at
                )
                resolved["proof"].revalidate()
                if resolved["row"]["version"] != change.expected_version:
                    raise EducationReadStale("原依据版本已变化。")
                content = resolved["content"]
            else:
                content = change.content.model_dump(exclude_none=True, exclude_unset=True)
            owner_kind = content.get("education_target_kind")
            owner_key = content.get("education_target_source_key")
            if owner_kind not in EDUCATION_DEPENDENCY_KINDS or not isinstance(owner_key, str):
                raise PermissionError("只能维护明确教育对象的质量或制度适用资料。")
            ep.append(self._education(actor, owner_kind, (owner_key,), ()))
            if kind == "fact_quality":
                target_kind = content.get("target_fact_kind")
                if (
                    target_kind not in EDUCATION_DEPENDENCY_KINDS
                    or content.get("owner_kind") != "education"
                ):
                    raise PermissionError("质量目标不属于明确教育事实。")
                if content.get("teaching_role") is not None and (
                    target_kind != "teaching_assignment"
                    or content["teaching_role"] not in {"lead", "co_teacher"}
                ):
                    raise PermissionError("Quality teaching role is not applicable to this target")
                if content.get("negative_code") == "no_teaching_assignment":
                    if (
                        owner_kind != "teaching_offering"
                        or target_kind != "teaching_assignment"
                        or content.get("teaching_role") not in {"lead", "co_teacher"}
                        or content.get("target_source_key")
                    ):
                        raise PermissionError(
                            "No-teaching statement needs an exact offering and role"
                        )
                    ep.append(
                        self._education(actor, owner_kind, (owner_key,), (), action="maintain")
                    )
                fields = (
                    (content["field_name"],)
                    if content.get("field_name")
                    else tuple(EDUCATION_DEPENDENCY_FIELDS[target_kind])
                )
                keys = tuple(
                    content[k]
                    for k in ("target_source_key", "replacement_source_key")
                    if content.get(k)
                )
                basis = content.get("coverage_basis")
                if isinstance(basis, dict) and basis.get("schema_version") == 2:
                    covered = basis.get("authorized_source_keys")
                    if (
                        target_kind != "education_grade_group"
                        or content.get("target_source_key") is not None
                        or type(covered) not in {list, tuple}
                        or len(covered) > 100
                        or any(type(key) is not str or not key for key in covered)
                        or len(set(covered)) != len(covered)
                    ):
                        raise PermissionError("年级组完整声明须有受控精确成员范围。")
                    keys = tuple(covered)
                ep.append(self._education(actor, target_kind, keys, fields, action="maintain"))
                if content.get("position_source_key"):
                    ep.append(
                        self._education(
                            actor,
                            "class_position_definition",
                            (content["position_source_key"],),
                            ("display_name",),
                        )
                    )
            else:
                ep.append(
                    self._education(
                        actor,
                        owner_kind,
                        (owner_key,),
                        tuple(EDUCATION_DEPENDENCY_FIELDS[owner_kind]),
                        action="maintain",
                    )
                )
                policy, ps = self.require_dependency_scope(actor, "policy", "read")
                if not ps.allows_key(content.get("policy_source_key")):
                    raise PermissionError("制度须有独立读取权限。")
                fp.append(policy)
            if content.get("campus_source_key"):
                campus, cs = self.require_dependency_scope(actor, "location", "read")
                if not cs.allows_key(content["campus_source_key"]):
                    raise PermissionError("校区须有独立读取权限。")
                fp.append(campus)
        proof = EducationDependencyProof(
            self, actor, tuple(fp), tuple(ep), self.projection.current_version(actor.school_id)
        )
        proof.revalidate()
        return proof

    def creation_coordinates(self, actor: StaffActorContext, kind: str, key: str) -> dict[str, Any]:
        permits: list[Any] = []

        def before() -> None:
            permit, scope = self.require_dependency_scope(actor, kind, "maintain")
            if kind not in {"fact_quality", "policy_scope"} or not scope.allows_key(
                key, is_creation=True
            ):
                raise PermissionError("未获准创建该教育依赖对象。")
            permits.append(permit)

        def after(raw: dict[str, Any]) -> None:
            EducationDependencyProof(
                self, actor, tuple(permits), (), raw["school_fact_version"]
            ).revalidate()

        raw = self.projection.coordinates(
            school_id=actor.school_id,
            kind=kind,
            source_keys=(key,),
            before_read=before,
            after_read=after,
        )
        rows = raw.get("records", [])
        if (
            len(rows) != 1
            or rows[0].get("source_key") != key
            or rows[0].get("status") != "not_recorded"
            or rows[0].get("version") != 0
        ):
            raise PermissionError("该对象已经存在或创建状态无法确认，请先读取正式对象。")
        if set(rows[0]) != {"source_key", "status", "version"}:
            raise ValueError("创建检查不允许返回事实内容。")
        return raw

    def quality_for(
        self,
        actor: StaffActorContext,
        *,
        target_kind: str,
        target_source_keys: tuple[str, ...],
        field_names: tuple[str, ...],
        as_of: datetime | None = None,
        owner_kind: str | None = None,
        owner_source_keys: tuple[str, ...] | None = None,
        academic_year: int | None = None,
        campus_source_keys: tuple[str, ...] = (),
        position_source_keys: tuple[str, ...] = (),
        teaching_roles: tuple[str, ...] = (),
        include_target_owner: bool = False,
    ) -> dict[str, Any]:
        result = self.read_dependency(
            actor,
            kind="fact_quality",
            target_kind=target_kind,
            target_source_keys=target_source_keys,
            field_names=field_names,
            as_of=as_of,
            owner_kind=owner_kind or target_kind,
            owner_source_keys=target_source_keys
            if owner_source_keys is None
            else owner_source_keys,
            academic_year=academic_year,
            campus_source_keys=campus_source_keys,
            position_source_keys=position_source_keys,
            teaching_roles=teaching_roles,
        )

        if (
            not include_target_owner
            or not target_source_keys
            or (owner_kind or target_kind) == target_kind
        ):
            return result
        exact = self.read_dependency(
            actor,
            kind="fact_quality",
            target_kind=target_kind,
            target_source_keys=target_source_keys,
            field_names=field_names,
            as_of=as_of,
            owner_kind=target_kind,
            owner_source_keys=target_source_keys,
            academic_year=academic_year,
            campus_source_keys=campus_source_keys,
            position_source_keys=position_source_keys,
            teaching_roles=teaching_roles,
        )
        if exact["school_fact_version"] != result["school_fact_version"]:
            raise EducationReadStale("关联记录质量在读取期间已变化。")
        rows = list(result["records"])
        seen = {row["source_key"] for row in rows}
        for row in exact["records"]:
            content = row["content"]
            key = content.get("target_source_key") or content.get("education_target_source_key")
            if key not in target_source_keys:
                raise PermissionError("单项质量超出当前获准目标。")
            if row["source_key"] not in seen:
                rows.append({**row, "applies_to_source_key": key})
                seen.add(row["source_key"])
        boundaries = [timestamp(part.get("next_valid_boundary")) for part in (result, exact)]
        proof = EducationQualityReadProof(
            (result["proof"], exact["proof"]), result["school_fact_version"]
        )
        proof.revalidate()
        return {
            **result,
            "records": rows,
            "statements": rows,
            "proof": proof,
            "next_valid_boundary": min((v for v in boundaries if v is not None), default=None),
        }

    def read_dependency(
        self,
        actor: StaffActorContext,
        *,
        kind: str,
        target_kind: str,
        target_source_keys: tuple[str, ...],
        field_names: tuple[str, ...],
        owner_kind: str,
        owner_source_keys: tuple[str, ...],
        as_of: datetime | None = None,
        academic_year: int | None = None,
        source_keys: tuple[str, ...] | None = None,
        policy_source_keys: tuple[str, ...] = (),
        campus_source_keys: tuple[str, ...] = (),
        position_source_keys: tuple[str, ...] = (),
        teaching_roles: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        if source_keys is None:
            _, scope = self.require_dependency_scope(actor, kind, "read")
            if scope.is_school_wide:
                return self._read_school_dependency(
                    actor,
                    kind=kind,
                    target_kind=target_kind,
                    target_source_keys=target_source_keys,
                    field_names=field_names,
                    owner_kind=owner_kind,
                    owner_source_keys=owner_source_keys,
                    as_of=as_of,
                    academic_year=academic_year,
                    policy_source_keys=policy_source_keys,
                    campus_source_keys=campus_source_keys,
                    position_source_keys=position_source_keys,
                    teaching_roles=teaching_roles,
                )
        if kind not in {"fact_quality", "policy_scope"} or not owner_source_keys or not field_names:
            raise ValueError("请明确教育归属对象与所问字段。")
        if (
            not set(teaching_roles) <= {"lead", "co_teacher"}
            or len(teaching_roles) != len(set(teaching_roles))
            or (teaching_roles and target_kind != "teaching_assignment")
        ):
            raise ValueError("任教质量只能使用明确的正式任教角色。")
        for keys in (
            target_source_keys,
            owner_source_keys,
            field_names,
            policy_source_keys,
            campus_source_keys,
            position_source_keys,
        ):
            if len(keys) > 100 or len(set(keys)) != len(keys) or any(not k for k in keys):
                raise ValueError("仅支持有限明确集合。")
        at = aware(self.clock() if as_of is None else as_of)
        fp: list[Any] = []
        ep: list[Any] = []
        chosen: list[tuple[str, ...]] = []

        def before() -> None:
            p, scope = self.require_dependency_scope(actor, kind, "read")
            selected = scope.object_keys if source_keys is None else source_keys
            if len(selected) > 100 or len(set(selected)) != len(selected):
                raise ValueError("仅支持有限明确依据集合。")
            if any(not scope.allows_key(key) for key in selected):
                raise PermissionError("超出原F2读取范围。")
            fp.append(p)
            chosen.append(tuple(selected))
            ep.append(self._education(actor, owner_kind, owner_source_keys, ()))
            ep.append(self._education(actor, target_kind, target_source_keys, field_names))
            if position_source_keys:
                ep.append(
                    self._education(
                        actor, "class_position_definition", position_source_keys, ("display_name",)
                    )
                )
            if campus_source_keys:
                campus, cs = self.require_dependency_scope(actor, "location", "read")
                if any(not cs.allows_key(key) for key in campus_source_keys):
                    raise PermissionError("校区须有独立读取权限。")
                fp.append(campus)
            if kind == "policy_scope":
                policy, ps = self.require_dependency_scope(actor, "policy", "read")
                if not policy_source_keys or any(
                    not ps.allows_key(key) for key in policy_source_keys
                ):
                    raise PermissionError("制度须有独立读取权限。")
                fp.append(policy)

        def after(raw: dict[str, Any]) -> None:
            EducationDependencyProof(
                self, actor, tuple(fp), tuple(ep), raw["school_fact_version"]
            ).revalidate()

        raw = self.projection.read(
            school_id=actor.school_id,
            kind=kind,
            source_keys=lambda: chosen[0],
            owner_kind=owner_kind,
            owner_source_keys=owner_source_keys,
            target_kind=target_kind,
            target_source_keys=target_source_keys,
            field_names=field_names,
            as_of=at,
            academic_year=academic_year,
            policy_source_keys=policy_source_keys,
            campus_source_keys=campus_source_keys,
            position_source_keys=position_source_keys,
            teaching_roles=teaching_roles,
            before_read=before,
            after_read=after,
        )
        rows: list[dict[str, Any]] = raw["records"]
        for row in rows:
            c = row["content"]
            if target_kind in F5_FACT_KINDS and kind == "fact_quality":
                verified = timestamp(c.get("verified_at"))
                if verified is None or verified > at:
                    raise PermissionError("教学质量尚未在所问时点核实。")
                if c.get("teaching_role") is not None and c["teaching_role"] not in teaching_roles:
                    raise PermissionError("质量声明不适用于所问任教角色。")
                if c.get("negative_code") == "no_teaching_assignment" and (
                    owner_kind != "teaching_offering"
                    or target_kind != "teaching_assignment"
                    or c.get("teaching_role") not in teaching_roles
                ):
                    raise PermissionError("明确未任教声明必须限定开课和角色。")
            if (
                row["source_key"] not in chosen[0]
                or c.get("education_target_kind") != owner_kind
                or c.get("education_target_source_key") not in owner_source_keys
                or c.get("academic_year") is not None
                and c["academic_year"] != academic_year
            ):
                raise PermissionError("超出获准归属对象或学年。")
            if kind == "fact_quality" and (
                c.get("target_fact_kind") != target_kind
                or c.get("target_source_key") is not None
                and c["target_source_key"] not in target_source_keys
                or c.get("field_name") is not None
                and c["field_name"] not in field_names
            ):
                raise PermissionError("超出获准目标或字段。")
            if (
                c.get("position_source_key") is not None
                and c["position_source_key"] not in position_source_keys
            ):
                raise PermissionError("岗位范围不符。")
            if (
                c.get("campus_source_key") is not None
                and c["campus_source_key"] not in campus_source_keys
            ):
                raise PermissionError("校区范围不符。")
            if (
                c.get("replacement_source_key") is not None
                and c["replacement_source_key"] not in target_source_keys
            ):
                raise PermissionError("质量替换目标没有取得读取权限。")
            if kind == "policy_scope" and c.get("policy_source_key") not in policy_source_keys:
                raise PermissionError("超出获准制度。")
        boundary = timestamp(raw.get("next_valid_boundary")) if as_of is None else None
        proof = EducationDependencyProof(
            self, actor, tuple(fp), tuple(ep), raw["school_fact_version"], boundary
        )
        proof.revalidate()
        if len(self.known_dependencies) > 1000:
            self.known_dependencies.clear()
        self.known_dependencies.update(
            (str(actor.school_id), kind, row["source_key"]) for row in rows
        )
        return {
            "statements": rows,
            "records": rows,
            "coverage": "unknown",
            "school_fact_version": raw["school_fact_version"],
            "proof": proof,
            "next_valid_boundary": raw.get("next_valid_boundary"),
        }

    def _read_school_dependency(self, actor: StaffActorContext, **selection: Any):
        current = selection["as_of"] is None
        selection["as_of"] = aware(selection["as_of"] or self.clock())
        # The empty fixed projection establishes every target/owner/field grant
        # before any school source metadata is requested.
        initial = self.read_dependency(actor, source_keys=(), **selection)
        version, proofs = initial["school_fact_version"], [initial["proof"]]
        rows, boundaries, after = [], [], None
        while True:
            initial["proof"].revalidate()
            page = self.projection.scope_keys(
                school_id=actor.school_id, kind=selection["kind"], after_key=after, limit=100
            )
            if (
                type(page) is not dict
                or type(page.get("keys")) is not list
                or len(page["keys"]) > 100
                or type(page.get("has_more")) is not bool
                or page.get("school_fact_version") != version
            ):
                raise EducationReadStale("教育依据枚举期间已变化。")
            keys = page["keys"]
            _, scope = self.require_dependency_scope(actor, selection["kind"], "read")
            if (
                any(not scope.allows_key(key) for key in keys)
                or keys != sorted(set(keys))
                or keys
                and after is not None
                and keys[0] <= after
            ):
                raise ValueError("Invalid education dependency source ordering")
            more = page["has_more"]
            next_after = keys[-1] if more and keys else None
            if page.get("next_after") != next_after or more and not keys:
                raise ValueError("Invalid education dependency continuation")
            if keys:
                batch = self.read_dependency(actor, source_keys=tuple(keys), **selection)
                if batch["school_fact_version"] != version:
                    raise EducationReadStale("教育依据读取期间已变化。")
                rows.extend(batch["records"])
                proofs.append(batch["proof"])
                boundary = timestamp(batch.get("next_valid_boundary"))
                if boundary is not None:
                    boundaries.append(boundary)
            if not more:
                break
            after = next_after
        if len({row["source_key"] for row in rows}) != len(rows):
            raise ValueError("Repeated education dependency source")
        boundary = min(boundaries) if current and boundaries else None
        proofs.append(replace(initial["proof"], next_boundary=boundary))
        proof = EducationQualityReadProof(tuple(proofs), version)
        proof.revalidate()
        return dict(
            statements=rows,
            records=rows,
            coverage="unknown",
            school_fact_version=version,
            proof=proof,
            next_valid_boundary=boundary.isoformat() if boundary else None,
        )


class SqlEducationDependencyProjection:
    """Fixed SQL function; permits are checked before and after its short snapshot."""

    def __init__(self, education_projection: Any, *, f5_enabled: bool = False) -> None:
        self.base = education_projection
        self.f5_enabled = f5_enabled

    def current_version(self, school_id: Any) -> int:
        return self.base.current_version(school_id)

    def scope_keys(self, *, school_id: Any, kind: str, after_key: str | None, limit: int):
        from sqlalchemy import func, select

        if kind not in {"fact_quality", "policy_scope", "policy"}:
            raise ValueError("Invalid education dependency source kind")
        with self.base.current_sessions()() as session:
            return session.scalar(
                select(func.app.school_fact_scope_keys(school_id, "f2", kind, after_key, limit))
            )

    def selection(
        self,
        *,
        school_id: Any,
        kind: str,
        source_key: str,
        as_of: datetime,
        before_read: Callable[[], None],
        after_read: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        from sqlalchemy import DateTime, func, literal, select

        from app.platform.authorization.secure_reader_compiler import CompiledReadRelation

        raw: dict[str, Any] | None = None

        def after() -> None:
            if raw is None:
                raise ValueError("Missing exact dependency selection")
            after_read(raw)

        with self.base.executor.validated_read(before_read, after) as transaction:
            function = (
                func.app.school_fact_f5_dependency_selection
                if self.f5_enabled
                else func.app.school_fact_f4_dependency_selection
            )
            payload = function(school_id, kind, source_key, literal(as_of, DateTime(timezone=True)))
            rows = transaction.fetch(
                CompiledReadRelation(
                    "school_facts.f5.dependency_selection"
                    if self.f5_enabled
                    else "school_facts.f4.dependency_selection",
                    as_of,
                    1,
                    ("payload",),
                    select(1),
                    select(payload.label("payload")),
                )
            )
            if len(rows) != 1 or type(rows[0].get("payload")) is not dict:
                raise ValueError("Invalid exact dependency selection")
            raw = rows[0]["payload"]
        if raw is None:
            raise ValueError("Missing exact dependency selection")
        return raw

    def coordinates(
        self,
        *,
        school_id: Any,
        kind: str,
        source_keys: tuple[str, ...],
        before_read: Callable[[], None],
        after_read: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        from sqlalchemy import Text, func, select
        from sqlalchemy.dialects.postgresql import array

        from app.platform.authorization.secure_reader_compiler import CompiledReadRelation

        raw: dict[str, Any] | None = None

        def after() -> None:
            if raw is None:
                raise ValueError("Missing dependency coordinates")
            after_read(raw)

        with self.base.executor.validated_read(before_read, after) as transaction:
            at = datetime.now(UTC)
            function = (
                func.app.school_fact_f5_dependency_coordinates
                if self.f5_enabled
                else func.app.school_fact_f4_dependency_coordinates
            )
            payload = function(school_id, kind, array(list(source_keys), type_=Text))
            rows = transaction.fetch(
                CompiledReadRelation(
                    "school_facts.f5.dependency_coordinates"
                    if self.f5_enabled
                    else "school_facts.f4.dependency_coordinates",
                    at,
                    1,
                    ("payload",),
                    select(1),
                    select(payload.label("payload")),
                )
            )
            if len(rows) != 1 or type(rows[0].get("payload")) is not dict:
                raise ValueError("Invalid dependency coordinates")
            raw = rows[0]["payload"]
        if raw is None:
            raise ValueError("Missing dependency coordinates")
        return raw

    def read(
        self,
        *,
        before_read: Callable[[], None],
        after_read: Callable[[dict[str, Any]], None],
        **selection: Any,
    ) -> dict[str, Any]:
        from sqlalchemy import BigInteger, DateTime, Text, func, literal, select
        from sqlalchemy.dialects.postgresql import array

        from app.platform.authorization.secure_reader_compiler import CompiledReadRelation

        raw: dict[str, Any] | None = None

        def after() -> None:
            if raw is None:
                raise ValueError("Missing dependency projection")
            after_read(raw)

        with self.base.executor.validated_read(before_read, after) as transaction:

            def arrays(key: str):
                return array(list(selection[key]), type_=Text)

            is_f5 = (
                selection["owner_kind"] in F5_FACT_KINDS
                or selection["target_kind"] in F5_FACT_KINDS
            )
            if is_f5 and not self.f5_enabled:
                raise PermissionError("F5 dependency configuration is unavailable")
            function = (
                func.app.school_fact_f5_dependencies
                if is_f5
                else func.app.school_fact_f4_dependencies
            )
            extra = (arrays("teaching_roles"),) if is_f5 else ()
            payload = function(
                selection["school_id"],
                selection["kind"],
                array(list(selection["source_keys"]()), type_=Text),
                selection["owner_kind"],
                arrays("owner_source_keys"),
                selection["target_kind"],
                arrays("target_source_keys"),
                arrays("field_names"),
                literal(selection["as_of"], DateTime(timezone=True)),
                literal(selection["academic_year"], BigInteger),
                arrays("policy_source_keys"),
                arrays("campus_source_keys"),
                arrays("position_source_keys"),
                *extra,
            )
            rows = transaction.fetch(
                CompiledReadRelation(
                    "school_facts.f5.dependencies" if is_f5 else "school_facts.f4.dependencies",
                    selection["as_of"],
                    1,
                    ("payload",),
                    select(1),
                    select(payload.label("payload")),
                )
            )
            if len(rows) != 1 or type(rows[0].get("payload")) is not dict:
                raise ValueError("Invalid dependency projection")
            raw = rows[0]["payload"]
        if raw is None:
            raise ValueError("Missing dependency projection")
        return raw


class EducationDependencyReferenceAdapter:
    """Translate public references only after each actual owner permits the object."""

    def __init__(
        self,
        *,
        bridge: EducationQualityBridge,
        education_repository: Any,
        directory_repository: Any,
    ) -> None:
        self.bridge, self.education_repository, self.directory_repository = (
            bridge,
            education_repository,
            directory_repository,
        )

    def reference_for(
        self, actor: StaffActorContext, kind: str, key: str, *, creation: bool = False
    ) -> str:
        if kind in EDUCATION_DEPENDENCY_KINDS:
            return self.education_repository.reference_for(actor, kind, key, creation=creation)
        p, scope = self.bridge.require_dependency_scope(
            actor, kind, "maintain" if creation else "read"
        )
        if not scope.allows_key(key, is_creation=creation):
            raise PermissionError("引用超出其真实F2授权范围。")
        self.bridge.f2_guard.revalidate(p, actor)
        return self.directory_repository.reference_for(
            actor=actor, fact_kind=kind, source_key=key, creation=creation
        )

    def source_for(
        self, actor: StaffActorContext, kind: str, reference: str, *, creation: bool = False
    ) -> str:
        if kind in EDUCATION_DEPENDENCY_KINDS:
            return self.education_repository.source_for_reference(
                actor, kind, reference, creation=creation
            )
        p, scope = self.bridge.require_dependency_scope(
            actor, kind, "maintain" if creation else "read"
        )
        if scope.is_school_wide:
            source = self.directory_repository.source_for(
                actor=actor, fact_kind=kind, reference=reference, creation=creation
            )
            if not scope.allows_key(source, is_creation=creation):
                raise PermissionError("引用超出其真实F2授权范围。")
            self.bridge.f2_guard.revalidate(p, actor)
            return source
        from hmac import compare_digest

        source = None
        for key in scope.creation_keys if creation else scope.object_keys:
            candidate = self.directory_repository.reference_for(
                actor=actor, fact_kind=kind, source_key=key, creation=creation
            )
            if compare_digest(candidate, reference):
                source = key
                break
        if source is None:
            raise PermissionError("引用超出其真实F2授权范围。")
        self.bridge.f2_guard.revalidate(p, actor)
        return source

    @staticmethod
    def reference_fields(content: dict[str, Any]) -> dict[str, tuple[str, str]]:
        owner = content.get("education_target_kind")
        target = content.get("target_fact_kind")
        if owner not in EDUCATION_DEPENDENCY_KINDS:
            raise ValueError("必须明确教育归属类型。")
        fields = {
            "education_target_ref": ("education_target_source_key", owner),
            "policy_ref": ("policy_source_key", "policy"),
            "campus_ref": ("campus_source_key", "location"),
            "position_ref": ("position_source_key", "class_position_definition"),
        }
        if target is not None:
            if target not in EDUCATION_DEPENDENCY_KINDS:
                raise ValueError("必须明确教育质量目标类型。")
            fields.update(
                target_ref=("target_source_key", target),
                replacement_ref=("replacement_source_key", target),
            )
        return fields

    def resolve_fields(
        self,
        actor: StaffActorContext,
        fields: dict[str, Any],
        *,
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if any(
            key.endswith("_source_key")
            or key in {"actor", "school_id", "scope_id", "expected_version"}
            for key in fields
        ):
            raise ValueError("维护输入只能使用已返回对象引用。")
        context = {**(existing or {}), **fields}
        result = dict(fields)
        for public, (canonical, kind) in self.reference_fields(context).items():
            if public not in result:
                continue
            value = result.pop(public)
            result[canonical] = None if value is None else self.source_for(actor, kind, value)
        return result

    def public_fields(self, actor: StaffActorContext, content: dict[str, Any]) -> dict[str, Any]:
        result = dict(content)
        for public, (canonical, kind) in self.reference_fields(content).items():
            if canonical in result:
                value = result.pop(canonical)
                result[public] = None if value is None else self.reference_for(actor, kind, value)
        if any(key.endswith("_source_key") for key in result):
            raise PermissionError("未知教育关联不能作为原始坐标发布。")
        return result
