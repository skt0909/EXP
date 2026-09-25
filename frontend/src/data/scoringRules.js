/**
 * Turns GET /scoring-rules into the sections the two "How Points Work" screens
 * render.
 *
 * Numeric values come from the backend payload so the UI cannot drift from the
 * scoring engines. This file owns presentation only: labels, ordering, icons,
 * grouping, tone, filter tags, and prose.
 */

/** Positive award, negative deduction, zero/not-applicable, or a multiplier. */
export const TONE = {
  POSITIVE: 'positive',
  NEGATIVE: 'negative',
  NEUTRAL: 'neutral',
  MULTIPLIER: 'multiplier',
  MULTIPLIER_STRONG: 'multiplier-strong',
}

const MINUS = '-'

function signed(value) {
  if (value === 0) return '0'
  return value > 0 ? `+${value}` : `${MINUS}${Math.abs(value)}`
}

function toneFor(value) {
  if (value > 0) return TONE.POSITIVE
  if (value < 0) return TONE.NEGATIVE
  return TONE.NEUTRAL
}

/** 2 -> "2x", 1.5 -> "1.5x". */
function multiplier(value) {
  return `${value}x`
}

/** A row whose pill and color both follow from one signed number. */
function valueRow(key, label, value, extra = {}) {
  return { key, label, value: signed(value), tone: toneFor(value), ...extra }
}

const POSITION_LABELS = {
  GK: 'Goalkeeper',
  DEF: 'Defender',
  MID: 'Midfielder',
  FWD: 'Forward',
}

const ORDERED_POSITIONS = ['GK', 'DEF', 'MID', 'FWD']

function tieredSublabel(tiered, noun, qualifier) {
  const each = `${signed(tiered.points)} per ${tiered.per} ${noun}`
  return qualifier ? `${each}, ${qualifier}` : each
}

function thresholdRows(keyPrefix, labelPrefix, tiers) {
  return (tiers ?? []).map((tier) =>
    valueRow(`${keyPrefix}-${tier.threshold}`, `${labelPrefix} ${tier.threshold}+`, tier.points, {
      dot: true,
    }),
  )
}

// ---------------------------------------------------------------- tactical FPL

