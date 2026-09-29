from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.logging import configure_safe_dingtalk_logging
from app.core.request_id import assign_request_id
from app.db.connection import (
    RuntimeDatabaseStartupRejected,
    check_database_connection,
    get_session,
)
from app.interfaces.http.errors import ApiError, HttpErrorResponse
from app.interfaces.http.framework_request import (
    api_error_handler,
    request_validation_error_handler,
)
from app.platform.authorization.router import router as authorization_router
from app.platform.identity.context import CurrentUser, get_current_user
from app.platform.identity.context_proof import ContextSigningKeyMismatch
from app.platform.identity.router import determine_access_status
from app.platform.identity.router import router as identity_router

configure_safe_dingtalk_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def application_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """在开始接收请求前核验数据库可用性和上下文证明密钥指纹。"""

    check_database_connection()
    yield


app = FastAPI(
    title="中学校园综合智能体 API",
    version="0.1.0",
    lifespan=application_lifespan,
    responses={422: {"model": HttpErrorResponse}},
)
app.middleware("http")(assign_request_id)
app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(RequestValidationError, request_validation_error_handler)
app.include_router(authorization_router)
app.include_router(identity_router)


class HealthResponse(BaseModel):
    """对外暴露的最小系统健康状态，不包含数据库连接细节。"""

    status: Literal["ok"]
    database: Literal["ok"]


class CurrentUserResponse(BaseModel):
    """向客户端返回当前用户的最小可信身份字段。"""

    id: int
    external_subject: str
    display_name: str
    access_state: Literal["ready", "pending_configuration"]
    can_manage_system: bool


@app.get("/api/v1/health", response_model=HealthResponse, tags=["system"])
def read_health() -> HealthResponse:
    """验证数据库可用性，并将内部异常转换为可安全展示的中文提示。"""

    try:
        check_database_connection()
    except (SQLAlchemyError, ContextSigningKeyMismatch, RuntimeDatabaseStartupRejected):
        logger.exception("数据库健康检查失败")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="数据库暂时不可用，请稍后重试。",
        ) from None

    return HealthResponse(status="ok", database="ok")


@app.get("/api/v1/me", response_model=CurrentUserResponse, tags=["identity"])
def read_current_user(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_session)],
) -> CurrentUserResponse:
    """返回当前已认证用户和派生访问状态，且不向前端泄露角色或完整权限。"""

    access_status = determine_access_status(session, current_user)
    return CurrentUserResponse(
        id=current_user.id,
        external_subject=current_user.external_subject,
        display_name=current_user.display_name,
        access_state=access_status.access_state,
        can_manage_system=access_status.can_manage_system,
    )
