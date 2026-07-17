"""OpenFootball identity catalog metadata.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _add_missing(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def upgrade() -> None:
    _add_missing("competitions", sa.Column("catalog_code", sa.String(length=50), nullable=True))
    _add_missing("competitions", sa.Column("competition_level", sa.Integer(), nullable=True))
    _add_missing("competitions", sa.Column("competition_type", sa.String(length=30), nullable=True))
    _add_missing("competitions", sa.Column("catalog_metadata", sa.JSON(), nullable=True))

    _add_missing("teams", sa.Column("city", sa.String(length=120), nullable=True))
    _add_missing("teams", sa.Column("founded_year", sa.Integer(), nullable=True))
    _add_missing("teams", sa.Column("catalog_metadata", sa.JSON(), nullable=True))

    _add_missing("players", sa.Column("height_m", sa.Float(), nullable=True))
    _add_missing("players", sa.Column("birthplace", sa.String(length=180), nullable=True))
    _add_missing("players", sa.Column("catalog_metadata", sa.JSON(), nullable=True))

    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("competitions")}
    if "ix_competitions_catalog_code" not in indexes:
        op.create_index("ix_competitions_catalog_code", "competitions", ["catalog_code"])


def downgrade() -> None:
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("competitions")}
    if "ix_competitions_catalog_code" in indexes:
        op.drop_index("ix_competitions_catalog_code", table_name="competitions")
    for table, columns in (
        ("players", ("catalog_metadata", "birthplace", "height_m")),
        ("teams", ("catalog_metadata", "founded_year", "city")),
        (
            "competitions",
            ("catalog_metadata", "competition_type", "competition_level", "catalog_code"),
        ),
    ):
        existing = _columns(table)
        for column in columns:
            if column in existing:
                op.drop_column(table, column)
