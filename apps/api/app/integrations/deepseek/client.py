from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final, cast

import httpx
from pydantic import ValidationError

from app.core.settings import Settings
from app.domains.repairs.contracts import (
    RepairCardEditCandidate,
    RepairCardEditSource,
    RepairDraftContinuation,
    RepairDraftSourceText,
    RepairEntryInterpretationCandidate,
)
from app.domains.repairs.ports import (
    RepairCardEditModelInvalidOutputError,
    RepairCardEditModelPort,
    RepairDraftModelPort,
    RepairDraftModelUnavailableError,
)

_SYSTEM_TASK: Final = json.dumps(
    {
        "current_capability": "只支持新建文字报修，不支持查询已有报修或办理其他校园事项。",
        "task": "一次判断本句是否属于新建报修或其续接，并同时提取本句有依据的地点和问题。",
        "decisions": ["repair", "unsupported", "unclear"],
        "rules": [
            "卫生间堵塞等设施故障是报修；卫生检查、请假、已有报修查询是当前未接入事项。",
            (
                "没有活动报修且本句只有明确地点、没有提出其他业务请求时，decision=repair，"
                "只返回本句逐字地点；问题字段和 problem_evidence 必须是空字符串。"
            ),
            "当前报修已有字段只作续接上下文，不得在输出中冒充本句依据。",
            (
                "current_repair.missing=problem 时不得复制已有地点，location_text 和 "
                "location_evidence 必须同时为空字符串；missing=location 时不得复制已有问题，"
                "problem_description 和 problem_evidence 必须同时为空字符串；已有字段由程序合并。"
            ),
            "repair 至少返回一个字段及其本句逐字依据。",
            "unsupported 和 unclear 的四个报修字段必须都是空字符串。",
            "不得增加原话没有的地点、问题、状态、责任或动作。",
            "只返回规定 JSON 对象，不得增加字段。",
        ],
        "output_fields": [
            "decision",
            "location_text",
            "location_evidence",
            "problem_description",
            "problem_evidence",
        ],
    },
    ensure_ascii=False,
    separators=(",", ":"),
)

_CARD_EDIT_SYSTEM_TASK: Final = json.dumps(
    {
        "current_capability": "只判断当前未提交报修卡的一项修改。",
        "task": "判断老师本句是否只修改当前卡片的地点或问题，并给出本句逐字依据。",
        "decisions": ["edit_location", "edit_problem", "unclear"],
        "rules": [
            "每次只修改地点或问题中的一项，不得同时修改两项。",
            "evidence 必须逐字来自老师本句，不得复制当前卡片字段作为依据。",
            "不得猜测老师本句没有的地点或问题。",
            "电话修改或无法明确判断修改哪一项时返回 unclear。",
            "unclear 的 replacement_text 和 evidence 必须都是空字符串。",
            "只返回规定 JSON 对象，不得增加字段。",
        ],
        "output_fields": ["kind", "replacement_text", "evidence"],
    },
    ensure_ascii=False,
    separators=(",", ":"),
)


class _InvalidProviderResponse(ValueError):
    """标记无法安全解释的供应商响应，不携带供应商正文。"""


class _InvalidProviderCandidate(ValueError):
    """标记供应商正文不是可校验的 JSON 候选，不携带正文。"""


