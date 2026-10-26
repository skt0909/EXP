// Single source of truth for squad shape. The backend validates the same rule
// (Game_logic/squad_selection.py); this copy only drives instant UI feedback.
export const SQUAD_SIZE = 15

export const POSITION_REQUIREMENTS = { GK: 2, DEF: 5, MID: 5, FWD: 3 }

const POSITION_NOUN = {
  GK: 'goalkeepers',
  DEF: 'defenders',
  MID: 'midfielders',
  FWD: 'forwards',
}

export function countByPosition(players) {
  const counts = { GK: 0, DEF: 0, MID: 0, FWD: 0 }
  for (const p of players) counts[p.position] = (counts[p.position] || 0) + 1
  return counts
}

export function isPositionFull(counts, position) {
  const required = POSITION_REQUIREMENTS[position]
  return required != null && (counts[position] || 0) >= required
}

// "2/2 goalkeepers selected" -- shown when a position has no room left.
export function positionFullHint(counts, position) {
  return `${counts[position] || 0}/${POSITION_REQUIREMENTS[position]} ${POSITION_NOUN[position]} selected`
}
