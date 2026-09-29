"""F6 quality requires both the existing F2 object grant and its F6 target grant."""

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

from app.platform.school_facts.application import SchoolFactChangeRejected
from app.platform.school_facts.f2_policy import F2FactScope
from app.platform.school_facts.f6_policy import F6_FACT_FIELDS, F6_FACT_KINDS


@dataclass(frozen=True)
class ResidenceQualityProof:
    bridge: Any
    actor: Any
    f2_permits: tuple[Any, ...]
    residence_permits: tuple[Any, ...]
    school_fact_version: int
    owner_proofs: tuple[Any, ...] = ()
    allowed_targets: frozenset[tuple[str, str]] = frozenset()

    def revalidate(self) -> Any:
        for permit in self.f2_permits:
            self.bridge.f2_guard.revalidate(permit, self.actor)
        for permit in self.residence_permits:
            self.bridge.authority.revalidate(
                permit, actor=self.actor, checked_at=self.bridge.authority.clock()
            )
        for proof in self.owner_proofs:
            proof.revalidate()
        if self.bridge.projection.current_version(self.actor.school_id) != self.school_fact_version:
            raise PermissionError("寄宿质量依据已变化。")


class ResidenceQualityBridge:
    def __init__(
        self,
        *,
        f2_guard: Any,
        residence_authority: Any,
        projection: Any,
        target_content_resolver: Any = None,
        owner_authorizer: Any = None,
        retained_f2_guard: Any = None,
    ) -> None:
        self.f2_guard, self.authority, self.projection = f2_guard, residence_authority, projection
        self.target_content_resolver = target_content_resolver
        self.owner_authorizer = owner_authorizer
        self.retained_f2_guard = retained_f2_guard

    def _f2(
        self, actor: Any, key: Any, action: Any, creation: Any = False, *, guard: Any = None
    ) -> Any:
        selected_guard = self.f2_guard if guard is None else guard
        permit = selected_guard.require_current(actor, "fact_quality", purpose=action)
        scope = F2FactScope.from_reference(permit.scope_plan.reference)
        if (
            scope.school_id != actor.school_id
            or scope.fact_kind != "fact_quality"
            or not scope.allows_key(key, is_creation=creation)
        ):
            raise PermissionError("该质量依据没有独立精确权限。")
        return permit

    def coordinates(self, actor: Any, key: Any) -> Any:
        return self._coordinates_with_guard(actor, key, self.f2_guard)

    def _coordinates_with_guard(self, actor: Any, key: Any, guard: Any) -> Any:
        permit = self._f2(actor, key, "read", guard=guard)
        value: dict[str, Any]
        if self.target_content_resolver is not None:
            value = self.target_content_resolver(actor, "fact_quality", key)
        else:
            value = self.projection.quality_coordinates(actor.school_id, key)
        guard.revalidate(permit, actor)
        if (
            type(value) is not dict
            or type(value.get("version")) is not int
            or not isinstance(value.get("content"), dict)
        ):
            raise PermissionError("质量依据的正式目标无法核实。")
        return value, permit

    def _classification_coordinates(self, actor: Any, key: Any) -> Any:
        try:
            return self.coordinates(actor, key)
        except (PermissionError, SchoolFactChangeRejected):
            if self.retained_f2_guard is None:
                raise
            return self._coordinates_with_guard(actor, key, self.retained_f2_guard)

    def recognizes_change(self, actor: Any, change: Any) -> Any:
        if change.fact_kind != "fact_quality":
            return False
        if change.expected_version:
            previous, _ = self._classification_coordinates(actor, change.source_key)
            if previous["content"].get("target_fact_kind") in F6_FACT_KINDS:
                return True
        return getattr(change.content, "target_fact_kind", None) in F6_FACT_KINDS

    def _target(self, actor: Any, content: Any, action: Any) -> Any:
        kind = content.get("target_fact_kind")
        if kind not in F6_FACT_KINDS:
            raise PermissionError("质量更新不可跨越正式事实所有者。")
        fields = (
            (content["field_name"],)
            if content.get("field_name")
            else tuple(sorted(F6_FACT_FIELDS[kind]))
        )
        keys = tuple(
            content[k] for k in ("target_source_key", "replacement_source_key") if content.get(k)
        )
        permit = self.authority.require_scope(
            actor, fact_kind=kind, action=action, checked_at=self.authority.clock()
        )
        self.authority.require_fields(permit, fields)
        if not keys:
            owner_kind = content.get("owner_kind")
            owner_key = content.get("owner_source_key") or content.get(f"{owner_kind}_source_key")
            if owner_kind == "school" and not owner_key:
                school_keys = [key for kind, key in permit.scope.quality_owners if kind == "school"]
                owner_key = school_keys[0] if len(school_keys) == 1 else None
            if (
                not permit.scope.is_school_wide
                and (owner_kind, owner_key) not in permit.scope.quality_owners
            ):
                raise PermissionError("该质量所有者不在明确的寄宿质量范围内。")
            if (
                owner_kind not in {"school", "person", "location"}
                or owner_kind != "school"
                and not owner_key
            ):
                raise PermissionError("寄宿质量所有者必须明确。")
            if self.owner_authorizer is None:
                raise PermissionError("范围级寄宿质量须有该正式所有者的独立授权。")
            proof = self.owner_authorizer(
                actor=actor,
                content={**content, "owner_source_key": owner_key},
                action="read",
                fields=fields,
            )
            proof.revalidate()
            return permit, proof
        for key in keys:
            self.authority.require_object(permit, key, is_creation=False)
        return permit, None

    def require_change_set(self, actor: Any, change_set: Any) -> Any:
        f2: list[Any]
        permits: list[Any]
        owners: list[Any]
        f2, permits, owners = [], [], []
        version = self.projection.current_version(actor.school_id)
        for change in change_set.changes:
            if change.fact_kind != "fact_quality":
                raise PermissionError("不是寄宿质量修改。")
            f2.append(self._f2(actor, change.source_key, "maintain", change.expected_version == 0))
            contents: list[Any] = []
            if change.expected_version:
                previous, read = self.coordinates(actor, change.source_key)
                if previous["version"] != change.expected_version:
                    raise PermissionError("原质量依据版本已变化。")
                f2.append(read)
                contents.append(previous["content"])
            if change.content is not None:
                contents.append(change.content.model_dump(exclude_none=True, exclude_unset=True))
            if not contents:
                raise PermissionError("寄宿质量目标未明确。")
            for content in contents:
                permit, owner = self._target(actor, content, "maintain")
                permits.append(permit)
                if owner is not None:
                    owners.append(owner)
        proof = ResidenceQualityProof(
            self, actor, tuple(f2), tuple(permits), version, tuple(owners)
        )
        proof.revalidate()
        return proof

    def require_read(self, actor: Any, source_key: Any) -> Any:
        coordinates, _ = self._classification_coordinates(actor, source_key)
        if coordinates["content"].get("target_fact_kind") not in F6_FACT_KINDS:
            return None
        # The retained grant only classifies old facts. F6 quality still needs
        # its independent current F6 dependency grant before target/body access.
        coordinates, permit = self.coordinates(actor, source_key)
        target, owner = self._target(actor, coordinates["content"], "read")
        proof = ResidenceQualityProof(
            self,
            actor,
            (permit,),
            (target,),
            self.projection.current_version(actor.school_id),
            () if owner is None else (owner,),
            frozenset({("fact_quality", source_key)}),
        )
        proof.revalidate()
        return proof

    def read_content(self, actor: Any, source_key: Any, action: Any = "read") -> Any:
        if action not in {"read", "maintain"}:
            raise PermissionError("未知质量读取用途。")
        version = self.projection.current_version(actor.school_id)
        coordinates, f2 = self.coordinates(actor, source_key)
        target, owner = self._target(actor, coordinates["content"], "read")
        f2s: list[Any]
        targets: list[Any]
        f2s, targets, owners = [f2], [target], [] if owner is None else [owner]
        if action == "maintain":
            f2s.append(self._f2(actor, source_key, "maintain"))
            target, owner = self._target(actor, coordinates["content"], "maintain")
            targets.append(target)
            if owner is not None:
                owners.append(owner)
        raw: dict[str, Any] = self.projection.quality_detail(actor.school_id, source_key)
        if (
            type(raw) is not dict
            or raw.get("version") != coordinates["version"]
            or not isinstance(raw.get("content"), dict)
        ):
            raise PermissionError("质量正文与已核验坐标不符。")
        proof = ResidenceQualityProof(
            self,
            actor,
            tuple(f2s),
            tuple(targets),
            version,
            tuple(owners),
            frozenset({("fact_quality", source_key)}),
        )
        proof.revalidate()
        return {**raw, "proof": proof, "school_fact_version": version}


