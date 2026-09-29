from __future__ import annotations

from typing import Literal, cast
from uuid import UUID

from .catalog import (
    AssistantModuleCatalogEntry,
    StaticAssistantCatalog,
    UnknownAssistantModuleError,
)
from .contracts import (
    ExpressionModelRequestV1,
    ExpressionModelResultV1,
    MeaningModelCorrelationV1,
    MeaningModelRequestV1,
    MeaningV1,
    ModuleCallV1,
    ModuleResultV1,
    NonNullJsonValue,
    TextSpanEvidenceV1,
)
from .controls import (
    AssistantControlError,
    AssistantControlService,
    ControlIssueContext,
)
from .ports import ExpressionModelPort, MeaningModelPort
from .presentation import (
    PreparedPresentation,
    contains_forbidden_local_token,
    fill_template_once,
    prepare_composed_template,
    prepare_fallback,
    prepare_precomposed,
)
from .runtime import (
    AcceptedRun,
    AssistantRuntime,
    AssistantRuntimeError,
    ConversationSnapshot,
    RunSettlement,
)

_MEANING_INSTRUCTION = "只返回一个符合 MeaningV1 的候选含义，不执行任何业务动作。"


class AssistantOrchestrationError(AssistantRuntimeError):
    """首脑或模块未返回 K3-B 可机械结算的信封。"""


