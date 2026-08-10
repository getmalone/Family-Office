"""add assets symbol-resolution columns

Supports the ticker resolver: provenance for auto-applied mappings
(resolution_source), a terminal "no_listing" marker so unlisted funds aren't
re-queried forever (resolution_status), and a stored medium-confidence
candidate awaiting one-click user confirmation (suggested_symbol/_note).

Revision ID: d8b0c2e4f6a8
Revises: c4f6a8b0d2e4
Create Date: 2026-08-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8b0c2e4f6a8"
down_revision: Union[str, None] = "c4f6a8b0d2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("resolution_source", sa.String(length=120), nullable=True))
    op.add_column("assets", sa.Column("resolution_status", sa.String(length=20), nullable=True))
    op.add_column("assets", sa.Column("suggested_symbol", sa.String(length=20), nullable=True))
    op.add_column("assets", sa.Column("suggested_note", sa.String(length=300), nullable=True))


def downgrade() -> None:
    op.drop_column("assets", "suggested_note")
    op.drop_column("assets", "suggested_symbol")
    op.drop_column("assets", "resolution_status")
    op.drop_column("assets", "resolution_source")
