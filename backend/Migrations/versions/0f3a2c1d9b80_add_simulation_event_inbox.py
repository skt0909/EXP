"""add simulation event inbox

Revision ID: 0f3a2c1d9b80
Revises: f6b8d2c91a44
Create Date: 2026-09-11 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0f3a2c1d9b80"
down_revision: Union[str, Sequence[str], None] = "f6b8d2c91a44"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE SCHEMA IF NOT EXISTS simulation;

        CREATE TABLE IF NOT EXISTS simulation.football_events (
            id bigserial PRIMARY KEY,
            provider_event_id varchar(255) NOT NULL,
            fixture_id integer NOT NULL REFERENCES ml.fixtures(id) ON DELETE CASCADE,
            season varchar(9) NOT NULL,
            gameweek smallint NOT NULL,
            event_type varchar(40) NOT NULL,
            player_fpl_id integer,
            related_player_fpl_id integer,
            minute smallint NOT NULL DEFAULT 0,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            processed_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_sim_football_events_provider_event UNIQUE (provider_event_id)
        );

        CREATE INDEX IF NOT EXISTS idx_sim_football_events_fixture
            ON simulation.football_events (fixture_id, minute, id);

        CREATE INDEX IF NOT EXISTS idx_sim_football_events_unprocessed
            ON simulation.football_events (fixture_id)
            WHERE processed_at IS NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS simulation.football_events;")
