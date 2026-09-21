"""Add tactic, Bonus Players, bench roles, Tactical Subs and their triggers.

DRAFT built from the v1.0 DDL plus ARCHITECTURE.md. Verify against the live
schema (pg_dump --schema-only) before running. Requires PostgreSQL 12+
(generated columns).

Bench layout in starting_xi (position_slot):
    1-11  starters
    12    backup GK          -> role 'auto_gk'
    13    outfield Auto Sub  -> role 'auto_outfield'
    14-15 Tactical Subs      -> role 'tactical'
'role' is GENERATED from position_slot so the two can never disagree.
Whether slot 12 really holds a goalkeeper is checked in the application,
because positions live in ml.players.

Revision ID: d58b3f10a7c2
Revises: c41a9e27d06b
Create Date: 2026-09-19
"""
from alembic import op

revision = "d58b3f10a7c2"
down_revision = "c41a9e27d06b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # gw_selections: the weekly tactic. NOT NULL with no default works
    # because the previous revision emptied the table.
    # ------------------------------------------------------------------
    # DEFAULT 'balanced' then DROP DEFAULT: the column is NOT NULL, and
    # adding a NOT NULL column with no default only works on an empty table.
    # The previous revision does empty gw_selections, but relying on that
    # made this revision silently order-dependent -- it would fail outright
    # if the truncate were ever skipped, guarded out, or run separately. The
    # default backfills whatever is there; dropping it immediately keeps the
    # original intent that every future row must state its tactic explicitly.
    op.execute("""
        ALTER TABLE gw_selections
            ADD COLUMN tactic VARCHAR(10) NOT NULL DEFAULT 'balanced'
                CONSTRAINT ck_gw_selections_tactic
                CHECK (tactic IN ('attack', 'defence', 'balanced'))
    """)
    op.execute("ALTER TABLE gw_selections ALTER COLUMN tactic DROP DEFAULT")

    # ------------------------------------------------------------------
    # starting_xi: generated role, Bonus flag.
    #
    # The slot range is NOT touched here. Phase 0 verified against the live
    # schema that starting_xi_position_slot_check is ALREADY
    # CHECK (position_slot >= 1 AND position_slot <= 15) -- the bench already
    # lives at slots 12-15. The draft previously dropped and re-added it as
    # ck_starting_xi_slot, believing the live check was 1-11. That was a
    # rename of an equivalent constraint, doing nothing but obscuring the
    # fact that nothing needed widening. Left exactly as it is.
    # ------------------------------------------------------------------
    op.execute("""
        ALTER TABLE starting_xi
            ADD COLUMN is_bonus BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN role TEXT GENERATED ALWAYS AS (
                CASE
                    WHEN position_slot <= 11 THEN 'starter'
                    WHEN position_slot = 12  THEN 'auto_gk'
                    WHEN position_slot = 13  THEN 'auto_outfield'
                    ELSE 'tactical'
                END
            ) STORED
    """)
    op.execute("""
        ALTER TABLE starting_xi
            ADD CONSTRAINT ck_starting_xi_bonus_is_starter
                CHECK (NOT is_bonus OR position_slot <= 11),
            ADD CONSTRAINT uq_starting_xi_sel_slot
                UNIQUE (gw_selection_id, position_slot)
    """)

    # ------------------------------------------------------------------
    # tactical_swaps: at most 2 rows per selection, because player_in must
    # be a 'tactical' bench slot and there are only two of them.
    # An unused Tactical Sub (no swap row) is currently allowed.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE tactical_swaps (
            id              SERIAL   PRIMARY KEY,
            gw_selection_id INTEGER  NOT NULL REFERENCES gw_selections(id) ON DELETE CASCADE,
            player_out_id   INTEGER  NOT NULL,
            player_in_id    INTEGER  NOT NULL,
            CONSTRAINT ck_swaps_distinct   CHECK (player_out_id <> player_in_id),
            CONSTRAINT uq_swaps_sel_out    UNIQUE (gw_selection_id, player_out_id),
            CONSTRAINT uq_swaps_sel_in     UNIQUE (gw_selection_id, player_in_id)
        )
    """)

    # ------------------------------------------------------------------
    # Trigger 1: exactly 2 Bonus Players per selection (checked at COMMIT,
    # so the app can delete-and-reinsert the XI in one transaction).
    # Skipped when the selection has no rows (e.g. cascade delete).
    # ------------------------------------------------------------------
    op.execute("""
        CREATE OR REPLACE FUNCTION enforce_bonus_count_fn()
        RETURNS TRIGGER AS $$
        DECLARE
            sel_id  INTEGER;
            bonus_n INTEGER;
            total_n INTEGER;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                sel_id := OLD.gw_selection_id;
            ELSE
                sel_id := NEW.gw_selection_id;
            END IF;

            SELECT COUNT(*) FILTER (WHERE is_bonus), COUNT(*)
              INTO bonus_n, total_n
              FROM starting_xi
             WHERE gw_selection_id = sel_id;

            IF total_n > 0 AND bonus_n <> 2 THEN
                RAISE EXCEPTION 'A selection needs exactly 2 Bonus Players (found %): gw_selection_id=%',
                    bonus_n, sel_id;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER enforce_bonus_count
            AFTER INSERT OR UPDATE OR DELETE ON starting_xi
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION enforce_bonus_count_fn()
    """)

    # ------------------------------------------------------------------
    # Trigger 2: a swap's outgoing player must be a non-Bonus starter and
    # its incoming player must be a Tactical Sub on the same selection.
    # Fires on swap INSERT/UPDATE only: if the XI changes afterwards, the
    # app must delete and recreate the swaps in the same transaction.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE OR REPLACE FUNCTION enforce_swap_refs_fn()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM starting_xi
                 WHERE gw_selection_id = NEW.gw_selection_id
                   AND player_id = NEW.player_out_id
                   AND role = 'starter'
                   AND is_bonus = FALSE
            ) THEN
                RAISE EXCEPTION 'Tactical swap: outgoing player % must be a non-Bonus starter', NEW.player_out_id;
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM starting_xi
                 WHERE gw_selection_id = NEW.gw_selection_id
                   AND player_id = NEW.player_in_id
                   AND role = 'tactical'
            ) THEN
                RAISE EXCEPTION 'Tactical swap: incoming player % must be a Tactical Sub', NEW.player_in_id;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER enforce_swap_refs
            AFTER INSERT OR UPDATE ON tactical_swaps
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION enforce_swap_refs_fn()
    """)

    # ------------------------------------------------------------------
    # Trigger 3: child rows cannot change once the selection is locked.
    # (v1.0 only protected gw_selections itself.) A missing parent row is
    # treated as unlocked so ON DELETE CASCADE keeps working.
    # ------------------------------------------------------------------
    op.execute("""
        CREATE OR REPLACE FUNCTION enforce_child_lock_fn()
        RETURNS TRIGGER AS $$
        DECLARE
            sel_id INTEGER;
            locked BOOLEAN;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                sel_id := OLD.gw_selection_id;
            ELSE
                sel_id := NEW.gw_selection_id;
            END IF;

            SELECT is_locked INTO locked FROM gw_selections WHERE id = sel_id;
            IF locked IS TRUE THEN
                RAISE EXCEPTION 'Selection is locked and cannot be modified: gw_selection_id=%', sel_id;
            END IF;

            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    for table in ("starting_xi", "tactical_swaps"):
        op.execute(f"""
            CREATE TRIGGER enforce_{table}_lock
                BEFORE INSERT OR UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION enforce_child_lock_fn()
        """)

    # ------------------------------------------------------------------
    # gw_scores. Convention going forward:
    #   raw_points   = General Points (XI + swaps + Auto Subs)
    #   final_points = raw_points + tactical_points + sub_bonus
    #   total_points = final_points (no hits any more)
    #
    # rules_version is NOT touched. It already exists as smallint NOT NULL
    # (added by c9a04e7b53d1; live values are 1 and 2). The draft previously
    # carried `ADD COLUMN IF NOT EXISTS rules_version VARCHAR(20)`, which was
    # a silent no-op against that column -- the migration would pass and the
    # scorer would then fail at runtime trying to store a string in a
    # smallint. The version stays an integer and the next generation is 3,
    # set in Shared/rules.py (CURRENT_RULES_VERSION) in a later phase, not
    # here: this revision changes schema, not rules.
    # ------------------------------------------------------------------
    op.execute("""
        ALTER TABLE gw_scores
            ADD COLUMN tactical_points SMALLINT NOT NULL DEFAULT 0,
            ADD COLUMN sub_bonus       SMALLINT NOT NULL DEFAULT 0
    """)

    # ------------------------------------------------------------------
    # NOTHING in the `ml` schema is changed by this revision, on purpose.
    # ml.player_gw_stats also feeds the ML pipeline, and the game layer only
    # ever READS it. Columns the new scorer will read already exist:
    #   creativity              (ICT input; verify with \d ml.player_gw_stats)
    #   defensive_contributions (plural, added by migration f6b8d2c91a44)
    # ------------------------------------------------------------------


def downgrade() -> None:
    """Restores the structures this revision added, and nothing else.

    gw_scores.rules_version is untouched because upgrade() no longer touches
    it either -- it predates this revision entirely.

    The slot range is likewise untouched. An earlier draft deleted every
    starting_xi row above slot 11 and restored a 1-11 check here; both were
    wrong. The live constraint was already 1-15 before this revision ran, so
    "restoring" 1-11 would impose a state the database never had and destroy
    the bench on the way. A downgrade returns the schema to its previous
    shape, which is 1-15. Never touches the ml schema.
    """
    op.execute("ALTER TABLE gw_scores DROP COLUMN IF EXISTS sub_bonus")
    op.execute("ALTER TABLE gw_scores DROP COLUMN IF EXISTS tactical_points")

    for table in ("tactical_swaps", "starting_xi"):
        op.execute(f"DROP TRIGGER IF EXISTS enforce_{table}_lock ON {table}")
    op.execute("DROP FUNCTION IF EXISTS enforce_child_lock_fn()")

    op.execute("DROP TRIGGER IF EXISTS enforce_swap_refs ON tactical_swaps")
    op.execute("DROP FUNCTION IF EXISTS enforce_swap_refs_fn()")
    op.execute("DROP TABLE IF EXISTS tactical_swaps")

    op.execute("DROP TRIGGER IF EXISTS enforce_bonus_count ON starting_xi")
    op.execute("DROP FUNCTION IF EXISTS enforce_bonus_count_fn()")

    # Bench rows are kept: slots 12-15 were legal before this revision and
    # remain legal after it is undone.
    op.execute("ALTER TABLE starting_xi DROP CONSTRAINT IF EXISTS uq_starting_xi_sel_slot")
    op.execute("ALTER TABLE starting_xi DROP CONSTRAINT IF EXISTS ck_starting_xi_bonus_is_starter")
    op.execute("ALTER TABLE starting_xi DROP COLUMN IF EXISTS role")
    op.execute("ALTER TABLE starting_xi DROP COLUMN IF EXISTS is_bonus")

    op.execute("ALTER TABLE gw_selections DROP COLUMN IF EXISTS tactic")
