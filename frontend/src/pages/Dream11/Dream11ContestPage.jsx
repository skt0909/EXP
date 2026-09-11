import { useEffect, useState } from 'react'
import { Link, useNavigate, useOutletContext, useParams } from 'react-router-dom'
import OpponentTeamPanel from '../../components/OpponentTeam/OpponentTeamPanel'
import TeamBadge from '../../components/TeamBadge/TeamBadge'
import { fetchContestLeaderboard } from '../../api/dream11'
import { MODE_CONTESTS, MODE_HOME } from '../../config/appMode'

function kickoffLabel(kickoffTime) {
  if (!kickoffTime) return 'Kickoff time TBC'
  const date = new Date(kickoffTime)
  if (Number.isNaN(date.getTime())) return 'Kickoff time TBC'
  return date.toLocaleString(undefined, {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/**
 * A Dream11 contest's leaderboard, and the entry point to every member's XI.
 *
 * Rows without has_submitted_team are shown but not tappable -- opening one
 * could only ever land on "this manager hasn't picked a team", so the row says
 * so upfront instead of making the user find out the hard way.
 */
function Dream11ContestPage() {
  const { contestId } = useParams()
  const { settings } = useOutletContext()
  const { user_id } = settings
  const navigate = useNavigate()

  const [data, setData] = useState(null)
  const [selectedUserId, setSelectedUserId] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  function handleBack() {
    if (window.history.state?.idx > 0) navigate(-1)
    else navigate(MODE_HOME[MODE_CONTESTS])
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')

    fetchContestLeaderboard({ contest_id: contestId, user_id })
      .then((body) => {
        if (!cancelled) setData(body)
      })
      .catch((err) => {
        if (!cancelled) setError(err.message || 'Could not load this contest')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [contestId, user_id])

  const contest = data?.contest ?? null
  const rows = data?.rows ?? []
  const selectedRow = rows.find((row) => row.user_id === selectedUserId) ?? null

  return (
    <main className="w-full px-safe-margin py-md flex flex-col gap-lg">
      {loading ? (
        <p className="font-body-md text-body-md text-on-surface-variant">Loading contest…</p>
      ) : error ? (
        <div className="bg-error-container text-on-error-container rounded-lg p-sm" role="alert">
          <p className="font-label-md text-label-md">{error}</p>
        </div>
      ) : (
        <>
          <div>
            {/* Back-arrow + static h1, same convention as the "How Points
                Work" screens (ScoringPageHeader) -- this is a drill-down from
                a contest card, not a BottomNav destination, so it doesn't get
                the hamburger/account/toggle header. The h1 must name the
                PAGE, never the contest -- contest.name moved to the
                subtitle below, alongside the rest of the contest's details. */}
            <div className="flex items-center gap-sm -ml-1 mb-1">
              <button
                aria-label="Go back"
                className="w-9 h-9 rounded-full flex items-center justify-center text-on-surface hover:bg-surface-container-high transition-colors"
                onClick={handleBack}
                type="button"
              >
                <span className="material-symbols-outlined">arrow_back</span>
              </button>
              <h1 className="font-headline-sm text-headline-sm text-on-surface">Leaderboard</h1>
            </div>
            <div className="flex items-center gap-xs">
              <TeamBadge shortName={contest.home_team} size="sm" />
              <p className="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider">
                {contest.home_team} v {contest.away_team} · GW{contest.gameweek}
              </p>
              <TeamBadge shortName={contest.away_team} size="sm" />
            </div>
            <h2 className="font-display-lg text-display-lg text-primary">{contest.name}</h2>
            <p className="font-body-md text-body-md text-on-surface-variant mt-sm">
              {contest.member_count}/{contest.max_members} members · code {contest.code}
            </p>
            <div className="flex items-center gap-2 mt-sm">
              <span
                className={`px-2 py-0.5 rounded text-[10px] font-bold tracking-wider uppercase ${
                  contest.is_locked
                    ? 'bg-secondary text-on-secondary'
                    : 'bg-surface-container-high text-on-surface-variant'
                }`}
              >
                {contest.is_locked ? 'Locked' : 'Open'}
              </span>
              <span className="font-label-md text-label-md text-on-surface-variant">
                {kickoffLabel(contest.kickoff_time)}
              </span>
            </div>

            {/* Dream11 scores nothing like classic -- assists are +20, clean
                sheets need 54 minutes, and both armbands apply at once. Anyone
                reading a leaderboard here needs that within reach. */}
            <Link
              className="inline-flex items-center gap-sm mt-md px-md py-sm rounded-full bg-surface-container-low text-on-surface hover:bg-surface-container-high transition-colors"
              to="/dream11/scoring"
            >
              <span className="material-symbols-outlined text-[18px] text-primary-container">
                help
              </span>
              <span className="font-label-md text-label-md">How points work</span>
            </Link>
          </div>

          <section aria-label="Leaderboard" className="flex flex-col gap-sm">
            <h2 className="font-headline-sm text-headline-sm text-primary">Leaderboard</h2>
            {!contest.is_locked && (
              <p className="font-label-md text-label-md text-on-surface-variant">
                Rival teams stay hidden until kickoff — you can still open your own.
              </p>
            )}
            {rows.map((row, index) => {
              const isMe = row.user_id === user_id
              const openable = row.has_submitted_team
              const label = row.team_name || row.username || `User ${row.user_id}`

              return (
                <button
                  aria-label={openable ? `View ${label}'s team` : `${label} has no team`}
                  className={`w-full text-left bg-surface-container-lowest rounded-xl p-md border shadow-sm transition-shadow flex items-center gap-md ${
                    row.user_id === selectedUserId ? 'border-primary-container' : 'border-outline-variant'
                  } ${openable ? 'hover:shadow-md' : 'opacity-70 cursor-default'}`}
                  data-testid="leaderboard-row"
                  disabled={!openable}
                  key={row.user_id}
                  onClick={() => setSelectedUserId(row.user_id)}
                  type="button"
                >
                  <span className="font-stats-number text-stats-number text-on-surface-variant w-8 shrink-0">
                    {row.rank || index + 1}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="font-bold text-primary flex items-center gap-2">
                      <span className="truncate">{label}</span>
                      {isMe && (
                        <span className="bg-primary text-on-primary text-[9px] px-1.5 py-0.5 rounded shrink-0">
                          ME
                        </span>
                      )}
                    </span>
                    <span className="block text-on-surface-variant text-xs">
                      {openable ? row.username ?? '' : 'No team submitted'}
                    </span>
                  </span>
                  <span className="text-right shrink-0">
                    <span className="block font-stats-number text-stats-number text-primary">
                      {row.total_points}
                    </span>
                    <span className="block font-label-md text-label-md text-on-surface-variant opacity-70">
                      Pts
                    </span>
                  </span>
                </button>
              )
            })}
          </section>

          {selectedRow && (
            <OpponentTeamPanel
              contestId={contestId}
              isSelf={selectedRow.user_id === user_id}
              kickoffTime={contest.kickoff_time}
              onClose={() => setSelectedUserId(null)}
              opponent={selectedRow}
            />
          )}
        </>
      )}
    </main>
  )
}

export default Dream11ContestPage
