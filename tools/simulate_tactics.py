#!/usr/bin/env python3
"""
Tactical Points balance simulation.

Reads the community FPL archive (vaastav/Fantasy-Premier-League, merged_gw.csv per
season) straight from GitHub. It never touches your Postgres database.

For every Gameweek and every tactic it picks the 2 Bonus Players a manager could
plausibly choose, scores their Tactical Points, then compares the three tactics.

Run:   python simulate_tactics.py
       python simulate_tactics.py --seasons 2024-25 2025-26
Needs: pip install pandas numpy

ALL TUNABLE VALUES ARE IN THE CONFIG BLOCK BELOW. Values are placeholders.
"""
import argparse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# CONFIG: edit these, re-run, compare
# ----------------------------------------------------------------------------
SEASONS = ["2022-23", "2023-24", "2024-25", "2025-26"]
URL = ("https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/"
       "master/data/{season}/gws/merged_gw.csv")

TACTICAL = {
    "attack":   {"goal": 3, "assist": 2},
    # Tiers are cumulative: the HIGHEST tier reached counts (e.g. 10 actions = +2, not +1 +2).
    # DC only exists from 2025-26. Archive column is `defensive_contribution` (singular);
    # the database column is `defensive_contributions` (plural).
    "defence":  {"clean_sheet": 2, "dc_tiers": [(8, 2), (10, 3)]},
    "balanced": {"goal_or_assist": 1, "creativity_tiers": [(20, 1), (40, 3)]},
}

# Which players can a manager realistically hold? The N most-owned per position.
POOL_SIZE = {"attack": 10, "defence": 15, "balanced": 15}
FORM_WINDOW = 5                        # trailing Gameweeks used by the "form" manager
MIN_GW = 4                             # skip early Gameweeks with too little form data
POSITION = {"attack": "FWD", "defence": "DEF", "balanced": "MID"}
TACTICS = ["attack", "defence", "balanced"]

GENERAL = {  # General Points table (validated: matches archive totals minus bonus)
    "goal": {"GK": 10, "DEF": 6, "MID": 5, "FWD": 4},
    "cs": {"GK": 4, "DEF": 4, "MID": 1, "FWD": 0},
}
# ----------------------------------------------------------------------------


def load_season(season: str, cache: Path) -> pd.DataFrame:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{season}_merged_gw.csv"
    if not path.exists():
        print(f"  downloading {season} ...")
        urllib.request.urlretrieve(URL.format(season=season), path)
    df = pd.read_csv(path, low_memory=False, on_bad_lines="skip")
    df = df[df["position"].isin(["GK", "DEF", "MID", "FWD"])].copy()  # drops manager rows
    df["season"] = season
    df["has_dc"] = "defensive_contribution" in df.columns
    if "defensive_contribution" not in df.columns:
        df["defensive_contribution"] = 0
    df["defensive_contribution"] = df["defensive_contribution"].fillna(0)
    return df


