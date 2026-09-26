import { useState } from 'react'
import { Link } from 'react-router-dom'
import AccountMenu from '../AccountMenu/AccountMenu'
import NavDrawer from '../NavDrawer/NavDrawer'
import DashboardIcon from '../DashboardIcon/DashboardIcon'

/**
 * The one header pattern every top-level page shares, FPL or Contests mode:
 *   [hamburger] {centered page title, one line} [account]
 *
 * Confirmed against real screenshots to be the intended pattern across
 * Dashboard, Squad Selection, Starting XI, Transfers, Leagues, Assist,
 * Matches and Contests -- none of the FPL six previously had it exactly (no
 * page had a hamburger, and no title was actually centered; a couple only
 * LOOKED centered because their two side elements happened to be similar
 * widths), and the two Contests-mode tabs used the old shared brand-bar
 * Header.jsx plus a separate left-aligned in-content title instead. This
 * component is the single source of that layout so these eight pages can't
 * drift from each other again the way they had before.
 *
 * The FPL | Contests segmented toggle that used to render directly under
 * this header is gone -- mode switching moved into NavDrawer's "Game Modes"
 * section (opened by the hamburger button below), renamed Tactic Mode /
 * Quick 11 Mode, per the current design. See NavDrawer.jsx.
 *
 * Drill-down screens (Pick Team, a contest leaderboard, a match's detail
 * page, How Points Work) deliberately do NOT use this -- they're not
 * BottomNav destinations, they keep a back-arrow + static h1 header instead
 * (ScoringRuleParts' ScoringPageHeader is that pattern's existing home).
 *
 * font-headline-sm/text-headline-sm (20px) rather than the larger
 * display-lg every page's old in-content title used: this is a compact
 * single-row app-bar with two flanking icons eating into the available
 * width, not a full-width page-content heading, and 32px bold was the
 * actual, measurable reason a two-word title like "Starting XI" was at
 * genuine risk of wrapping in that narrower center column. Matches
 * Squad Selection's own pre-existing (and correct) title size exactly.
 */
function FplHeader({ title, showHelp = false, helpTo = '/scoring' }) {
  const [drawerOpen, setDrawerOpen] = useState(false)

  return (
    <>
      {/* z-50, one above the pages' own sticky bars (z-40): at equal z the
          later element wins, so SquadSelectionPage's budget bar covered this
          header's account menu -- "Log out" couldn't be clicked there. */}
      <header className="sticky top-0 z-50 bg-surface/95 backdrop-blur-xl border-b border-outline-variant/60">
        <div className="flex items-center gap-sm h-16 px-md w-full max-w-[600px] mx-auto">
          <button
            aria-label="Menu"
            className="w-10 h-10 rounded-full bg-surface-container-lowest border border-outline-variant/70 shadow-sm flex items-center justify-center hover:bg-surface-container-high active:scale-95 transition-all shrink-0"
            onClick={() => setDrawerOpen(true)}
            type="button"
          >
            <DashboardIcon className="text-on-surface" name="menu" size={20} />
          </button>

          <h1 className="flex-1 min-w-0 text-left truncate font-headline-sm text-headline-sm font-semibold text-on-surface">
            {title}
          </h1>

          {showHelp && (
            <Link
              aria-label="Help and scoring rules"
              className="w-10 h-10 rounded-full bg-surface-container-lowest border border-outline-variant/70 shadow-sm flex items-center justify-center hover:bg-surface-container-high active:scale-95 transition-all shrink-0"
              to={helpTo}
            >
              <DashboardIcon className="text-on-surface" name="help" size={19} />
            </Link>
          )}
          <AccountMenu />
        </div>
      </header>

      <NavDrawer onClose={() => setDrawerOpen(false)} open={drawerOpen} />
    </>
  )
}

export default FplHeader
