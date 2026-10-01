"""HTTP and business codes copied from ErrorCode and ApiExceptionHandler."""

from enum import IntEnum

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.responses import error


class ErrorCode(IntEnum):
    INVALID_ARGUMENT = 40000
    VALIDATION_FAILED = 40001
    UNAUTHORIZED = 40100
    FORBIDDEN = 40300
    NOT_FOUND = 40400
    CONFLICT = 40900
    UNPROCESSABLE = 42200
    RATE_LIMITED = 42900
    INTERNAL_ERROR = 50000
    SERVICE_UNAVAILABLE = 50300

    @property
    def http_status(self) -> int:
        return int(self) // 100


class BusinessError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(BusinessError)
    async def business_error(_request: Request, exc: BusinessError) -> JSONResponse:
        return JSONResponse(error(int(exc.code), str(exc)), status_code=exc.code.http_status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        body_errors = [
            item for item in exc.errors()
            if item.get("loc", (None,))[0] == "body" and item.get("type") != "json_invalid"
        ]
        if body_errors:
            detail = "; ".join(
                f"{'.'.join(str(part) for part in item['loc'][1:])}: {item['msg']}"
                for item in body_errors
            )
            return JSONResponse(
                error(int(ErrorCode.VALIDATION_FAILED), detail or "请求参数校验失败"),
                status_code=400,
            )
        return JSONResponse(error(int(ErrorCode.INVALID_ARGUMENT), "请求参数不合法"), status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 405:
            code, message = ErrorCode.INVALID_ARGUMENT, "请求方法不被支持"
        elif exc.status_code == 404:
            code, message = ErrorCode.NOT_FOUND, "资源不存在"
        else:
            code, message = ErrorCode.INTERNAL_ERROR, "服务暂时不可用"
        return JSONResponse(error(int(code), message), status_code=exc.status_code)

    @app.exception_handler(ValueError)
    async def invalid_argument(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(error(int(ErrorCode.INVALID_ARGUMENT), str(exc) or "请求参数不合法"), status_code=400)

    @app.exception_handler(PermissionError)
    async def forbidden(_request: Request, exc: PermissionError) -> JSONResponse:
        return JSONResponse(error(int(ErrorCode.FORBIDDEN), str(exc) or "无访问权限"), status_code=403)

    @app.exception_handler(Exception)
    async def internal_error(_request: Request, _exc: Exception) -> JSONResponse:
        return JSONResponse(error(int(ErrorCode.INTERNAL_ERROR), "服务暂时不可用"), status_code=500)
