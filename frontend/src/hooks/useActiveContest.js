import { useEffect, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { fetchUserContests } from '../api/dream11'
import { useAuth } from '../auth/AuthContext'

/**
 * Backs BottomNav's "Team" tab in Quick 11 Mode: there is no single global
 * team (a team is built per contest, see PickTeamPage), so the tab has to
 * pick ONE contest to open. "Most recently created or joined" is the
 * closest stand-in for "the team you're currently working on" -- sorted by
 * created_at rather than USER_CONTESTS_QUERY's own kickoff-time ordering,
 * which answers a different question ("what's coming up") than this one
 * ("what did I touch last").
 *
 * `to` routes into /edit when a team's already submitted (re-POSTing to the
 * create-only endpoint would 409) and into /pick otherwise, matching the
 * same branch MatchDetailPage's own contest links already use.
 */
export function useActiveContest() {
  const { user } = useAuth()
  // BottomNav is a persistent layout component -- it doesn't remount when a
  // contest is created on a different page (MatchDetailPage), so a fetch
  // that only ran once on mount would keep "Team" locked forever after the
  // first contest. Refetching on every navigation (same trick AppModeProvider
  // uses on pathname to keep the mode toggle in sync) means the tab notices
  // the new contest as soon as you land back on any Quick 11 Mode page.
  const { pathname } = useLocation()
  const [contest, setContest] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!user) {
      setLoading(false)
      return
    }
    let cancelled = false
    fetchUserContests({ user_id: user.id })
      .then((rows) => {
        if (cancelled) return
        const latest = rows
          .slice()
          .sort((a, b) => new Date(b.created_at) - new Date(a.created_at))[0]
        setContest(latest ?? null)
      })
      .catch(() => {
        if (!cancelled) setContest(null)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [user, pathname])

  const to = contest
    ? contest.user_has_team
      ? `/dream11/contests/${contest.contest_id}/edit`
      : `/dream11/contests/${contest.contest_id}/pick`
    : null

  return { contest, to, loading }
}
