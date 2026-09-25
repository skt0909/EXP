import { NavLink } from 'react-router-dom'
import { useSquadStatus } from '../../hooks/useSquadStatus'
import { useActiveContest } from '../../hooks/useActiveContest'
import { MODE_CONTESTS, useAppMode } from '../../config/appMode'
import DashboardIcon from '../DashboardIcon/DashboardIcon'

/**
 * The tab set depends on the active game mode (see config/appMode.jsx).
 *
 * FPL mode keeps its five destinations, in this order. The Stitch export
 * disagreed with itself here -- most screens showed these five, but the Chat
 * Intro screen showed "Status / Squad / Transfers / Leagues / More". There is
 * no Stats page and no Draft feature, so neither appears. Squad Selection is
 * deliberately absent: it is reachable from the Dashboard, not from the nav.
 *
 * Contests mode has its own four -- Home / Team / Leagues / Chats, per
 * request -- rather than the Matches design's three. "Team" has no single
 * global destination (a team is built per contest, see PickTeamPage), so its
 * `to` isn't in this static list: useActiveContest resolves it at render
 * time to whichever contest was most recently created or joined. Chats is
 * the only destination both modes share, since the assistant isn't
 * mode-specific.
 *
 * Labels are full words on one line -- `whitespace-nowrap` plus `min-w-0` on
 * the flex children keeps them from wrapping or being clipped at 390px, which
 * has regressed before.
 */
// Exported so NavDrawer.jsx (opened from FplHeader's hamburger) lists the
// exact same destinations as the bottom tab bar, from one definition --
// duplicating this array would mean the two navs could silently drift.
export const FPL_ITEMS = [
  { key: 'home', label: 'Home', icon: 'home', to: '/dashboard' },
  { key: 'starting-xi', label: 'Starting XI', icon: 'squad', to: '/squad', needsSquad: true },
  { key: 'transfers', label: 'Transfers', icon: 'transfers', to: '/transfers' },
  { key: 'leagues', label: 'Leagues', icon: 'leagues', to: '/leagues' },
  { key: 'chats', label: 'Chats', icon: 'chat', to: '/chat' },
]

// 'team' has no static `to` -- see the doc comment above. NavDrawer only
// renders label/icon, so the missing `to` there is inert; BottomNav is the
// one place that fills it in from useActiveContest.
export const CONTESTS_ITEMS = [
  { key: 'home', label: 'Home', icon: 'calendar', to: '/matches' },
  { key: 'team', label: 'Team', icon: 'squad' },
  { key: 'leagues', label: 'Leagues', icon: 'leagues', to: '/dream11' },
  { key: 'chats', label: 'Chats', icon: 'chat', to: '/chat' },
]

const ITEM_BASE =
  'group flex flex-col items-center justify-center flex-1 min-w-0 px-1 py-xs rounded-full transition-all duration-150 hover:bg-surface-container-low active:scale-90'

function BottomNav() {
  const { hasSquad, loading: squadLoading } = useSquadStatus()
  const { to: activeContestTo, loading: contestLoading } = useActiveContest()
  const { mode } = useAppMode()
  const items = mode === MODE_CONTESTS ? CONTESTS_ITEMS : FPL_ITEMS

  return (
    <nav className="fixed bottom-3 left-1/2 -translate-x-1/2 w-[calc(100%-32px)] max-w-[568px] h-[62px] z-50 flex justify-around items-center px-sm py-xs bg-surface-container-lowest/95 backdrop-blur-xl border border-outline-variant/60 rounded-full shadow-[0_8px_24px_rgba(23,24,22,0.10)]">
      {items.map((item) => {
        // A real gate, not decoration: with no 15-player squad there is no XI
        // to arrange, and POST /gw_selection would reject the submission.
        // Locked while still loading too, so the nav never briefly offers a
        // link it is about to take away. 'team' gets the same treatment: with
        // no contest yet there's nothing for it to open (see useActiveContest).
        const isTeamItem = item.key === 'team'
        const locked =
          (item.needsSquad && (squadLoading || !hasSquad)) ||
          (isTeamItem && (contestLoading || !activeContestTo))
        const lockedTitle = isTeamItem
          ? 'Create or join a contest first'
          : 'Submit your 15-player squad first'

        if (locked) {
          return (
            <div
              aria-disabled="true"
              className={`${ITEM_BASE} text-outline-variant cursor-not-allowed`}
              data-testid={`nav-${item.key}-locked`}
              key={item.key}
              title={lockedTitle}
            >
              <DashboardIcon name="lock" size={22} />
              <span className="font-label-md text-[10px] mt-1 whitespace-nowrap">{item.label}</span>
            </div>
          )
        }

        return (
          <NavLink
            className={({ isActive }) =>
              `${ITEM_BASE} ${
                isActive
                  ? 'text-primary font-bold'
                  : 'text-on-surface-variant hover:text-primary'
              }`
            }
            data-testid={`nav-${item.key}`}
            // NavLink's `to` is a PREFIX match by default (its `end` prop
            // defaults to false), not an exact one: without `end`, "Leagues"
            // (to="/dream11") reads as active on /dream11/contests/:id/edit
            // too, since that path starts with /dream11 -- lighting up
            // alongside "Team" (whose own `to` resolves to that exact edit
            // URL) instead of Team alone. `end` makes every tab require an
            // exact pathname match, the only correct behavior for a tab bar
            // where each destination should own exactly one page.
            end
            key={item.key}
            to={isTeamItem ? activeContestTo : item.to}
          >
            {({ isActive }) => (
              <>
                <span className={`w-10 h-7 flex items-center justify-center rounded-full transition-all duration-150 group-hover:scale-105 ${isActive ? 'bg-[#78952D] text-white shadow-sm' : ''}`}>
                  <DashboardIcon name={item.icon} size={20} />
                </span>
                <span className="font-label-md text-[10px] mt-1 whitespace-nowrap">{item.label}</span>
              </>
            )}
          </NavLink>
        )
      })}
    </nav>
  )
}

export default BottomNav
