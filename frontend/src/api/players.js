import { request } from './client'

export function fetchPlayers({ season, gameweek }) {
  const params = new URLSearchParams({ season })
  if (gameweek != null) params.set('gameweek', gameweek)
  return request(`/players?${params}`)
}
