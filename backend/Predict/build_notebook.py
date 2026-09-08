"""Assembles and executes predict_gameweek.ipynb. Not part of the pipeline.

The notebook it writes is BUILD OUTPUT and is gitignored -- a fresh clone
will not have it. Run this from backend/Predict/ (paths below resolve
against the working directory) with the database reachable, since every
cell is executed against real data before the file is written.

It was previously checked in beside this script and drifted: the
db_utils consolidation changed an import the notebook depended on, this
generator was updated, the generated file was not, and nothing in CI
executes it -- so it stayed broken until it was opened by hand. Treat
the notebook as disposable and regenerate it after any change here, to
feature_builder.py, or to model.json.

EVERYTHING BELOW LIVES INSIDE main(), guarded by `if __name__ ==
"__main__"` at the bottom -- deliberately, not by default. This used to
be top-level module code, which meant merely IMPORTING this module (not
running it as a script) executed the whole notebook against the real
database and wrote a file as a side effect. Confirmed by hand: importing
it from the wrong cwd (anything other than backend/Predict/) made a
notebook cell's own sys.path.insert(0, Path.cwd().parent / ...) resolve
against the wrong directory and fail with a ModuleNotFoundError for
feature_builder that had nothing to do with a missing dependency --
purely an import-time side effect firing in an unexpected working
directory. Wrapping it in main() makes `import Predict.build_notebook`
inert; only running it directly (`python build_notebook.py`) builds and
executes the notebook.

NOTE FOR ANY FUTURE EDIT: the content INSIDE each triple-quoted string
below is the SOURCE CODE OF A NOTEBOOK CELL, handed to a real Python
kernel by nbclient -- it must stay flush at column 0, not indented to
match this function's own nesting, or the generated cell is a
SyntaxError the moment it runs ("unexpected indent" on its first line).
Only the surrounding cells.append(...)/nb.../client... statements are
indented for being inside main().
"""
import nbformat as nbf
from nbclient import NotebookClient


