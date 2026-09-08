import { request } from './client'

export function fetchTeamDashboard({ season, gameweek }) {
  const params = new URLSearchParams({ season, gameweek })
  return request(`/team?${params}`)
}
