import { useEffect, useState } from 'react'
import { Link, useNavigate, useOutletContext, useParams } from 'react-router-dom'
import { fetchFixtures } from '../../api/fixtures'
import { fetchFixtureContests, joinContest, ValidationError } from '../../api/dream11'
import { MODE_CONTESTS, MODE_HOME } from '../../config/appMode'
import TeamBadge from '../../components/TeamBadge/TeamBadge'
import DetailHeader from '../../components/DetailHeader/DetailHeader'

const FIELD =
  'rounded-lg border border-outline-variant bg-surface px-3 py-2 font-body-md text-body-md text-on-surface ' +
  'focus:border-secondary focus:ring-1 focus:ring-secondary outline-none h-10'

function normalizeErrors(err) {
  if (err instanceof ValidationError) return err.errors
  return [err.message || 'Something went wrong']
}

function countdown(kickoffTime) {
  if (!kickoffTime) return null
  const ms = new Date(kickoffTime).getTime() - Date.now()
  if (Number.isNaN(ms) || ms <= 0) return null
  const totalMinutes = Math.floor(ms / 60000)
  const days = Math.floor(totalMinutes / 1440)
  const hours = Math.floor((totalMinutes % 1440) / 60)
  const minutes = totalMinutes % 60
  if (days > 0) return `${days}d ${hours}h ${minutes}m`
  if (hours > 0) return `${hours}h ${minutes}m`
  return `${minutes}m`
}

/**
 * One match, its contests, and the join/create forms.
 *
 * The rules strip ("11 players · 100 credits · Captain 2x, Vice 1.5x") is
 * rendered from a contest's own summary rather than hardcoded, so it can't
 * drift from dream11_scoring.py. That means it only appears once at least one
 * contest exists on this fixture -- those numbers reach the API attached to a
 * contest, and a match with none has nothing to read them from.
 */
