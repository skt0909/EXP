// Pure function: given the CURRENT on-screen Starting XI state and the
// fixture_windows GET /gw_selection now returns, work out -- for each
// Tactical Sub (bench slot 14/15) -- which non-Bonus starters of the same
// position he could legally replace, and why the others are blocked.
//
// Mirrors backend/Gameplay/selection_rules.py's swap-timing rule exactly
// (incoming's first kickoff strictly after outgoing's last fixture ends),
// but this is presentational only: it decides what the Starting XI screen
// shows BEFORE a save, never what the server accepts. That is still decided
// by POST /gw_selection's own validate_selection call -- if the two ever
// drift, the submitError banner (not this function) is what surfaces it.
//
// Recomputes fully from its arguments every call -- no internal state -- so
// the caller can re-run it on every render as Bonus Players or bench slots
// change, per the brief.

/**
 * @param {Object} args
 * @param {Array<{player_id:number, name:string, position:string}>} args.starters
 * @param {number[]} args.bonusPlayerIds
 * @param {Array<{player_id:number, name:string, position:string}>} args.tacticalSubs
 *   The two players in bench slots 14 and 15, in that order.
 * @param {Object<string, {first_kickoff:string, last_end:string}>} args.fixtureWindows
 *   Keyed by player_id (string or number; both are checked).
 * @param {Array<{player_out_id:number, player_in_id:number}>} [args.existingSwaps]
 *   Swaps already saved on the selection -- surfaced via `savedOutgoingId` so
 *   the caller can render them as planned even when this function would now
 *   call them ineligible (a kickoff moved after they were saved). The server
 *   validates a swap once, at submission, and never re-checks it (decision
 *   D4) -- this function must not pretend otherwise.
 * @returns {Array<{
 *   incoming: {player_id:number, name:string, position:string},
 *   incomingWindow: {first_kickoff:string, last_end:string} | null,
 *   eligible: Array<{player_id:number, name:string, position:string}>,
 *   ineligible: Array<{player_id:number, name:string, position:string, reason:string}>,
 *   savedOutgoingId: number | null,
 * }>} One entry per tactical sub, in the order given.
 */
export function computeSwapEligibility({
  starters,
  bonusPlayerIds,
  tacticalSubs,
  fixtureWindows,
  existingSwaps = [],
}) {
  const bonusSet = new Set(bonusPlayerIds)
  const savedOutgoingByIncoming = new Map(
    existingSwaps.map((s) => [s.player_in_id, s.player_out_id])
  )

  const windowFor = (playerId) =>
    fixtureWindows?.[playerId] ?? fixtureWindows?.[String(playerId)] ?? null

  return tacticalSubs.map((incoming) => {
    const incomingWindow = windowFor(incoming.player_id)
    const candidates = starters.filter(
      (p) => p.position === incoming.position && !bonusSet.has(p.player_id)
    )

    const eligible = []
    const ineligible = []

    for (const outgoing of candidates) {
      const outgoingWindow = windowFor(outgoing.player_id)
      const reason = ineligibilityReason(incoming, incomingWindow, outgoing, outgoingWindow)
      if (reason) {
        ineligible.push({ ...outgoing, reason })
      } else {
        eligible.push(outgoing)
      }
    }

    return {
      incoming,
      incomingWindow,
      eligible,
      ineligible,
      savedOutgoingId: savedOutgoingByIncoming.get(incoming.player_id) ?? null,
    }
  })
}

/** Null means eligible. Otherwise the human-readable reason. */
function ineligibilityReason(incoming, incomingWindow, outgoing, outgoingWindow) {
  if (!outgoingWindow) return `${outgoing.name} has no fixture this gameweek`
  if (!incomingWindow) return `${incoming.name} has no fixture this gameweek`

  const incomingKickoff = new Date(incomingWindow.first_kickoff).getTime()
  const outgoingEnd = new Date(outgoingWindow.last_end).getTime()

  // Strictly after, matching selection_rules.py exactly: a kickoff exactly
  // as the outgoing player's fixture ends is NOT "after" it.
  if (incomingKickoff <= outgoingEnd) {
    return `kicks off before ${outgoing.name}'s match ends`
  }
  return null
}
