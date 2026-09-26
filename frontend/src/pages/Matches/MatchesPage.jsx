import { useEffect, useMemo, useState } from 'react'
import { Link, useOutletContext } from 'react-router-dom'
import FplHeader from '../../components/FplHeader/FplHeader'
import TeamBadge from '../../components/TeamBadge/TeamBadge'
import { fetchFixtures } from '../../api/fixtures'
import { kickoffLabel } from '../../data/kickoff'
import DashboardIcon from '../../components/DashboardIcon/DashboardIcon'

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
      <div className="flex items-center gap-sm flex-1 min-w-0">
        <TeamBadge name={fixture.home_team_name} shortName={fixture.home_team} />
        <div className="min-w-0">
          <p className="font-headline-sm text-headline-sm text-on-surface truncate">{fixture.home_team}</p>
          <p className="font-label-md text-[9px] font-bold tracking-wider text-on-surface-variant">HOME</p>
        </div>
      </div>
      {hasScore ? (
        <p className="font-stats-number text-stats-number text-on-surface shrink-0 px-sm">
          {fixture.home_score}–{fixture.away_score}
        </p>
      ) : (
        <p className="font-label-md text-[10px] font-bold tracking-wider text-on-surface-variant shrink-0 px-sm">VS</p>
      )}
      <div className="flex items-center justify-end gap-sm flex-1 min-w-0 text-right">
        <div className="min-w-0">
          <p className="font-headline-sm text-headline-sm text-on-surface truncate">{fixture.away_team}</p>
          <p className="font-label-md text-[9px] font-bold tracking-wider text-on-surface-variant">AWAY</p>
        </div>
        <TeamBadge name={fixture.away_team_name} shortName={fixture.away_team} />
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
    <div className="mt-3 flex items-center justify-between gap-sm bg-[#F5F3F0] rounded-xl px-3 py-3">
      <span className="flex items-center gap-2 min-w-0">
        <DashboardIcon className="text-on-surface-variant" name="chart" size={18} />
        <span className="font-label-md text-label-md text-on-surface-variant truncate">
          {ranked
            ? `Your performance · Rank ${fixture.user_rank} of ${fixture.user_contest_size}`
            : 'Your performance · not scored yet'}
        </span>
      </span>
      <span className="font-label-md text-label-md font-bold text-white bg-[#667D28] rounded-full px-2.5 py-1 shrink-0">
        {fixture.user_points} pts
      </span>
    </div>
  )
}

/**
 * flat = true renders without its own card chrome (border/rounded/shadow) --
 * used inside Section's `grouped` container, which draws one shared border
 * around a divide-y stack of rows instead of each match getting its own
 * card. Matches the Stitch "Matches" mockup's Completed section, which is
 * one bordered container of rows, not a stack of separate cards.
 */
function MatchCard({ fixture, now, flat = false }) {
  const remaining = countdown(fixture.kickoff_time, now)
  const inContests = fixture.user_contest_count > 0

  // The whole card is the tap target. It used to be a plain div with only the
  // small CTA at the bottom navigating, so tapping the teams or the score --
  // the obvious place to press -- did nothing at all.
  return (
    <Link
      className={
        flat
          ? 'block p-4 hover:bg-[#F7F7F2] transition-colors'
          : `relative block overflow-hidden bg-white rounded-[20px] p-4 border border-[#E5E6E1] shadow-[0_2px_8px_rgba(0,0,0,0.04)] hover:shadow-md transition-shadow ${inContests ? 'border-l-4 border-l-[#78952D]' : ''}`
      }
      data-testid="match-card"
      to={`/matches/${fixture.fixture_id}`}
    >
      <div className="flex items-center justify-between gap-sm mb-sm">
        <span className="font-label-md text-label-md text-on-surface-variant">
          {fixture.status === 'live' ? `GW${fixture.gameweek}` : kickoffLabel(fixture.kickoff_time)}
        </span>
        <span className="flex items-center gap-2 shrink-0">
          {fixture.contest_count > 0 && (
            <span className="bg-[#78952D] text-white text-[10px] font-bold px-2.5 py-1 rounded-full whitespace-nowrap">
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
            <span className="rounded-full bg-[#F0F0EA] px-2 py-1 font-label-md text-label-md tabular-nums text-on-surface-variant whitespace-nowrap">
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
        <span className="mt-sm flex items-center justify-center gap-2 text-[#667D28] font-label-md text-label-md font-bold py-1">
          View Leagues
          <DashboardIcon name="chevronRight" size={16} />
        </span>
      ) : fixture.status === 'completed' ? (
        <span className="mt-sm flex items-center justify-center gap-2 text-[#667D28] font-label-md text-label-md font-bold py-1">
          View match
          <DashboardIcon name="chevronRight" size={16} />
        </span>
      ) : (
        <span className="mt-3 flex items-center justify-center gap-2 bg-[#EDF2DF] text-[#4D631B] rounded-xl py-2.5 font-label-md text-label-md font-bold">
          <DashboardIcon name="add" size={17} strokeWidth={2.2} />
          Create Team
        </span>
      )}
    </Link>
  )
}

