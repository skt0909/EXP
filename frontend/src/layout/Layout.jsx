import { Outlet } from 'react-router-dom'
import Header from '../components/Header/Header'
import BottomNav from '../components/BottomNav/BottomNav'
import ModeToggle from '../components/ModeToggle/ModeToggle'
import { useAuth } from '../auth/AuthContext'
import { CURRENT_GAMEWEEK, CURRENT_SEASON } from '../config/season'

/**
 * Shell for the in-app pages that don't draw their own chrome.
 *
 * The old settings panel (free-text user_id / season / gameweek) is gone: the
 * user is now whoever the JWT says they are, and season/gameweek come from
 * config/season.js so every page agrees. Pages still read these through the
 * outlet context, so `const { settings } = useOutletContext()` keeps working --
 * the values just can't be typed in by hand any more.
 *
 * WHY user_id IS STILL HERE after the auth cutover. No classic endpoint takes
 * it any more -- the api/ modules send identity in the bearer header and
 * nothing else -- but it is still load-bearing twice over:
 *
 *   1. Dream11's endpoints were out of scope for the cutover and still take a
 *      user_id parameter, so Dream11ContestPage/ContestsPage/PickTeamPage and
 *      MatchDetailPage's contest calls read it from here.
 *   2. It is the identity key in several pages' useEffect dependency arrays.
 *      Those arrays are now the ONLY thing making a page refetch when the
 *      signed-in manager changes, since the requests themselves no longer
 *      mention who is asking. Dropping it here would silently stop
 *      Transfers/StartingXI/Leagues/Matches refetching after a logout+login
 *      without a full page reload -- no error, just stale data.
 *
 * It is derived from useAuth() rather than stored, so it still tracks identity
 * exactly. Remove it only when Dream11 is cut over too, and then only after
 * repointing those dependency arrays at user.id directly.
 */
function Layout() {
  const { user } = useAuth()
  const settings = {
    user_id: user.id,
    season: CURRENT_SEASON,
    gameweek: CURRENT_GAMEWEEK,
  }

  return (
    <div className="min-h-screen bg-background text-on-background flex flex-col">
      <Header />
      {/* The FPL | Contests segmented control sits between the app bar and the
          page, matching the Matches design. It lives here rather than inside
          Header so it spans the content column and every page gets it. */}
      <div className="w-full max-w-[600px] mx-auto px-md pt-sm">
        <ModeToggle />
      </div>
      {/* pb clears the fixed BottomNav. */}
      <div className="flex-1 w-full max-w-[600px] mx-auto pb-[88px]">
        <Outlet context={{ settings }} />
      </div>
      <BottomNav />
    </div>
  )
}

export default Layout
