import { createContext, useContext, useEffect, useMemo, useState } from 'react'
import { fetchCurrentGameweek } from '../api/gameweeks'

/**
 * Replaces the old hardcoded CURRENT_SEASON/CURRENT_GAMEWEEK constants
 * (config/season.js, now deleted) with the real thing: GET /gameweeks/current
 * resolves, from actual fixture kickoff times, either the soonest gameweek
 * whose deadline hasn't passed yet, or -- if every ingested gameweek is
 * already past -- the most recent past one (read-only from here on; Starting
 * XI/Transfers already render a locked banner for a passed deadline, so
 * nothing downstream needs to know which case this is).
 *
 * Fetched once per app session, same lifetime as AppModeProvider, and for the
 * same reason: DashboardPage and SquadSelectionPage draw their own chrome
 * outside Layout, so this has to wrap the whole authenticated app to reach
 * them, not just Layout's outlet context.
 *
 * Children don't render until this resolves -- every consumer used to read a
 * synchronous constant, so making season/gameweek briefly null would mean
 * teaching five call sites to each guard against that. One shared loading
 * state here is simpler than five smaller ones.
 */
const GameweekContext = createContext({ season: null, gameweek: null, deadline: null })

export function GameweekProvider({ children }) {
  const [state, setState] = useState({ status: 'loading', season: null, gameweek: null, deadline: null })

  useEffect(() => {
    let cancelled = false
    fetchCurrentGameweek()
      .then((data) => {
        if (cancelled) return
        if (!data.found) {
          setState({ status: 'empty', season: null, gameweek: null, deadline: null })
          return
        }
        setState({ status: 'ready', season: data.season, gameweek: data.gameweek, deadline: data.deadline })
      })
      .catch((err) => {
        if (!cancelled) setState({ status: 'error', season: null, gameweek: null, deadline: null, error: err.message })
      })
    return () => {
      cancelled = true
    }
  }, [])

  const value = useMemo(
    () => ({ season: state.season, gameweek: state.gameweek, deadline: state.deadline }),
    [state.season, state.gameweek, state.deadline]
  )

  if (state.status === 'loading') {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background text-on-background font-body-md text-body-md text-on-surface-variant">
        Loading…
      </div>
    )
  }

  if (state.status === 'empty') {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background text-on-background font-body-md text-body-md text-on-surface-variant px-safe-margin text-center">
        No season is scheduled yet -- check back once fixtures are published.
      </div>
    )
  }

  if (state.status === 'error') {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background text-error font-body-md text-body-md px-safe-margin text-center" role="alert">
        Couldn't load the current gameweek: {state.error}
      </div>
    )
  }

  return <GameweekContext.Provider value={value}>{children}</GameweekContext.Provider>
}

export function useGameweek() {
  return useContext(GameweekContext)
}
