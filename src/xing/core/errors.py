"""统一错误契约（RFC 9457 problem+json）与数据库错误映射。

两条原则：

1. 错误必须用真实 HTTP 状态码。返回 200 + {"error": ...} 会让网关、监控、
   重试策略、缓存、以及 OpenAPI 生成的前端客户端全部失效。
2. 数据库约束冲突不能变成 500。唯一冲突、外键、CHECK、RLS 拒绝都有明确语义，
   前端需要据此决定是「提示用户改输入」还是「弹权限提示」。
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError

log = structlog.get_logger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"
PROBLEM_TYPE_BASE = "https://xing.local/errors/"


class ApiError(Exception):
    """业务错误。路由和服务层统一抛这个，由下面的 handler 转成 problem+json。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def problem_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"{PROBLEM_TYPE_BASE}{code.lower()}",
        "title": code,
        "status": status_code,
        "detail": message,
        "code": code,
    }
    if details:
        body["details"] = details
    return JSONResponse(status_code=status_code, content=body, media_type=PROBLEM_CONTENT_TYPE)


def _sqlstate(exc: DBAPIError) -> str | None:
    """从驱动异常里取 SQLSTATE。

    asyncpg 的异常会先被 SQLAlchemy 的 DBAPI 适配层包一层，所以既要看 orig，
    也要看它的 __cause__。不同版本暴露的属性名不一致（sqlstate / pgcode）。
    """
    orig = exc.orig
    for obj in (orig, getattr(orig, "__cause__", None)):
        if obj is None:
            continue
        for attr in ("sqlstate", "pgcode", "code"):
            value = getattr(obj, attr, None)
            if isinstance(value, str) and len(value) == 5 and value.isalnum():
                return value.upper()
    return None


def _constraint(exc: DBAPIError) -> str | None:
    orig = exc.orig
    for obj in (orig, getattr(orig, "__cause__", None)):
        value = getattr(obj, "constraint_name", None)
        if isinstance(value, str) and value:
            return value
    return None


# SQLSTATE -> (状态码, 错误码, 面向用户的说明)
_SQLSTATE_MAP: dict[str, tuple[int, str, str]] = {
    "23505": (409, "CONFLICT", "资源已存在或违反唯一约束"),
    "23503": (422, "FOREIGN_KEY_VIOLATION", "引用的对象不存在"),
    "23502": (422, "NOT_NULL_VIOLATION", "缺少必填字段"),
    "23514": (422, "CONSTRAINT_VIOLATION", "违反业务约束"),
    "42501": (403, "WORKSPACE_FORBIDDEN", "当前用户无权访问该工作区"),
}


async def _dbapi_handler(_request: Request, exc: DBAPIError) -> JSONResponse:
    state = _sqlstate(exc)
    constraint = _constraint(exc)

    if state in _SQLSTATE_MAP:
        status_code, code, message = _SQLSTATE_MAP[state]
        details: dict[str, Any] = {}
        if constraint:
            details["constraint"] = constraint
        # 唯一冲突是我们最常靠约束兜底的场景（幂等、并发），给到 warning 级别，
        # 便于排查「前端说没成功，库里其实已经有了」。
        log.warning("db.constraint_violation", sqlstate=state, constraint=constraint)
        return problem_response(
            status_code=status_code, code=code, message=message, details=details or None
        )

    # 未知 SQL 错误：完整记日志，对外不泄露 SQL 文本。
    log.error("db.error", sqlstate=state, error=str(exc.orig), exc_info=exc)
    return problem_response(
        status_code=500,
        code="DATABASE_ERROR",
        message="数据库执行失败",
        details={"sqlstate": state} if state else None,
    )


async def _api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return problem_response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details or None,
    )


async def _validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """把 Pydantic 的校验错误压成稳定结构，前端不用解析 FastAPI 的嵌套 loc。"""
    fields = [
        {
            "field": ".".join(str(part) for part in err.get("loc", ()) if part != "body"),
            "message": err.get("msg", ""),
        }
        for err in exc.errors()
    ]
    return problem_response(
        status_code=422,
        code="VALIDATION_ERROR",
        message="请求参数校验失败",
        details={"fields": fields},
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_handler)  # type: ignore[arg-type]
    app.add_exception_handler(DBAPIError, _dbapi_handler)  # type: ignore[arg-type]
