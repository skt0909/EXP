import { describe, expect, it } from 'vitest'
import { computeSwapEligibility } from './tacticalSwapEligibility'

const saka = { player_id: 1, name: 'Saka', position: 'MID' }
const rice = { player_id: 2, name: 'Rice', position: 'MID' }
const odegaard = { player_id: 3, name: 'Ødegaard', position: 'MID' }
const gabriel = { player_id: 4, name: 'Gabriel', position: 'DEF' }
const palmer = { player_id: 5, name: 'Palmer', position: 'MID' }
const noFixtureMid = { player_id: 7, name: 'Blank', position: 'MID' }

const windows = {
  1: { first_kickoff: '2026-10-10T15:00:00+01:00', last_end: '2026-10-10T16:55:00+01:00' },
  2: { first_kickoff: '2026-10-10T17:30:00+01:00', last_end: '2026-10-10T19:25:00+01:00' },
  3: { first_kickoff: '2026-10-11T14:00:00+01:00', last_end: '2026-10-11T15:55:00+01:00' },
  4: { first_kickoff: '2026-10-10T15:00:00+01:00', last_end: '2026-10-10T16:55:00+01:00' },
  5: { first_kickoff: '2026-10-11T16:30:00+01:00', last_end: '2026-10-11T18:25:00+01:00' },
  // 6 (Havertz) deliberately has no fixture -- a blank gameweek.
}

