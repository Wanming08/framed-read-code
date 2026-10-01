"""User table operations; transaction ownership stays with the API service."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import User


class UserRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_by_username(self, username: str) -> User | None:
        return self.session.scalar(select(User).where(User.username == username))

    def get_by_id(self, user_id: int) -> User | None:
        return self.session.get(User, user_id)

    def insert(self, user: User) -> User:
        self.session.add(user)
        self.session.flush()
        return user

    def update_password(self, user_id: int, encoded_password: str) -> None:
        self.session.execute(
            update(User).where(User.id == user_id).values(password=encoded_password)
        )
