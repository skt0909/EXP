import { request } from './client'

export function fetchCurrentSquad({ season, gameweek }) {
  const params = new URLSearchParams({ season })
  if (gameweek != null) params.set('gameweek', gameweek)
  return request(`/squad?${params}`)
}

// Throws ValidationError on a 422 so the caller can render the backend's full
// error list; see api/client.js. team_name is deliberately NOT sent here --
// it's captured at registration and written to public.users.team_name, which
// is the column GET /team and the league leaderboard already read.
export function submitSquad({ season, player_ids }) {
  return request('/squad/select', {
    method: 'POST',
    body: { season, player_ids },
  })
}
