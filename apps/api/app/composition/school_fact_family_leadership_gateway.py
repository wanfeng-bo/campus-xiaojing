"""Finite F2-owned leadership tool with release and history revalidation."""

import hashlib
import json
from copy import deepcopy
from typing import Literal

from app.composition.school_fact_directory_context import bind_directory_identity
from app.composition.school_fact_read_evidence import validate_fallback_read
from app.platform.assistant.runtime import AssistantRuntimeError, HistoryCheck
from app.platform.assistant.tool_loop import ToolDescriptor, ToolExecutionResult
from app.platform.contracts import FrozenContractModel

NAME = "school_facts.family.leadership"


class FamilyLeadershipInput(FrozenContractModel):
    role_kind: Literal["all", "school_leader", "campus_leader"] = "all"
    include_official_contacts: bool = False


class FamilyLeadershipToolGateway:
    descriptors = (
        ToolDescriptor(
            name=NAME,
            owner="school_facts.f2",
            kind="read",
            description="查询当前有效监护关系所在学校的现任校级或校区领导。由正式职务语义配置定位，与是否教孩子无关。只在需要联系时请求公开组织/办公地点渠道；无号码仍回答姓名。未知覆盖不能推出唯一、全部或不存在；不支持历史未来、人事审批、私号或同学名单。",
            parameters=FamilyLeadershipInput.model_json_schema(),
        ),
    )

    def __init__(self, *, resolve_actor, query):
        self.resolve_actor, self.query = resolve_actor, query

    def for_turn(self, actor_context_ref):
        return _Turn(self, actor_context_ref)

    def school_read_eligibility(
        self, actor_context_ref: str,
    ) -> Literal["available", "denied", "unavailable"]:
        actor = self.resolve_actor(actor_context_ref).actor
        with bind_directory_identity(actor_context_ref):
            return self.query.current_read_eligibility(actor)


class _Turn:
    def __init__(self, owner, actor_context_ref):
        self.owner, self.actor_context_ref = owner, actor_context_ref
        self._checks = []

    @property
    def descriptors(self):
        return self.owner.descriptors

    def revalidate(self, actor_context_ref):
        if actor_context_ref != self.actor_context_ref:
            raise AssistantRuntimeError("Leadership identity changed")
        actor = self.owner.resolve_actor(actor_context_ref).actor
        with bind_directory_identity(actor_context_ref):
            if any(not self._valid(evidence, actor) for evidence in self._checks):
                raise AssistantRuntimeError("Leadership evidence changed")

    def _valid(self, evidence, actor):
        def current():
            # The callback runs before this trusted release scope exits, after
            # duplicate gateway/history checks, and never survives a model wait.
            latest = self.owner.resolve_actor(self.actor_context_ref).actor
            with bind_directory_identity(self.actor_context_ref):
                return latest == actor and evidence.valid(actor) is True

        return validate_fallback_read(
            (NAME, self.actor_context_ref, actor, id(evidence)),
            current,
            time_window=getattr(evidence, "time_window", None),
        )

    def execute(self, call, context):
        self.revalidate(context.actor_context_ref)
        if call.name != NAME:
            raise AssistantRuntimeError("Leadership capability not installed")
        args = FamilyLeadershipInput.model_validate_json(json.dumps(call.arguments))
        actor = self.owner.resolve_actor(context.actor_context_ref).actor
        with bind_directory_identity(context.actor_context_ref):
            result = self.owner.query.query(actor=actor, **args.model_dump())
        public = result.public()
        material = deepcopy(public)
        for leader in material.get("leaders", []):
            source = leader.get("source", {})
            leader["source"] = {
                "label": "学校正式发布的任职资料",
                "published_at": source.get("published_at"),
            }
            for contact in leader.get("official_contacts", []):
                source = contact.get("source", {})
                contact["source"] = {
                    "label": "学校正式发布的组织或办公地点公开渠道",
                    "published_at": source.get("published_at"),
                }
        for statement in material.get("quality", []):
            source = statement.get("source", {})
            statement["source"] = {
                "label": "学校正式发布的质量依据",
                "published_at": source.get("published_at"),
            }
        evidence = result.evidence
        if evidence is None:
            return ToolExecutionResult(model_content=material, fact_card=public)
        self._checks.append(evidence)

        def current():
            # Register the original actor; _valid resolves the live identity and
            # checks complete evidence before this release batch can finish.
            with bind_directory_identity(self.actor_context_ref):
                return self._valid(evidence, actor)

        key = hashlib.sha256(
            (self.actor_context_ref + NAME + json.dumps(call.arguments, sort_keys=True)).encode()
        ).hexdigest()
        return ToolExecutionResult(
            model_content=material,
            fact_card=public,
            history_check=HistoryCheck(
                current, revalidate=current, key=key, revalidation_covers_content=True
            ),
        )
