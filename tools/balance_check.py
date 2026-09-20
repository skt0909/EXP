#!/usr/bin/env python3
"""
Balance acceptance check for the three tactics.

Uses the rule values in the CONFIG block of simulate_tactics.py (edit them there,
then re-run this file). It reads the archive from GitHub; it never touches Postgres.

Run:   python balance_check.py
Needs: pip install pandas numpy   (simulate_tactics.py must be in the same folder)

Only 2025-26 is used because Defensive Contribution only exists from that season.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import simulate_tactics as S

# ----------------------------------------------------------------------------
# ACCEPTANCE SPEC (tune the spec here, not the scoring rules)
# ----------------------------------------------------------------------------
SEASON = "2025-26"
BASE_POOL = {"attack": 10, "defence": 15, "balanced": 15}   # what simulate_tactics.py assumes
EQUAL_POOL = {"attack": 15, "defence": 15, "balanced": 15}  # robustness check

AVG_BAND = (3.7, 4.1)             # T1a: each form-based average, absolute
AVG_REL = 0.05                    # T1b: each average within +-5% of the mean of the three
SWING = {"attack": (0.80, 9.99), "defence": (0.65, 0.78), "balanced": (0.50, 0.65)}  # T2
MIN_SWING_GAP = 0.10              # T2b: adjacent tactics must differ by at least this
CASUAL_TARGET = 1.12              # T3: max/min casual average, target
CASUAL_LIMIT = 1.25               # T3: above this is a hard FAIL (structural limit for Defence)
MAX_WIN_SHARE = 0.40              # T4: no tactic best in more than this share of Gameweeks
MAX_THRESHOLDS = 6                # T5: clean sheet + all tier thresholds across Defence and Balanced
BOOTSTRAPS = 3000
# ----------------------------------------------------------------------------


def simulate(raw: pd.DataFrame, pool: dict) -> pd.DataFrame:
    S.POOL_SIZE = dict(pool)
    return S.simulate(S.per_gameweek(S.tactical_points(raw)))


def wide(res: pd.DataFrame, policy: str) -> pd.DataFrame:
    return res.pivot_table(index=["season", "gw"], columns="tactic", values=policy).dropna()


def summarise(w: pd.DataFrame):
    mean, swing = w.mean(), w.std() / w.mean()
    best = w.eq(w.max(axis=1), axis=0)
    win = best.div(best.sum(axis=1), axis=0).mean()
    return mean, swing, win


def status(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def main() -> None:
    raw = S.load_season(SEASON, Path("archive_cache"))
    T = S.TACTICS

    res = simulate(raw, BASE_POOL)
    f_mean, f_swing, f_win = summarise(wide(res, "form"))
    c_mean, _, _ = summarise(wide(res, "random"))
    res15 = simulate(raw, EQUAL_POOL)
    f_mean15, _, _ = summarise(wide(res15, "form"))
    c_mean15, _, _ = summarise(wide(res15, "random"))

    fmt = lambda s, p=2: " / ".join(f"{s[t]:.{p}f}" for t in T)
    print(f"Season {SEASON}, {len(wide(res, 'form'))} Gameweeks.  Order: Attack / Defence / Balanced\n")
    print(f"Form-based average : {fmt(f_mean)}")
    print(f"Relative swing     : {fmt(f_swing)}")
    print(f"Best-of-three share: {' / '.join(f'{f_win[t]:.0%}' for t in T)}")
    print(f"Casual average     : {fmt(c_mean)}")
    print(f"Equal pools (all 15): form {fmt(f_mean15)}  |  casual {fmt(c_mean15)}\n")

    d, b = S.TACTICAL["defence"], S.TACTICAL["balanced"]
    thresholds = 1 + len(d["dc_tiers"]) + len(b["creativity_tiers"])
    centre = f_mean.mean()
    swing_ok = all(SWING[t][0] <= f_swing[t] <= SWING[t][1] for t in T)
    gaps_ok = (f_swing["attack"] - f_swing["defence"] >= MIN_SWING_GAP
               and f_swing["defence"] - f_swing["balanced"] >= MIN_SWING_GAP)
    cas = c_mean.max() / c_mean.min()
    cas15 = c_mean15.max() / c_mean15.min()

    rows = [
        ("T1a", f"each average within {AVG_BAND[0]}-{AVG_BAND[1]}", all(AVG_BAND[0] <= f_mean[t] <= AVG_BAND[1] for t in T)),
        ("T1b", f"each average within +-{AVG_REL:.0%} of their mean ({centre:.2f})", all(abs(f_mean[t] / centre - 1) <= AVG_REL for t in T)),
        ("T2a", "swing inside each tactic's band", swing_ok),
        ("T2b", f"adjacent swings at least {MIN_SWING_GAP} apart", gaps_ok),
        ("T3a", f"casual max/min <= {CASUAL_TARGET} (now {cas:.2f})", cas <= CASUAL_TARGET),
        ("T3b", f"casual max/min <= {CASUAL_LIMIT} hard limit", cas <= CASUAL_LIMIT),
        ("T3c", f"casual max/min <= {CASUAL_LIMIT} with equal pools (now {cas15:.2f})", cas15 <= CASUAL_LIMIT),
        ("T4", f"no tactic best in more than {MAX_WIN_SHARE:.0%} of Gameweeks", f_win.max() <= MAX_WIN_SHARE),
        ("T5", f"at most {MAX_THRESHOLDS} thresholds (now {thresholds})", thresholds <= MAX_THRESHOLDS),
    ]
    print("Acceptance tests")
    for code, text, ok in rows:
        print(f"  {code:<4}{status(ok):<6}{text}")

    # How much can these point estimates be trusted? Resample the Gameweeks.
    w = wide(res, "form")
    rng = np.random.default_rng(7)
    means, swings = [], []
    for _ in range(BOOTSTRAPS):
        s = w.iloc[rng.integers(0, len(w), len(w))]
        means.append(s.mean())
        swings.append(s.std() / s.mean())
    means, swings = pd.DataFrame(means), pd.DataFrame(swings)
    print(f"\n90% bootstrap intervals ({len(w)} Gameweeks): how far the estimates could move")
    for t in T:
        print(f"  {t:<9} average {means[t].quantile(.05):.2f}-{means[t].quantile(.95):.2f}   "
              f"swing {swings[t].quantile(.05):.2f}-{swings[t].quantile(.95):.2f}")
    print("\nIf the intervals are wider than the spec bands, treat every result as PROVISIONAL "
          "and re-run after more Gameweeks.")


if __name__ == "__main__":
    sys.exit(main())
