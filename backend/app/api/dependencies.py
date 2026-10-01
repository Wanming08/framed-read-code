"""Request scoped database, Redis and authentication dependencies."""

from collections.abc import Iterator
from threading import Lock

import redis
from fastapi import Depends, Header, Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.errors import BusinessError, ErrorCode
from app.services.auth import AuthService


_RESOURCE_LOCK = Lock()


def get_session(request: Request) -> Iterator[Session]:
    if not hasattr(request.app.state, "session_factory"):
        with _RESOURCE_LOCK:
            if not hasattr(request.app.state, "session_factory"):
                settings = request.app.state.settings
                engine = create_engine(
                    settings.mysql_url,
                    pool_pre_ping=True,
                    pool_size=settings.db_pool_max_size,
                    pool_timeout=3,
                )
                request.app.state.db_engine = engine
                request.app.state.session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with request.app.state.session_factory() as session:
        yield session


def get_redis(request: Request) -> redis.Redis:
    if not hasattr(request.app.state, "redis_client"):
        with _RESOURCE_LOCK:
            if not hasattr(request.app.state, "redis_client"):
                settings = request.app.state.settings
                request.app.state.redis_client = redis.Redis(
                    host=settings.redis_host,
                    port=settings.redis_port,
                    db=settings.redis_database,
                    password=settings.redis_password or None,
                    socket_connect_timeout=3,
                    socket_timeout=3,
                    decode_responses=True,
                )
    return request.app.state.redis_client


def get_auth_service(
    session: Session = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis),
) -> AuthService:
    from app.db.repositories.users import UserRepository

    return AuthService(UserRepository(session), redis_client)


def get_current_user(
    authorization: str | None = Header(default=None),
    auth: AuthService = Depends(get_auth_service),
) -> int:
    try:
        return auth.resolve_user(authorization)
    except PermissionError as exc:
        raise BusinessError(ErrorCode.UNAUTHORIZED, str(exc)) from exc


def get_admin_user(
    user_id: int = Depends(get_current_user),
    auth: AuthService = Depends(get_auth_service),
) -> int:
    try:
        auth.require_admin(user_id)
    except PermissionError as exc:
        raise BusinessError(ErrorCode.FORBIDDEN, str(exc)) from exc
    return user_id
