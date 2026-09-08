import { request } from './client'

/**
 * GET /scoring-rules — the published point values for both games.
 *
 * `auth: false` because the route is public (see Data/scoring_rules.py and
 * test_auth_enforcement.py's PUBLIC_ROUTES). request() would send the token
 * anyway if one exists; passing false keeps the call working for a caller who
 * has none, rather than depending on one being present.
 *
 * Returns { classic, dream11 } together in one read -- the Dream11 screen
 * compares the two, which is only sound if both halves came from the same
 * response.
 */
export function fetchScoringRules() {
  return request('/scoring-rules', { auth: false })
}
