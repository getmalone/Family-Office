"""Common Pydantic schemas used across the Family Office API."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class HealthCheck(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"


class Pagination(BaseModel):
    page: int = 1
    per_page: int = 50
    total: int = 0

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page


class ErrorResponse(BaseModel):
    detail: str
    code: str | None = None


class DateRange(BaseModel):
    start_date: date
    end_date: date = Field(default_factory=date.today)


class MoneyAmount(BaseModel):
    amount: Decimal
    currency: str = "USD"