class DeepSeekRepairDraftClient(RepairDraftModelPort, RepairCardEditModelPort):
    """用一次有界 OpenAI-compatible 调用解释窄入口并提取字段依据。"""

    def __init__(
        self,
        *,
        settings: Settings,
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._http_transport = http_transport

    def interpret(
        self,
        *,
        raw_text: str,
        continuation: RepairDraftContinuation | None,
    ) -> RepairEntryInterpretationCandidate:
        """发送本句和最小续接摘要；可预期失败统一为无正文异常。"""

        try:
            source = RepairDraftSourceText(raw_text=raw_text)
        except ValidationError:
            raise RepairDraftModelUnavailableError from None

        user_payload = {
            "teacher_text": source.raw_text,
            "current_repair": None if continuation is None else continuation.model_dump(),
        }
        try:
            candidate_payload = self._complete_json(_SYSTEM_TASK, user_payload)
            return RepairEntryInterpretationCandidate.model_validate(candidate_payload)
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            json.JSONDecodeError,
            UnicodeDecodeError,
            ValidationError,
            _InvalidProviderCandidate,
            _InvalidProviderResponse,
        ):
            raise RepairDraftModelUnavailableError from None

    def interpret_card_edit(
        self,
        source: RepairCardEditSource,
    ) -> RepairCardEditCandidate:
        """发送一次最小改卡请求；供应商候选无效与 transport 失败分开。"""

        user_payload = {
            "teacher_text": source.raw_text,
            "current_card": {
                "location_text": source.current_location_text,
                "problem_description": source.current_problem_description,
            },
        }
        try:
            candidate_payload = self._complete_json(_CARD_EDIT_SYSTEM_TASK, user_payload)
        except _InvalidProviderCandidate:
            raise RepairCardEditModelInvalidOutputError from None
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            json.JSONDecodeError,
            UnicodeDecodeError,
            _InvalidProviderResponse,
        ):
            raise RepairDraftModelUnavailableError from None
        try:
            return RepairCardEditCandidate.model_validate(candidate_payload)
        except (_InvalidProviderCandidate, ValidationError, TypeError):
            raise RepairCardEditModelInvalidOutputError from None

    def _complete_json(
        self,
        system_prompt: str,
        task_payload: Mapping[str, object],
    ) -> object:
        """以同一配置发送一次严格 JSON completion，不做重试。"""

        configuration = self._configured_request_values()
        if configuration is None:
            raise RepairDraftModelUnavailableError from None
        endpoint, model, api_key = configuration
        request_body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        task_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "temperature": 0,
            "max_tokens": 4_096,
            "stream": False,
        }
        transport = self._http_transport
        if transport is None:
            transport = httpx.HTTPTransport(retries=0)

        with httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(self._settings.repair_draft_model_timeout_seconds),
            trust_env=False,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        ) as client:
            response = client.post(endpoint, json=request_body)
            response.raise_for_status()
            return _parse_provider_json(response.json())

    def _configured_request_values(self) -> tuple[str, str, str] | None:
        """读取已验证配置；关闭或绕过 Settings 校验时仍失败关闭。"""

        settings = self._settings
        api_key = settings.repair_draft_model_api_key
        base_url = settings.repair_draft_model_base_url
        model = settings.repair_draft_model_model
        if (
            not settings.repair_draft_model_enabled
            or base_url is None
            or not base_url.strip()
            or model is None
            or not model.strip()
            or api_key is None
            or not api_key.get_secret_value().strip()
        ):
            return None
        endpoint = f"{base_url.rstrip('/')}/chat/completions"
        return endpoint, model, api_key.get_secret_value()


def _parse_provider_json(payload: object) -> object:
    """只接受单一、正常停止且正文为裸 JSON 的供应商响应。"""

    if type(payload) is not dict:
        raise _InvalidProviderResponse
    payload_object = cast(dict[object, object], payload)
    choices_value = payload_object.get("choices")
    if type(choices_value) is not list:
        raise _InvalidProviderResponse
    choices = cast(list[object], choices_value)
    if len(choices) != 1:
        raise _InvalidProviderResponse
    choice = choices[0]
    if type(choice) is not dict:
        raise _InvalidProviderResponse
    choice_object = cast(dict[object, object], choice)
    if choice_object.get("finish_reason") != "stop":
        raise _InvalidProviderResponse
    message = choice_object.get("message")
    if type(message) is not dict:
        raise _InvalidProviderResponse
    message_object = cast(dict[object, object], message)
    content = message_object.get("content")
    if type(content) is not str or not content.strip():
        raise _InvalidProviderResponse
    try:
        return json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise _InvalidProviderCandidate from None
