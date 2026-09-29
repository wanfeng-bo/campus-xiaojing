from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import cast

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.core.settings import Settings
from app.domains.repairs.contracts import (
    RepairCardEditCandidate,
    RepairCardEditSource,
    RepairDraftCandidate,
    RepairDraftContinuation,
    RepairDraftSourceText,
    RepairEntryInterpretationCandidate,
)
from app.domains.repairs.ports import (
    RepairCardEditModelInvalidOutputError,
    RepairDraftModelUnavailableError,
)
from app.integrations.deepseek.client import DeepSeekRepairDraftClient

API_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CARD_EDIT_CONTENT = json.dumps(
    {
        "kind": "edit_location",
        "replacement_text": "东边男生厕所",
        "evidence": "东边男生厕所",
    },
    ensure_ascii=False,
    separators=(",", ":"),
)


def build_enabled_settings(**overrides: object) -> Settings:
    return Settings.model_validate(
        {
            "app_env": "test",
            "postgres_host": "127.0.0.1",
            "postgres_port": 5432,
            "postgres_db": "campus_ai_test",
            "postgres_user": "tester",
            "postgres_password": "not-a-real-password",
            "rls_context_signing_key": "test-runtime-context-signing-key-32-bytes",
            "redis_host": "127.0.0.1",
            "redis_port": 6379,
            "redis_password": "not-a-real-redis-password",
            "repair_draft_model_enabled": True,
            "repair_draft_model_service_name": "学校选择的模型",
            "repair_draft_model_base_url": "https://model.example",
            "repair_draft_model_model": "current-json-model",
            "repair_draft_model_api_key": "synthetic-provider-secret",
            "repair_draft_model_timeout_seconds": 2,
            **overrides,
        }
    )


def successful_provider_response(
    content: str = (
        '{"decision":"repair","location_text":"办公楼三楼",'
        '"location_evidence":"办公楼三楼","problem_description":"灯坏了",'
        '"problem_evidence":"灯坏了"}'
    ),
) -> dict[str, object]:
    return {
        "id": "synthetic-completion-id",
        "object": "chat.completion",
        "created": 1_786_752_000,
        "model": "current-json-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50},
    }


def successful_card_edit_provider_response(
    content: str = _DEFAULT_CARD_EDIT_CONTENT,
) -> dict[str, object]:
    return successful_provider_response(content)


