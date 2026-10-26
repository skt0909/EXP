import { describe, expect, it } from 'vitest'
import {
  POSITION_REQUIREMENTS,
  SQUAD_SIZE,
  countByPosition,
  isPositionFull,
  positionFullHint,
} from './squadRules'

const pick = (position, n) => Array.from({ length: n }, () => ({ position }))

describe('squadRules', () => {
  it('position quotas add up to the squad size', () => {
    const total = Object.values(POSITION_REQUIREMENTS).reduce((a, b) => a + b, 0)
    expect(total).toBe(SQUAD_SIZE)
  })

  it('counts players per position', () => {
    const counts = countByPosition([...pick('GK', 1), ...pick('DEF', 3)])
    expect(counts).toEqual({ GK: 1, DEF: 3, MID: 0, FWD: 0 })
  })

  it('reports a position full only at its quota', () => {
    expect(isPositionFull(countByPosition(pick('GK', 1)), 'GK')).toBe(false)
    expect(isPositionFull(countByPosition(pick('GK', 2)), 'GK')).toBe(true)
    expect(isPositionFull(countByPosition(pick('FWD', 2)), 'FWD')).toBe(false)
    expect(isPositionFull(countByPosition(pick('FWD', 3)), 'FWD')).toBe(true)
  })

  it('re-opens a position once a player is removed', () => {
    expect(isPositionFull(countByPosition(pick('GK', 1)), 'GK')).toBe(false)
  })

  it('builds the hint text', () => {
    expect(positionFullHint(countByPosition(pick('GK', 2)), 'GK')).toBe('2/2 goalkeepers selected')
    expect(positionFullHint(countByPosition(pick('MID', 5)), 'MID')).toBe('5/5 midfielders selected')
  })
})
