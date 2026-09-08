import { request } from './client'

export function fetchChipsUsed({ season, gameweek }) {
  const params = new URLSearchParams({ season, gameweek })
  return request(`/chips/used?${params}`)
}
