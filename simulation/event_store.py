"""Durable simulation event inbox with provider-event idempotency."""

from __future__ import annotations

import json

from sqlalchemy import text

from .events import MatchEvent, MatchEventType


CREATE_EVENT_STORE_SQL = """
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


INSERT_EVENT_SQL = text(
    """
    INSERT INTO simulation.football_events (
        provider_event_id, fixture_id, season, gameweek, event_type,
        player_fpl_id, related_player_fpl_id, minute, payload
    )
    VALUES (
        :provider_event_id, :fixture_id, :season, :gameweek, :event_type,
        :player_fpl_id, :related_player_fpl_id, :minute, CAST(:payload AS jsonb)
    )
    ON CONFLICT (provider_event_id) DO NOTHING
    RETURNING provider_event_id
    """
)

LOAD_EVENTS_SQL = text(
    """
    SELECT provider_event_id, event_type, player_fpl_id, related_player_fpl_id, minute, payload
    FROM simulation.football_events
    WHERE fixture_id = :fixture_id
    ORDER BY minute, id
    """
)

MARK_PROCESSED_SQL = text(
    """
    UPDATE simulation.football_events
    SET processed_at = COALESCE(processed_at, now())
    WHERE fixture_id = :fixture_id AND processed_at IS NULL
    """
)


def ensure_event_store(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(CREATE_EVENT_STORE_SQL))


def insert_events(engine, fixture_id: int, season: str, gameweek: int, events: list[MatchEvent]) -> tuple[list[str], list[str]]:
    ensure_event_store(engine)
    inserted, duplicates = [], []
    with engine.begin() as conn:
        for event in events:
            row = conn.execute(
                INSERT_EVENT_SQL,
                {
                    "provider_event_id": event.provider_event_id,
                    "fixture_id": fixture_id,
                    "season": season,
                    "gameweek": gameweek,
                    "event_type": event.event_type.value,
                    "player_fpl_id": event.player_id,
                    "related_player_fpl_id": event.related_player_id,
                    "minute": event.minute,
                    "payload": json.dumps(event.metadata),
                },
            ).first()
            if row:
                inserted.append(event.provider_event_id)
            else:
                duplicates.append(event.provider_event_id)
    return inserted, duplicates


def load_fixture_events(engine, fixture_id: int) -> list[MatchEvent]:
    ensure_event_store(engine)
    with engine.connect() as conn:
        rows = conn.execute(LOAD_EVENTS_SQL, {"fixture_id": fixture_id}).all()
    return [
        MatchEvent(
            event_type=MatchEventType(row.event_type),
            player_id=row.player_fpl_id,
            related_player_id=row.related_player_fpl_id,
            minute=row.minute,
            provider_event_id=row.provider_event_id,
            metadata=dict(row.payload or {}),
        )
        for row in rows
    ]


def mark_fixture_events_processed(engine, fixture_id: int) -> None:
    ensure_event_store(engine)
    with engine.begin() as conn:
        conn.execute(MARK_PROCESSED_SQL, {"fixture_id": fixture_id})