function MatchDetailPage() {
  const { fixtureId } = useParams()
  const { settings } = useOutletContext()
  const { user_id, season } = settings
  const navigate = useNavigate()

  const [fixture, setFixture] = useState(null)
  const [contests, setContests] = useState([])
  const [loading, setLoading] = useState(true)
  const [errors, setErrors] = useState([])
  const [submitting, setSubmitting] = useState(false)
  const [joinCode, setJoinCode] = useState('')

  function handleBack() {
    if (window.history.state?.idx > 0) navigate(-1)
    else navigate(MODE_HOME[MODE_CONTESTS])
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setErrors([])

    // No single-fixture endpoint exists; a season is 380 rows at most and the
    // list is already the Matches screen's payload, so this reuses it rather
    // than adding a route for one object.
    Promise.all([
      fetchFixtures({ season }),
      fetchFixtureContests({ fixture_id: Number(fixtureId), user_id }),
    ])
      .then(([allFixtures, fixtureContests]) => {
        if (cancelled) return
        setFixture(allFixtures.find((f) => f.fixture_id === Number(fixtureId)) ?? null)
        setContests(fixtureContests)
      })
      .catch((err) => {
        if (!cancelled) setErrors(normalizeErrors(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [fixtureId, season, user_id])

  async function handleJoin(event) {
    event.preventDefault()
    setSubmitting(true)
    setErrors([])
    try {
      const joined = await joinContest({ user_id, code: joinCode.trim().toUpperCase() })
      setJoinCode('')
      // Same as Create: joining isn't the goal, picking a team is. Landing
      // back on this page left "select a team" as an easy-to-skip second
      // tap, and the code you just typed doesn't name a fixture to pick
      // from until you're already a member (the player pool is frozen per
      // contest_id, not per fixture -- see dream11.py's get_contest_pool).
      navigate(`/dream11/contests/${joined.contest_id}/pick`)
    } catch (err) {
      setErrors(normalizeErrors(err))
    } finally {
      setSubmitting(false)
    }
  }

  const rules = contests[0] ?? null
  const remaining = countdown(fixture?.kickoff_time)

  return (
    <main className="w-full px-safe-margin py-md flex flex-col gap-lg">
      <DetailHeader onBack={handleBack} title="Match" />

      {loading ? (
        <p className="font-body-md text-body-md text-on-surface-variant">Loading match…</p>
      ) : !fixture ? (
        <p className="font-body-md text-body-md text-on-surface-variant">Match not found.</p>
      ) : (
        <>
          <div className="bg-surface-container-lowest rounded-xl p-md border border-outline-variant shadow-sm">
            <p className="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider">
              GW{fixture.gameweek} · Premier League
            </p>
            <div className="flex items-center justify-between gap-sm mt-sm">
              <div className="flex items-center gap-sm flex-1 min-w-0">
                <TeamBadge name={fixture.home_team_name} shortName={fixture.home_team} />
                <p className="font-headline-sm text-headline-sm text-primary truncate">
                  {fixture.home_team}
                </p>
              </div>
              <p className="font-label-md text-label-md text-on-surface-variant px-sm shrink-0">vs</p>
              <div className="flex items-center justify-end gap-sm flex-1 min-w-0">
                <p className="font-headline-sm text-headline-sm text-primary text-right truncate">
                  {fixture.away_team}
                </p>
                <TeamBadge name={fixture.away_team_name} shortName={fixture.away_team} />
              </div>
            </div>
            <p className="font-label-md text-label-md text-on-surface-variant mt-sm text-center">
              {remaining ? `Starts in ${remaining}` : fixture.status === 'live' ? 'In progress' : 'Started'}
            </p>
            {rules && (
              <p
                className="font-label-md text-label-md text-on-surface-variant mt-sm text-center bg-surface-container-high rounded-lg px-3 py-2"
                data-testid="contest-rules"
              >
                {rules.team_size} players from this match · {rules.budget_cap} credits · Captain{' '}
                {rules.captain_multiplier}x, Vice {rules.vice_captain_multiplier}x · Locks at kickoff
                · No substitutions
              </p>
            )}
          </div>

          {errors.length > 0 && (
            <div className="bg-error-container text-on-error-container rounded-lg p-sm flex flex-col gap-xs" role="alert">
              {errors.map((message) => (
                <p className="font-label-md text-label-md leading-normal" key={message}>
                  {message}
                </p>
              ))}
            </div>
          )}

          <section aria-label="Your contests" className="flex flex-col gap-sm">
            <h2 className="font-headline-sm text-headline-sm text-primary">Your Contests</h2>
            {contests.length === 0 ? (
              <p className="font-body-md text-body-md text-on-surface-variant">
                You&apos;re not in a contest on this match yet.
              </p>
            ) : (
              contests.map((contest) => (
                <Link
                  className="bg-surface-container-lowest rounded-xl p-md border border-outline-variant shadow-sm flex justify-between items-start gap-sm"
                  data-testid="fixture-contest"
                  key={contest.contest_id}
                  // The design's "Pick Team" CTA: a contest you haven't picked
                  // for yet opens the builder, anything else opens the contest.
                  to={
                    contest.user_has_team || contest.is_locked
                      ? `/dream11/contests/${contest.contest_id}`
                      : `/dream11/contests/${contest.contest_id}/pick`
                  }
                >
                  <div className="min-w-0">
                    <h3 className="font-headline-sm text-headline-sm text-primary truncate">
                      {contest.name}
                    </h3>
                    <p className="font-label-md text-label-md text-on-surface-variant mt-1">
                      {contest.member_count}/{contest.max_members} members · code {contest.code}
                    </p>
                    {!contest.user_has_team && (
                      <p className="font-label-md text-label-md text-error flex items-center gap-1 mt-1">
                        <span className="material-symbols-outlined text-[16px]">warning</span>
                        Team not submitted
                      </p>
                    )}
                  </div>
                  <span className="material-symbols-outlined text-on-surface-variant shrink-0">
                    chevron_right
                  </span>
                </Link>
              ))
            )}
          </section>

          {/* Building a team no longer creates a contest as a side effect --
              that conflated two separate decisions (this fixture's team you
              want to keep around vs. a specific contest you want to run) and
              silently named the contest after whatever you'd typed as your
              team's name. Creating a contest is now exclusively a Leagues-
              page action; this just gets you to the picker. */}
          <Link
            className="bg-surface-container-lowest rounded-xl p-md border border-outline-variant shadow-sm flex items-center justify-between gap-sm hover:shadow-md transition-shadow"
            data-testid="build-team-link"
            to={`/matches/${fixtureId}/build`}
          >
            <div className="min-w-0">
              <h2 className="font-headline-sm text-headline-sm text-primary">Build & Save a Team</h2>
              <p className="font-label-md text-label-md text-on-surface-variant mt-1">
                Pick your XI for this match and save it for later — no contest needed yet.
                Create one from the Leagues page when you're ready to play it.
              </p>
            </div>
            <span className="material-symbols-outlined text-on-surface-variant shrink-0">
              chevron_right
            </span>
          </Link>

          <form
            className="bg-surface-container-lowest rounded-xl p-md border border-outline-variant shadow-sm flex flex-col gap-sm"
            onSubmit={handleJoin}
          >
            <h2 className="font-headline-sm text-headline-sm text-primary">Join a Contest</h2>
            <p className="font-label-md text-label-md text-on-surface-variant">
              Enter a friend&apos;s invite code, then pick your XI — you&apos;re not fully in until
              you&apos;ve submitted a team.
            </p>
            <div className="flex gap-2">
              <input
                aria-label="Contest invite code"
                className={`${FIELD} flex-1 min-w-0 uppercase placeholder:normal-case placeholder:text-outline`}
                data-testid="contest-join-code"
                onChange={(event) => setJoinCode(event.target.value.toUpperCase())}
                placeholder="Enter invite code"
                required
                value={joinCode}
              />
              <button
                className="bg-primary text-on-primary font-label-md text-label-md px-4 rounded-lg h-10 shrink-0 disabled:opacity-50 whitespace-nowrap"
                disabled={submitting}
                type="submit"
              >
                {submitting ? 'Joining…' : 'Join & Pick Team'}
              </button>
            </div>
          </form>
        </>
      )}
    </main>
  )
}

export default MatchDetailPage
