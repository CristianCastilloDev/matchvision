"""OpenFootball provenance, mappings and source coverage.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "result_details" not in _columns("matches"):
        op.add_column("matches", sa.Column("result_details", sa.JSON(), nullable=True))
    if "import_metrics" not in _columns("data_ingestion_runs"):
        op.add_column(
            "data_ingestion_runs",
            sa.Column("import_metrics", sa.JSON(), nullable=False, server_default="{}"),
        )
    if "preview_payload" not in _columns("data_ingestion_runs"):
        op.add_column(
            "data_ingestion_runs", sa.Column("preview_payload", sa.JSON(), nullable=True)
        )

    tables = _tables()
    if "match_source_records" not in tables:
        op.create_table(
            "match_source_records",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("match_id", sa.Integer(), nullable=False),
            sa.Column("ingestion_run_id", sa.String(length=36), nullable=True),
            sa.Column("source_name", sa.String(length=50), nullable=False),
            sa.Column("source_repository", sa.String(length=120), nullable=False),
            sa.Column("source_record_id", sa.String(length=128), nullable=False),
            sa.Column("source_file", sa.String(length=500), nullable=True),
            sa.Column("raw_payload", sa.JSON(), nullable=False),
            sa.Column("normalized_payload", sa.JSON(), nullable=False),
            sa.Column("field_provenance", sa.JSON(), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("conflict_status", sa.String(length=30), nullable=False),
            sa.Column("conflict_details", sa.JSON(), nullable=True),
            sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["ingestion_run_id"], ["data_ingestion_runs.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source_name",
                "source_repository",
                "source_record_id",
                name="uq_match_source_identity",
            ),
        )
        op.create_index(
            "ix_match_source_records_match_source",
            "match_source_records",
            ["match_id", "source_name"],
        )
        for column in (
            "match_id",
            "ingestion_run_id",
            "source_name",
            "source_repository",
            "source_record_id",
            "content_hash",
            "conflict_status",
        ):
            op.create_index(f"ix_match_source_records_{column}", "match_source_records", [column])

    if "openfootball_entity_mappings" not in tables:
        op.create_table(
            "openfootball_entity_mappings",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("entity_type", sa.String(length=30), nullable=False),
            sa.Column("original_name", sa.String(length=255), nullable=False),
            sa.Column("normalized_name", sa.String(length=255), nullable=False),
            sa.Column("internal_entity_id", sa.Integer(), nullable=True),
            sa.Column("source_repository", sa.String(length=120), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("manually_verified", sa.Boolean(), nullable=False),
            sa.Column("resolution_status", sa.String(length=30), nullable=False),
            sa.Column("resolution_notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "entity_type",
                "source_repository",
                "normalized_name",
                name="uq_openfootball_entity_mapping_identity",
            ),
        )
        for column in (
            "entity_type",
            "normalized_name",
            "internal_entity_id",
            "source_repository",
            "resolution_status",
        ):
            op.create_index(
                f"ix_openfootball_entity_mappings_{column}",
                "openfootball_entity_mappings",
                [column],
            )

    if "competition_source_coverage" not in tables:
        op.create_table(
            "competition_source_coverage",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("competition_id", sa.Integer(), nullable=False),
            sa.Column("source_name", sa.String(length=50), nullable=False),
            sa.Column("source_repository", sa.String(length=120), nullable=False),
            sa.Column("first_match_date", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_match_date", sa.DateTime(timezone=True), nullable=True),
            sa.Column("total_matches", sa.Integer(), nullable=False),
            sa.Column("finished_matches", sa.Integer(), nullable=False),
            sa.Column("scheduled_matches", sa.Integer(), nullable=False),
            sa.Column("seasons_available", sa.JSON(), nullable=False),
            sa.Column("fields_available", sa.JSON(), nullable=False),
            sa.Column("last_imported_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["competition_id"], ["competitions.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "competition_id",
                "source_name",
                "source_repository",
                name="uq_competition_source_coverage",
            ),
        )
        for column in ("competition_id", "source_name", "source_repository"):
            op.create_index(
                f"ix_competition_source_coverage_{column}",
                "competition_source_coverage",
                [column],
            )


def downgrade() -> None:
    tables = _tables()
    for table in (
        "competition_source_coverage",
        "openfootball_entity_mappings",
        "match_source_records",
    ):
        if table in tables:
            op.drop_table(table)
    columns = _columns("data_ingestion_runs")
    if "preview_payload" in columns:
        op.drop_column("data_ingestion_runs", "preview_payload")
    if "import_metrics" in columns:
        op.drop_column("data_ingestion_runs", "import_metrics")
