from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.datastructures import State

from app.interfaces.http.errors import render_api_error, render_request_validation_error


@dataclass(frozen=True, slots=True)
class FrameworkRequestContext:
    """只发布框架生成的机械请求标识，不发布任何客户端输入。"""

    request_id: UUID


@dataclass(frozen=True, slots=True)
class FrameworkRequestPresence:
    """只表示请求是否携带查询参数或正文，不保留其内容。"""

    context: FrameworkRequestContext
    has_query_parameters: bool
    has_body: bool


def _context_from_state(state: State) -> FrameworkRequestContext:
    return FrameworkRequestContext(request_id=UUID(str(state.request_id)))


def get_framework_request_context(request: Request) -> FrameworkRequestContext:
    """把原始框架请求收敛为服务端签发的机械 request id。"""

    return _context_from_state(request.state)


async def get_framework_request_presence(request: Request) -> FrameworkRequestPresence:
    """读取输入是否存在；正文值不离开本适配器，也不写入错误。"""

    return FrameworkRequestPresence(
        context=_context_from_state(request.state),
        has_query_parameters=bool(request.query_params),
        has_body=bool(await request.body()),
    )


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return render_api_error(_context_from_state(request.state).request_id, exc)


async def request_validation_error_handler(
    request: Request,
    _exc: Exception,
) -> JSONResponse:
    return render_request_validation_error(_context_from_state(request.state).request_id)


__all__ = [
    "FrameworkRequestContext",
    "FrameworkRequestPresence",
    "api_error_handler",
    "get_framework_request_context",
    "get_framework_request_presence",
    "request_validation_error_handler",
]
