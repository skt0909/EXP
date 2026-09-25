"""
backfill_predictions.py — write ml.ml_predictions for one gameweek, run by
hand instead of by Celery.

Replaces the (commented-out) run_ml_pipeline / schedule_predictions tasks
in Worker/tasks.py. Same three steps (build_features -> predict_points ->
build_tiers) and the same DELETE-then-INSERT write, since
ml.ml_predictions' immutability trigger forbids UPDATE. Run it on a dev
machine: a run peaks around 270 MB (xgboost + pandas), which the server's
long-lived worker should not carry. Point DATABASE_URL at the target
database (e.g. the server's Postgres through an SSH tunnel) to write there.

Dry run by default -- prints the tier distribution and writes nothing:
    python Predict/backfill_predictions.py --season 2026-27 --gw 7
Write it:
    python Predict/backfill_predictions.py --season 2026-27 --gw 7 --write

Features for gameweek N are built from gameweeks before N, so backfill N
only once N-1 has been played and its stats ingested.

--auto is the scheduled form (deploy/systemd/fpl-backfill.timer runs it
daily): it picks the next upcoming gameweek with no predictions
(prediction_scheduling.find_next_gameweek_needing_predictions) and writes
it, but only once the gameweek before it is complete -- every fixture
finished and its stats settled. Otherwise it exits without writing and
the next day's run tries again. It runs as its own short-lived process,
so the ~270 MB it peaks at is released the moment it exits; the Celery
worker never loads xgboost.
    python Predict/backfill_predictions.py --auto

/chat reads the latest backfilled tier per player (Context_assembler/
main.py's PREDICTIONS_QUERY), so a gameweek that is never backfilled
still gets the previous one's tiers, not "New/Insufficient Data".
"""

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "Feature_engineering"))
sys.path.insert(0, str(_ROOT / "Predict"))

from sqlalchemy import text

from Shared.db_utils import get_engine, safe_url
from feature_builder import build_features
from predictor import predict_points
from tier_builder import build_tiers
from prediction_scheduling import find_next_gameweek_needing_predictions

MODEL_VERSION = "xgboost_v1"  # must match Context_assembler/main.py's MODEL_VERSION

# Is the gameweek before the one being predicted over and settled? Its
# fixtures all finished, at least one stats row, and none still live
# (halftime/fulltime checkpoint rows are is_live=TRUE until the final one).
PREVIOUS_GAMEWEEK_STATE_QUERY = text(
    """
    SELECT
        (SELECT COUNT(*) FROM ml.fixtures
          WHERE season = :season AND gameweek = :gameweek) AS fixtures,
        (SELECT COUNT(*) FROM ml.fixtures
          WHERE season = :season AND gameweek = :gameweek AND finished) AS finished,
        (SELECT COUNT(*) FROM ml.player_gw_stats
          WHERE season = :season AND gameweek = :gameweek) AS stats_rows,
        (SELECT COUNT(*) FROM ml.player_gw_stats
          WHERE season = :season AND gameweek = :gameweek AND is_live) AS live_rows
    """
)


def previous_gameweek_not_ready(engine, season: str, gameweek: int) -> str | None:
    """Why gameweek-1 can't feed a prediction for gameweek yet, or None if it can."""
    if gameweek <= 1:
        return None
    with engine.connect() as conn:
        s = conn.execute(PREVIOUS_GAMEWEEK_STATE_QUERY, {"season": season, "gameweek": gameweek - 1}).one()
    prev = f"GW{gameweek - 1}"
    if s.fixtures == 0:
        return f"{prev} has no fixtures"
    if s.finished < s.fixtures:
        return f"{prev}: only {s.finished}/{s.fixtures} fixtures finished"
    if s.stats_rows == 0:
        return f"{prev} has no player stats"
    if s.live_rows:
        return f"{prev} still has {s.live_rows} live (unsettled) stats rows"
    return None


def backfill(engine, season: str, gameweek: int, write: bool) -> dict:
    features = build_features(engine, season, gameweek)
    predictions = predict_points(features)
    tiers = build_tiers(engine, season, features, predictions)

    rows = tiers[["player_id", "predicted_points", "tier_or_label"]].copy()
    rows["season"] = season
    rows["gameweek"] = gameweek
    rows["model_version"] = MODEL_VERSION

    if write:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM ml.ml_predictions "
                    "WHERE season = :season AND gameweek = :gameweek AND model_version = :mv"
                ),
                {"season": season, "gameweek": gameweek, "mv": MODEL_VERSION},
            )
            rows.to_sql("ml_predictions", conn, schema="ml", if_exists="append", index=False)

    return {
        "rows": len(rows),
        "written": write,
        "tier_distribution": tiers["tier_or_label"].value_counts().to_dict(),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--season", help="e.g. 2026-27")
    ap.add_argument("--gw", type=int, help="gameweek to write predictions for")
    ap.add_argument("--write", action="store_true", help="actually write (default is a dry run)")
    ap.add_argument("--auto", action="store_true",
                    help="pick the next gameweek needing predictions and write it once the one before is complete")
    args = ap.parse_args()
    if args.auto == bool(args.season or args.gw):
        ap.error("give either --auto, or --season and --gw")
    if not args.auto and not (args.season and args.gw):
        ap.error("--season and --gw go together")

    engine = get_engine()
    print(f"database: {safe_url()}")
    if args.auto:
        target = find_next_gameweek_needing_predictions(engine)
        if target is None:
            print("nothing to do: no upcoming gameweek is missing predictions")
            return
        args.season, args.gw = target
        reason = previous_gameweek_not_ready(engine, args.season, args.gw)
        if reason:
            print(f"skipping {args.season} GW{args.gw}: {reason} -- will retry on the next run")
            return
        args.write = True

    result = backfill(engine, args.season, args.gw, args.write)
    print(f"season={args.season} gameweek={args.gw} rows={result['rows']} "
          f"{'WRITTEN' if result['written'] else 'dry run, nothing written'}")
    for tier, n in sorted(result["tier_distribution"].items(), key=lambda kv: -kv[1]):
        print(f"  {tier}: {n}")


if __name__ == "__main__":
    main()