def main() -> None:
    nb = nbf.v4.new_notebook()
    cells = []

    cells.append(nbf.v4.new_markdown_cell(
"""# Predict GW4 and check against real results

Loads the GW4 feature DataFrame from `Feature_engineering.feature_builder`,
runs it through the trained xgboost model, and compares
`predicted_points` against the actual GW4 `total_points` already sitting
in `ml.player_gw_stats` — our first genuine out-of-sample accuracy check.

Read-only end to end: no writes to the database, no changes to `model.pkl`."""
    ))

    cells.append(nbf.v4.new_code_cell(
"""import sys
from pathlib import Path

# Run from Predict/, so cwd().parent is backend/ -- needed for Shared.db_utils,
# which replaced the five per-package db_utils.py copies.
sys.path.insert(0, str(Path.cwd().parent))
sys.path.insert(0, str(Path.cwd().parent / "Feature_engineering"))

import json
import numpy as np
import pandas as pd
import xgboost as xgb
from sqlalchemy import text

from Shared.db_utils import get_engine
from feature_builder import build_features, FEATURE_COLS

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 30)

SEASON = "2025-26"
TARGET_GW = 4

engine = get_engine()
"""
    ))

    cells.append(nbf.v4.new_markdown_cell("## 1. Build features for the target gameweek"))

    cells.append(nbf.v4.new_code_cell(
"""features = build_features(engine, SEASON, TARGET_GW)
print("features shape:", features.shape)
features.head()"""
    ))

    cells.append(nbf.v4.new_markdown_cell(
"""## 2. Load the trained model

**Note:** the training notebook (`FPL_Model_1.ipynb`) saved the model two
ways: `pickle.dump(best_xgb, f)` -> `model.pkl`, and
`best_xgb.get_booster().save_model('model.json')` -> a native xgboost JSON
booster dump. The pickle is not portable across xgboost versions/builds --
it fails under this venv's xgboost 3.3.0 with `XGBoostError: input stream
corrupted` (confirmed identical, byte-for-byte, across three separate
re-saves/re-downloads of the .pkl). The JSON dump has no such issue, so
`backend/Data/model.json` (feature names verified to match `feature_cols`
exactly) is the canonical model artifact for this project -- load that
directly rather than touching the pickle at all."""
    ))

    cells.append(nbf.v4.new_code_cell(
"""MODEL_JSON = Path.cwd().parent / "Data" / "model.json"

booster = xgb.Booster()
booster.load_model(str(MODEL_JSON))

print("Model loaded from:", MODEL_JSON)
print("Booster feature_names match FEATURE_COLS order:", booster.feature_names == FEATURE_COLS)"""
    ))

    cells.append(nbf.v4.new_markdown_cell("## 3. Predict"))

    cells.append(nbf.v4.new_code_cell(
"""dmatrix = xgb.DMatrix(features[FEATURE_COLS], feature_names=FEATURE_COLS, missing=np.nan)
predicted = booster.predict(dmatrix)

predictions = pd.DataFrame({"predicted_points": predicted}, index=features.index)
predictions.head()"""
    ))

    cells.append(nbf.v4.new_markdown_cell("## 4. Pull real GW4 results and compare"))

    cells.append(nbf.v4.new_code_cell(
"""actuals = pd.read_sql(
    text(\"\"\"
        SELECT player_id, total_points AS actual_points
        FROM ml.player_gw_stats
        WHERE season = :season AND gameweek = :gw
    \"\"\"),
    engine,
    params={"season": SEASON, "gw": TARGET_GW},
).set_index("player_id")

names = pd.read_sql(
    text("SELECT id AS player_id, web_name FROM ml.players WHERE season = :season"),
    engine,
    params={"season": SEASON},
).set_index("player_id")

compare = names.join(predictions).join(actuals, how="inner")
print("players with both a prediction and a real GW4 result:", len(compare))
compare.head()"""
    ))

    cells.append(nbf.v4.new_markdown_cell(
"""## 5. Accuracy check

Compare against the validation metrics recorded in `model_metadata.json`
(xgboost_v1: RMSE 2.0156, MAE 0.9842) as a sanity bound -- this is a single
live gameweek, not the validation set, so some drift is expected, especially
given the ~5.9% of feature importance (FBref features) we can't populate
yet and the 129 players with no rolling history this gameweek."""
    ))

    cells.append(nbf.v4.new_code_cell(
"""compare_scored = compare.dropna(subset=["predicted_points", "actual_points"])
error = compare_scored["predicted_points"] - compare_scored["actual_points"]

mae = error.abs().mean()
rmse = np.sqrt((error ** 2).mean())
within_2 = (error.abs() <= 2).mean() * 100
within_4 = (error.abs() <= 4).mean() * 100

print(f"n = {len(compare_scored)}")
print(f"MAE:  {mae:.4f}   (val MAE was 0.9842)")
print(f"RMSE: {rmse:.4f}   (val RMSE was 2.0156)")
print(f"within 2 pts: {within_2:.1f}%   (val was 85.5%)")
print(f"within 4 pts: {within_4:.1f}%   (val was 94.3%)")"""
    ))

    cells.append(nbf.v4.new_markdown_cell(
"""### 5b. A real gotcha: rows with zero history

XGBoost's `missing=nan` handling routes an all-NaN row down a fixed
default branch at every split -- it is not a "give up, predict the
average" signal, it's just whichever direction each tree happened to
default to at training time. Players who joined after GW1-3 (transfers,
new signings) but did play in GW4 have every rolling/price/ownership
feature as NaN, and the model still emits a confident-looking, near-identical
prediction (~7.5 pts) for all of them regardless of who they are. Splitting
the error by whether a player had any history at all shows how much this
drags down the headline numbers above."""
    ))

    cells.append(nbf.v4.new_code_cell(
"""has_history = compare_scored.index.map(lambda pid: pd.notna(features.loc[pid, "pts_rolling_3gw"]))

for label, mask in [("has >=1 prior GW", has_history), ("zero prior history", ~has_history)]:
    sub = compare_scored[mask]
    err = sub["predicted_points"] - sub["actual_points"]
    print(f"{label:22s} n={len(sub):4d}  MAE={err.abs().mean():.4f}  RMSE={np.sqrt((err ** 2).mean()):.4f}")"""
    ))

    cells.append(nbf.v4.new_markdown_cell("## 6. Top 20 predicted, with actual GW4 result alongside"))

    cells.append(nbf.v4.new_code_cell(
"""compare["error"] = compare["predicted_points"] - compare["actual_points"]
compare.sort_values("predicted_points", ascending=False).head(20)"""
    ))

    cells.append(nbf.v4.new_markdown_cell("## 7. Biggest misses -- worth a look before trusting this further"))

    cells.append(nbf.v4.new_code_cell(
"""compare_scored.assign(
    web_name=compare.loc[compare_scored.index, "web_name"],
    error=error,
).reindex(columns=["web_name", "predicted_points", "actual_points", "error"]).sort_values(
    "error", key=lambda s: s.abs(), ascending=False
).head(15)"""
    ))

    cells.append(nbf.v4.new_markdown_cell(
"""## 8. Confidence flags and tiers (`tier_builder.build_tiers`)

Two problems with using `predicted_points` directly, both visible above:
- The flat ~7.5 pt prediction for players with zero prior history (section
  5b) can outrank real in-form players in a naive "top predicted" sort.
- The model has no idea whether a player is actually going to play --
  `ml.players.status` does.

`build_tiers` only assigns a percentile tier (Elite/Strong/Average/Weak) to
players who are both known (`has_history`) and available (`status == 'a'`).
Everyone else gets a status label instead of a number -- read-only, no
writes, no changes to `feature_builder.py` or `model.json`."""
    ))

    cells.append(nbf.v4.new_code_cell(
"""from tier_builder import build_tiers

tiers = build_tiers(engine, SEASON, features, predictions)
tiers.head()"""
    ))

    cells.append(nbf.v4.new_markdown_cell("### 8a. Tier distribution"))

    cells.append(nbf.v4.new_code_cell(
"""tiers["tier_or_label"].value_counts()"""
    ))

    cells.append(nbf.v4.new_markdown_cell(
"""### 8b. The flat-prediction cluster now gets labeled, not ranked

Traoré, Cuiabano, Savona (and Hincapié, one of the zero-history players we
spot-checked against real FPL data earlier) all had the same ~7.5 pt flat
prediction. They should now show `"New/Insufficient Data"` instead of a
tier."""
    ))

    cells.append(nbf.v4.new_code_cell(
"""flat_prediction_players = [703, 652, 651, 35]  # Traore, Cuiabano, Savona, Hincapie
tiers[tiers["player_id"].isin(flat_prediction_players)]"""
    ))

    cells.append(nbf.v4.new_markdown_cell(
"""### 8c. Corrected top 10 -- tiered players only, ranked by predicted_points

Compare this against section 6's top 20 above, which was dominated by the
flat-prediction cluster. This one should read like real in-form players."""
    ))

    cells.append(nbf.v4.new_code_cell(
"""tiered_only = tiers[tiers["tier_or_label"].isin(["Elite", "Strong", "Average", "Weak"])]
tiered_only.sort_values("predicted_points", ascending=False).head(10)"""
    ))

    nb["cells"] = cells

    client = NotebookClient(nb, timeout=120, kernel_name="python3")
    client.execute()

    with open("predict_gameweek.ipynb", "w", encoding="utf-8") as f:
        nbf.write(nb, f)

    print("done")


if __name__ == "__main__":
    main()
