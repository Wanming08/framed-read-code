"""Authentication rules ported from the upstream Java AuthService.

The HTTP layer owns request validation and response wrapping. This module keeps
the Java service's decisions and storage format independent of the web stack.
``user_factory`` may be supplied by a test or adapter; production defaults to
the SQLAlchemy User model.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable


PASSWORD_ITERATIONS = 210_000
PASSWORD_KEY_BYTES = 32
SALT_BYTES = 16
TOKEN_BYTES = 32
SESSION_SECONDS = 24 * 60 * 60
LOGIN_FAILURE_SECONDS = 10 * 60
MAX_LOGIN_FAILURES = 8
SESSION_PREFIX = "auth:session:"
LOGIN_FAILURE_PREFIX = "auth:login-failures:"
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_]{3,32}\Z")


class DuplicateUsernameError(Exception):
    """Repository's unique-username violation, including a concurrent insert."""


@dataclass(frozen=True)
class AuthResult:
    code: int
    message: str
    user_info: dict[str, Any] | None = None
    token: str | None = None


def _b64(data: bytes, *, urlsafe: bool = False) -> str:
    encode = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    return encode(data).decode("ascii").rstrip("=")


def _java_trim(value: str) -> str:
    """String.trim() removes only code points through U+0020."""
    return value.strip("".join(chr(i) for i in range(0x21)))


def _java_length(value: str) -> int:
    return len(value.encode("utf-16-le", "surrogatepass")) // 2