/**
 * A season is 380 fixtures, and GET /fixtures returns all of them -- rendering
 * the lot produced a 54,000px page. Sections are capped and expandable rather
 * than the request being narrowed to one gameweek, because this list is
 * meant to show the whole season's matches (live/upcoming/completed), not
 * just the current gameweek -- GET /gameweeks/current (config/gameweek.jsx)
 * exists now, but it answers "what gameweek can I set a team for," a
 * different question from "what should Matches display."
 * Live is never capped -- there are at most a handful, and they're the point.
 */
const SECTION_LIMIT = 6

function Section({ title, dot, fixtures, now, limit = SECTION_LIMIT, grouped = false }) {
  const [expanded, setExpanded] = useState(false)
  if (fixtures.length === 0) return null

  const shown = expanded ? fixtures : fixtures.slice(0, limit)
  const hidden = fixtures.length - shown.length

  const cards = shown.map((f) => <MatchCard flat={grouped} fixture={f} key={f.fixture_id} now={now} />)

  return (
    <section aria-label={title} className="flex flex-col gap-3">
      <h2 className="font-headline-sm text-[17px] font-bold text-on-surface flex items-center gap-2">
        {title}
        {dot && <span className="w-2 h-2 rounded-full bg-error animate-pulse" />}
        <span className="rounded-full bg-[#F0F0EA] px-2 py-0.5 font-label-md text-[10px] text-on-surface-variant font-bold">
          {fixtures.length}
        </span>
      </h2>
      {grouped ? (
        <div className="bg-white rounded-[20px] border border-[#E5E6E1] shadow-[0_2px_8px_rgba(0,0,0,0.04)] divide-y divide-[#E5E6E1] overflow-hidden">
          {cards}
          {hidden > 0 && (
            <button
              className="w-full p-md text-center text-primary-container font-label-md text-label-md hover:bg-surface-container-high transition-colors"
              data-testid="section-show-more"
              onClick={() => setExpanded(true)}
              type="button"
            >
              View all {fixtures.length} completed matches
            </button>
          )}
        </div>
      ) : (
        <>
          {cards}
          {hidden > 0 && (
            <button
              className="self-center rounded-full bg-white border border-[#E5E6E1] px-5 py-2 text-on-surface font-label-md text-label-md font-semibold shadow-sm"
              data-testid="section-show-more"
              onClick={() => setExpanded(true)}
              type="button"
            >
              Show {hidden} more matches
              <DashboardIcon className="inline-block ml-1 align-[-3px]" name="expandDown" size={15} />
            </button>
          )}
        </>
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
  const { user_id, season, gameweek } = settings

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

  const byStatus = useMemo(() => {
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
    <>
      <FplHeader helpTo="/dream11/scoring" showHelp title="Matches" />
      <main className="w-full px-4 py-3 pb-28 flex flex-col gap-4 bg-[#FBF9F5] min-h-screen font-['Helvetica_Neue',Helvetica,Arial,sans-serif]">
        <div className="flex items-center justify-between gap-3">
          <p className="font-body-md text-body-md text-on-surface-variant">
            {season} · Pick a match to create or join a contest.
          </p>
          <span className="shrink-0 rounded-full bg-[#8DAA3C]/15 px-2.5 py-1 font-label-md text-xs font-bold text-[#4D631B]">
            GW {gameweek}
          </span>
        </div>

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
          <Section dot fixtures={byStatus.live} limit={Infinity} now={now} title="Live Now" />
          <Section fixtures={byStatus.upcoming} now={now} title="Upcoming" />
          <Section grouped fixtures={byStatus.completed} now={now} title="Completed" />
        </>
      )}
      </main>
    </>
  )
}

export default MatchesPage
