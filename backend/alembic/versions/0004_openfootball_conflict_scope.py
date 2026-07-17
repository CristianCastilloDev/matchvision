"""Scope OpenFootball entity conflicts to one source repository.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    if "source_repository" not in _columns("entity_resolution_conflicts"):
        op.add_column(
            "entity_resolution_conflicts",
            sa.Column("source_repository", sa.String(length=120), nullable=True),
        )
    index_name = "ix_entity_resolution_conflicts_source_repository"
    if index_name not in _indexes("entity_resolution_conflicts"):
        op.create_index(
            index_name,
            "entity_resolution_conflicts",
            ["source_repository"],
        )

    # Backfill only when the old conflict has exactly one possible pending
    # mapping. Ambiguous legacy rows remain NULL and the service refuses to
    # resolve them automatically, preventing cross-repository merges.
    connection = op.get_bind()
    conflicts = connection.execute(
        sa.text(
            "SELECT id, entity_type, normalized_name "
            "FROM entity_resolution_conflicts "
            "WHERE provider = :provider AND source_repository IS NULL"
        ),
        {"provider": "openfootball"},
    ).mappings().all()
    for conflict in conflicts:
        repositories = connection.execute(
            sa.text(
                "SELECT DISTINCT source_repository "
                "FROM openfootball_entity_mappings "
                "WHERE entity_type = :entity_type "
                "AND normalized_name = :normalized_name "
                "AND resolution_status IN ('ambiguous', 'manual_review', 'pending')"
            ),
            {
                "entity_type": conflict["entity_type"],
                "normalized_name": conflict["normalized_name"],
            },
        ).scalars().all()
        if len(repositories) == 1:
            connection.execute(
                sa.text(
                    "UPDATE entity_resolution_conflicts "
                    "SET source_repository = :repository WHERE id = :conflict_id"
                ),
                {"repository": repositories[0], "conflict_id": conflict["id"]},
            )


def downgrade() -> None:
    index_name = "ix_entity_resolution_conflicts_source_repository"
    if index_name in _indexes("entity_resolution_conflicts"):
        op.drop_index(index_name, table_name="entity_resolution_conflicts")
    if "source_repository" in _columns("entity_resolution_conflicts"):
        op.drop_column("entity_resolution_conflicts", "source_repository")
