"""
Key/value application settings, stored in the (encrypted) database.

Lets a user configure the app from the Settings page — e.g. their Anthropic API
key — without hand-editing a .env file. Because these live in the SQLCipher
database, sensitive values are encrypted at rest along with the rest of the data.
"""

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AppSetting(Base, TimestampMixin):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