def test_repair_and_deepseek_packages_are_registered_and_strict_checked() -> None:
    project = tomllib.loads((API_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    packages = set(project["tool"]["setuptools"]["packages"])
    strict_paths = set(project["tool"]["pyright"]["strict"])
    assert "app.domains.repairs" in packages
    assert "app.integrations.deepseek" in packages
    assert "app/domains/repairs" in strict_paths
    assert "app/integrations/deepseek" in strict_paths


def test_repair_draft_candidate_is_trimmed_strict_frozen_and_extra_forbidden() -> None:
    candidate = RepairDraftCandidate(
        location_text="  女教职工宿舍楼后面菜地  ",
        problem_description="\n水管一直漏水\n",
    )

    assert candidate.location_text == "女教职工宿舍楼后面菜地"
    assert candidate.problem_description == "水管一直漏水"
    with pytest.raises(ValidationError):
        candidate.location_text = "办公楼"  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(ValidationError):
        RepairDraftCandidate.model_validate({"location_text": 305, "problem_description": "灯坏了"})


def test_repair_draft_source_text_is_strict_frozen_and_preserves_exact_original() -> None:
    raw_text = "  女教职工宿舍楼后面菜地\n水管漏水 13800000000  "
    source = RepairDraftSourceText(raw_text=raw_text)

    assert source.raw_text == raw_text
    with pytest.raises(ValidationError):
        source.raw_text = "办公楼灯坏了"  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(ValidationError):
        RepairDraftSourceText.model_validate({"raw_text": raw_text, "status": "已确定"})
    with pytest.raises(ValidationError):
        RepairDraftSourceText.model_validate({"raw_text": 305})


@pytest.mark.parametrize("invalid_raw_text", ["", " \n\t ", "x" * 2_001])
def test_repair_draft_source_text_rejects_blank_or_oversized_text(
    invalid_raw_text: str,
) -> None:
    with pytest.raises(ValidationError):
        RepairDraftSourceText(raw_text=invalid_raw_text)


@pytest.mark.parametrize("extra_field", ["status", "action", "actor", "unexpected"])
def test_repair_draft_candidate_rejects_every_non_contract_field(extra_field: str) -> None:
    with pytest.raises(ValidationError):
        RepairDraftCandidate.model_validate(
            {
                "location_text": "办公楼",
                "problem_description": "灯坏了",
                extra_field: "untrusted-value",
            }
        )


@pytest.mark.parametrize(
    ("location_text", "problem_description"),
    [
        ("办" * 201, "灯坏了"),
        ("办公楼", "坏" * 2_001),
    ],
)
def test_repair_draft_candidate_enforces_trimmed_field_lengths(
    location_text: str,
    problem_description: str,
) -> None:
    with pytest.raises(ValidationError):
        RepairDraftCandidate(
            location_text=location_text,
            problem_description=problem_description,
        )


def test_repair_draft_candidate_allows_empty_unknown_fields() -> None:
    assert RepairDraftCandidate(location_text=" \n", problem_description="\t ").model_dump() == {
        "location_text": "",
        "problem_description": "",
    }


def test_deepseek_request_contains_one_task_exact_raw_text_and_minimal_continuation() -> None:
    raw_text = "  女教职工宿舍楼后面菜地\n水管漏水，现场电话 13800000000  "
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=successful_provider_response())

    client = DeepSeekRepairDraftClient(
        settings=build_enabled_settings(),
        http_transport=httpx.MockTransport(handler),
    )

    candidate = client.interpret(
        raw_text=raw_text,
        continuation=RepairDraftContinuation(
            missing="problem",
            location_text="男生厕所",
            problem_description="",
        ),
    )

    assert candidate == RepairEntryInterpretationCandidate(
        decision="repair",
        location_text="办公楼三楼",
        location_evidence="办公楼三楼",
        problem_description="灯坏了",
        problem_evidence="灯坏了",
    )
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url == "https://model.example/chat/completions"
    assert request.headers["Authorization"] == "Bearer synthetic-provider-secret"
    body = json.loads(request.content)
    assert set(body) == {
        "model",
        "messages",
        "response_format",
        "thinking",
        "temperature",
        "max_tokens",
        "stream",
    }
    assert body["model"] == "current-json-model"
    assert len(body["messages"]) == 2
    user_payload = json.loads(body["messages"][1]["content"])
    assert user_payload == {
        "teacher_text": raw_text,
        "current_repair": {
            "missing": "problem",
            "location_text": "男生厕所",
            "problem_description": "",
        },
    }
    system_task = json.loads(body["messages"][0]["content"])
    assert system_task["decisions"] == ["repair", "unsupported", "unclear"]
    assert system_task["output_fields"] == [
        "decision",
        "location_text",
        "location_evidence",
        "problem_description",
        "problem_evidence",
    ]
    assert any("卫生间堵塞" in rule and "卫生检查" in rule for rule in system_task["rules"])
    assert any(
        "只有明确地点" in rule
        and "decision=repair" in rule
        and "问题" in rule
        and "空字符串" in rule
        for rule in system_task["rules"]
    )
    assert any("本句依据" in rule for rule in system_task["rules"])
    assert any("不得增加原话没有" in rule for rule in system_task["rules"])
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0
    assert body["max_tokens"] == 4096
    assert body["stream"] is False
    encoded_body = json.dumps(body, ensure_ascii=False)
    assert "13800000000" in user_payload["teacher_text"]
    assert "history-canary" not in encoded_body
    assert "server-identity-canary" not in encoded_body
    for forbidden_key in (
        '"name"',
        '"phone"',
        '"mobile"',
        '"person"',
        '"school"',
        '"assignment"',
        '"context"',
        '"permission"',
        '"actor"',
        '"status"',
        '"action"',
    ):
        assert forbidden_key not in encoded_body


@pytest.mark.skipif(
    os.environ.get("RUN_DEEPSEEK_LIVE_CONTRACT") != "1",
    reason="需要显式启用真实 DeepSeek 单地点契约门",
)
def test_live_deepseek_treats_one_explicit_location_as_repair_start() -> None:
    settings = Settings()  # pyright: ignore[reportCallIssue]
    assert settings.repair_draft_model_enabled is True
    client = DeepSeekRepairDraftClient(settings=settings)
    source = RepairDraftSourceText(raw_text="男生宿舍楼")

    candidate = client.interpret(raw_text=source.raw_text, continuation=None)
    assert candidate == RepairEntryInterpretationCandidate(
        decision="repair",
        location_text="男生宿舍楼",
        location_evidence="男生宿舍楼",
        problem_description="",
        problem_evidence="",
    )


def test_card_edit_uses_one_strict_request_with_only_current_card_fields() -> None:
    raw_text = "  地点错了，改成东边男生厕所  "
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=successful_card_edit_provider_response())

    client = DeepSeekRepairDraftClient(
        settings=build_enabled_settings(),
        http_transport=httpx.MockTransport(handler),
    )

    candidate = client.interpret_card_edit(
        RepairCardEditSource(
            raw_text=raw_text,
            current_location_text="女卫生间",
            current_problem_description="堵了",
        )
    )

    assert candidate == RepairCardEditCandidate(
        kind="edit_location",
        replacement_text="东边男生厕所",
        evidence="东边男生厕所",
    )
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert set(body) == {
        "model",
        "messages",
        "response_format",
        "thinking",
        "temperature",
        "max_tokens",
        "stream",
    }
    assert body["thinking"] == {"type": "disabled"}
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0
    assert body["stream"] is False
    assert json.loads(body["messages"][1]["content"]) == {
        "teacher_text": raw_text,
        "current_card": {
            "location_text": "女卫生间",
            "problem_description": "堵了",
        },
    }
    system_task = json.loads(body["messages"][0]["content"])
    assert system_task["output_fields"] == ["kind", "replacement_text", "evidence"]
    assert any("只修改地点或问题中的一项" in rule for rule in system_task["rules"])
    assert any("逐字来自老师本句" in rule for rule in system_task["rules"])
    assert any("电话" in rule and "unclear" in rule for rule in system_task["rules"])
    encoded_task = json.dumps(
        {
            "system_task": system_task,
            "user_payload": json.loads(body["messages"][1]["content"]),
        },
        ensure_ascii=False,
    )
    for forbidden in (
        '"phone"',
        '"name"',
        '"person"',
        '"assignment"',
        '"context_ref"',
        '"role"',
    ):
        assert forbidden not in encoded_task


