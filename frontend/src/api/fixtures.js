import { request } from './client'

/**
 * A season's matches, with both teams, kickoff, and Dream11 contest counts.
 *
 * Only `season` is required. Note `upcoming_only` defaults to false server-side
 * and is left that way here: every ingested fixture is currently in the past, so
 * passing true returns an empty list rather than an empty-looking bug. Pass it
 * explicitly when the screen genuinely wants only future matches.
 *
 * user_contest_count is scoped to the signed-in manager, taken from the bearer
 * token; contest_count is a plain total anyone can see.
 */
export function fetchFixtures({ season, gameweek, upcoming_only } = {}) {
  const params = new URLSearchParams({ season })
  if (gameweek != null) params.set('gameweek', String(gameweek))
  if (upcoming_only != null) params.set('upcoming_only', String(upcoming_only))
  return request(`/fixtures?${params}`)
}
