import { useEffect, useState } from 'react'
import { Link, useNavigate, useOutletContext } from 'react-router-dom'
import FplHeader from '../../components/FplHeader/FplHeader'
import TeamBadge from '../../components/TeamBadge/TeamBadge'
import DashboardIcon from '../../components/DashboardIcon/DashboardIcon'
import { CONTEST_STATUS_CLASSES, contestStatus, isContestLocked } from '../../data/contestStatus'
import { fetchFixtures } from '../../api/fixtures'
import {
  createContest,
  deleteContest,
  fetchSavedTeams,
  fetchUserContests,
  joinContest,
  submitContestTeam,
  ValidationError,
} from '../../api/dream11'

const FIELD =
  'rounded-xl border border-transparent bg-[#F5F3F0] px-4 py-3 font-body-md text-body-md text-on-surface ' +
  'focus:border-[#8DAA3C] focus:ring-1 focus:ring-[#8DAA3C] outline-none h-12'

const MIN_MEMBERS = 2
const MAX_MEMBERS = 50

function normalizeErrors(err) {
  if (err instanceof ValidationError) return err.errors
  return [err.message || 'Something went wrong']
}

// Same small helper PickTeamPage.jsx's SavedTeamCard uses, for the same
// "1-4-4-2"-style summary in the create form's saved-team dropdown.
function formationOf(players) {
  const counts = { GK: 0, DEF: 0, MID: 0, FWD: 0 }
  for (const p of players) if (counts[p.position] != null) counts[p.position] += 1
  return ['GK', 'DEF', 'MID', 'FWD'].map((p) => counts[p]).join('-')
}

/**
 * The Leagues page: create a contest (tied to one match -- this is the
 * fixture picker MatchDetailPage's own forms don't need, since
 * MatchDetailPage already has a fixture from its URL), join one by code,
 * and the list of contests you're already in, each opening straight onto
 * its leaderboard.
 *
 * Create stays on this page and refreshes "Your Leagues" in place, rather
 * than redirecting into the team builder: picking a team is now a
 * decoupled action (build/save one from the Match screen, or open the new
 * contest's own picker whenever you're ready), not something creating a
 * contest should force immediately. Join still lands in the builder --
 * you're not fully a member of someone else's contest until you've
 * actually submitted a team, so skipping that step there would leave you
 * in a half-joined state.
 */