describe('computeSwapEligibility', () => {
  it('marks a same-position starter eligible when the sub kicks off after the starter\'s match ends', () => {
    const result = computeSwapEligibility({
      starters: [saka, rice, odegaard, gabriel],
      bonusPlayerIds: [],
      tacticalSubs: [palmer], // kicks off Sun 16:30, after Saka/Rice/Ødegaard all end
      fixtureWindows: windows,
    })

    expect(result).toHaveLength(1)
    const [entry] = result
    expect(entry.incoming).toEqual(palmer)
    expect(entry.eligible.map((p) => p.player_id)).toEqual(
      expect.arrayContaining([saka.player_id, rice.player_id, odegaard.player_id])
    )
    expect(entry.ineligible).toEqual([])
  })

  it('marks a same-position starter ineligible when the sub kicks off before the starter\'s match ends, with a reason', () => {
    const result = computeSwapEligibility({
      starters: [saka], // Sat 15:00, ends 16:55
      bonusPlayerIds: [],
      tacticalSubs: [rice], // Sat 17:30 -- actually after, so pick odegaard as the blocked one instead
      fixtureWindows: windows,
    })
    // Rice (17:30) is legitimately after Saka (ends 16:55) -- eligible.
    expect(result[0].eligible.map((p) => p.player_id)).toEqual([saka.player_id])

    const blocked = computeSwapEligibility({
      starters: [rice], // Sat 17:30, ends 19:25
      bonusPlayerIds: [],
      tacticalSubs: [saka], // Sat 15:00 -- BEFORE Rice's match even starts
      fixtureWindows: windows,
    })
    expect(blocked[0].eligible).toEqual([])
    expect(blocked[0].ineligible).toHaveLength(1)
    expect(blocked[0].ineligible[0].player_id).toBe(rice.player_id)
    expect(blocked[0].ineligible[0].reason).toMatch(/kicks off before/)
  })

  it('excludes a Bonus Player from the candidate list entirely (not even as ineligible)', () => {
    const result = computeSwapEligibility({
      starters: [saka, rice],
      bonusPlayerIds: [saka.player_id],
      tacticalSubs: [palmer],
      fixtureWindows: windows,
    })
    const allListed = [...result[0].eligible, ...result[0].ineligible].map((p) => p.player_id)
    expect(allListed).not.toContain(saka.player_id)
    expect(allListed).toContain(rice.player_id)
  })

  it('recomputes when Bonus Players change -- a starter freed from Bonus becomes a candidate again', () => {
    const withBonus = computeSwapEligibility({
      starters: [saka, rice],
      bonusPlayerIds: [rice.player_id],
      tacticalSubs: [palmer],
      fixtureWindows: windows,
    })
    expect([...withBonus[0].eligible, ...withBonus[0].ineligible].map((p) => p.player_id)).not.toContain(rice.player_id)

    const withoutBonus = computeSwapEligibility({
      starters: [saka, rice],
      bonusPlayerIds: [],
      tacticalSubs: [palmer],
      fixtureWindows: windows,
    })
    expect(withoutBonus[0].eligible.map((p) => p.player_id)).toContain(rice.player_id)
  })

  it('only offers same-position starters', () => {
    const result = computeSwapEligibility({
      starters: [saka, gabriel], // MID, DEF
      bonusPlayerIds: [],
      tacticalSubs: [palmer], // MID
      fixtureWindows: windows,
    })
    const allListed = [...result[0].eligible, ...result[0].ineligible].map((p) => p.player_id)
    expect(allListed).not.toContain(gabriel.player_id)
  })

  it('boundary: an incoming kickoff exactly equal to the outgoing end is ineligible (strictly after required)', () => {
    const tie = {
      out: { player_id: 10, name: 'Outgoing', position: 'MID' },
      in: { player_id: 11, name: 'Incoming', position: 'MID' },
    }
    const tieWindows = {
      10: { first_kickoff: '2026-10-10T15:00:00Z', last_end: '2026-10-10T16:55:00Z' },
      // Kicks off at the EXACT instant the outgoing player's fixture ends.
      11: { first_kickoff: '2026-10-10T16:55:00Z', last_end: '2026-10-10T18:50:00Z' },
    }
    const result = computeSwapEligibility({
      starters: [tie.out],
      bonusPlayerIds: [],
      tacticalSubs: [tie.in],
      fixtureWindows: tieWindows,
    })
    expect(result[0].eligible).toEqual([])
    expect(result[0].ineligible[0].player_id).toBe(tie.out.player_id)

    // One minute later is enough to flip it to eligible.
    const justAfter = computeSwapEligibility({
      starters: [tie.out],
      bonusPlayerIds: [],
      tacticalSubs: [{ ...tie.in }],
      fixtureWindows: {
        10: tieWindows[10],
        11: { first_kickoff: '2026-10-10T16:56:00Z', last_end: '2026-10-10T18:51:00Z' },
      },
    })
    expect(justAfter[0].ineligible).toEqual([])
    expect(justAfter[0].eligible.map((p) => p.player_id)).toEqual([tie.out.player_id])
  })

  it('a player with no fixture this gameweek is never eligible, with a distinct reason', () => {
    const result = computeSwapEligibility({
      starters: [saka], // MID, same position as the blank-gameweek sub below
      bonusPlayerIds: [],
      tacticalSubs: [noFixtureMid], // MID, no window entry at all
      fixtureWindows: windows,
    })
    expect(result[0].eligible).toEqual([])
    expect(result[0].ineligible[0].reason).toMatch(/no fixture this gameweek/)
    expect(result[0].incomingWindow).toBeNull()
  })

  it('a saved swap keeps its outgoing id even when current timing would mark it ineligible', () => {
    // Rice starts (17:30, ends 19:25). Saka was saved as his Tactical
    // Sub earlier -- but Saka now kicks off at 15:00, BEFORE Rice's match
    // even starts (e.g. a kickoff moved after the swap was saved).
    const result = computeSwapEligibility({
      starters: [rice],
      bonusPlayerIds: [],
      tacticalSubs: [saka],
      fixtureWindows: windows,
      existingSwaps: [{ player_out_id: rice.player_id, player_in_id: saka.player_id }],
    })
    // Still correctly reported as timing-ineligible under CURRENT data --
    expect(result[0].ineligible.map((p) => p.player_id)).toContain(rice.player_id)
    // -- but the saved pairing itself is still surfaced, distinctly, so the
    // caller can render it as "planned" rather than dropping it or erroring.
    expect(result[0].savedOutgoingId).toBe(rice.player_id)
  })

  it('savedOutgoingId is null when nothing was saved for this sub', () => {
    const result = computeSwapEligibility({
      starters: [saka],
      bonusPlayerIds: [],
      tacticalSubs: [palmer],
      fixtureWindows: windows,
      existingSwaps: [],
    })
    expect(result[0].savedOutgoingId).toBeNull()
  })
})
