"""
scoring_rules.py — GET /scoring-rules, the published point values for both games.

WHY THIS EXISTS. The frontend's "How Points Work" screens previously restated
every constant in JavaScript. That is the same failure mode MatchDetailPage's
rules strip was written to avoid (it reads a contest's own summary precisely so
it cannot drift from dream11_scoring.py): a second copy of a number that nothing
forces to agree with the first. A manager reading "+3 for an assist" off a screen
that the scorer has since stopped agreeing with is a worse bug than a crash,
because nothing surfaces it.

So this endpoint serves the constants THEMSELVES -- imported from the modules
that apply them, never retyped:

    classic -> Shared.rules, used by Results/tactical_scoring.py's general_points
    dream11 -> Game_logic.dream11_scoring, used by calculate_dream11_points

Extracting scoring.py's remaining inline literals into Shared.rules was part of
building this; without it the endpoint would have been a third copy rather than
a single source. If you add a scoring rule, add it there and expose it here --
if it only exists as a literal in the scorer, this endpoint cannot tell anyone
about it.

WHAT THIS DOES NOT SERVE: labels, ordering, icons, section grouping, or prose.
Those are presentation and belong to whatever renders them -- the same values
appear on two screens that group them differently. This returns numbers and the
thresholds that qualify them, nothing else.

PUBLIC, like GET /players. It is the same reference data for every caller, with
nothing user-scoped in it, and the rules of the game are not a secret from
someone who has not signed in yet. Registered in test_auth_enforcement.py's
PUBLIC_ROUTES with that reasoning, so the route-coverage guard stays honest.
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from Shared import rules as classic
from Game_logic import dream11_scoring as d11

router = APIRouter()


class PositionPoints(BaseModel):
    """A value that depends on the scoring player's position."""

    GK: int
    DEF: int
    MID: int
    FWD: int


class TieredPoints(BaseModel):
    """A rule applied by integer division: `points` per `per` of the stat.

    Both sides matter to a reader. "-1 per 2 conceded" is not "-0.5 each":
    one goal costs nothing and three cost the same as two, which is only
    derivable if the divisor travels with the value.
    """

    points: int
    per: int


class AppearanceRules(BaseModel):
    """Dream11 has no appearance points at all, hence `full`/`partial` being
    absent there rather than zero -- zero would claim a rule exists and pays
    nothing, which is a different (and wrong) statement."""

    partial: int
    full: int
    full_minutes: int


class ClassicScoringRules(BaseModel):
    appearance: AppearanceRules
    goal: PositionPoints
    assist: int
    clean_sheet: PositionPoints
    clean_sheet_minutes: int
    defensive_contribution: int
    defensive_contribution_threshold: PositionPoints
    goals_conceded: TieredPoints
    saves: TieredPoints
    penalty_saved: int
    yellow_card: int
    red_card: int
    own_goal: int
    penalty_missed: int
    squad_size: int
    starting_xi_size: int
    bench_size: int
    max_per_club: int


class TacticBonusRules(BaseModel):
    eligible_position: str
    goal: int | None = None
    assist: int | None = None
    clean_sheet: int | None = None
    defensive_contribution_tiers: list[dict] = Field(default_factory=list)
    goal_or_assist: int | None = None
    creativity_tiers: list[dict] = Field(default_factory=list)


class TacticalScoringRules(BaseModel):
    rules_version: int
    tactics: dict[str, TacticBonusRules]
    bonus_player_count: int
    tactical_swap_limit: int
    auto_sub_slots: list[int]
    tactical_sub_slots: list[int]
    fixture_duration_minutes: int
    free_transfers_per_gameweek: int
    free_transfer_bank_cap: int
    squad_size: int
    starting_xi_size: int
    bench_size: int


class Dream11ScoringRules(BaseModel):
    goal: PositionPoints
    assist: int
    clean_sheet: PositionPoints
    clean_sheet_minutes: int
    goals_conceded: TieredPoints
    saves: TieredPoints
    penalty_saved: int
    yellow_card: int
    red_card: int
    own_goal: int
    penalty_missed: int
    captain_multiplier: float
    vice_captain_multiplier: float
    team_size: int


class ScoringRulesResponse(BaseModel):
    classic: ClassicScoringRules
    tactical: TacticalScoringRules
    dream11: Dream11ScoringRules


# Goalkeepers are not eligible for the defensive-contribution bonus at all
# (Results/scoring.py gates it on position in DEF/MID/FWD). A threshold of 0
# would read as "any action earns it", the opposite of the truth, so the
# ineligible position is published as an unreachable requirement instead.
GK_DEFCON_THRESHOLD_UNREACHABLE = 0