@pytest.mark.parametrize(
    "content",
    (
        "not-json",
        "[]",
        '{"kind":"edit_location","replacement_text":"东边男生厕所"}',
        ('{"kind":"edit_problem","replacement_text":"漏水","evidence":"漏水","role":"worker"}'),
    ),
)
def test_card_edit_invalid_provider_candidate_is_not_retried(content: str) -> None:
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json=successful_card_edit_provider_response(content))

    client = DeepSeekRepairDraftClient(
        settings=build_enabled_settings(),
        http_transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RepairCardEditModelInvalidOutputError) as error:
        client.interpret_card_edit(
            RepairCardEditSource(
                raw_text="地点改成东边男生厕所",
                current_location_text="女卫生间",
                current_problem_description="堵了",
            )
        )

    assert str(error.value) == ""
    assert error.value.__cause__ is None
    assert request_count == 1


@pytest.mark.parametrize(
    "failure_kind",
    ("timeout", "network", "http", "redirect", "empty-content", "invalid-outer"),
)
def test_card_edit_transport_failures_are_safe_and_never_retried(
    failure_kind: str,
) -> None:
    request_count = 0
    unsafe_marker = "card-edit-provider-marker-13800000000"

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if failure_kind == "timeout":
            raise httpx.ReadTimeout(unsafe_marker)
        if failure_kind == "network":
            raise httpx.ConnectError(unsafe_marker)
        if failure_kind == "http":
            return httpx.Response(503, text=unsafe_marker)
        if failure_kind == "redirect":
            return httpx.Response(307, headers={"Location": "https://redirect.example"})
        if failure_kind == "empty-content":
            return httpx.Response(200, json=successful_card_edit_provider_response(""))
        return httpx.Response(200, json={"choices": []})

    client = DeepSeekRepairDraftClient(
        settings=build_enabled_settings(),
        http_transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RepairDraftModelUnavailableError) as error:
        client.interpret_card_edit(
            RepairCardEditSource(
                raw_text="问题改成漏水",
                current_location_text="女卫生间",
                current_problem_description="堵了",
            )
        )

    assert unsafe_marker not in str(error.value)
    assert error.value.__cause__ is None
    assert request_count == 1


def test_continuation_prompt_does_not_repeat_the_already_confirmed_field() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=successful_provider_response(
                '{"decision":"repair","location_text":"",'
                '"location_evidence":"","problem_description":"堵了",'
                '"problem_evidence":"堵了"}'
            ),
        )

    client = DeepSeekRepairDraftClient(
        settings=build_enabled_settings(),
        http_transport=httpx.MockTransport(handler),
    )

    candidate = client.interpret(
        raw_text="那里堵了",
        continuation=RepairDraftContinuation(
            missing="problem",
            location_text="男生厕所",
            problem_description="",
        ),
    )

    assert candidate.problem_description == "堵了"
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    system_task = json.loads(body["messages"][0]["content"])
    assert any(
        "missing=problem" in rule
        and "location_text 和 location_evidence 必须同时为空字符串" in rule
        and "不得复制" in rule
        for rule in system_task["rules"]
    )


