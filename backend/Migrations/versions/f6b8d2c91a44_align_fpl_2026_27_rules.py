"""align core FPL 2026/27 rule storage

Revision ID: f6b8d2c91a44
Revises: e3a91b7c2d40
Create Date: 2026-08-29 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "f6b8d2c91a44"
down_revision: Union[str, Sequence[str], None] = "e3a91b7c2d40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
ALTER TABLE ml.players
    ADD COLUMN IF NOT EXISTS now_cost smallint;

UPDATE ml.players SET now_cost = cost_start WHERE now_cost IS NULL;

ALTER TABLE ml.player_gw_stats
    ADD COLUMN IF NOT EXISTS defensive_contributions smallint NOT NULL DEFAULT 0;

ALTER TABLE ml.player_gw_stats
    DROP CONSTRAINT IF EXISTS uq_pgws_player_season_gw;

DROP INDEX IF EXISTS ml.uq_pgws_player_season_gw_fixture;

CREATE UNIQUE INDEX uq_pgws_player_season_gw_fixture
    ON ml.player_gw_stats (player_id, season, gameweek, COALESCE(fixture_id, -1));

DROP TRIGGER IF EXISTS enforce_chip_limit ON public.chips;
DROP FUNCTION IF EXISTS public.enforce_chip_limit_fn();

CREATE FUNCTION public.enforce_chip_limit_fn() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    new_half integer;
BEGIN
    new_half := CASE WHEN NEW.gameweek_used <= 19 THEN 1 ELSE 2 END;

    IF EXISTS (
        SELECT 1 FROM chips c
        WHERE c.user_id = NEW.user_id
          AND c.season = NEW.season
          AND c.chip_type = NEW.chip_type
          AND (CASE WHEN c.gameweek_used <= 19 THEN 1 ELSE 2 END) = new_half
    ) THEN
        RAISE EXCEPTION 'Chip already used in this half-season: user_id=%, chip=%',
            NEW.user_id, NEW.chip_type;
    END IF;

    IF NEW.chip_type = 'free_hit' AND EXISTS (
        SELECT 1 FROM chips c
        WHERE c.user_id = NEW.user_id
          AND c.season = NEW.season
          AND c.chip_type = 'free_hit'
          AND abs(c.gameweek_used - NEW.gameweek_used) = 1
    ) THEN
        RAISE EXCEPTION 'Free Hit cannot be used in consecutive gameweeks: user_id=%',
            NEW.user_id;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER enforce_chip_limit
    BEFORE INSERT ON public.chips
    FOR EACH ROW EXECUTE FUNCTION public.enforce_chip_limit_fn();

DROP INDEX IF EXISTS public.uq_chips_user_season_type_restricted;

CREATE UNIQUE INDEX uq_chips_user_season_type_half
    ON public.chips (
        user_id,
        season,
        chip_type,
        (CASE WHEN gameweek_used <= 19 THEN 1 ELSE 2 END)
    );
"""


_DOWNGRADE_SQL = """
DROP INDEX IF EXISTS public.uq_chips_user_season_type_half;

DROP TRIGGER IF EXISTS enforce_chip_limit ON public.chips;
DROP FUNCTION IF EXISTS public.enforce_chip_limit_fn();

CREATE FUNCTION public.enforce_chip_limit_fn() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF NEW.chip_type IN ('bench_boost', 'triple_captain', 'free_hit') THEN
                IF EXISTS (
                    SELECT 1 FROM chips
                    WHERE user_id = NEW.user_id
                      AND season = NEW.season
                      AND chip_type = NEW.chip_type
                ) THEN
                    RAISE EXCEPTION 'Chip already used this season: user_id=%, chip=%',
                        NEW.user_id, NEW.chip_type;
                END IF;
            ELSIF NEW.chip_type = 'wildcard' THEN
                IF (
                    SELECT COUNT(*) FROM chips
                    WHERE user_id = NEW.user_id
                      AND season = NEW.season
                      AND chip_type = 'wildcard'
                ) >= 2 THEN
                    RAISE EXCEPTION 'Wildcard already used twice this season: user_id=%',
                        NEW.user_id;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE TRIGGER enforce_chip_limit
    BEFORE INSERT ON public.chips
    FOR EACH ROW EXECUTE FUNCTION public.enforce_chip_limit_fn();

CREATE UNIQUE INDEX uq_chips_user_season_type_restricted
    ON public.chips (user_id, season, chip_type)
    WHERE ((chip_type)::text <> 'wildcard'::text);

DROP INDEX IF EXISTS ml.uq_pgws_player_season_gw_fixture;

ALTER TABLE ml.player_gw_stats
    ADD CONSTRAINT uq_pgws_player_season_gw UNIQUE (player_id, season, gameweek);

ALTER TABLE ml.player_gw_stats
    DROP COLUMN IF EXISTS defensive_contributions;

ALTER TABLE ml.players
    DROP COLUMN IF EXISTS now_cost;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
