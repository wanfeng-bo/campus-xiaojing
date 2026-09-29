from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict


class HttpErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    request_id: str


# Exceptions must allow Python/contextlib to update __traceback__ and chaining state.
@dataclass(slots=True)
class ApiError(Exception):
    """让 HTTP 适配层以稳定且不含原始输入的错误信封失败。"""

    status_code: int
    code: str
    message: str


def render_api_error(request_id: UUID, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ApiError):
        body = HttpErrorResponse(
            code="system.unavailable",
            message="系统暂时不可用，请稍后重试。",
            request_id=str(request_id),
        )
        return JSONResponse(status_code=500, content=body.model_dump(mode="json"))
    body = HttpErrorResponse(
        code=exc.code,
        message=exc.message,
        request_id=str(request_id),
    )
    return JSONResponse(status_code=exc.status_code, content=body.model_dump(mode="json"))


def render_request_validation_error(request_id: UUID) -> JSONResponse:
    body = HttpErrorResponse(
        code="http.request_invalid",
        message="请求内容不符合已发布的接口契约。",
        request_id=str(request_id),
    )
    return JSONResponse(status_code=422, content=body.model_dump(mode="json"))


__all__ = [
    "ApiError",
    "HttpErrorResponse",
    "render_api_error",
    "render_request_validation_error",
]
