"""
Application configuration via pydantic-settings.

All settings are loaded from environment variables with the KFO_ prefix,
or from a .env file in the project root.
"""

from decimal import Decimal
from pathlib import Path

from pydantic_settings import BaseSettings

# Absolute path to the .env file regardless of cwd when the server starts
_ENV_FILE = Path(__file__).parent.parent / ".env"


class Settings(BaseSettings):
    """
    Central configuration for the Family Office system.

    This platform operates as a tax-efficient investment vehicle whose core
    purposes include preserving, growing, and diversifying the family's capital through
    active and passive investments, maximizing after-tax returns, and maintaining
    complete IRS-compliant books and records.
    """

    # Database
    db_path: str = "data/family_office.db"
    db_passphrase: str = ""

    # LLM
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-4-20250514"

    # Market Data
    market_data_cache_ttl_minutes: int = 15

    # Approval Policies
    trade_approval_threshold: Decimal = Decimal("50000")
    gift_approval_threshold: Decimal = Decimal("18000")

    # Server
    host: str = "127.0.0.1"
    port: int = 8000

    # Encryption key for application-level field encryption (SSNs, tax IDs)
    field_encryption_key: str = ""

    model_config = {
        "env_prefix": "KFO_",
        "env_file": str(_ENV_FILE),
        "env_file_encoding": "utf-8",
    }

    @property
    def db_url(self) -> str:
        """SQLAlchemy database URL. Uses plain SQLite (sqlcipher optional)."""
        return f"sqlite:///{self.db_path}"

    @property
    def abs_db_path(self) -> Path:
        return Path(self.db_path).resolve()


settings = Settings()