@dataclass(frozen=True)
class ResidenceQualityOwnerProof:
    owner: Any
    actor: Any
    permit: Any
    family: bool

    def revalidate(self) -> Any:
        if not self.family:
            self.owner.f2_guard.revalidate(self.permit, self.actor)
            return
        authority = self.owner.family_authority
        with self.owner._family_scope(self.actor):
            authority.revalidate_permit(
                self.permit, self.actor, self.permit.target, self.owner.clock()
            )


class ResidenceQualityOwnerAuthorizer:
    """Only verify owner readability. Quality writes come from explicit F6 owners."""

    def __init__(self, *, f2_guard: Any, family_authority: Any, clock: Any) -> None:
        self.f2_guard, self.family_authority, self.clock = f2_guard, family_authority, clock

    def _family_scope(self, actor: Any) -> Any:
        connection = getattr(self.family_authority, "connection_authority", None)
        return connection.read_scope(actor) if connection else nullcontext()

    def __call__(self, *, actor: Any, content: Any, action: Any, fields: Any) -> Any:
        if action != "read":
            raise PermissionError("该口子只验证正式所有者读取。")
        kind, key = content.get("owner_kind"), content.get("owner_source_key")
        if kind == "person":
            if self.family_authority is None:
                raise PermissionError("学生所有者读取未接通。")
            with self._family_scope(actor):
                permit = self.family_authority.require_scope(
                    actor,
                    action="read",
                    fact_kind="person",
                    source_key=key,
                    is_creation=False,
                    checked_at=self.clock(),
                )
            proof = ResidenceQualityOwnerProof(self, actor, permit, True)
        elif kind in {"location", "school"}:
            permit = self.f2_guard.require_current(actor, kind, purpose="read")
            scope = F2FactScope.from_reference(permit.scope_plan.reference)
            if (
                scope.school_id != actor.school_id
                or scope.fact_kind != kind
                or not (
                    scope.allows_key(key)
                    or kind == "school"
                    and key is None
                    and scope.is_school_wide
                )
            ):
                raise PermissionError("该正式所有者没有精确读取权限。")
            proof = ResidenceQualityOwnerProof(self, actor, permit, False)
        else:
            raise PermissionError("不是支持的寄宿质量所有者。")
        proof.revalidate()
        return proof
