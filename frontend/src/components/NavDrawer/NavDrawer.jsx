import { NavLink } from 'react-router-dom'
import { FPL_ITEMS, CONTESTS_ITEMS } from '../BottomNav/BottomNav'
import { useSquadStatus } from '../../hooks/useSquadStatus'
import { MODE_CONTESTS, useAppMode } from '../../config/appMode'

/**
 * The slide-out drawer opened by FplHeader's hamburger button.
 *
 * Lists the SAME destinations as BottomNav, imported from there rather than
 * redefined here -- there was no menu/drawer anywhere in the app before this,
 * so rather than invent a second set of destinations, this is just a second
 * way to reach the ones that already exist in the tab bar. Reads useAppMode()
 * itself (same as BottomNav) so it always shows the right list even though
 * today only FPL-mode pages mount a hamburger at all.
 *
 * The Starting XI "needs a squad first" lock is replicated from BottomNav's
 * own rendering rather than shared as a component: the two lists render as a
 * horizontal pill bar and a vertical sheet respectively, different enough
 * that forcing one shared row-renderer would fight both layouts more than it
 * would save.
 */
function NavDrawer({ open, onClose }) {
  const { hasSquad, loading } = useSquadStatus()
  const { mode } = useAppMode()
  const items = mode === MODE_CONTESTS ? CONTESTS_ITEMS : FPL_ITEMS

  if (!open) return null

  return (
    <div className="fixed inset-0 z-[60]" role="dialog" aria-modal="true" aria-label="Navigation menu">
      {/* eslint has no opinion here, but this mirrors StartingXIPage's own
          click-to-dismiss overlay convention (bg-black/40, onClick closes). */}
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />

      <nav className="absolute inset-y-0 left-0 w-64 max-w-[80%] bg-surface-container-lowest shadow-lg flex flex-col">
        <div className="flex items-center justify-between h-16 px-md border-b border-outline-variant shrink-0">
          <span className="font-headline-sm text-headline-sm font-extrabold text-primary-container">
            Menu
          </span>
          <button
            aria-label="Close menu"
            className="w-8 h-8 rounded-full flex items-center justify-center hover:bg-surface-container-high transition-colors"
            onClick={onClose}
            type="button"
          >
            <span className="material-symbols-outlined text-on-surface-variant text-[20px]">close</span>
          </button>
        </div>

        <div className="flex flex-col gap-1 p-sm overflow-y-auto">
          {items.map((item) => {
            const locked = item.needsSquad && (loading || !hasSquad)

            if (locked) {
              return (
                <div
                  aria-disabled="true"
                  className="flex items-center gap-sm px-sm py-sm rounded-lg text-outline-variant cursor-not-allowed"
                  key={item.key}
                  title="Submit your 15-player squad first"
                >
                  <span className="material-symbols-outlined text-[20px]">lock</span>
                  <span className="font-body-md text-body-md">{item.label}</span>
                </div>
              )
            }

            return (
              <NavLink
                className={({ isActive }) =>
                  `flex items-center gap-sm px-sm py-sm rounded-lg transition-colors ${
                    isActive
                      ? 'bg-secondary-container text-on-secondary-container font-bold'
                      : 'text-on-surface-variant hover:bg-surface-container-high'
                  }`
                }
                key={item.key}
                onClick={onClose}
                to={item.to}
              >
                <span className="material-symbols-outlined text-[20px]">{item.icon}</span>
                <span className="font-body-md text-body-md">{item.label}</span>
              </NavLink>
            )
          })}
        </div>
      </nav>
    </div>
  )
}

export default NavDrawer