class AssistantTurnOrchestrator:
    """一次首脑调用、至多一个模块投递的最小机械纵切。"""

    __slots__ = ("_catalog", "_control_service", "_expression_model", "_meaning_model")

    def __init__(
        self,
        *,
        meaning_model: MeaningModelPort,
        catalog: StaticAssistantCatalog,
        expression_model: ExpressionModelPort | None = None,
        control_service: AssistantControlService | None = None,
    ) -> None:
        self._meaning_model = meaning_model
        self._expression_model = expression_model
        self._catalog = catalog
        self._control_service = control_service

    def run_accepted(
        self,
        *,
        runtime: AssistantRuntime,
        accepted: AcceptedRun,
        actor_context_ref: str,
        request_id: UUID,
    ) -> ConversationSnapshot:
        request = MeaningModelRequestV1(
            version="1",
            current_teacher_utterance=accepted.teacher_utterance,
            model_safe_context=(
                _MEANING_INSTRUCTION,
                *self._catalog.model_safe_context(),
            ),
        )
        correlation = MeaningModelCorrelationV1(
            conversation_ref=accepted.conversation_ref,
            turn_ref=accepted.turn_ref,
            run_ref=accepted.run_ref,
        )
        try:
            meaning = self._meaning_model.interpret(request, correlation=correlation)
        except Exception:
            raise AssistantOrchestrationError("meaning model failed") from None
        self._require_current_meaning(
            meaning,
            correlation,
            accepted.teacher_utterance,
        )
        runtime.require_current_run(
            accepted.conversation_ref,
            actor_context_ref,
            accepted.run_ref,
            accepted.generation,
        )
        if meaning.mode != "dispatch":
            return self._settle_expression_only(
                runtime=runtime,
                accepted=accepted,
                actor_context_ref=actor_context_ref,
                request_id=request_id,
                meaning=meaning,
            )
        assert meaning.module_ref is not None
        try:
            entry = self._catalog.resolve(meaning.module_ref)
        except UnknownAssistantModuleError:
            raise AssistantOrchestrationError("meaning selected an unavailable module") from None
        module_call = ModuleCallV1(
            version="1",
            module_ref=meaning.module_ref,
            conversation_ref=accepted.conversation_ref,
            turn_ref=accepted.turn_ref,
            run_ref=accepted.run_ref,
            actor_context_ref=actor_context_ref,
            current_teacher_utterance=accepted.teacher_utterance,
            meaning_payload=meaning.meaning_payload,
            evidence_refs=meaning.evidence_refs,
            candidate_steps=meaning.candidate_steps,
            module_state_ref=None,
            state_revision=None,
            precomposition_candidate=meaning.precomposition_candidate,
        )
        try:
            result = entry.module.handle(module_call)
        except Exception:
            raise AssistantOrchestrationError("assistant module failed") from None
        presentation = self._module_presentation(
            entry,
            result,
            runtime=runtime,
            accepted=accepted,
            actor_context_ref=actor_context_ref,
        )
        if presentation.model_safe_template is not None and contains_forbidden_local_token(
            presentation.model_safe_template,
            json_values=(
                ()
                if result.fallback_presentation is None
                else (result.fallback_presentation.display_payload,)
            ),
            opaque_values=self._opaque_local_values(
                result,
                accepted=accepted,
                actor_context_ref=actor_context_ref,
            ),
        ):
            if result.fallback_presentation is None:
                raise AssistantOrchestrationError("unsafe template has no fallback")
            presentation = prepare_fallback(result.fallback_presentation)
        controls = ()
        if any(
            control is not None
            for control in (
                result.action_offer,
                result.recovery_control,
                result.result_check_handle,
            )
        ):
            if self._control_service is None:
                raise AssistantOrchestrationError("control service is unavailable")
            try:
                controls = self._control_service.issue_from_result(
                    context=ControlIssueContext(
                        actor_context_ref=actor_context_ref,
                        conversation_ref=accepted.conversation_ref,
                        turn_ref=accepted.turn_ref,
                        run_ref=accepted.run_ref,
                        generation=accepted.generation,
                        module_ref=entry.module_ref,
                        module_state_ref=result.staged_state_ref,
                        state_revision=result.state_revision,
                        issuance_request_id=request_id,
                    ),
                    result=result,
                )
            except AssistantControlError:
                raise AssistantOrchestrationError("control issuance failed") from None
        return runtime.settle_run(
            accepted.conversation_ref,
            actor_context_ref,
            accepted.run_ref,
            accepted.generation,
            request_id,
            RunSettlement(
                display_payload=presentation.display_payload,
                display_kind=presentation.display_kind,
                model_safe_note=self._safe_model_note(
                    result,
                    accepted=accepted,
                    actor_context_ref=actor_context_ref,
                ),
                model_safe_template=presentation.model_safe_template,
                module_ref=entry.module_ref,
                module_state_ref=result.staged_state_ref,
                state_revision=result.state_revision,
                controls=controls,
            ),
        )

    def _settle_expression_only(
        self,
        *,
        runtime: AssistantRuntime,
        accepted: AcceptedRun,
        actor_context_ref: str,
        request_id: UUID,
        meaning: MeaningV1,
    ) -> ConversationSnapshot:
        if self._expression_model is None or meaning.model_safe_expression_input is None:
            raise AssistantOrchestrationError("expression model is unavailable")
        if contains_forbidden_local_token(
            meaning.model_safe_expression_input,
            opaque_values=self._run_opaque_values(
                accepted=accepted,
                actor_context_ref=actor_context_ref,
            ),
        ):
            raise AssistantOrchestrationError("expression input is invalid")
        runtime.require_current_run(
            accepted.conversation_ref,
            actor_context_ref,
            accepted.run_ref,
            accepted.generation,
        )
        request = ExpressionModelRequestV1(
            version="1",
            model_safe_expression_input=meaning.model_safe_expression_input,
            neutral_slot_names=(),
        )
        try:
            expression = cast(object, self._expression_model.compose(request))
        except Exception:
            raise AssistantOrchestrationError("expression model failed") from None
        if not isinstance(expression, ExpressionModelResultV1):
            raise AssistantOrchestrationError("expression result is invalid")
        rendered = fill_template_once(
            expression.rendered_template,
            {},
            placeholder_kind="slot",
        )
        if rendered is None:
            raise AssistantOrchestrationError("expression result is invalid")
        if contains_forbidden_local_token(
            expression.rendered_template,
            opaque_values=self._run_opaque_values(
                accepted=accepted,
                actor_context_ref=actor_context_ref,
            ),
        ):
            raise AssistantOrchestrationError("expression result is invalid")
        return runtime.settle_run(
            accepted.conversation_ref,
            actor_context_ref,
            accepted.run_ref,
            accepted.generation,
            request_id,
            RunSettlement(
                display_kind="text",
                display_payload=rendered,
                model_safe_template=expression.rendered_template,
            ),
        )

    def _module_presentation(
        self,
        entry: AssistantModuleCatalogEntry,
        result: object,
        *,
        runtime: AssistantRuntime,
        accepted: AcceptedRun,
        actor_context_ref: str,
    ) -> PreparedPresentation:
        if not isinstance(result, ModuleResultV1):
            raise AssistantOrchestrationError("module result envelope is invalid")
        if result.presentation_mode not in entry.presentation_capabilities:
            if result.fallback_presentation is None:
                raise AssistantOrchestrationError("presentation capability is not declared")
            return prepare_fallback(result.fallback_presentation)
        if result.presentation_mode == "direct":
            display_kind, display_payload = self._direct_presentation(result)
            return PreparedPresentation(
                display_kind=display_kind,
                display_payload=display_payload,
            )
        if result.fallback_presentation is None:
            raise AssistantOrchestrationError("generated presentation has no fallback")
        if result.presentation_mode == "precomposed":
            if result.display_template is None or result.field_map is None:
                raise AssistantOrchestrationError("precomposed envelope is invalid")
            return prepare_precomposed(
                display_kind=result.display_kind,
                display_template=result.display_template,
                field_map=result.field_map,
                declared_field_names=entry.model_visible_field_names,
                fallback=result.fallback_presentation,
            )
        if result.display_kind != "text":
            return prepare_fallback(result.fallback_presentation)
        if result.model_safe_narration is None or result.slot_map is None:
            raise AssistantOrchestrationError("compose envelope is invalid")
        if self._expression_model is None:
            return prepare_fallback(result.fallback_presentation)
        if contains_forbidden_local_token(
            result.model_safe_narration,
            json_values=(
                *result.slot_map.values(),
                result.fallback_presentation.display_payload,
            ),
            opaque_values=self._opaque_local_values(
                result,
                accepted=accepted,
                actor_context_ref=actor_context_ref,
            ),
        ):
            return prepare_fallback(result.fallback_presentation)
        runtime.require_current_run(
            accepted.conversation_ref,
            actor_context_ref,
            accepted.run_ref,
            accepted.generation,
        )
        request = ExpressionModelRequestV1(
            version="1",
            model_safe_expression_input=result.model_safe_narration,
            neutral_slot_names=tuple(sorted(result.slot_map)),
        )
        try:
            expression = cast(object, self._expression_model.compose(request))
        except Exception:
            return prepare_fallback(result.fallback_presentation)
        if not isinstance(expression, ExpressionModelResultV1):
            return prepare_fallback(result.fallback_presentation)
        return prepare_composed_template(
            rendered_template=expression.rendered_template,
            slot_map=result.slot_map,
            fallback=result.fallback_presentation,
        )

    @classmethod
    def _safe_model_note(
        cls,
        result: ModuleResultV1,
        *,
        accepted: AcceptedRun,
        actor_context_ref: str,
    ) -> str | None:
        note = result.model_safe_note
        if note is None:
            return None
        local_values: list[object] = []
        if result.display_payload is not None:
            local_values.append(result.display_payload)
        if result.field_map is not None:
            local_values.extend(result.field_map.values())
        if result.slot_map is not None:
            local_values.extend(result.slot_map.values())
        if result.fallback_presentation is not None:
            local_values.append(result.fallback_presentation.display_payload)
        if contains_forbidden_local_token(
            note,
            json_values=tuple(local_values),
            opaque_values=cls._opaque_local_values(
                result,
                accepted=accepted,
                actor_context_ref=actor_context_ref,
            ),
        ):
            return None
        return note

    @classmethod
    def _opaque_local_values(
        cls,
        result: ModuleResultV1,
        *,
        accepted: AcceptedRun,
        actor_context_ref: str,
    ) -> tuple[str | None, ...]:
        return (
            *cls._run_opaque_values(
                accepted=accepted,
                actor_context_ref=actor_context_ref,
            ),
            result.staged_state_ref,
            None if result.action_offer is None else result.action_offer.action_offer_ref,
            (
                None
                if result.recovery_control is None
                else result.recovery_control.recovery_control_ref
            ),
            (
                None
                if result.result_check_handle is None
                else result.result_check_handle.result_check_ref
            ),
            result.diagnostic_ref,
        )

    @staticmethod
    def _run_opaque_values(
        *,
        accepted: AcceptedRun,
        actor_context_ref: str,
    ) -> tuple[str, ...]:
        return (
            actor_context_ref,
            accepted.conversation_ref,
            accepted.turn_ref,
            accepted.run_ref,
        )

    @staticmethod
    def _require_current_meaning(
        meaning: object,
        correlation: MeaningModelCorrelationV1,
        teacher_utterance: str,
    ) -> None:
        if not isinstance(meaning, MeaningV1):
            raise AssistantOrchestrationError("meaning envelope is invalid")
        if (
            meaning.conversation_ref != correlation.conversation_ref
            or meaning.turn_ref != correlation.turn_ref
            or meaning.run_ref != correlation.run_ref
        ):
            raise AssistantOrchestrationError("meaning envelope is not current")
        if any(
            isinstance(evidence, TextSpanEvidenceV1) and evidence.end > len(teacher_utterance)
            for evidence in meaning.evidence_refs
        ):
            raise AssistantOrchestrationError("meaning evidence is outside the current utterance")

    @staticmethod
    def _direct_presentation(
        result: object,
    ) -> tuple[Literal["text", "card"], NonNullJsonValue]:
        if (
            not isinstance(result, ModuleResultV1)
            or result.presentation_mode != "direct"
            or result.display_payload is None
        ):
            raise AssistantOrchestrationError("module result is not a direct presentation")
        return result.display_kind, cast(NonNullJsonValue, result.display_payload)


__all__ = ["AssistantOrchestrationError", "AssistantTurnOrchestrator"]
