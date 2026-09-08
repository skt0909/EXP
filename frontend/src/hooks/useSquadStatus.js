import { useEffect, useState } from 'react'
import { fetchCurrentSquad } from '../api/squad'
import { useAuth } from '../auth/AuthContext'
import { CURRENT_SEASON } from '../config/season'

const SQUAD_SIZE = 15

/**
 * Whether this user has actually submitted a full 15-player squad.
 *
 * Backs the real gate on Starting XI: with no squad there is nothing to pick
 * an XI from, and POST /gw_selection would reject the submission anyway. The
 * check is "exactly 15 active players", not "the /squad call succeeded" --
 * GET /squad returns 200 with an empty list for a user who has never
 * submitted, so truthiness of the response proves nothing.
 *
 * `loading` starts true so the nav renders the item disabled while the answer
 * is unknown, rather than briefly offering a link that is about to be locked.
 */
export function useSquadStatus() {
  const { user } = useAuth()
  const [hasSquad, setHasSquad] = useState(false)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!user) {
      setLoading(false)
      return
    }
    let cancelled = false
    fetchCurrentSquad({ season: CURRENT_SEASON })
      .then((squad) => {
        if (!cancelled) setHasSquad((squad.players?.length ?? 0) === SQUAD_SIZE)
      })
      .catch(() => {
        if (!cancelled) setHasSquad(false)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [user])

  return { hasSquad, loading }
}
