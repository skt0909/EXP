import { useEffect, useMemo, useState } from 'react'
import { Link, useOutletContext } from 'react-router-dom'
import { fetchFixtures } from '../../api/fixtures'

function kickoffLabel(kickoffTime) {
  if (!kickoffTime) return 'Time TBC'
  const date = new Date(kickoffTime)
  if (Number.isNaN(date.getTime())) return 'Time TBC'

  const now = new Date()
  const sameDay = date.toDateString() === now.toDateString()
  const tomorrow = new Date(now)
  tomorrow.setDate(now.getDate() + 1)
  const isTomorrow = date.toDateString() === tomorrow.toDateString()

  const time = date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  if (sameDay) return `Today ${time}`
  if (isTomorrow) return `Tomorrow ${time}`
  return `${date.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })} ${time}`
}

/** "1d 5h" / "2h 15m" / "12m" -- null once kickoff has passed. */
function countdown(kickoffTime, now) {
  if (!kickoffTime) return null
  const ms = new Date(kickoffTime).getTime() - now
  if (Number.isNaN(ms) || ms <= 0) return null

  const totalMinutes = Math.floor(ms / 60000)
  const days = Math.floor(totalMinutes / 1440)
  const hours = Math.floor((totalMinutes % 1440) / 60)
  const minutes = totalMinutes % 60
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${minutes}m`
  return `${minutes}m`
}

function Scoreline({ fixture }) {
  const hasScore = fixture.home_score != null && fixture.away_score != null
  return (
    <div className="flex items-center justify-between gap-sm">
      <div className="flex-1 min-w-0">
        <p className="font-headline-sm text-headline-sm text-primary truncate">{fixture.home_team}</p>
        <p className="font-label-md text-label-md text-on-surface-variant">HOME</p>
      </div>
      {hasScore ? (
        <p className="font-stats-number text-stats-number text-on-surface shrink-0 px-sm">
          {fixture.home_score}–{fixture.away_score}
        </p>
      ) : (
        <p className="font-label-md text-label-md text-on-surface-variant shrink-0 px-sm">vs</p>
      )}
      <div className="flex-1 min-w-0 text-right">
        <p className="font-headline-sm text-headline-sm text-primary truncate">{fixture.away_team}</p>
        <p className="font-label-md text-label-md text-on-surface-variant">AWAY</p>
      </div>
    </div>
  )
}

/** The user's result on this match, when they have one.
 *
 *  Plain markup, not a link: the whole card is the tap target now, and an <a>
 *  inside an <a> is invalid HTML that browsers resolve unpredictably. The
 *  contest it used to point at is one tap further on, from the match page. */
function PerformanceStrip({ fixture }) {
  if (!fixture.user_contest_id) return null

  const ranked = fixture.user_rank > 0
  return (
    <div className="mt-sm flex items-center justify-between gap-sm bg-surface-container-high rounded-lg px-3 py-2">
      <span className="flex items-center gap-2 min-w-0">
        <span className="material-symbols-outlined text-[18px] text-on-surface-variant">leaderboard</span>
        <span className="font-label-md text-label-md text-on-surface-variant truncate">
          {ranked
            ? `Your performance · Rank ${fixture.user_rank} of ${fixture.user_contest_size}`
            : 'Your performance · not scored yet'}
        </span>
      </span>
      <span className="font-label-md text-label-md text-on-primary bg-secondary rounded px-2 py-0.5 shrink-0">
        {fixture.user_points} pts
      </span>
    </div>
  )
}

function MatchCard({ fixture, now }) {
  const remaining = countdown(fixture.kickoff_time, now)
  const inContests = fixture.user_contest_count > 0

  // The whole card is the tap target. It used to be a plain div with only the
  // small CTA at the bottom navigating, so tapping the teams or the score --
  // the obvious place to press -- did nothing at all.
  return (
    <Link
      className="block bg-surface-container-lowest rounded-xl p-md border border-outline-variant shadow-sm hover:shadow-md transition-shadow"
      data-testid="match-card"
      to={`/matches/${fixture.fixture_id}`}
    >
      <div className="flex items-center justify-between gap-sm mb-sm">
        <span className="font-label-md text-label-md text-on-surface-variant">
          {fixture.status === 'live' ? `GW${fixture.gameweek}` : kickoffLabel(fixture.kickoff_time)}
        </span>
        <span className="flex items-center gap-2 shrink-0">
          {fixture.contest_count > 0 && (
            <span className="bg-primary text-on-primary text-[10px] font-bold px-2 py-0.5 rounded whitespace-nowrap">
              {inContests
                ? `In ${fixture.user_contest_count} contest${fixture.user_contest_count === 1 ? '' : 's'}`
                : `${fixture.contest_count} contest${fixture.contest_count === 1 ? '' : 's'}`}
            </span>
          )}
          {/* No match minute: nothing in the schema stores a clock, and
              guessing one from kickoff_time goes wrong by ~15 minutes across
              halftime -- precisely while the user can see the real clock. */}
          {fixture.status === 'live' && (
            <span
              className="flex items-center gap-1 text-error font-label-md text-label-md"
              data-testid="live-pill"
            >
              <span className="w-2 h-2 rounded-full bg-error animate-pulse" />
              LIVE
            </span>
          )}
          {remaining && (
            <span className="font-label-md text-label-md text-on-surface-variant whitespace-nowrap">
              {remaining}
            </span>
          )}
        </span>
      </div>

      <Scoreline fixture={fixture} />
      <PerformanceStrip fixture={fixture} />

      {/* Affordances only -- the card itself navigates. Completed matches get
          one too, so a finished contest is still reachable from here. */}
      {inContests ? (
        <span className="mt-sm flex items-center justify-center gap-2 text-secondary font-label-md text-label-md py-1">
          View contests
          <span className="material-symbols-outlined text-[16px]">chevron_right</span>
        </span>
      ) : fixture.status === 'completed' ? (
        <span className="mt-sm flex items-center justify-center gap-2 text-on-surface-variant font-label-md text-label-md py-1">
          View match
          <span className="material-symbols-outlined text-[16px]">chevron_right</span>
        </span>
      ) : (
        <span className="mt-sm flex items-center justify-center gap-2 bg-surface-container-high text-on-surface rounded-lg py-2 font-label-md text-label-md">
          <span className="material-symbols-outlined text-[18px]">add_circle</span>
          Create or Join Contest
        </span>
      )}
    </Link>
  )
}

/**
 * A season is 380 fixtures, and GET /fixtures returns all of them -- rendering
 * the lot produced a 54,000px page. Sections are capped and expandable rather
 * than the request being narrowed, because there is no trustworthy "current
 * gameweek" to filter on (see config/season.js: CURRENT_GAMEWEEK is a
 * hardcoded constant precisely because the backend has no such endpoint).
 * Live is never capped -- there are at most a handful, and they're the point.
 */
const SECTION_LIMIT = 6

function Section({ title, dot, fixtures, now, limit = SECTION_LIMIT }) {
  const [expanded, setExpanded] = useState(false)
  if (fixtures.length === 0) return null

  const shown = expanded ? fixtures : fixtures.slice(0, limit)
  const hidden = fixtures.length - shown.length

  return (
    <section aria-label={title} className="flex flex-col gap-sm">
      <h2 className="font-headline-sm text-headline-sm text-primary flex items-center gap-2">
        {title}
        {dot && <span className="w-2 h-2 rounded-full bg-error animate-pulse" />}
        <span className="font-label-md text-label-md text-on-surface-variant font-normal">
          {fixtures.length}
        </span>
      </h2>
      {shown.map((f) => (
        <MatchCard fixture={f} key={f.fixture_id} now={now} />
      ))}
      {hidden > 0 && (
        <button
          className="text-secondary font-label-md text-label-md py-2"
          data-testid="section-show-more"
          onClick={() => setExpanded(true)}
          type="button"
        >
          Show {hidden} more
        </button>
      )}
    </section>
  )
}

/**
 * Contests-mode match list: Live Now / Upcoming / Completed.
 *
 * The grouping comes from the server's `status`, not from `finished` --
 * ml.fixtures.finished can lag a match's actual end (see
 * Game_logic/fixtures.py's LIVE_WINDOW_MINUTES), so deciding it here would
 * strand ended matches under "Live Now".
 */
function MatchesPage() {
  const { settings } = useOutletContext()
  const { user_id, season } = settings

  const [fixtures, setFixtures] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')

    fetchFixtures({ season })
      .then((data) => {
        if (!cancelled) setFixtures(data)
      })
      .catch((err) => {
        if (!cancelled) setError(err.message || 'Could not load matches')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [season, user_id])

  // Drives the countdowns only -- one timer for the page, not one per card.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30000)
    return () => clearInterval(id)
  }, [])

  const grouped = useMemo(() => {
    const live = fixtures.filter((f) => f.status === 'live')
    const upcoming = fixtures.filter((f) => f.status === 'upcoming')
    // Newest first: the match that just ended is the one you want to see.
    const completed = fixtures
      .filter((f) => f.status === 'completed')
      .slice()
      .reverse()
    return { live, upcoming, completed }
  }, [fixtures])

  return (
    <main className="w-full px-safe-margin py-md flex flex-col gap-lg">
      <header>
        <h1 className="font-display-lg text-display-lg text-primary">Matches</h1>
        <p className="font-body-md text-body-md text-on-surface-variant mt-sm">
          {season} · pick a match to create or join a contest.
        </p>
      </header>

      {error && (
        <div className="bg-error-container text-on-error-container rounded-lg p-sm" role="alert">
          <p className="font-label-md text-label-md">{error}</p>
        </div>
      )}

      {loading ? (
        <p className="font-body-md text-body-md text-on-surface-variant">Loading matches…</p>
      ) : fixtures.length === 0 ? (
        <p className="font-body-md text-body-md text-on-surface-variant">
          No matches for this season yet.
        </p>
      ) : (
        <>
          <Section dot fixtures={grouped.live} limit={Infinity} now={now} title="Live Now" />
          <Section fixtures={grouped.upcoming} now={now} title="Upcoming" />
          <Section fixtures={grouped.completed} now={now} title="Completed" />
        </>
      )}
    </main>
  )
}

export default MatchesPage
