import { useEffect, useState } from 'react'
import { fetchScoringRules } from '../api/scoringRules'

/**
 * The published point values for both games, from GET /scoring-rules.
 *
 * Shared by the two "How Points Work" screens. Both need the whole payload --
 * the Dream11 screen compares its half against the classic half to decide which
 * rows genuinely differ -- so this returns it whole and lets each page's builder
 * (data/scoringRules.js) shape it.
 *
 * No user in the dependency array and no token required: the route is public
 * and the answer is the same for everyone, so unlike useSquadStatus this does
 * not refetch when identity changes.
 *
 * Deliberately not cached across mounts. The rules change by deploy, and a
 * module-level cache would hand a stale ruleset to a long-lived tab that had
 * already visited the screen -- the exact drift this endpoint was built to end,
 * reintroduced client-side.
 */
export function useScoringRules() {
  const [rules, setRules] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')

    fetchScoringRules()
      .then((data) => {
        if (!cancelled) setRules(data)
      })
      .catch((err) => {
        // Nothing here is recoverable client-side, and a half-rendered rules
        // screen would be worse than none: a missing section reads as "that
        // rule doesn't exist" rather than "we couldn't load it".
        if (!cancelled) setError(err.message || 'Could not load the scoring rules.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [])

  return { rules, loading, error }
}
