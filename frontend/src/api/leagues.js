import { request } from './client'

// Re-exported under the old name so LeaguesPage's `err instanceof
// LeagueValidationError` check keeps working; the shared client raises one
// error type for every endpoint now.
export { ValidationError as LeagueValidationError } from './client'

export function fetchUserLeagues({ season }) {
  const params = new URLSearchParams({ season })
  return request(`/leagues?${params}`)
}

// The viewer's own row is highlighted from the bearer token now, so there is
// no longer an anonymous vs identified variant of this call.
export function fetchLeagueTable({ league_id }) {
  return request(`/leagues/${league_id}/table`)
}

export function createLeague({ name, season, league_type, scoring_type, max_members }) {
  return request('/leagues', {
    method: 'POST',
    body: { name, season, league_type, scoring_type, max_members },
  })
}

export function joinLeague({ code }) {
  return request('/leagues/join', { method: 'POST', body: { code } })
}
