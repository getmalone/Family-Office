"""
Application configuration via pydantic-settings.

All settings are loaded from environment variables with the KFO_ prefix,
or from a .env file in the project root.
"""

from decimal import Decimal
from pathlib import Path

from pydantic import model_validator
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

    # Web access control.
    # When access_code is empty (default), the app is open — preserving the
    # local-first, single-user experience on 127.0.0.1. Set KFO_ACCESS_CODE to a
    # shared passphrase before exposing the app on a network (KFO_HOST=0.0.0.0)
    # so every request must authenticate via the /login page.
    access_code: str = ""
    # Days a login session cookie stays valid.
    session_max_age_days: int = 30

    # Single master password (set by the desktop launcher at startup). One secret
    # that both encrypts the database and — when the app is exposed on a network —
    # gates the web UI. See the validator below for how it's applied.
    master_password: str = ""

    # Encryption key for application-level field encryption (SSNs, tax IDs)
    field_encryption_key: str = ""

    model_config = {
        "env_prefix": "KFO_",
        "env_file": str(_ENV_FILE),
        "env_file_encoding": "utf-8",
    }

    @model_validator(mode="after")
    def _apply_master_password(self) -> "Settings":
        """Derive the DB key and (network-only) web gate from the master password.

        - ``db_passphrase`` is always filled from the master password when not set
          explicitly, so the database is encrypted at rest. SQLCipher runs its own
          PBKDF2 over the passphrase, so the raw value is used directly.
        - ``access_code`` is filled only when the app is bound to all interfaces
          (``0.0.0.0``), i.e. exposed on a network — that's when a login gate is
          warranted. On localhost the single launcher prompt is enough, so the
          browser isn't asked for the password a second time.

        Explicitly-set values always win; this only fills blanks.
        """
        if self.master_password:
            if not self.db_passphrase:
                self.db_passphrase = self.master_password
            if not self.access_code and self.host == "0.0.0.0":
                self.access_code = self.master_password
        return self

    @property
    def db_url(self) -> str:
        """SQLAlchemy database URL. Uses plain SQLite (sqlcipher optional)."""
        return f"sqlite:///{self.db_path}"

    @property
    def abs_db_path(self) -> Path:
        return Path(self.db_path).resolve()


settings = Settings()
