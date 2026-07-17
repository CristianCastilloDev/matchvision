"""Initial normalized MatchVision schema.

Revision ID: 0001
Revises:
Create Date: 2026-07-13

The first revision intentionally creates the complete metadata snapshot. Later
schema changes must use explicit incremental Alembic operations.
"""

from alembic import op

from app import models  # noqa: F401
from app.db import Base


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