@router.get("/scoring-rules", response_model=ScoringRulesResponse)
def get_scoring_rules() -> ScoringRulesResponse:
    """Both rulesets in one response.

    One call, not two: the Dream11 screen's whole point is telling a manager
    where the two games differ, and comparing them client-side is only sound if
    both halves came from the same read.

    No season parameter. Rules are versioned by Shared.rules.CURRENT_RULES_VERSION
    and change by deploy, not by season -- accepting a season would imply this
    can answer for a past one, which it cannot.
    """
    return ScoringRulesResponse(
        classic=ClassicScoringRules(
            appearance=AppearanceRules(
                partial=classic.APPEARANCE_POINTS,
                full=classic.APPEARANCE_FULL_POINTS,
                full_minutes=classic.APPEARANCE_FULL_MINUTES,
            ),
            goal=PositionPoints(**classic.GOAL_POINTS),
            assist=classic.ASSIST_POINTS,
            clean_sheet=PositionPoints(**classic.CLEAN_SHEET_POINTS),
            clean_sheet_minutes=classic.CLEAN_SHEET_MINUTES_THRESHOLD,
            defensive_contribution=classic.DEF_CONTRIBUTION_POINTS,
            defensive_contribution_threshold=PositionPoints(
                GK=GK_DEFCON_THRESHOLD_UNREACHABLE,
                DEF=classic.DEF_CONTRIBUTION_THRESHOLD,
                MID=classic.MID_FWD_CONTRIBUTION_THRESHOLD,
                FWD=classic.MID_FWD_CONTRIBUTION_THRESHOLD,
            ),
            goals_conceded=TieredPoints(
                points=classic.GOALS_CONCEDED_POINTS, per=classic.GOALS_CONCEDED_PER
            ),
            saves=TieredPoints(points=classic.SAVES_POINTS, per=classic.SAVES_PER),
            penalty_saved=classic.PENALTY_SAVED_POINTS,
            yellow_card=classic.YELLOW_CARD_POINTS,
            red_card=classic.RED_CARD_POINTS,
            own_goal=classic.OWN_GOAL_POINTS,
            penalty_missed=classic.PENALTY_MISSED_POINTS,
            squad_size=classic.SQUAD_SIZE,
            starting_xi_size=classic.STARTING_XI_SIZE,
            bench_size=classic.BENCH_SIZE,
            max_per_club=classic.MAX_PER_CLUB,
        ),
        tactical=TacticalScoringRules(
            rules_version=classic.RULES_VERSION,
            tactics={
                "attack": TacticBonusRules(
                    eligible_position="FWD",
                    goal=classic.ATTACK_POINTS["goal"],
                    assist=classic.ATTACK_POINTS["assist"],
                ),
                "defence": TacticBonusRules(
                    eligible_position="DEF",
                    clean_sheet=classic.DEFENCE_CLEAN_SHEET_POINTS,
                    defensive_contribution_tiers=[
                        {"threshold": threshold, "points": points}
                        for threshold, points in classic.DC_TIERS
                    ],
                ),
                "balanced": TacticBonusRules(
                    eligible_position="MID",
                    goal_or_assist=classic.BALANCED_GOAL_OR_ASSIST_POINTS,
                    creativity_tiers=[
                        {"threshold": float(threshold), "points": points}
                        for threshold, points in classic.CREATIVITY_TIERS
                    ],
                ),
            },
            bonus_player_count=2,
            tactical_swap_limit=2,
            auto_sub_slots=[12, 13],
            tactical_sub_slots=[14, 15],
            fixture_duration_minutes=classic.FIXTURE_DURATION_MIN,
            free_transfers_per_gameweek=classic.FREE_TRANSFERS_EARNED_PER_GAMEWEEK,
            free_transfer_bank_cap=classic.FREE_TRANSFER_BANK_CAP,
            squad_size=classic.SQUAD_SIZE,
            starting_xi_size=classic.STARTING_XI_SIZE,
            bench_size=classic.BENCH_SIZE,
        ),
        dream11=Dream11ScoringRules(
            goal=PositionPoints(**d11.GOAL_POINTS),
            assist=d11.ASSIST_POINTS,
            clean_sheet=PositionPoints(**d11.CLEAN_SHEET_POINTS),
            clean_sheet_minutes=d11.CLEAN_SHEET_MINUTES_THRESHOLD,
            goals_conceded=TieredPoints(
                points=d11.GOALS_CONCEDED_PENALTY, per=d11.GOALS_CONCEDED_PER
            ),
            saves=TieredPoints(points=d11.SAVES_POINTS, per=d11.SAVES_PER),
            penalty_saved=d11.PENALTY_SAVED_POINTS,
            yellow_card=d11.YELLOW_CARD_POINTS,
            red_card=d11.RED_CARD_POINTS,
            own_goal=d11.OWN_GOAL_POINTS,
            penalty_missed=d11.PENALTY_MISSED_POINTS,
            captain_multiplier=d11.CAPTAIN_MULTIPLIER,
            vice_captain_multiplier=d11.VICE_CAPTAIN_MULTIPLIER,
            team_size=d11.DREAM11_TEAM_SIZE,
        ),
    )
