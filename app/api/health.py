"""Health check endpoint."""

from fastapi import APIRouter

from app.schemas.common import HealthCheck

router = APIRouter()


@router.get("/health", response_model=HealthCheck)
def health():
    return HealthCheck()