@pytest.mark.parametrize("invalid_raw_text", [None, 305, "", " \n\t ", "x" * 2_001])
def test_raw_text_must_be_a_nonblank_string_of_at_most_two_thousand_characters(
    invalid_raw_text: object,
) -> None:
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json=successful_provider_response())

    client = DeepSeekRepairDraftClient(
        settings=build_enabled_settings(),
        http_transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RepairDraftModelUnavailableError) as error:
        client.interpret(raw_text=cast(str, invalid_raw_text), continuation=None)

    assert str(error.value) == "报修文字整理暂时不可用，请直接填写地点和问题。"
    assert error.value.__cause__ is None
    assert request_count == 0


def test_disabled_model_fails_safely_without_making_a_request() -> None:
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json=successful_provider_response())

    settings = build_enabled_settings(repair_draft_model_enabled=False)
    client = DeepSeekRepairDraftClient(
        settings=settings,
        http_transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RepairDraftModelUnavailableError) as error:
        client.interpret(raw_text="办公楼灯坏了", continuation=None)

    assert str(error.value) == "报修文字整理暂时不可用，请直接填写地点和问题。"
    assert error.value.__cause__ is None
    assert request_count == 0


def test_bypassed_invalid_base_url_fails_safely_without_making_a_request() -> None:
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json=successful_provider_response())

    settings = Settings.model_construct(
        repair_draft_model_enabled=True,
        repair_draft_model_service_name="学校选择的模型",
        repair_draft_model_base_url="https://model.example:not-a-port/v4",
        repair_draft_model_model="current-json-model",
        repair_draft_model_api_key=SecretStr("synthetic-provider-secret"),
        repair_draft_model_timeout_seconds=2,
    )
    client = DeepSeekRepairDraftClient(
        settings=settings,
        http_transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RepairDraftModelUnavailableError) as error:
        client.interpret(raw_text="办公楼灯坏了", continuation=None)

    assert str(error.value) == "报修文字整理暂时不可用，请直接填写地点和问题。"
    assert error.value.__cause__ is None
    assert request_count == 0


def test_two_thousand_character_raw_text_is_sent_without_normalization() -> None:
    raw_text = " \n" + ("原" * 1_997) + "1"
    captured_payloads: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured_payloads.append(json.loads(body["messages"][1]["content"]))
        return httpx.Response(200, json=successful_provider_response())

    client = DeepSeekRepairDraftClient(
        settings=build_enabled_settings(),
        http_transport=httpx.MockTransport(handler),
    )

    client.interpret(raw_text=raw_text, continuation=None)

    assert len(raw_text) == 2_000
    assert captured_payloads == [{"teacher_text": raw_text, "current_repair": None}]


