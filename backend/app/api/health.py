"""Original public health endpoint."""

from fastapi import APIRouter

from app.api.responses import ok


router = APIRouter()


@router.get("/health")
def health() -> dict[str, object]:
    return ok("UP")
