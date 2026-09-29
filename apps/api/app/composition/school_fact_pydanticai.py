"""Approved F2 PydanticAI assembly; the caller selects it once at application startup."""

from collections.abc import Callable
from typing import TypedDict

from pydantic_ai.models import Model

from app.composition.school_fact_current_model_budget import (
    ApprovedModelStageBudget,
    SqliteCurrentModelCallPermit,
)
from app.composition.school_fact_live_model_policy import guard_live_wire
from app.composition.school_fact_live_settings import (
    ApprovedLiveProductModel,
    LiveSchoolFactProductConfiguration,
    build_live_model_call_permit,
)
from app.composition.school_fact_product_settings import (
    ProductConfigurationError,
    SchoolFactProductConfiguration,
    load_product_model_api_key,
)
from app.composition.school_fact_pydanticai_model import build_guarded_model
from app.platform.assistant.controls import AssistantControlService
from app.platform.assistant.pydanticai_runner import (
    CandidateToolRunner,
    SynchronousCandidateRunner,
)
from app.platform.assistant.tool_loop import OwnerInstruction, ToolGateway, TurnInstruction


class _StageBudgetOptions(TypedDict, total=False):
    stage_budget: ApprovedModelStageBudget


FINAL_REVIEW_INSTRUCTION = (
    "发布前按原任务与当前可核实证据，逐句核对对象、时点、关系、数量和执行状态。"
    "删除无依据的推断、额外办事建议和未问的正常状态，不遗漏原请求各部分或真实限制。"
    "核对每句话是在回答用户的问题，还是在解释后台怎样登记、分类或核验；"
    "只保留所问事实、所问依据及会影响结论或下一步的限制。把证据分类的说法改成"
    "该事实对用户意味着什么；不可把术语替换成同样绕口的长定义。"
    "用老师和家长自然、简洁的说法重新给出最终答案；不报告复核过程，不用免责声明保留错误断言。"
    "不能为简短漏掉原请求、真实冲突、无权或故障，也不能把未明确的范围说成全部。"
    "本次只复核表述，不能再调用工具；缺少必要资料时如实说明缺失，不猜测。"
)


def _live_transmission_check(
    current: Callable[[], None], model: ApprovedLiveProductModel
) -> Callable[[], None]:
    """Recheck the fixed live window after reservation, adjacent to HTTP release."""

    def check() -> None:
        current()
        model.require_current_window()

    return check


def build_pydanticai_loop(
    *,
    configuration: SchoolFactProductConfiguration | LiveSchoolFactProductConfiguration,
    gateway_factory: Callable[[str], ToolGateway],
    control_service: AssistantControlService,
    instruction: OwnerInstruction,
    turn_instruction: TurnInstruction | None = None,
) -> SynchronousCandidateRunner:
    model = configuration.model
    live = isinstance(configuration, LiveSchoolFactProductConfiguration)
    if (
        (
            not live
            and configuration.purpose
            not in {
                "school-facts-f2-synthetic",
                "school-facts-f3-synthetic",
                "school-facts-f4-synthetic",
                "school-facts-f5-synthetic",
                "school-facts-f6-synthetic",
                "school-facts-f7-synthetic",
                "school-facts-live-integration-synthetic",
            }
        )
        or configuration.directory is None
        or model is None
        or model.model != "deepseek-flash"
        or model.current_approval is None
        or model.current_approval.model != model.model
        or model.thinking != "enabled"
        or model.reasoning_effort != "high"
    ):
        raise ProductConfigurationError(
            "The formal assistant requires the approved current product model policy."
        )
    key = load_product_model_api_key(model)
    stage_options: _StageBudgetOptions = {}
    known_credentials: tuple[str, ...] = ()
    live_model: ApprovedLiveProductModel | None = None
    if isinstance(configuration, LiveSchoolFactProductConfiguration):
        live_model = configuration.model
        assert live_model is not None  # Rejected above; retain the narrowed live model type.
        permit = build_live_model_call_permit(live_model)
        runtime = configuration.runtime
        known_credentials = (
            key.get_secret_value(),
            configuration.control_signing_key.get_secret_value(),
            runtime.postgres_password,
            runtime.redis_password,
            runtime.rls_context_signing_key.get_secret_value(),
            runtime.dingtalk_app_secret or "",
            configuration.reader.database_password.get_secret_value(),
            configuration.reader.cursor_hmac_key.get_secret_value(),
            configuration.executor.caller_auth_key.get_secret_value(),
            configuration.executor.context_transport_hmac_key.get_secret_value(),
        )
    else:
        engineering_model = configuration.model
        if engineering_model is None or engineering_model.current_approval is None:
            raise ProductConfigurationError(
                "The formal assistant requires the approved current product model policy."
            )
        if engineering_model.stage_budget is not None:
            stage_options["stage_budget"] = engineering_model.stage_budget
        permit = SqliteCurrentModelCallPermit(
            path=engineering_model.state_directory / "model-budget.sqlite3",
            ledger_policy=engineering_model.budget,
            current_approval=engineering_model.current_approval,
            **stage_options,
        )

    approval = model.current_approval

    def model_factory(current: Callable[[], None]) -> Model:
        return build_guarded_model(
            api_key=key,
            permit=permit,
            approval=approval,
            current=_live_transmission_check(current, live_model) if live_model else current,
            max_input_tokens=model.max_input_tokens,
            max_output_tokens=model.max_output_tokens,
            live_wire_guard=(
                lambda encoded: guard_live_wire(encoded, known_credentials=known_credentials)
            )
            if live_model
            else None,
        )

    return SynchronousCandidateRunner(
        CandidateToolRunner(
            model_factory=model_factory,
            gateway_factory=gateway_factory,
            control_service=control_service,
            instruction=instruction,
            turn_instruction=turn_instruction,
            final_review_instruction=FINAL_REVIEW_INSTRUCTION,
        )
    )


# Historical injection name; both entries use one implementation.
build_candidate_loop = build_pydanticai_loop
