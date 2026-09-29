import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { gameweekContribution, ruleLabel, tacticalBreakdownRows } from './tacticalRuleLabels'

describe('tacticalBreakdownRows', () => {
  it('passes the backend rows through in order, only adding labels', () => {
    const backend = [
      { rule: 'appearance_60_plus', points: 2 },
      { rule: 'goals', points: 5 },
      { rule: 'yellow_card', points: -1 },
    ]
    expect(tacticalBreakdownRows(backend)).toEqual([
      { rule: 'appearance_60_plus', label: 'Played 60+ minutes', points: 2 },
      { rule: 'goals', label: 'Goals', points: 5 },
      { rule: 'yellow_card', label: 'Yellow card', points: -1 },
    ])
  })

  it('does no arithmetic: the points are exactly what the backend sent', () => {
    const backend = [{ rule: 'balanced_creativity_tier', points: 3 }, { rule: 'balanced_goal_or_assist', points: 1 }]
    expect(tacticalBreakdownRows(backend).map((r) => r.points)).toEqual([3, 1])
  })

  it('handles a missing or empty breakdown', () => {
    expect(tacticalBreakdownRows(undefined)).toEqual([])
    expect(tacticalBreakdownRows([])).toEqual([])
  })

  it('labels an unknown rule readably', () => {
    expect(ruleLabel('some_new_rule')).toBe('Some new rule')
  })
})

describe('gameweekContribution', () => {
  it('is unavailable (null), never zero, when the backend withholds both values', () => {
    expect(gameweekContribution({ general_points: null, tactical_points: null })).toBeNull()
    expect(gameweekContribution({})).toBeNull()
  })

  it('adds whatever the backend sent', () => {
    expect(gameweekContribution({ general_points: 7, tactical_points: 4 })).toBe(11)
    expect(gameweekContribution({ general_points: 2, tactical_points: null })).toBe(2)
    expect(gameweekContribution({ general_points: null, tactical_points: 3 })).toBe(3)
  })

  it('keeps a real zero as zero', () => {
    expect(gameweekContribution({ general_points: 0, tactical_points: 0 })).toBe(0)
  })
})

describe('TacticalPlayerSheet scope', () => {
  function sourceFiles(dir) {
    return readdirSync(dir).flatMap((name) => {
      const path = join(dir, name)
      if (statSync(path).isDirectory()) return sourceFiles(path)
      return /\.(jsx?|tsx?)$/.test(name) && !name.includes('.test.') ? [path] : []
    })
  }

  it('is used only by the Dashboard', () => {
    const src = join(import.meta.dirname, '..')
    const importers = sourceFiles(src)
      .filter((f) => !f.includes('TacticalPlayerSheet'))
      .filter((f) => readFileSync(f, 'utf8').includes('TacticalPlayerSheet'))
      .map((f) => f.slice(src.length + 1).replaceAll('\\', '/'))
    expect(importers).toEqual(['pages/DashboardPage/DashboardPage.jsx'])
  })
})