export function buildClassicRules(api) {
  const r = api.classic
  const tactical = api.tactical
  const defcon = r.defensive_contribution_threshold
  const attack = tactical.tactics.attack
  const defence = tactical.tactics.defence
  const balanced = tactical.tactics.balanced

  return {
    eyebrow: 'Tactical Rules',
    title: 'Scoring System',
    statusLabel: `Rules v${tactical.rules_version}`,
    intro:
      'General Points come from match events. Tactical Points, Sub Bonus, and transfer limits are driven by your selected gameweek plan.',

    highlights: [
      {
        key: 'max-goal',
        label: 'Max Goal',
        value: `+${Math.max(...ORDERED_POSITIONS.map((p) => r.goal[p]))} PTS`,
        tone: TONE.POSITIVE,
      },
      {
        key: 'bonus-players',
        label: 'Bonus Players',
        value: `${tactical.bonus_player_count} PICKS`,
        tone: TONE.MULTIPLIER,
      },
      {
        key: 'swap-limit',
        label: 'Tactical Swaps',
        value: `${tactical.tactical_swap_limit} MAX`,
        tone: TONE.MULTIPLIER,
      },
    ],

    sections: [
      {
        key: 'appearance',
        title: 'Appearance',
        icon: 'timer',
        rows: [
          valueRow('played-full', `Played ${r.appearance.full_minutes}+ minutes`, r.appearance.full),
          valueRow(
            'played-partial',
            `Played 1-${r.appearance.full_minutes - 1} minutes`,
            r.appearance.partial,
          ),
          { key: 'unused', label: 'Did not play', value: '0', tone: TONE.NEUTRAL },
        ],
      },
      {
        key: 'attacking',
        title: 'Attacking',
        icon: 'sports_soccer',
        rows: [
          ...ORDERED_POSITIONS.map((position) =>
            valueRow(`goal-${position}`, `Goal (${POSITION_LABELS[position]})`, r.goal[position], {
              dot: true,
            }),
          ),
          valueRow('assist', 'Assist', r.assist, { dot: true }),
        ],
      },
      {
        key: 'defensive',
        title: 'Defensive',
        icon: 'shield',
        rows: [
          valueRow(
            'cs-gkdef',
            `Clean Sheet (${r.clean_sheet_minutes}+ min) - GK & DEF`,
            r.clean_sheet.GK,
            { dot: true },
          ),
          valueRow(
            'cs-mid',
            `Clean Sheet (${r.clean_sheet_minutes}+ min) - MID`,
            r.clean_sheet.MID,
            { dot: true },
          ),
          valueRow(
            'cs-fwd',
            `Clean Sheet (${r.clean_sheet_minutes}+ min) - FWD`,
            r.clean_sheet.FWD,
            { dot: true },
          ),
          valueRow('defcon', 'Defensive Contribution', r.defensive_contribution, {
            sublabel: `${defcon.DEF} actions DEF / ${defcon.MID} actions MID-FWD`,
          }),
          {
            key: 'conceded',
            label: 'Goals Conceded',
            sublabel: tieredSublabel(r.goals_conceded, 'goals', 'GK & DEF only'),
            value: signed(r.goals_conceded.points),
            tone: toneFor(r.goals_conceded.points),
          },
        ],
      },
      {
        key: 'discipline',
        title: 'Discipline',
        icon: 'warning',
        rows: [
          valueRow('yellow', 'Yellow Card', r.yellow_card, { dot: true }),
          valueRow('red', 'Red Card', r.red_card, { dot: true }),
          valueRow('own-goal', 'Own Goal', r.own_goal, { dot: true }),
          valueRow('pen-missed', 'Penalty Missed', r.penalty_missed, { dot: true }),
        ],
      },
      {
        key: 'goalkeeping',
        title: 'Goalkeeping',
        icon: 'sports_handball',
        rows: [
          {
            key: 'saves',
            label: 'Saves Tier',
            sublabel: tieredSublabel(r.saves, 'saves recorded'),
            value: signed(r.saves.points),
            tone: toneFor(r.saves.points),
          },
          valueRow('pen-saved', 'Penalty Saved', r.penalty_saved, { dot: true }),
        ],
      },
      {
        key: 'tactics',
        title: 'Tactics & Bonus Players',
        icon: 'bolt',
        rows: [
          valueRow('attack-goal', `Attack Bonus Goal (${attack.eligible_position})`, attack.goal, {
            dot: true,
          }),
          valueRow('attack-assist', `Attack Bonus Assist (${attack.eligible_position})`, attack.assist, {
            dot: true,
          }),
          valueRow(
            'defence-clean-sheet',
            `Defence Bonus Clean Sheet (${defence.eligible_position})`,
            defence.clean_sheet,
            { dot: true },
          ),
          ...thresholdRows('defence-actions', 'Defence Bonus Actions', defence.defensive_contribution_tiers),
          valueRow(
            'balanced-ga',
            `Balanced Goal or Assist (${balanced.eligible_position})`,
            balanced.goal_or_assist,
            { dot: true },
          ),
          ...thresholdRows('balanced-creativity', 'Balanced Creativity', balanced.creativity_tiers),
        ],
        notes: [
          {
            key: 'bonus-eligibility',
            icon: 'stars',
            title: 'Bonus Player Rule',
            body: `Choose exactly ${tactical.bonus_player_count} Bonus Players from the tactic's eligible position before saving your lineup.`,
          },
        ],
      },
      {
        key: 'bench',
        title: 'Bench & Swaps',
        icon: 'groups',
        rows: [
          {
            key: 'auto-sub-slots',
            label: 'Auto Sub Slots',
            sublabel: 'Slot 12 is Auto GK; slot 13 is Auto Sub',
            value: tactical.auto_sub_slots.join(', '),
            tone: TONE.NEUTRAL,
          },
          {
            key: 'tactical-sub-slots',
            label: 'Tactical Sub Slots',
            sublabel: "Incoming player's kickoff must follow the outgoing player's match window",
            value: tactical.tactical_sub_slots.join(', '),
            tone: TONE.NEUTRAL,
          },
        ],
        notes: [
          {
            key: 'sub-bonus',
            icon: 'swap_horiz',
            title: 'Sub Bonus',
            body: `Plan up to ${tactical.tactical_swap_limit} same-position Tactical Swaps. Positive replacement gains are added as Sub Bonus.`,
          },
        ],
      },
      {
        key: 'transfers',
        title: 'Transfers',
        icon: 'swap_horiz',
        rows: [
          {
            key: 'weekly-free',
            label: 'Free Transfers Earned',
            sublabel: 'Added each gameweek, then banked if unused',
            value: `+${tactical.free_transfers_per_gameweek}`,
            tone: TONE.POSITIVE,
          },
          {
            key: 'bank-cap',
            label: 'Bank Cap',
            sublabel: 'Transfers beyond your allowance are rejected',
            value: tactical.free_transfer_bank_cap,
            tone: TONE.NEUTRAL,
          },
        ],
        notes: [
          {
            key: 'rollover',
            icon: 'hourglass_top',
            title: 'Rollover Cap',
            body: `Up to ${tactical.free_transfer_bank_cap} free transfers can be held. Transfers beyond that allowance are rejected.`,
          },
        ],
      },
    ],
  }
}

