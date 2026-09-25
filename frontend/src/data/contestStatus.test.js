import { describe, expect, it } from 'vitest'
import { CONTEST_STATUS, contestStatus, isContestLocked } from './contestStatus'

const now = new Date('2026-10-10T15:05:00+01:00')
const future = '2026-10-10T17:30:00+01:00'
const past = '2026-10-10T15:00:00+01:00'

const contest = (overrides) => ({
  is_locked: false,
  is_finalized: false,
  void_reason: null,
  kickoff_time: future,
  ...overrides,
})

describe('contestStatus', () => {
  it('is open before kickoff', () => {
    expect(contestStatus(contest({}), now).status).toBe(CONTEST_STATUS.OPEN)
    expect(isContestLocked(contest({}), now)).toBe(false)
  })

  it('is live from kickoff, before the lock sweep has set is_locked', () => {
    const c = contest({ kickoff_time: past })
    expect(contestStatus(c, now).status).toBe(CONTEST_STATUS.LIVE)
    expect(isContestLocked(c, now)).toBe(true)
  })

  it('is live once locked', () => {
    expect(contestStatus(contest({ is_locked: true }), now).label).toBe('Live')
  })

  it('is completed once finalized', () => {
    const c = contest({ is_locked: true, is_finalized: true, kickoff_time: past })
    expect(contestStatus(c, now)).toEqual({ status: CONTEST_STATUS.COMPLETED, label: 'Completed', detail: null })
  })

  it('is cancelled, with the reason, when voided', () => {
    const postponed = contest({ is_locked: true, is_finalized: true, void_reason: 'postponed', kickoff_time: null })
    expect(contestStatus(postponed, now)).toEqual({
      status: CONTEST_STATUS.CANCELLED,
      label: 'Cancelled',
      detail: 'Match postponed',
    })
    expect(contestStatus(contest({ void_reason: 'abandoned', is_finalized: true }), now).detail).toBe('Match abandoned')
    expect(isContestLocked(postponed, now)).toBe(true)
  })

  it('treats a missing kickoff on an open contest as open', () => {
    expect(isContestLocked(contest({ kickoff_time: null }), now)).toBe(false)
  })
})
