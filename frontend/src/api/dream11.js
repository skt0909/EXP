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

/**
 * A fixture's player pool with LIVE-computed prices, no contest required --
 * lets a team be built and saved (saveContestTeam) before any contest
 * exists, e.g. the Match screen's "Build & Save a Team" action. Prices here
 * are only a preview: a contest created later prices itself fresh at that
 * moment and may differ if the rolling averages have moved since.
 */
export function fetchFixturePool({ fixture_id }) {
  return request(`/dream11/fixtures/${fixture_id}/players`)
}

/** Members with points, rank, and has_submitted_team, plus the contest summary. */
export function fetchContestLeaderboard({ contest_id, user_id }) {
  const params = user_id ? `?${new URLSearchParams({ user_id: String(user_id) })}` : ''
  return request(`/dream11/contests/${contest_id}/leaderboard${params}`)
}

/**
 * This user's saved teams (reusable lineup templates) for one fixture.
 * Scoped to a fixture, not a contest: Game_logic/dream11.py's player pool is
 * drawn from a fixture's two clubs, so a saved team only ever makes sense
 * for another contest on that SAME fixture. Newest first.
 */
export function fetchSavedTeams({ fixture_id }) {
  const params = new URLSearchParams({ fixture_id: String(fixture_id) })
  return request(`/dream11/saved-teams?${params}`)
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

/**
 * player_ids are raw FPL ids, exactly 11, matching captain_id/vice_captain_id.
 * team_name is optional -- omitting it (or passing null/undefined) leaves the
 * leaderboard showing your account's own team_name, same as before this
 * field existed (Game_logic/dream11.py's dream11.teams.entry_name).
 */
export function submitContestTeam({ contest_id, user_id, player_ids, captain_id, vice_captain_id, team_name }) {
  return request(`/dream11/contests/${contest_id}/team`, {
    method: 'POST',
    body: { user_id, player_ids, captain_id, vice_captain_id, team_name },
  })
}

/**
 * Replaces an already-submitted team's full 11 + captain/vice in one call
 * (edit-only -- Game_logic/dream11.py's PATCH 404s if there's no existing
 * team yet, matching submitContestTeam's POST being create-only). Same
 * body shape as submitContestTeam so PickTeamPage can post to either.
 */
/** team_name is optional and additive -- omitting it leaves whatever name
 *  the entry already had (Game_logic/dream11.py's UPDATE_TEAM_ENTRY_NAME_STMT
 *  is a COALESCE, not a plain overwrite). */
export function editContestTeam({ contest_id, user_id, player_ids, captain_id, vice_captain_id, team_name }) {
  return request(`/dream11/contests/${contest_id}/team`, {
    method: 'PATCH',
    body: { user_id, player_ids, captain_id, vice_captain_id, team_name },
  })
}

/**
 * Saves the current lineup as a reusable template for this fixture --
 * independent of any contest, validated against the same formation rules
 * submitContestTeam uses (just no budget/price check, since no contest's
 * frozen prices apply here). Not a submission to any contest by itself.
 */
export function saveContestTeam({ fixture_id, name, player_ids, captain_id, vice_captain_id }) {
  return request('/dream11/saved-teams', {
    method: 'POST',
    body: { fixture_id, name, player_ids, captain_id, vice_captain_id },
  })
}

export function deleteSavedTeam({ saved_team_id }) {
  return request(`/dream11/saved-teams/${saved_team_id}`, { method: 'DELETE' })
}

/** Creator-only, and only before the contest locks -- see
 *  Game_logic/dream11.py's delete_contest for the exact 403/422 cases. */
export function deleteContest({ contest_id }) {
  return request(`/dream11/contests/${contest_id}`, { method: 'DELETE' })
}
