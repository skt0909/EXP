import { Outlet } from 'react-router-dom'
import BottomNav from '../components/BottomNav/BottomNav'
import { useAuth } from '../auth/AuthContext'
import { useGameweek } from '../config/gameweek'

/**
 * Shell for the in-app pages that don't draw their own chrome.
 *
 * The old settings panel (free-text user_id / season / gameweek) is gone: the
 * user is now whoever the JWT says they are, and season/gameweek come from
 * GameweekProvider (config/gameweek.jsx) -- the real current gameweek from
 * GET /gameweeks/current, not a hardcoded constant -- so every page agrees.
 * Pages still read these through the outlet context, so
 * `const { settings } = useOutletContext()` keeps working -- the values just
 * can't be typed in by hand any more.
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
 *
 * No Header, no ModeToggle here any more: both used to render unconditionally
 * above whatever the Outlet returned, which put the mode toggle above every
 * page's own title instead of below it. Every top-level tab destination now
 * draws its own FplHeader (menu/title/account, then the toggle right under
 * it); drill-down screens (Pick Team, a contest leaderboard, How Points Work)
 * keep their own back-arrow header and no toggle at all, matching how they
 * already behaved. Layout's only remaining job is the shared scroll column
 * and BottomNav.
 */
function Layout() {
  const { user } = useAuth()
  const { season, gameweek } = useGameweek()
  const settings = {
    user_id: user.id,
    season,
    gameweek,
  }

  return (
    <div className="min-h-screen bg-background text-on-background flex flex-col">
      <div className="flex-1 w-full max-w-[600px] mx-auto pb-[88px]">
        <Outlet context={{ settings }} />
      </div>
      <BottomNav />
    </div>
  )
}

export default Layout
