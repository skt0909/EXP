import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { useLocation } from 'react-router-dom'

/**
 * The app runs in one of two modes, per the Stitch designs' FPL | Contests
 * toggle. Mode is not a route: it decides which tab set BottomNav renders, so
 * the two games get their own navigation rather than nine tabs sharing one bar.
 *
 * Persisted so it survives reloads -- a user who lives in Contests shouldn't be
 * dropped back into FPL every time they refresh. Reads are wrapped because
 * localStorage throws outright in some contexts (private windows, blocked site
 * data), and an unusable toggle is a far better failure than a blank app.
 */
export const MODE_FPL = 'fpl'
export const MODE_CONTESTS = 'contests'

const STORAGE_KEY = 'pitchside.mode'

/** Where switching into a mode lands you. */
export const MODE_HOME = {
  [MODE_FPL]: '/dashboard',
  [MODE_CONTESTS]: '/matches',
}

// Routes that belong to Contests mode. Used to self-correct the toggle when a
// user arrives by deep link (or by the in-page links between the two), so the
// header can never say "FPL" while a contest screen is on-screen.
const CONTESTS_PREFIXES = ['/matches', '/dream11']

export function modeForPath(pathname) {
  return CONTESTS_PREFIXES.some((prefix) => pathname.startsWith(prefix)) ? MODE_CONTESTS : MODE_FPL
}

function readStoredMode() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    return stored === MODE_CONTESTS || stored === MODE_FPL ? stored : null
  } catch {
    return null
  }
}

const AppModeContext = createContext({ mode: MODE_FPL, setMode: () => {} })

/**
 * Wraps the whole authenticated app, not just Layout: DashboardPage and
 * SquadSelectionPage draw their own chrome and sit outside Layout, and without
 * the provider reaching them FPL's own home screen had no way to switch into
 * Contests at all.
 */
export function AppModeProvider({ children }) {
  const { pathname } = useLocation()
  // The current path wins over storage on first paint: landing on /matches
  // should show Contests immediately, not flash FPL and correct itself.
  const [mode, setModeState] = useState(() => readStoredMode() ?? modeForPath(pathname))

  // Navigating between the two games (e.g. a contest link from an FPL page)
  // moves the toggle with it.
  useEffect(() => {
    setModeState(modeForPath(pathname))
  }, [pathname])

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, mode)
    } catch {
      // Non-fatal: the toggle still works for this session.
    }
  }, [mode])

  const setMode = useCallback((next) => setModeState(next), [])
  const value = useMemo(() => ({ mode, setMode }), [mode, setMode])

  return <AppModeContext.Provider value={value}>{children}</AppModeContext.Provider>
}

export function useAppMode() {
  return useContext(AppModeContext)
}