function Dream11ContestsPage() {
  const { settings } = useOutletContext()
  const { user_id, season } = settings
  const navigate = useNavigate()

  const [contests, setContests] = useState([])
  const [fixtures, setFixtures] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [createForm, setCreateForm] = useState({ fixture_id: '', name: '', max_members: 50, saved_team_id: '' })
  const [savedTeamsForFixture, setSavedTeamsForFixture] = useState([])
  const [joinCode, setJoinCode] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [formErrors, setFormErrors] = useState([])
  const [formNotice, setFormNotice] = useState('')
  const [deletingId, setDeletingId] = useState(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')

    Promise.all([
      fetchUserContests({ user_id }),
      fetchFixtures({ season, upcoming_only: true }),
    ])
      .then(([contestRows, fixtureRows]) => {
        if (cancelled) return
        setContests(contestRows)
        setFixtures(fixtureRows)
        setCreateForm((f) => (f.fixture_id ? f : { ...f, fixture_id: fixtureRows[0]?.fixture_id ?? '' }))
      })
      .catch((err) => {
        if (!cancelled) setError(err.message || 'Could not load leagues')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [user_id, season])

  // Refreshed whenever the selected match changes -- a saved team only ever
  // makes sense for its own fixture (Game_logic/dream11.py's whole player
  // pool is drawn from a fixture's two clubs), so switching matches must
  // drop whatever was selected for the previous one.
  useEffect(() => {
    if (!createForm.fixture_id) {
      setSavedTeamsForFixture([])
      return undefined
    }
    let cancelled = false
    fetchSavedTeams({ fixture_id: createForm.fixture_id })
      .then((teams) => {
        if (cancelled) return
        setSavedTeamsForFixture(teams)
        // Drop a stale selection from the PREVIOUS fixture rather than
        // silently submitting the wrong team.
        setCreateForm((f) => (teams.some((t) => t.saved_team_id === f.saved_team_id) ? f : { ...f, saved_team_id: '' }))
      })
      .catch(() => {
        // Non-fatal: the form still works without a saved team to offer.
      })
    return () => {
      cancelled = true
    }
  }, [createForm.fixture_id])

  async function handleCreate(event) {
    event.preventDefault()
    setSubmitting(true)
    setFormErrors([])
    setFormNotice('')
    try {
      const created = await createContest({
        fixture_id: Number(createForm.fixture_id),
        name: createForm.name.trim(),
        user_id,
        max_members: Number(createForm.max_members),
      })

      // Optional: submit a saved team as this contest's entry immediately,
      // so creating the league and picking a team for it happen in one
      // step when you already have a lineup ready. A failure here (e.g.
      // the saved team no longer fits this contest's freshly frozen
      // prices) must NOT read as "contest creation failed" -- the contest
      // is real either way, just report it separately and let the picker
      // pick up the rest.
      const savedTeam = savedTeamsForFixture.find(
        (t) => t.saved_team_id === createForm.saved_team_id
      )
      if (savedTeam) {
        try {
          await submitContestTeam({
            contest_id: created.contest_id,
            user_id,
            player_ids: savedTeam.players.map((p) => p.player_id),
            captain_id: savedTeam.players.find((p) => p.is_captain)?.player_id,
            vice_captain_id: savedTeam.players.find((p) => p.is_vice_captain)?.player_id,
            // The saved team's own name follows through onto the leaderboard --
            // this was the actual bug report: picking "Guaranteed Cheap Team"
            // here used to show your account's team_name instead everywhere.
            team_name: savedTeam.name,
          })
        } catch (err) {
          setFormNotice(
            `League created, but "${savedTeam.name}" couldn't be submitted automatically ` +
              `(${normalizeErrors(err).join('; ')}) -- open it to pick a team.`
          )
        }
      }

      // Stay on this page -- refetch rather than hand-build the new card's
      // shape locally, since CreateContestResponse and the ContestSummaryResponse
      // this list renders are two different response models (this list needs
      // home_team/away_team/gameweek/is_locked/user_* fields create's response
      // doesn't carry).
      setCreateForm((f) => ({ ...f, name: '', saved_team_id: '' }))
      const refreshed = await fetchUserContests({ user_id })
      setContests(refreshed)
    } catch (err) {
      setFormErrors(normalizeErrors(err))
    } finally {
      setSubmitting(false)
    }
  }

  async function handleJoin(event) {
    event.preventDefault()
    setSubmitting(true)
    setFormErrors([])
    try {
      const joined = await joinContest({ user_id, code: joinCode.trim().toUpperCase() })
      navigate(`/dream11/contests/${joined.contest_id}/pick`)
    } catch (err) {
      setFormErrors(normalizeErrors(err))
      setSubmitting(false)
    }
  }

  // Same one-click-with-native-confirm pattern AccountMenu's own delete uses.
  // Only the creator sees the button at all (gated below, at the card), so
  // the 403 this can still hit is just a stale-view race, not the normal
  // path -- surfaced the same way any other failure here is.
  async function handleDeleteContest(event, contestId) {
    event.preventDefault()
    event.stopPropagation()
    if (deletingId) return
    const confirmed = window.confirm(
      'Delete this league? Every member and their submitted team will be removed too. This cannot be undone.'
    )
    if (!confirmed) return

    setDeletingId(contestId)
    try {
      await deleteContest({ contest_id: contestId })
      setContests((rows) => rows.filter((c) => c.contest_id !== contestId))
    } catch (err) {
      setError(err.message || 'Could not delete this league')
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <>
      <FplHeader helpTo="/dream11/scoring" showHelp title="Leagues" />
      <main className="w-full px-4 py-3 pb-28 flex flex-col gap-4 bg-[#FBF9F5] min-h-screen font-['Helvetica_Neue',Helvetica,Arial,sans-serif]">
        {formErrors.length > 0 && (
          <div className="bg-error-container text-on-error-container rounded-lg p-sm flex flex-col gap-xs" role="alert">
            {formErrors.map((message) => (
              <p className="font-label-md text-label-md leading-normal" key={message}>
                {message}
              </p>
            ))}
          </div>
        )}

        {formNotice && (
          <div className="bg-secondary-container text-on-secondary-container rounded-lg p-sm" role="status">
            <p className="font-label-md text-label-md leading-normal">{formNotice}</p>
          </div>
        )}

        <form
          className="bg-white rounded-[20px] p-5 border border-[#E5E6E1] shadow-[0_2px_8px_rgba(0,0,0,0.04)] flex flex-col gap-3"
          onSubmit={handleCreate}
        >
          <h2 className="font-headline-sm text-[17px] font-bold text-on-surface">Create League</h2>
          <p className="font-label-md text-label-md text-on-surface-variant">
            Pick a match and name your league. It'll appear below, ready to open whenever you want
            to pick a team.
          </p>
          <label className="font-label-md text-[11px] font-bold uppercase tracking-wider text-on-surface" htmlFor="league-fixture">
            Match
          </label>
          <select
            className={`${FIELD} w-full`}
            data-testid="league-fixture"
            id="league-fixture"
            onChange={(event) => setCreateForm((f) => ({ ...f, fixture_id: event.target.value }))}
            required
            value={createForm.fixture_id}
          >
            {fixtures.length === 0 && <option value="">No upcoming matches</option>}
            {fixtures.map((f) => (
              <option key={f.fixture_id} value={f.fixture_id}>
                {f.home_team} v {f.away_team} · {kickoffLabel(f.kickoff_time)}
              </option>
            ))}
          </select>
          <input
            aria-label="League name"
            className={`${FIELD} w-full`}
            data-testid="league-name"
            maxLength={100}
            onChange={(event) => setCreateForm((f) => ({ ...f, name: event.target.value }))}
            placeholder="e.g. Weekend Warriors"
            required
            value={createForm.name}
          />
          <label className="font-label-md text-label-md text-on-surface-variant" htmlFor="league-max-members">
            Max members — how many friends can join ({MIN_MEMBERS}-{MAX_MEMBERS})
          </label>
          <input
            className={`${FIELD} w-full`}
            data-testid="league-max-members"
            id="league-max-members"
            max={MAX_MEMBERS}
            min={MIN_MEMBERS}
            onChange={(event) => setCreateForm((f) => ({ ...f, max_members: event.target.value }))}
            type="number"
            value={createForm.max_members}
          />
          {/* Optional -- scoped to whichever match is selected above
              (Game_logic/dream11.py's player pool is fixture-specific, so a
              saved team only ever fits its own fixture). Picking one here
              submits it as your entry the moment the league is created,
              instead of leaving you to open the new contest's picker
              separately afterward. */}
          <label className="font-label-md text-label-md text-on-surface-variant" htmlFor="league-saved-team">
            Use a saved team (optional)
          </label>
          <select
            className={`${FIELD} w-full`}
            data-testid="league-saved-team"
            id="league-saved-team"
            onChange={(event) =>
              // Number(), not the raw string -- <select> values are always
              // strings, but team.saved_team_id from the API is a number,
              // and a strict === comparison against it (below, and in the
              // fixture-change effect above) would otherwise never match.
              setCreateForm((f) => ({
                ...f,
                saved_team_id: event.target.value ? Number(event.target.value) : '',
              }))
            }
            value={createForm.saved_team_id}
          >
            <option value="">
              {savedTeamsForFixture.length === 0 ? 'No saved teams for this match yet' : "Pick later — don't submit one now"}
            </option>
            {savedTeamsForFixture.map((team) => (
              <option key={team.saved_team_id} value={team.saved_team_id}>
                {team.name} ({formationOf(team.players)})
              </option>
            ))}
          </select>
          <button
            className="mt-1 w-full bg-[#8DAA3C] text-white rounded-xl py-3.5 font-label-md text-label-md font-bold shadow-sm flex items-center justify-center gap-2 disabled:opacity-50 active:scale-[0.99] transition-all"
            data-testid="league-create"
            disabled={submitting || !createForm.fixture_id}
            type="submit"
          >
            <DashboardIcon name="add" size={18} strokeWidth={2.2} />
            {submitting ? 'Creating…' : 'Create Contest'}
          </button>
        </form>

        <form
          className="bg-white rounded-[20px] p-5 border border-[#E5E6E1] shadow-[0_2px_8px_rgba(0,0,0,0.04)] flex flex-col gap-3"
          onSubmit={handleJoin}
        >
          <h2 className="font-headline-sm text-[17px] font-bold text-on-surface">Join League</h2>
          <p className="font-label-md text-label-md text-on-surface-variant">
            Enter a friend&apos;s invite code, then pick your XI.
          </p>
          <div className="flex gap-2">
            <input
              aria-label="League invite code"
              className={`${FIELD} flex-1 min-w-0 font-mono uppercase tracking-wider placeholder:font-sans placeholder:normal-case placeholder:text-outline`}
              data-testid="league-join-code"
              onChange={(event) => setJoinCode(event.target.value.toUpperCase())}
              placeholder="ENTER INVITE CODE"
              required
              value={joinCode}
            />
            <button
              className="bg-[#8DAA3C] text-white font-label-md text-label-md font-bold px-5 rounded-xl h-12 shrink-0 disabled:opacity-50 whitespace-nowrap"
              disabled={submitting}
              type="submit"
            >
              {submitting ? 'Joining…' : 'Join Team'}
            </button>
          </div>
        </form>

        {/* Every league you've created or joined, each opening onto its own
            leaderboard. Create refreshes this list in place (see
            handleCreate); Join still redirects into the team builder. */}
        <section aria-label="Your leagues" className="flex flex-col gap-3">
          <h2 className="font-headline-sm text-[18px] font-bold text-on-surface">Your Leagues</h2>
          <p className="font-body-md text-body-md text-on-surface-variant">
            {contests.length} league{contests.length === 1 ? '' : 's'} · single-match, 100 credits, 11
            players.
          </p>

          {error && (
            <div className="bg-error-container text-on-error-container rounded-lg p-sm" role="alert">
              <p className="font-label-md text-label-md">{error}</p>
            </div>
          )}

          {loading ? (
            <p className="font-body-md text-body-md text-on-surface-variant">Loading leagues…</p>
          ) : contests.length === 0 ? (
            <p className="font-body-md text-body-md text-on-surface-variant">
              No leagues yet — create one or join one with a code above to get started.
            </p>
          ) : (
            <div className="flex flex-col gap-sm">
              {contests.map((contest) => (
                <Link
                  className="bg-white rounded-[20px] p-4 border border-[#E5E6E1] shadow-[0_2px_8px_rgba(0,0,0,0.04)] hover:shadow-md transition-shadow flex justify-between items-start gap-sm"
                  data-testid="dream11-contest-card"
                  key={contest.contest_id}
                  to={`/dream11/contests/${contest.contest_id}`}
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 mb-1 flex-wrap">
                      <h2 className="font-headline-sm text-[16px] font-bold text-on-surface truncate">
                        {contest.name}
                      </h2>
                      <span
                        className={`px-2 py-0.5 rounded-full text-[10px] font-bold tracking-wider uppercase whitespace-nowrap ${
                          CONTEST_STATUS_CLASSES[contestStatus(contest).status]
                        }`}
                        data-testid="dream11-contest-status"
                      >
                        {contestStatus(contest).label}
                      </span>
                    </div>
                    <div className="flex items-center gap-2 rounded-xl bg-[#F5F3F0] px-2 py-2">
                      <TeamBadge shortName={contest.home_team} size="sm" />
                      <p className="font-body-md text-body-md text-on-surface-variant">
                        {contest.home_team} v {contest.away_team} · GW{contest.gameweek}
                      </p>
                      <TeamBadge shortName={contest.away_team} size="sm" />
                    </div>
                    <p className="font-label-md text-label-md text-on-surface-variant mt-1">
                      {contest.member_count}/{contest.max_members} members
                      {contest.user_has_team || isContestLocked(contest) ? '' : ' · you haven’t picked yet'}
                    </p>
                  </div>
                  <div className="text-right shrink-0 flex flex-col items-end gap-1">
                    {/* Creator-only, and only before kickoff -- matches
                        delete_contest's own 403/422 rules, so this never
                        shows somewhere the click would just bounce off the
                        backend anyway. */}
                    {contest.created_by === user_id && !isContestLocked(contest) && (
                      <button
                        aria-label={`Delete ${contest.name}`}
                        className="w-8 h-8 rounded-full bg-[#F5F3F0] text-on-surface-variant hover:bg-error-container hover:text-on-error-container flex items-center justify-center transition-colors disabled:opacity-50"
                        data-testid="dream11-contest-delete"
                        disabled={deletingId === contest.contest_id}
                        onClick={(event) => handleDeleteContest(event, contest.contest_id)}
                        type="button"
                      >
                        <DashboardIcon name={deletingId === contest.contest_id ? 'sync' : 'trash'} size={15} />
                      </button>
                    )}
                    <p className="font-stats-number text-stats-number tabular-nums text-on-surface">
                      {contest.user_total_points}
                    </p>
                    <p className="font-label-md text-label-md text-on-surface-variant opacity-70">
                      {contest.user_rank ? `Rank ${contest.user_rank}` : 'Pts'}
                    </p>
                    {!isContestLocked(contest) && (
                      <span className="mt-1 inline-flex items-center gap-1 font-label-md text-[10px] font-bold text-[#658223]">
                        <span className="h-1.5 w-1.5 rounded-full bg-[#8DAA3C] animate-pulse" /> Active now
                      </span>
                    )}
                  </div>
                </Link>
              ))}
            </div>
          )}
        </section>
      </main>
    </>
  )
}

function kickoffLabel(kickoffTime) {
  if (!kickoffTime) return 'Kickoff TBC'
  const date = new Date(kickoffTime)
  if (Number.isNaN(date.getTime())) return 'Kickoff TBC'
  return date.toLocaleString(undefined, {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export default Dream11ContestsPage
