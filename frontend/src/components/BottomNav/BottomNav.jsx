import { NavLink } from 'react-router-dom'
import { useSquadStatus } from '../../hooks/useSquadStatus'
import { MODE_CONTESTS, useAppMode } from '../../config/appMode'

/**
 * The tab set depends on the active game mode (see config/appMode.jsx).
 *
 * FPL mode keeps its five destinations, in this order. The Stitch export
 * disagreed with itself here -- most screens showed these five, but the Chat
 * Intro screen showed "Status / Squad / Transfers / Leagues / More". There is
 * no Stats page and no Draft feature, so neither appears. Squad Selection is
 * deliberately absent: it is reachable from the Dashboard, not from the nav.
 *
 * Contests mode has its own three, per the Matches design. Chats is the only
 * destination both modes share, since the assistant isn't mode-specific.
 *
 * Labels are full words on one line -- `whitespace-nowrap` plus `min-w-0` on
 * the flex children keeps them from wrapping or being clipped at 390px, which
 * has regressed before.
 */
const FPL_ITEMS = [
  { key: 'home', label: 'Home', icon: 'house', to: '/dashboard' },
  { key: 'starting-xi', label: 'Starting XI', icon: 'groups', to: '/squad', needsSquad: true },
  { key: 'transfers', label: 'Transfers', icon: 'swap_horiz', to: '/transfers' },
  { key: 'leagues', label: 'Leagues', icon: 'emoji_events', to: '/leagues' },
  { key: 'chats', label: 'Chats', icon: 'chat', to: '/chat' },
]

const CONTESTS_ITEMS = [
  { key: 'matches', label: 'Matches', icon: 'calendar_month', to: '/matches' },
  { key: 'my-contests', label: 'My Contests', icon: 'leaderboard', to: '/dream11' },
  { key: 'chats', label: 'Chats', icon: 'chat', to: '/chat' },
]

const ITEM_BASE =
  'flex flex-col items-center justify-center flex-1 min-w-0 px-1 py-xs rounded-xl transition-colors'

function BottomNav() {
  const { hasSquad, loading } = useSquadStatus()
  const { mode } = useAppMode()
  const items = mode === MODE_CONTESTS ? CONTESTS_ITEMS : FPL_ITEMS

  return (
    <nav className="fixed bottom-0 left-1/2 -translate-x-1/2 w-full max-w-[600px] z-50 flex justify-around items-center px-sm py-xs bg-surface border-t border-outline-variant shadow-[0_-4px_6px_-1px_rgba(0,0,0,0.05)]">
      {items.map((item) => {
        // A real gate, not decoration: with no 15-player squad there is no XI
        // to arrange, and POST /gw_selection would reject the submission.
        // Locked while still loading too, so the nav never briefly offers a
        // link it is about to take away.
        const locked = item.needsSquad && (loading || !hasSquad)

        if (locked) {
          return (
            <div
              aria-disabled="true"
              className={`${ITEM_BASE} text-outline-variant cursor-not-allowed`}
              data-testid={`nav-${item.key}-locked`}
              key={item.key}
              title="Submit your 15-player squad first"
            >
              <span className="material-symbols-outlined text-[24px]">lock</span>
              <span className="font-label-md text-[10px] mt-1 whitespace-nowrap">{item.label}</span>
            </div>
          )
        }

        return (
          <NavLink
            className={({ isActive }) =>
              `${ITEM_BASE} ${
                isActive
                  ? 'bg-secondary-container text-on-secondary-container font-bold'
                  : 'text-on-surface-variant hover:text-primary'
              }`
            }
            data-testid={`nav-${item.key}`}
            key={item.key}
            to={item.to}
          >
            <span className="material-symbols-outlined text-[24px]">{item.icon}</span>
            <span className="font-label-md text-[10px] mt-1 whitespace-nowrap">{item.label}</span>
          </NavLink>
        )
      })}
    </nav>
  )
}

export default BottomNav
