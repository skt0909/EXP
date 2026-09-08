import PlayerJersey from '../PlayerJersey/PlayerJersey'

/**
 * Squad-building pitch: one dashed "add" slot per unfilled place, matching the
 * 2/5/5/3 shape a legal 15-man squad has to take. Rows run FWD at the top down
 * to GK at the bottom, the way the design draws it.
 *
 * Tapping an empty slot filters the list below to that position -- the slot is
 * the fastest way to say "I need another defender".
 */
const ROWS = [
  { position: 'FWD', size: 3, big: true },
  { position: 'MID', size: 5, big: false },
  { position: 'DEF', size: 5, big: false },
  { position: 'GK', size: 2, big: true },
]

function PitchView({ selectedPlayers, onEmptySlotClick }) {
  const byPosition = {}
  for (const player of selectedPlayers) {
    byPosition[player.position] = byPosition[player.position] || []
    byPosition[player.position].push(player)
  }

  return (
    <div className="w-full aspect-[3/4] bg-secondary rounded-xl relative overflow-hidden flex flex-col justify-evenly py-sm border-4 border-secondary shadow-inner bg-[repeating-linear-gradient(0deg,transparent,transparent_12%,rgba(255,255,255,0.08)_12%,rgba(255,255,255,0.08)_24%)]">
      {ROWS.map(({ position, size, big }) => {
        const players = byPosition[position] || []
        // Over-filling a position is a validation error, not a rendering one --
        // show the extras as a counter rather than silently dropping players.
        const overflow = players.length - size
        const box = big ? 'w-14 h-14' : 'w-12 h-12'
        const jerseySize = big ? 'sm' : 'xs'

        return (
          <div
            className={`flex w-full px-sm relative z-10 ${big ? 'justify-center gap-xl' : 'justify-between'}`}
            key={position}
          >
            {Array.from({ length: size }).map((_, slot) =>
              players[slot] ? (
                <div className="flex items-center justify-center" key={players[slot].id} title={players[slot].name}>
                  <PlayerJersey player={players[slot]} size={jerseySize} />
                </div>
              ) : (
                <button
                  aria-label={`Browse ${position} players`}
                  className={`${box} rounded-full border-2 border-dashed border-on-secondary/60 flex items-center justify-center bg-surface/10 hover:bg-surface/20 transition-colors backdrop-blur-sm shadow-sm`}
                  key={`${position}-${slot}`}
                  onClick={() => onEmptySlotClick(position)}
                  type="button"
                >
                  <span className="material-symbols-outlined text-on-secondary">add</span>
                </button>
              )
            )}
            {overflow > 0 && (
              <span className="self-center font-label-md text-label-md text-on-secondary font-bold">
                +{overflow}
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}

export default PitchView
