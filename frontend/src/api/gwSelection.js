import { request } from './client'

export function fetchCurrentSelection({ season, gameweek }) {
  const params = new URLSearchParams({ season, gameweek })
  return request(`/gw_selection?${params}`)
}

// Throws ValidationError (or LockedError once the deadline has passed -- the
// DB trigger enforce_selection_lock_fn surfaces as a clean 422 mentioning the
// lock) so the page can show a locked state rather than an error. See
// api/client.js for how the two are told apart.
export function submitGwSelection({
  season,
  gameweek,
  player_ids,
  bench_order,
  captain_id,
  vice_captain_id,
  chip_used,
}) {
  return request('/gw_selection', {
    method: 'POST',
    body: {
      season,
      gameweek: Number(gameweek),
      player_ids,
      bench_order,
      captain_id,
      vice_captain_id,
      chip_used,
    },
  })
}
