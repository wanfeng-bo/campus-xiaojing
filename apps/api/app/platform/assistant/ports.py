from __future__ import annotations

from typing import Protocol

from .contracts import (
    ExpressionModelRequestV1,
    ExpressionModelResultV1,
    MeaningModelCorrelationV1,
    MeaningModelRequestV1,
    MeaningV1,
    ModuleCallV1,
    ModuleConfirmationCallV1,
    ModuleRecoveryCallV1,
    ModuleResultCheckCallV1,
    ModuleResultV1,
)


class MeaningModelPort(Protocol):
    """provider 只见 request；适配器用本地 correlation 补封 MeaningV1。"""

    def interpret(
        self,
        request: MeaningModelRequestV1,
        *,
        correlation: MeaningModelCorrelationV1,
    ) -> MeaningV1: ...


class ExpressionModelPort(Protocol):
    """第二次模型只组织安全句式，不接触中性槽的本地值。"""

    def compose(self, request: ExpressionModelRequestV1) -> ExpressionModelResultV1: ...


class AssistantModulePort(Protocol):
    """模块自行验证、鉴权和处理；内核只投递协议信封。"""

    def handle(self, request: ModuleCallV1) -> ModuleResultV1: ...

    def confirm(self, request: ModuleConfirmationCallV1) -> ModuleResultV1: ...

    def recover(self, request: ModuleRecoveryCallV1) -> ModuleResultV1: ...

    def check_result(self, request: ModuleResultCheckCallV1) -> ModuleResultV1: ...


class ControlCredentialStore(Protocol):
    """短寿命点击凭据的外部持久化边界；具体 Redis 适配器留在内核外。"""

    def put(
        self,
        *,
        credential_ref: str,
        payload: str,
        ttl_seconds: int,
    ) -> bool: ...

    def get(self, *, credential_ref: str) -> str | None: ...

    def consume(
        self,
        *,
        credential_ref: str,
        expected_payload: str,
        expires_at: int,
    ) -> bool: ...

    def consume_and_put(
        self,
        *,
        credential_ref: str,
        expected_payload: str,
        expires_at: int,
        replacement_credential_ref: str,
        replacement_payload: str,
        ttl_seconds: int,
    ) -> bool: ...

    def discard(self, *, credential_ref: str, expected_payload: str) -> bool: ...