@pytest.mark.parametrize(
    "provider_payload",
    [
        [],
        {},
        {"choices": []},
        {
            "choices": [
                {
                    "message": {
                        "content": '{"location_text":"办公楼","problem_description":"灯坏"}'
                    },
                    "finish_reason": "stop",
                },
                {
                    "message": {
                        "content": '{"location_text":"办公楼","problem_description":"灯坏"}'
                    },
                    "finish_reason": "stop",
                },
            ]
        },
        {"choices": ["invalid-choice"]},
        {
            "choices": [
                {
                    "message": {
                        "content": '{"location_text":"办公楼","problem_description":"灯坏"}'
                    },
                    "finish_reason": "length",
                }
            ]
        },
        {"choices": [{"message": {}, "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
    ],
)
def test_invalid_outer_provider_response_maps_to_one_safe_error(
    provider_payload: object,
) -> None:
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json=provider_payload)

    client = DeepSeekRepairDraftClient(
        settings=build_enabled_settings(),
        http_transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RepairDraftModelUnavailableError) as error:
        client.interpret(raw_text="办公楼灯坏了", continuation=None)

    assert str(error.value) == "报修文字整理暂时不可用，请直接填写地点和问题。"
    assert error.value.__cause__ is None
    assert request_count == 1


@pytest.mark.parametrize(
    "content",
    [
        "not-json",
        "[]",
        "",
        '```json\n{"decision":"unclear"}\n```',
        '{"decision":"repair","location_text":"办公楼"}',
        (
            '{"decision":"repair","location_text":"办公楼",'
            '"location_evidence":"办公楼","problem_description":3,'
            '"problem_evidence":"灯坏"}'
        ),
        (
            '{"decision":"repair","location_text":"办公楼",'
            '"location_evidence":"办公楼","problem_description":"灯坏",'
            '"problem_evidence":"灯坏","status":"已确定"}'
        ),
        (
            '{"decision":"unsupported","location_text":"办公楼",'
            '"location_evidence":"办公楼","problem_description":"",'
            '"problem_evidence":""}'
        ),
        (
            '{"decision":"repair","location_text":"",'
            '"location_evidence":"","problem_description":"",'
            '"problem_evidence":""}'
        ),
        json.dumps(
            {
                "decision": "repair",
                "location_text": "办" * 201,
                "location_evidence": "办公楼",
                "problem_description": "灯坏",
                "problem_evidence": "灯坏",
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {
                "decision": "repair",
                "location_text": "办公楼",
                "location_evidence": "办公楼",
                "problem_description": "坏" * 2_001,
                "problem_evidence": "灯坏",
            },
            ensure_ascii=False,
        ),
    ],
)
def test_invalid_inner_candidate_maps_to_one_safe_error(content: str) -> None:
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json=successful_provider_response(content))

    client = DeepSeekRepairDraftClient(
        settings=build_enabled_settings(),
        http_transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RepairDraftModelUnavailableError) as error:
        client.interpret(raw_text="办公楼灯坏了", continuation=None)

    assert str(error.value) == "报修文字整理暂时不可用，请直接填写地点和问题。"
    assert error.value.__cause__ is None
    assert request_count == 1


@pytest.mark.parametrize("decision", ["unsupported", "unclear"])
def test_adapter_strictly_decodes_nonrepair_decisions(decision: str) -> None:
    content = json.dumps(
        {
            "decision": decision,
            "location_text": "",
            "location_evidence": "",
            "problem_description": "",
            "problem_evidence": "",
        }
    )

    client = DeepSeekRepairDraftClient(
        settings=build_enabled_settings(),
        http_transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=successful_provider_response(content))
        ),
    )

    candidate = client.interpret(raw_text="帮我请假", continuation=None)

    assert candidate.decision == decision
    assert candidate.location_text == ""
    assert candidate.problem_description == ""


def test_adapter_returns_candidates_without_deciding_if_a_card_is_ready() -> None:
    request_count = 0
    raw_text = "  女生宿舍503漏水  "
    content = json.dumps(
        {
            "decision": "repair",
            "location_text": "教学楼一层配电间",
            "location_evidence": "教学楼一层配电间",
            "problem_description": "女生宿舍503漏水",
            "problem_evidence": "女生宿舍503漏水",
        },
        ensure_ascii=False,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json=successful_provider_response(content))

    client = DeepSeekRepairDraftClient(
        settings=build_enabled_settings(),
        http_transport=httpx.MockTransport(handler),
    )

    candidate = client.interpret(raw_text=raw_text, continuation=None)

    assert candidate == RepairEntryInterpretationCandidate(
        decision="repair",
        location_text="教学楼一层配电间",
        location_evidence="教学楼一层配电间",
        problem_description="女生宿舍503漏水",
        problem_evidence="女生宿舍503漏水",
    )
    assert request_count == 1


@pytest.mark.parametrize(
    "failure_kind",
    ["timeout", "network", "http", "redirect", "invalid-json"],
)
def test_transport_and_http_failures_are_not_retried_or_exposed(failure_kind: str) -> None:
    request_count = 0
    unsafe_marker = "provider-body-marker-13800000000"

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if failure_kind == "timeout":
            raise httpx.ReadTimeout(unsafe_marker)
        if failure_kind == "network":
            raise httpx.ConnectError(unsafe_marker)
        if failure_kind == "http":
            return httpx.Response(503, text=unsafe_marker)
        if failure_kind == "redirect":
            return httpx.Response(307, headers={"Location": "https://redirect.example/secret"})
        return httpx.Response(200, content=b"not-json")

    client = DeepSeekRepairDraftClient(
        settings=build_enabled_settings(),
        http_transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RepairDraftModelUnavailableError) as error:
        client.interpret(raw_text="办公楼灯坏了", continuation=None)

    assert str(error.value) == "报修文字整理暂时不可用，请直接填写地点和问题。"
    assert unsafe_marker not in str(error.value)
    assert error.value.__cause__ is None
    assert request_count == 1
