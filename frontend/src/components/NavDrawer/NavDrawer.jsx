import { useNavigate } from 'react-router-dom'
import { MODE_CONTESTS, MODE_FPL, MODE_HOME, useAppMode } from '../../config/appMode'

// The FPL | Contests segmented toggle that used to sit under every FplHeader
// is gone -- this "Game Modes" section, at the top of the drawer, is now the
// only way to switch. Renamed for the tactical rulebook: FPL mode -> Tactic
// Mode (the full lineup/formation game this whole codebase implements),
// Contests mode -> Quick 11 Mode (Dream11's one-match pick, still called
// MODE_CONTESTS internally since only the display name changed).
const MODE_CARDS = [
  {
    mode: MODE_FPL,
    label: 'Tactic Mode',
    description: 'Full tactical line-up and formation builder with AI insights',
    icon: 'tune',
    badge: 'Full Game',
  },
  {
    mode: MODE_CONTESTS,
    label: 'Quick 11 Mode',
    description: 'Rapid single-match draft & contest picks in under 60s',
    icon: 'bolt',
    badge: 'Fast Pick',
  },
]

/**
 * The slide-out drawer opened by FplHeader's hamburger button.
 *
 * Only the "Game Modes" switcher lives here now -- Home, Starting XI,
 * Transfers, Leagues and Chats were removed on request since they duplicate
 * BottomNav exactly and this drawer's one remaining job is switching
 * between Tactic Mode and Quick 11 Mode. If per-mode destination links are
 * wanted back here later, BottomNav.jsx still exports FPL_ITEMS/
 * CONTESTS_ITEMS for that.
 */
function NavDrawer({ open, onClose }) {
  const { mode, setMode } = useAppMode()
  const navigate = useNavigate()

  if (!open) return null

  function selectMode(next) {
    onClose()
    if (next === mode) return
    setMode(next)
    navigate(MODE_HOME[next])
  }

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

        <div className="flex flex-col gap-sm p-sm">
          <span className="font-label-md text-[11px] font-bold uppercase tracking-wider text-on-surface-variant px-1">
            Game Modes
          </span>
          {MODE_CARDS.map((card) => {
            const active = card.mode === mode
            return (
              <button
                aria-pressed={active}
                className={`p-sm rounded-xl border flex items-start gap-sm text-left transition-colors ${
                  active
                    ? 'bg-surface-container-low border-secondary/30'
                    : 'border-transparent hover:bg-surface-container-low hover:border-outline-variant'
                }`}
                data-testid={`mode-${card.mode}`}
                key={card.mode}
                onClick={() => selectMode(card.mode)}
                type="button"
              >
                <span className={`material-symbols-outlined text-[22px] mt-0.5 ${active ? 'text-secondary' : 'text-on-surface-variant'}`}>
                  {card.icon}
                </span>
                <span className="flex flex-col flex-1 min-w-0">
                  <span className="flex items-center justify-between gap-sm">
                    <span className="font-body-md text-body-md font-bold text-on-surface">{card.label}</span>
                    <span
                      className={`text-[9px] uppercase tracking-wide font-extrabold px-1.5 py-0.5 rounded-full whitespace-nowrap ${
                        active
                          ? 'bg-secondary-container text-on-secondary-container'
                          : 'bg-surface-container-highest text-on-surface-variant'
                      }`}
                    >
                      {active ? 'Active' : card.badge}
                    </span>
                  </span>
                  <span className="text-[11px] text-on-surface-variant leading-tight mt-0.5">
                    {card.description}
                  </span>
                </span>
              </button>
            )
          })}
        </div>
      </nav>
    </div>
  )
}

export default NavDrawer
