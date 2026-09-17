import { request } from './client'

// Re-exported so Dream11 screens can branch on these without also importing
// client.js -- fetchContestTeam is the one call in the app whose 403 and 404
// are both ordinary game states (see its comment below).
export { ForbiddenError, LockedError, NotFoundError, ValidationError } from './client'

// ---------------------------------------------------------------- reads

/** Contests this user has joined, each carrying their own rank/points inline. */
export function fetchUserContests({ user_id }) {
  const params = new URLSearchParams({ user_id: String(user_id) })
  return request(`/dream11/contests?${params}`)
}

/**
 * This user's contests on one fixture -- the match-detail counterpart to
 * fetchUserContests.
 *
 * user_id is required, and the list is scoped to contests the user has joined:
 * contests are joined by code, so the API deliberately won't enumerate ones you
 * don't belong to. A browsable list of public contests would need an is_public
 * flag on the contest, which the backend doesn't have yet.
 */
export function fetchFixtureContests({ fixture_id, user_id }) {
  const params = new URLSearchParams({ user_id: String(user_id) })
  return request(`/dream11/fixtures/${fixture_id}/contests?${params}`)
}

/**
 * One contest's summary: code, member_count, is_locked, the fixture
 * (home_team/away_team/kickoff_time) and the rules the UI shouldn't hardcode
 * (budget_cap, team_size).
 *
 * user_id is optional -- omitting it is the pre-join case, where the contest
 * still resolves but every user_* field stays at its empty default.
 */
export function fetchContest({ contest_id, user_id }) {
  const params = user_id ? `?${new URLSearchParams({ user_id: String(user_id) })}` : ''
  return request(`/dream11/contests/${contest_id}${params}`)
}

/** The contest's full player pool with its frozen credit prices. */
export function fetchContestPool({ contest_id }) {
  return request(`/dream11/contests/${contest_id}/players`)
}

/** Members with points, rank, and has_submitted_team, plus the contest summary. */
export function fetchContestLeaderboard({ contest_id, user_id }) {
  const params = user_id ? `?${new URLSearchParams({ user_id: String(user_id) })}` : ''
  return request(`/dream11/contests/${contest_id}/leaderboard${params}`)
}

/**
 * One member's submitted XI, with per-player live points.
 *
 * user_id names WHOSE team to fetch; who is ASKING comes from the bearer token
 * that client.js attaches. This is the only endpoint in the API that
 * authenticates rather than trusting user_id, and the only one where a non-2xx
 * is routinely not a failure:
 *   403 -> the contest hasn't locked yet (rival XIs stay hidden until kickoff),
 *          or the caller isn't a member of this contest
 *   404 -> that member never submitted a team
 * Both should render as information. Callers catch ForbiddenError/NotFoundError.
 */
export function fetchContestTeam({ contest_id, user_id }) {
  const params = new URLSearchParams({ user_id: String(user_id) })
  return request(`/dream11/contests/${contest_id}/team?${params}`)
}

// ---------------------------------------------------------------- writes

export function createContest({ fixture_id, name, user_id, max_members }) {
  return request('/dream11/contests', {
    method: 'POST',
    body: { fixture_id, name, user_id, max_members },
  })
}

export function joinContest({ user_id, code }) {
  return request('/dream11/contests/join', { method: 'POST', body: { user_id, code } })
}

/** player_ids are raw FPL ids, exactly 11, matching captain_id/vice_captain_id. */
export function submitContestTeam({ contest_id, user_id, player_ids, captain_id, vice_captain_id }) {
  return request(`/dream11/contests/${contest_id}/team`, {
    method: 'POST',
    body: { user_id, player_ids, captain_id, vice_captain_id },
  })
}

/**
 * Replaces an already-submitted team's full 11 + captain/vice in one call
 * (edit-only -- Game_logic/dream11.py's PATCH 404s if there's no existing
 * team yet, matching submitContestTeam's POST being create-only). Same
 * body shape as submitContestTeam so PickTeamPage can post to either.
 */
export function editContestTeam({ contest_id, user_id, player_ids, captain_id, vice_captain_id }) {
  return request(`/dream11/contests/${contest_id}/team`, {
    method: 'PATCH',
    body: { user_id, player_ids, captain_id, vice_captain_id },
  })
}
