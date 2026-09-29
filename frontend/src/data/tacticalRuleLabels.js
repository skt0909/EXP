// Display names for Tactic mode scoring rules. Display only: the rule keys,
// points and which rules apply all come from GET /team
// (Results/tactical_scoring.py's general_points_breakdown /
// tactical_points_breakdown, or the stored gw_scores snapshot for a settled
// gameweek). Nothing here scores anything.

const RULE_LABELS = {
  appearance_60_plus: 'Played 60+ minutes',
  appearance_under_60: 'Played under 60 minutes',
  goals: 'Goals',
  assists: 'Assists',
  clean_sheet: 'Clean sheet',
  saves: 'Saves',
  penalties_saved: 'Penalty saved',
  goals_conceded: 'Goals conceded',
  defensive_contribution: 'Defensive contribution',
  yellow_card: 'Yellow card',
  red_card: 'Red card',
  own_goal: 'Own goal',
  penalty_missed: 'Penalty missed',
  attack_goals: 'Attack: goals',
  attack_assists: 'Attack: assists',
  defence_clean_sheet: 'Defence: clean sheet',
  defence_contribution_tier: 'Defence: contribution tier',
  balanced_goal_or_assist: 'Balanced: goal or assist',
  balanced_creativity_tier: 'Balanced: creativity tier',
}

// An unknown key (a rule added on the backend first) still reads sensibly.
export function ruleLabel(rule) {
  if (RULE_LABELS[rule]) return RULE_LABELS[rule]
  const words = String(rule).replaceAll('_', ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

// A player's gameweek contribution: General + Tactical, from the backend.
// Both null means the backend deliberately withheld them (a legacy gameweek
// it can't reconstruct under today's rules) -- that is "unavailable", never 0.
export function gameweekContribution(player) {
  const general = player?.general_points
  const tactical = player?.tactical_points
  if (general == null && tactical == null) return null
  return (general ?? 0) + (tactical ?? 0)
}

// The backend's rows, in the backend's order, with a label added. Points are
// passed through untouched.
export function tacticalBreakdownRows(breakdown) {
  return (breakdown ?? []).map((row) => ({ rule: row.rule, label: ruleLabel(row.rule), points: row.points }))
}