def general_points(df: pd.DataFrame) -> pd.Series:
    """General Points per row. Used only to validate the data, not for the comparison."""
    m, pos = df["minutes"], df["position"]
    p = np.where(m >= 60, 2, np.where(m > 0, 1, 0))
    p = p + df["goals_scored"] * pos.map(GENERAL["goal"]) + df["assists"] * 3
    p = p + np.where(m >= 60, df["clean_sheets"] * pos.map(GENERAL["cs"]), 0)
    p = p - np.where(pos.isin(["GK", "DEF"]), df["goals_conceded"] // 2, 0)
    p = p - df["yellow_cards"] - 3 * df["red_cards"] - 2 * df["own_goals"] - 2 * df["penalties_missed"]
    p = p + df["saves"] // 3 + 5 * df["penalties_saved"]
    thr = np.where(pos == "DEF", 10, 12)
    return p + np.where(df["has_dc"] & (df["defensive_contribution"] >= thr), 2, 0)


def tier_points(values: pd.Series, tiers) -> np.ndarray:
    """Points for the highest tier reached. tiers = [(minimum, cumulative_points), ...]."""
    out = np.zeros(len(values))
    for minimum, pts in sorted(tiers):
        out = np.where(values >= minimum, pts, out)
    return out


def tactical_points(df: pd.DataFrame) -> pd.DataFrame:
    """Adds one Tactical Points column per tactic (per fixture row)."""
    a, d, b = TACTICAL["attack"], TACTICAL["defence"], TACTICAL["balanced"]
    played = df["minutes"] > 0
    cs60 = (df["minutes"] >= 60) & (df["clean_sheets"] > 0)
    out = df.copy()
    out["attack"] = np.where((df["position"] == "FWD") & played,
                             df["goals_scored"] * a["goal"] + df["assists"] * a["assist"], 0)
    out["defence"] = np.where(
        (df["position"] == "DEF") & played,
        cs60 * d["clean_sheet"]
        + np.where(df["has_dc"], tier_points(df["defensive_contribution"], d["dc_tiers"]), 0),
        0)
    out["balanced"] = np.where(
        (df["position"] == "MID") & played,
        (df["goals_scored"] + df["assists"]) * b["goal_or_assist"]
        + tier_points(df["creativity"], b["creativity_tiers"]),
        0)
    return out


def per_gameweek(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse fixtures to one row per player per Gameweek (double GWs are summed)."""
    g = df.groupby(["season", "GW", "element"], as_index=False).agg(
        position=("position", "first"), selected=("selected", "max"),
        attack=("attack", "sum"), defence=("defence", "sum"), balanced=("balanced", "sum"),
        has_dc=("has_dc", "first"))
    g = g.sort_values(["season", "element", "GW"])
    for t in TACTICS:  # trailing mean of PRIOR Gameweeks only (no look-ahead)
        g[f"form_{t}"] = (g.groupby(["season", "element"])[t]
                          .transform(lambda s: s.shift(1).rolling(FORM_WINDOW, min_periods=2).mean()))
    return g


def simulate(g: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (season, gw), day in g.groupby(["season", "GW"]):
        if gw < MIN_GW:
            continue
        for t in TACTICS:
            pool = day[day["position"] == POSITION[t]].nlargest(POOL_SIZE[t], "selected")
            if len(pool) < 2:
                continue
            ranked = pool.dropna(subset=[f"form_{t}"])
            rows.append({
                "season": season, "gw": gw, "tactic": t,
                "random": 2 * pool[t].mean(),                                   # expected value
                "form": ranked.nlargest(2, f"form_{t}")[t].sum() if len(ranked) >= 2 else np.nan,
                "oracle": pool.nlargest(2, t)[t].sum(),                          # hindsight ceiling
                "has_dc": bool(pool["has_dc"].iloc[0]),
            })
    return pd.DataFrame(rows)


def report(res: pd.DataFrame, title: str) -> None:
    print(f"\n=== {title} ===")
    for policy in ["random", "form", "oracle"]:
        wide = res.pivot_table(index=["season", "gw"], columns="tactic", values=policy).dropna()
        if wide.empty:
            continue
        means, stds = wide.mean(), wide.std()
        # share of Gameweeks each tactic is best (ties split equally)
        best = wide.eq(wide.max(axis=1), axis=0)
        win = best.div(best.sum(axis=1), axis=0).mean()
        spread = (means.max() - means.min()) / means.mean() * 100
        print(f"\n  Manager: {policy:<7} ({len(wide)} Gameweeks)   mean spread: {spread:.0f}%")
        print(f"  {'tactic':<10}{'mean':>7}{'std':>7}{'win share':>11}")
        for t in TACTICS:
            print(f"  {t:<10}{means[t]:>7.2f}{stds[t]:>7.2f}{win[t]:>10.0%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", nargs="+", default=SEASONS)
    ap.add_argument("--cache", default="archive_cache")
    ap.add_argument("--out", default="tactical_sim_results.csv")
    args = ap.parse_args()

    print("Loading seasons ...")
    raw = pd.concat([load_season(s, Path(args.cache)) for s in args.seasons], ignore_index=True)

    ok = (general_points(raw) == raw["total_points"] - raw["bonus"])
    print(f"Data check: recomputed General Points match the archive on {ok.mean():.1%} of rows")

    res = simulate(per_gameweek(tactical_points(raw)))
    res.to_csv(args.out, index=False)

    report(res, "ALL SEASONS (Defence lacks its Defensive Contribution event before 2025-26)")
    dc = res[res["has_dc"]]
    if not dc.empty:
        report(dc, "2025-26 ONLY (all events available: the fair comparison for Defence)")
    print(f"\nPer-Gameweek results written to {args.out}")


if __name__ == "__main__":
    main()
