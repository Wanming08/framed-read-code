"""Original /user routes and distinct register/login validation groups."""

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import get_auth_service, get_current_user, get_session
from app.api.errors import BusinessError, ErrorCode
from app.api.responses import ok
from app.services.auth import AuthResult, AuthService


router = APIRouter(prefix="/user")


def _java_length(value: str) -> int:
    return len(value.encode("utf-16-le", "surrogatepass")) // 2


class RegisterRequest(BaseModel):
    username: str
    password: str
    nickname: str | None = None

    @field_validator("username")
    @classmethod
    def username_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("账号不能为空")
        if not 3 <= _java_length(value) <= 32:
            raise ValueError("账号需为 3-32 位")
        return value

    @field_validator("password")
    @classmethod
    def password_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("密码不能为空")
        if not 8 <= _java_length(value) <= 128:
            raise ValueError("密码需为 8-128 位")
        return value

    @field_validator("nickname")
    @classmethod
    def nickname_valid(cls, value: str | None) -> str | None:
        if value is not None and _java_length(value) > 50:
            raise ValueError("昵称不能超过 50 个字符")
        return value


class LoginRequest(BaseModel):
    username: str
    password: str

    @field_validator("username")
    @classmethod
    def username_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("账号不能为空")
        if _java_length(value) > 32:
            raise ValueError("账号不能超过 32 位")
        return value

    @field_validator("password")
    @classmethod
    def password_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("密码不能为空")
        if _java_length(value) > 128:
            raise ValueError("密码不能超过 128 位")
        return value


def _unwrap(result: AuthResult) -> dict[str, object]:
    if result.code != 200:
        code = {
            400: ErrorCode.INVALID_ARGUMENT,
            401: ErrorCode.UNAUTHORIZED,
            409: ErrorCode.CONFLICT,
            429: ErrorCode.RATE_LIMITED,
        }.get(result.code, ErrorCode.INTERNAL_ERROR)
        raise BusinessError(code, result.message)
    return ok({"userInfo": result.user_info, "token": result.token})


@router.post("/register")
def register(
    payload: RegisterRequest,
    auth: AuthService = Depends(get_auth_service),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    try:
        result = auth.register(payload.username, payload.password, payload.nickname)
        if result.code == 200:
            session.commit()
        return _unwrap(result)
    except IntegrityError as exc:
        session.rollback()
        raise BusinessError(ErrorCode.CONFLICT, "该账号已存在") from exc


@router.post("/login")
def login(
    payload: LoginRequest,
    auth: AuthService = Depends(get_auth_service),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    result = auth.login(payload.username, payload.password)
    if result.code == 200:
        try:
            session.commit()
        except Exception:
            if result.token is not None:
                auth.revoke_session("Bearer " + result.token)
            raise
    return _unwrap(result)


@router.post("/logout")
def logout(
    authorization: str | None = Header(default=None),
    _user_id: int = Depends(get_current_user),
    auth: AuthService = Depends(get_auth_service),
) -> dict[str, object]:
    auth.revoke_session(authorization)
    return ok()
