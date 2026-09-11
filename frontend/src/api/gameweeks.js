import { request } from './client'

// found=false means ml.fixtures has no rows at all (nothing ingested yet) --
// not an error, same "unstarted state" stance as GET /gw_selection's
// has_selection. See Game_logic/fixtures.py's get_current_gameweek for the
// two-tier "soonest open gameweek, else most recent past one" rule.
export function fetchCurrentGameweek() {
  return request('/gameweeks/current')
}
