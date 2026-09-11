import { useEffect, useState } from 'react'
import { Link, useOutletContext } from 'react-router-dom'
import FplHeader from '../../components/FplHeader/FplHeader'
import TeamBadge from '../../components/TeamBadge/TeamBadge'
import { fetchUserContests } from '../../api/dream11'

/**
 * The user's Dream11 contests. Deliberately minimal -- it exists so the
 * contest/leaderboard screen is reachable in the app rather than only by URL.
 * Creating and joining contests, and the team builder, are separate screens
 * that don't exist yet.
 */
function Dream11ContestsPage() {
  const { settings } = useOutletContext()
  const { user_id } = settings

  const [contests, setContests] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')

    fetchUserContests({ user_id })
      .then((data) => {
        if (!cancelled) setContests(data)
      })
      .catch((err) => {
        if (!cancelled) setError(err.message || 'Could not load contests')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [user_id])

  return (
    <>
      <FplHeader title="Contests" />
      <main className="w-full px-safe-margin py-md flex flex-col gap-lg">
        <p className="font-body-md text-body-md text-on-surface-variant">
          {contests.length} contest{contests.length === 1 ? '' : 's'} · single-match, 100 credits,
          11 players.
        </p>

      {error && (
        <div className="bg-error-container text-on-error-container rounded-lg p-sm" role="alert">
          <p className="font-label-md text-label-md">{error}</p>
        </div>
      )}

      {loading ? (
        <p className="font-body-md text-body-md text-on-surface-variant">Loading contests…</p>
      ) : contests.length === 0 ? (
        <p className="font-body-md text-body-md text-on-surface-variant">
          No contests yet — join one with a code to get started.
        </p>
      ) : (
        <div className="flex flex-col gap-sm">
          {contests.map((contest) => (
            <Link
              className="bg-surface-container-lowest rounded-xl p-md border border-outline-variant shadow-sm hover:shadow-md transition-shadow flex justify-between items-start gap-sm"
              data-testid="dream11-contest-card"
              key={contest.contest_id}
              to={`/dream11/contests/${contest.contest_id}`}
            >
              <div className="min-w-0">
                <div className="flex items-center gap-2 mb-1 flex-wrap">
                  <h2 className="font-headline-sm text-headline-sm text-primary truncate">
                    {contest.name}
                  </h2>
                  <span
                    className={`px-2 py-0.5 rounded text-[10px] font-bold tracking-wider uppercase whitespace-nowrap ${
                      contest.is_locked
                        ? 'bg-secondary text-on-secondary'
                        : 'bg-surface-container-high text-on-surface-variant'
                    }`}
                  >
                    {contest.is_locked ? 'Locked' : 'Open'}
                  </span>
                </div>
                <div className="flex items-center gap-xs">
                  <TeamBadge shortName={contest.home_team} size="sm" />
                  <p className="font-body-md text-body-md text-on-surface-variant">
                    {contest.home_team} v {contest.away_team} · GW{contest.gameweek}
                  </p>
                  <TeamBadge shortName={contest.away_team} size="sm" />
                </div>
                <p className="font-label-md text-label-md text-on-surface-variant mt-1">
                  {contest.member_count}/{contest.max_members} members
                  {contest.user_has_team ? '' : ' · you haven’t picked yet'}
                </p>
              </div>
              <div className="text-right shrink-0">
                <p className="font-stats-number text-stats-number text-primary">
                  {contest.user_total_points}
                </p>
                <p className="font-label-md text-label-md text-on-surface-variant opacity-70">
                  {contest.user_rank ? `Rank ${contest.user_rank}` : 'Pts'}
                </p>
              </div>
            </Link>
          ))}
        </div>
      )}
      </main>
    </>
  )
}

export default Dream11ContestsPage
