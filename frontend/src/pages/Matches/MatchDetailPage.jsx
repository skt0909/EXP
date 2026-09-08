import { useEffect, useState } from 'react'
import { Link, useNavigate, useOutletContext, useParams } from 'react-router-dom'
import { fetchFixtures } from '../../api/fixtures'
import { createContest, fetchFixtureContests, joinContest, ValidationError } from '../../api/dream11'

const FIELD =
  'rounded-lg border border-outline-variant bg-surface px-3 py-2 font-body-md text-body-md text-on-surface ' +
  'focus:border-secondary focus:ring-1 focus:ring-secondary outline-none h-10'

const MIN_MEMBERS = 2
const MAX_MEMBERS = 50

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
  const [notice, setNotice] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [joinCode, setJoinCode] = useState('')
  const [createForm, setCreateForm] = useState({ name: '', max_members: 50 })

  async function loadContests() {
    const data = await fetchFixtureContests({ fixture_id: Number(fixtureId), user_id })
    setContests(data)
    return data
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
    setNotice('')
    try {
      const joined = await joinContest({ user_id, code: joinCode.trim().toUpperCase() })
      await loadContests()
      setJoinCode('')
      setNotice(`Joined ${joined.name}.`)
    } catch (err) {
      setErrors(normalizeErrors(err))
    } finally {
      setSubmitting(false)
    }
  }

  async function handleCreate(event) {
    event.preventDefault()
    setSubmitting(true)
    setErrors([])
    setNotice('')
    try {
      const created = await createContest({
        fixture_id: Number(fixtureId),
        name: createForm.name.trim(),
        user_id,
        max_members: Number(createForm.max_members),
      })
      // Straight into the team builder. Creating a contest is never the goal in
      // itself -- you create one so you can pick a team -- and stopping here
      // left people on a page whose most visible control was "Join a Contest".
      // The invite code isn't lost: the builder shows it, and so does the
      // contest card back on this page.
      navigate(`/dream11/contests/${created.contest_id}/pick`)
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
              <p className="font-headline-sm text-headline-sm text-primary flex-1 truncate">
                {fixture.home_team}
              </p>
              <p className="font-label-md text-label-md text-on-surface-variant px-sm">vs</p>
              <p className="font-headline-sm text-headline-sm text-primary flex-1 text-right truncate">
                {fixture.away_team}
              </p>
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
          {notice && (
            <div className="bg-secondary-container text-on-secondary-container rounded-lg p-sm font-body-md text-body-md">
              {notice}
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

          <form
            className="bg-surface-container-lowest rounded-xl p-md border border-outline-variant shadow-sm flex flex-col gap-sm"
            onSubmit={handleCreate}
          >
            <h2 className="font-headline-sm text-headline-sm text-primary">Pick a Team</h2>
            {/* Creating a contest is the means, not the end -- say what it
                actually gets you, since "Create a Contest" read as admin work
                next to a Join box that needs a code most people don't have. */}
            <p className="font-label-md text-label-md text-on-surface-variant">
              Start a contest for this match, then pick your XI. Invite friends
              with the code afterwards.
            </p>
            <input
              aria-label="Contest name"
              className={`${FIELD} w-full`}
              data-testid="contest-name"
              maxLength={100}
              onChange={(event) => setCreateForm((f) => ({ ...f, name: event.target.value }))}
              placeholder="e.g. Weekend Warriors"
              required
              value={createForm.name}
            />
            <label className="font-label-md text-label-md text-on-surface-variant" htmlFor="max-members">
              Max members — how many friends can join ({MIN_MEMBERS}-{MAX_MEMBERS})
            </label>
            <input
              className={`${FIELD} w-full`}
              data-testid="contest-max-members"
              id="max-members"
              max={MAX_MEMBERS}
              min={MIN_MEMBERS}
              onChange={(event) => setCreateForm((f) => ({ ...f, max_members: event.target.value }))}
              type="number"
              value={createForm.max_members}
            />
            <button
              className="w-full bg-primary-container text-on-primary rounded-lg py-2.5 font-label-md text-label-md flex items-center justify-center gap-2 disabled:opacity-50"
              data-testid="contest-create"
              disabled={submitting}
              type="submit"
            >
              <span className="material-symbols-outlined text-[18px]">add_circle</span>
              {submitting ? 'Creating…' : 'Create & Pick Team'}
            </button>
          </form>

          <form
            className="bg-surface-container-lowest rounded-xl p-md border border-outline-variant shadow-sm flex flex-col gap-sm"
            onSubmit={handleJoin}
          >
            <h2 className="font-headline-sm text-headline-sm text-primary">Join a Contest</h2>
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
                className="bg-primary text-on-primary font-label-md text-label-md px-4 rounded-lg h-10 shrink-0 disabled:opacity-50"
                disabled={submitting}
                type="submit"
              >
                Join
              </button>
            </div>
          </form>
        </>
      )}
    </main>
  )
}

export default MatchDetailPage
