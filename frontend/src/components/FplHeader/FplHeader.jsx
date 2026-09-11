import { useState } from 'react'
import AccountMenu from '../AccountMenu/AccountMenu'
import NavDrawer from '../NavDrawer/NavDrawer'
import ModeToggle from '../ModeToggle/ModeToggle'

/**
 * The one header pattern every top-level page shares, FPL or Contests mode:
 *   [hamburger] {centered page title, one line} [account]
 *   then the FPL | Contests toggle, directly underneath
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
 * The toggle lives here, not in Layout, because Layout renders before it
 * knows what a page's own header looks like -- when Layout rendered the
 * toggle unconditionally above the Outlet, every page whose title lived
 * inside the Outlet (Starting XI, Transfers, Leagues, Chat, and Contests'
 * Matches/Contests once they got this header) ended up with the toggle
 * ABOVE its title instead of below it. Putting the toggle inside the same
 * component as the title guarantees the order every time.
 *
 * Drill-down screens (Pick Team, a contest leaderboard, a match's detail
 * page, How Points Work) deliberately do NOT use this -- they're not
 * BottomNav destinations, they keep a back-arrow + static h1 header instead
 * (ScoringRuleParts' ScoringPageHeader is that pattern's existing home), and
 * none of them show the mode toggle, matching how they already behaved.
 *
 * font-headline-sm/text-headline-sm (20px) rather than the larger
 * display-lg every page's old in-content title used: this is a compact
 * single-row app-bar with two flanking icons eating into the available
 * width, not a full-width page-content heading, and 32px bold was the
 * actual, measurable reason a two-word title like "Starting XI" was at
 * genuine risk of wrapping in that narrower center column. Matches
 * Squad Selection's own pre-existing (and correct) title size exactly.
 */
function FplHeader({ title }) {
  const [drawerOpen, setDrawerOpen] = useState(false)

  return (
    <>
      <header className="sticky top-0 z-40 bg-surface border-b border-outline-variant">
        <div className="flex items-center gap-sm h-16 px-md w-full max-w-[600px] mx-auto">
          <button
            aria-label="Menu"
            className="w-8 h-8 rounded-full flex items-center justify-center hover:bg-surface-container-high transition-colors shrink-0"
            onClick={() => setDrawerOpen(true)}
            type="button"
          >
            <span className="material-symbols-outlined text-on-surface-variant text-[20px]">menu</span>
          </button>

          <h1 className="flex-1 min-w-0 text-center truncate font-headline-sm text-headline-sm font-bold text-primary">
            {title}
          </h1>

          <AccountMenu />
        </div>
      </header>

      <div className="w-full max-w-[600px] mx-auto px-md pt-sm">
        <ModeToggle />
      </div>

      <NavDrawer onClose={() => setDrawerOpen(false)} open={drawerOpen} />
    </>
  )
}

export default FplHeader
