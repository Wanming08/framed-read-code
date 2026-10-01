"""Media rows with the caller owning the transaction boundary."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import MediaFile


class MediaRepository:
    def __init__(self, session: Session):
        self.session = session

    def insert(self, media: MediaFile) -> MediaFile:
        self.session.add(media)
        self.session.flush()
        return media

    def get_by_id(self, media_id: int) -> MediaFile | None:
        return self.session.get(MediaFile, media_id)

    def list_by_user(self, user_id: int) -> list[MediaFile]:
        return list(self.session.scalars(
            select(MediaFile).where(MediaFile.user_id == user_id).order_by(MediaFile.id.desc())
        ))

    def delete_by_id(self, media_id: int) -> None:
        media = self.session.get(MediaFile, media_id)
        if media is not None:
            self.session.delete(media)

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()
