/**
 * Turns GET /scoring-rules into the sections the two "How Points Work" screens
 * render.
 *
 * NO POINT VALUES LIVE HERE. They used to, and that was the problem: a number
 * restated in JavaScript can disagree with the scorer that applies it, and
 * nothing catches it -- the same failure MatchDetailPage's rules strip was
 * written to avoid. Everything numeric below reads off the API payload, which
 * the backend builds from the constants Results/scoring.py and
 * dream11_scoring.py actually use (see backend/Data/scoring_rules.py).
 *
 * What DOES live here is presentation: labels, ordering, icons, section
 * grouping, tone, filter tags and prose. The same values appear on two screens
 * that group them differently, so grouping is not the backend's business.
 *
 * If you need a new number on a screen, add it to the endpoint -- do not
 * reintroduce a literal here. The one exception is prose that describes a rule
 * without restating its value ("No bench or autosubs"), which is copy.
 */

/** Positive award, negative deduction, zero/not-applicable, or a multiplier. */
export const TONE = {
  POSITIVE: 'positive',
  NEGATIVE: 'negative',
  NEUTRAL: 'neutral',
  MULTIPLIER: 'multiplier',
  MULTIPLIER_STRONG: 'multiplier-strong',
}

// U+2212 MINUS SIGN, not a hyphen: at the pill's weight a hyphen reads as a
// dash against the digit. Positives keep an explicit "+" so the two columns
// stay symmetrical.
const MINUS = '−'

function signed(value) {
  if (value === 0) return '0'
  return value > 0 ? `+${value}` : `${MINUS}${Math.abs(value)}`
}

function toneFor(value) {
  if (value > 0) return TONE.POSITIVE
  if (value < 0) return TONE.NEGATIVE
  return TONE.NEUTRAL
}

/** 2 -> "2x", 1.5 -> "1.5x" (with a true multiplication sign). */
function multiplier(value) {
  return `${value}×`
}

/** A row whose pill and colour both follow from one signed number. */
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

/** "−1 per 2 goals, GK & DEF only" — the divisor travels with the value. */
function tieredSublabel(tiered, noun, qualifier) {
  const each = `${signed(tiered.points)} per ${tiered.per} ${noun}`
  return qualifier ? `${each}, ${qualifier}` : each
}

// ---------------------------------------------------------------- classic FPL

export function buildClassicRules(api) {
  const r = api.classic
  const defcon = r.defensive_contribution_threshold

  return {
    eyebrow: 'Classic FPL Rules',
    title: 'Scoring System',
    statusLabel: 'Gameweek Live',
    intro:
      'Standard Fantasy Premier League rules and scoring breakdown. Tactical calculations applied automatically at full-time.',

    highlights: [
      {
        key: 'max-goal',
        label: 'Max Goal',
        // The best goal on the board rather than a hardcoded position: if the
        // weighting ever changes, the headline follows it.
        value: `+${Math.max(...ORDERED_POSITIONS.map((p) => r.goal[p]))} PTS`,
        tone: TONE.POSITIVE,
      },
      {
        key: 'hit-penalty',
        label: 'Hit Penalty',
        value: `${signed(r.transfer_hit)} PTS`,
        tone: TONE.NEGATIVE,
      },
      {
        key: 'chip-boost',
        label: 'Chip Boost',
        value: `${multiplier(r.triple_captain_multiplier)} MULTI`,
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
          // The only hardcoded number on either screen, and not a constant:
          // it states the ABSENCE of a rule (no appearance, no appearance
          // points), so there is nothing on the backend for it to drift from.
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
          // GK and DEF share a value, so they share a row rather than repeating
          // the same number twice under different names.
          valueRow(
            'cs-gkdef',
            `Clean Sheet (${r.clean_sheet_minutes}+ min) — GK & DEF`,
            r.clean_sheet.GK,
            { dot: true },
          ),
          valueRow(
            'cs-mid',
            `Clean Sheet (${r.clean_sheet_minutes}+ min) — MID`,
            r.clean_sheet.MID,
            { dot: true },
          ),
          valueRow(
            'cs-fwd',
            `Clean Sheet (${r.clean_sheet_minutes}+ min) — FWD`,
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
        key: 'multipliers',
        title: 'Multipliers & Squad',
        icon: 'bolt',
        rows: [
          {
            key: 'captain',
            label: 'Captain Multiplier',
            value: multiplier(r.captain_multiplier),
            tone: TONE.MULTIPLIER,
            dot: true,
          },
          {
            key: 'triple-captain',
            label: 'Triple Captain Chip',
            value: multiplier(r.triple_captain_multiplier),
            tone: TONE.MULTIPLIER_STRONG,
            dot: true,
          },
        ],
        notes: [
          {
            key: 'vice',
            icon: 'swap_vert',
            title: 'Vice-Captaincy Protocol',
            body: `If your Captain logs 0 minutes, the ${multiplier(
              r.captain_multiplier,
            )} armband automatically transfers to your designated Vice-Captain.`,
          },
          {
            key: 'bench-boost',
            icon: 'groups',
            title: 'Bench Boost Power',
            body: `All ${r.squad_size} players (${r.starting_xi_size} starters and ${r.bench_size} bench subs) actively score points for your overall Gameweek aggregate.`,
          },
        ],
      },
      {
        key: 'transfers',
        title: 'Transfers',
        icon: 'swap_horiz',
        rows: [
          {
            key: 'hit',
            label: 'Extra Transfer Hit',
            sublabel: 'Per transfer beyond free weekly allowance',
            value: signed(r.transfer_hit),
            tone: toneFor(r.transfer_hit),
          },
        ],
        notes: [
          {
            key: 'rollover',
            icon: 'hourglass_top',
            title: 'Rollover Cap',
            body: `Up to ${r.max_banked_free_transfers} free transfers can be accumulated across gameweeks without loss.`,
          },
        ],
      },
    ],
  }
}

// ------------------------------------------------------------------- Dream11

/**
 * `differs` drives the "Differs from Classic FPL" badge, and it is COMPUTED by
 * comparing the two halves of the payload rather than hand-set. Hand-set, it
 * would keep claiming a divergence after the two rulesets converged -- the one
 * thing on this screen a user would most reasonably trust.
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
            // Classic has no vice multiplier of its own -- the armband only
            // transfers when the captain blanks -- so this always differs.
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
            body: `No bench or autosubs — your starting ${r.team_size} is final. Ensure all selected players are verified in the officially announced starting lineups prior to scheduled match kickoff. Inactive starters will score 0 points.`,
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