def _decode_redis(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _new_user(**fields: Any) -> Any:
    from app.db.models import User

    return User(**fields)


class AuthService:
    def __init__(
        self,
        repository: Any,
        redis: Any,
        *,
        user_factory: Callable[..., Any] = _new_user,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.repository = repository
        self.redis = redis
        self.user_factory = user_factory
        self.random_bytes = random_bytes
        self.clock_ms = clock_ms

    @staticmethod
    def normalize_username(username: str | None) -> str | None:
        if username is None:
            return None
        normalized = _java_trim(username)
        return normalized if USERNAME_PATTERN.fullmatch(normalized) else None

    @staticmethod
    def normalize_nickname(nickname: str | None) -> str | None:
        if nickname is None or nickname.isspace() or nickname == "":
            return ""
        normalized = _java_trim(nickname)
        return normalized if _java_length(normalized) <= 50 else None

    @staticmethod
    def user_view(user: Any) -> dict[str, Any]:
        return {
            "id": user.id,
            "username": user.username,
            "nickname": user.nickname,
            "avatar": user.avatar,
            "role": user.role,
        }

    def register(self, username: str | None, password: str | None, nickname: str | None = None) -> AuthResult:
        username = self.normalize_username(username)
        if username is None or password is None or not 8 <= _java_length(password) <= 128:
            return AuthResult(400, "账号需为 3-32 位字母、数字或下划线，密码需为 8-128 位")
        nickname = self.normalize_nickname(nickname)
        if nickname is None:
            return AuthResult(400, "昵称不能超过 50 个字符")
        if self.repository.get_by_username(username) is not None:
            return AuthResult(409, "该账号已存在")
        user = self.user_factory(
            username=username,
            password=self.hash_password(password),
            nickname=nickname if nickname.strip() else f"用户{self.clock_ms()}",
            role="USER",
        )
        try:
            inserted = self.repository.insert(user)
        except DuplicateUsernameError:
            return AuthResult(409, "该账号已存在")
        user = inserted if inserted is not None else user
        return AuthResult(200, "注册成功", self.user_view(user))

    def login(self, username: str | None, password: str | None) -> AuthResult:
        username = self.normalize_username(username)
        if username is None or password is None or password.isspace() or password == "":
            return AuthResult(400, "请输入账号和密码")
        if not self.login_attempt_allowed(username):
            return AuthResult(429, "登录尝试过于频繁，请稍后再试")
        user = self.repository.get_by_username(username)
        if user is None or not self.password_matches(password, user.password):
            self.record_login_failure(username)
            return AuthResult(401, "账号或密码错误")
        if not self.is_hashed(user.password):
            encoded = self.hash_password(password)
            self.repository.update_password(user.id, encoded)
            user.password = encoded
        self.clear_login_failures(username)
        token = self.create_session(user.id)
        return AuthResult(200, "登录成功", self.user_view(user), token)

    def require_admin(self, user_id: int) -> None:
        user = self.repository.get_by_id(user_id)
        if user is None or user.role != "ADMIN":
            raise PermissionError("仅管理员可操作失败任务")

    def hash_password(self, password: str) -> str:
        salt = self.random_bytes(SALT_BYTES)
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS, dklen=PASSWORD_KEY_BYTES)
        return f"pbkdf2${PASSWORD_ITERATIONS}${_b64(salt)}${_b64(derived)}"

    @staticmethod
    def is_hashed(password: str | None) -> bool:
        return password is not None and password.startswith("pbkdf2$")

    @classmethod
    def password_matches(cls, raw_password: str | None, stored_password: str | None) -> bool:
        if raw_password is None or stored_password is None:
            return False
        if not cls.is_hashed(stored_password):
            return hmac.compare_digest(raw_password.encode("utf-8"), stored_password.encode("utf-8"))
        try:
            parts = stored_password.split("$")
            iterations = int(parts[1])
            salt = base64.b64decode(parts[2] + "=" * (-len(parts[2]) % 4), validate=True)
            expected = base64.b64decode(parts[3] + "=" * (-len(parts[3]) % 4), validate=True)
            actual = hashlib.pbkdf2_hmac("sha256", raw_password.encode("utf-8"), salt, iterations, dklen=PASSWORD_KEY_BYTES)
            return hmac.compare_digest(actual, expected)
        except (IndexError, ValueError, TypeError, OverflowError):
            return False

    @staticmethod
    def session_key(token: str) -> str:
        return SESSION_PREFIX + _b64(hashlib.sha256(token.encode("utf-8")).digest(), urlsafe=True)

    def create_session(self, user_id: int) -> str:
        token = _b64(self.random_bytes(TOKEN_BYTES), urlsafe=True)
        self.redis.set(self.session_key(token), str(user_id), ex=SESSION_SECONDS)
        return token

    @staticmethod
    def bearer_token(authorization: str | None) -> str:
        if authorization is None or not authorization.startswith("Bearer "):
            raise PermissionError("请先登录")
        token = _java_trim(authorization[len("Bearer "):])
        if len(token) < 32:
            raise PermissionError("无效的登录凭证")
        return token

    def resolve_user(self, authorization: str | None) -> int:
        token = self.bearer_token(authorization)
        user_id = _decode_redis(self.redis.get(self.session_key(token)))
        if user_id is None:
            raise PermissionError("登录状态已失效")
        return int(user_id)

    def revoke_session(self, authorization: str | None) -> None:
        self.redis.delete(self.session_key(self.bearer_token(authorization)))

    @staticmethod
    def _failure_key(username: str) -> str:
        return LOGIN_FAILURE_PREFIX + username

    def login_attempt_allowed(self, username: str) -> bool:
        key = self._failure_key(username)
        count = _decode_redis(self.redis.get(key))
        if count is None:
            return True
        try:
            parsed = int(count)
            if not -(2**63) <= parsed < 2**63:
                raise ValueError("outside Java long range")
            return parsed < MAX_LOGIN_FAILURES
        except ValueError:
            self.redis.delete(key)
            return True

    def record_login_failure(self, username: str) -> None:
        key = self._failure_key(username)
        failures = self.redis.incr(key)
        if failures == 1:
            self.redis.expire(key, LOGIN_FAILURE_SECONDS)

    def clear_login_failures(self, username: str) -> None:
        self.redis.delete(self._failure_key(username))
