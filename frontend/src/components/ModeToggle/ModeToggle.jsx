import { useNavigate } from 'react-router-dom'
import { MODE_CONTESTS, MODE_FPL, MODE_HOME, useAppMode } from '../../config/appMode'

const OPTIONS = [
  { mode: MODE_FPL, label: 'FPL' },
  { mode: MODE_CONTESTS, label: 'Contests' },
]

/**
 * The FPL | Contests segmented control from the Matches design: one rounded
 * container split in two, active half filled.
 *
 * Switching navigates to that mode's home rather than only flipping a flag --
 * leaving a user on a Dream11 contest page with the FPL tab bar underneath
 * would be the confusing half-state this toggle exists to avoid.
 */
function ModeToggle() {
  const { mode, setMode } = useAppMode()
  const navigate = useNavigate()

  function select(next) {
    if (next === mode) return
    setMode(next)
    navigate(MODE_HOME[next])
  }

  return (
    <div
      aria-label="Game mode"
      className="flex bg-surface-container-high rounded-full p-1 w-full max-w-[280px] mx-auto"
      role="tablist"
    >
      {OPTIONS.map((option) => {
        const active = option.mode === mode
        return (
          <button
            aria-selected={active}
            className={`flex-1 rounded-full py-1.5 font-label-md text-label-md transition-colors ${
              active
                ? 'bg-primary text-on-primary font-bold shadow-sm'
                : 'text-on-surface-variant hover:text-primary'
            }`}
            data-testid={`mode-${option.mode}`}
            key={option.mode}
            onClick={() => select(option.mode)}
            role="tab"
            type="button"
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}

export default ModeToggle