// ------------------------------------------------------------------- Dream11

/**
 * `differs` drives the "Differs from Classic FPL" badge, and it is computed by
 * comparing the two halves of the payload rather than hand-set.
 */
export function buildDream11Rules(api) {
  const r = api.dream11
  const c = api.classic

  return {
    eyebrow: 'Dream11 Rules',
    title: 'Scoring Breakdown',
    statusLabel: 'Live Formula',
    intro:
      'Single-Match Daily Fantasy scoring dynamics. Points accumulate in real-time across confirmed active squad members.',

    filters: [
      { key: 'all', label: 'All Rules' },
      { key: 'attacking', label: 'Attacking' },
      { key: 'defensive', label: 'Defensive' },
      { key: 'multipliers', label: 'Multipliers' },
    ],

    sections: [
      {
        key: 'attacking',
        title: 'Attacking',
        icon: 'sports_soccer',
        filter: 'attacking',
        rows: [
          ...ORDERED_POSITIONS.map((position) =>
            valueRow(`goal-${position}`, `Goal (${POSITION_LABELS[position]})`, r.goal[position], {
              differs: r.goal[position] !== c.goal[position],
            }),
          ),
          valueRow('assist', 'Assist (All Positions)', r.assist, {
            differs: r.assist !== c.assist,
          }),
        ],
      },
      {
        key: 'defensive',
        title: 'Defensive',
        icon: 'shield',
        filter: 'defensive',
        groups: [
          {
            key: 'clean-sheet',
            title: `Clean Sheet (at ${r.clean_sheet_minutes}+ mins)`,
            differs: r.clean_sheet_minutes !== c.clean_sheet_minutes,
            rows: [
              valueRow('cs-gkdef', 'GK & DEF', r.clean_sheet.GK),
              valueRow('cs-mid', 'Midfielders (MID)', r.clean_sheet.MID),
              valueRow('cs-fwd', 'Forwards (FWD)', r.clean_sheet.FWD),
            ],
          },
        ],
        rows: [
          {
            key: 'conceded',
            label: 'Goals Conceded',
            sublabel: tieredSublabel(r.goals_conceded, 'conceded', 'GK & DEF only'),
            value: signed(r.goals_conceded.points),
            tone: toneFor(r.goals_conceded.points),
          },
        ],
      },
      {
        key: 'discipline',
        title: 'Discipline',
        icon: 'warning',
        rows: [
          valueRow('yellow', 'Yellow Card', r.yellow_card),
          valueRow('red', 'Red Card', r.red_card),
          valueRow('own-goal', 'Own Goal', r.own_goal),
          valueRow('pen-missed', 'Penalty Missed', r.penalty_missed),
        ],
      },
      {
        key: 'goalkeeping',
        title: 'Goalkeeping',
        icon: 'sports_handball',
        filter: 'defensive',
        rows: [
          {
            key: 'saves',
            label: 'Saves Tier',
            sublabel: tieredSublabel(r.saves, 'saves recorded'),
            value: signed(r.saves.points),
            tone: toneFor(r.saves.points),
          },
          valueRow('pen-saved', 'Penalty Saved', r.penalty_saved),
        ],
      },
      {
        key: 'captaincy',
        title: 'Captain & Vice',
        icon: 'military_tech',
        filter: 'multipliers',
        tiles: [
          { key: 'captain', label: 'Captain (C)', value: multiplier(r.captain_multiplier) },
          { key: 'vice', label: 'Vice-Capt (VC)', value: multiplier(r.vice_captain_multiplier) },
        ],
        notes: [
          {
            key: 'simultaneous',
            differs: true,
            body: `Both captain (${multiplier(r.captain_multiplier)}) and vice-captain (${multiplier(
              r.vice_captain_multiplier,
            )}) multipliers are always applied simultaneously during match play. No fallback or substitution rule applies if one rests.`,
          },
        ],
      },
      {
        key: 'squad',
        title: 'Squad & Lineup',
        icon: 'groups',
        notes: [
          {
            key: 'no-bench',
            differs: true,
            body: `No bench or autosubs - your starting ${r.team_size} is final. Ensure all selected players are verified in the officially announced starting lineups prior to scheduled match kickoff. Inactive starters will score 0 points.`,
          },
          {
            key: 'lock',
            icon: 'schedule',
            body: 'Lineups locked precisely at official kickoff whistle.',
          },
        ],
      },
    ],
  }
}
