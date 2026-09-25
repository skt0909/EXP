import PlayerJersey from '../PlayerJersey/PlayerJersey'

// ml.players.status codes (see Data_ingestion/fpl_ingest.py). 'a' (available)
// is the common case and intentionally has no badge.
const STATUS_LABEL = {
  i: 'Injured',
  d: 'Doubtful',
  s: 'Suspended',
  u: 'Unavailable',
}

// FPL prices are £m; Dream11 pools are priced in credits, which carry no
// currency symbol. Defaulted so every existing caller is unaffected.
const asMillions = (price) => `£${price?.toFixed(1)}m`
const playerPoints = (player) => Number(player?.points ?? player?.fpl_points ?? 0)

function PlayerCard({
  player,
  selected,
  addDisabled,
  onToggle,
  formatPrice = asMillions,
  showPoints = false,
}) {
  const statusLabel = STATUS_LABEL[player.status]

  return (
    <div
      className={`rounded-[20px] p-3 flex items-center justify-between shadow-[0_2px_8px_rgba(0,0,0,0.04)] transition-shadow relative overflow-hidden ${
        selected
          ? 'bg-white border border-[#CBD8A8]'
          : 'bg-white border border-[#E5E6E1] hover:shadow-md'
      }`}
    >
      {selected && (
        <div className="absolute inset-0 bg-gradient-to-r from-[#EDF2DF]/50 to-transparent pointer-events-none" />
      )}

      <div className="flex items-center gap-3 relative z-10 min-w-0">
        <PlayerJersey player={player} size="sm" showName={false} />
        <div className="flex flex-col min-w-0">
          <span className="font-headline-sm text-headline-sm text-on-surface truncate">
            {player.name}
          </span>
          <div className="flex items-center gap-2 mt-0.5">
            <span className="font-label-md text-label-md text-on-surface-variant">{player.club}</span>
            <span className="w-1 h-1 rounded-full bg-outline" />
            <span className="font-label-md text-label-md text-on-surface-variant">
              {player.position}
            </span>
            {statusLabel && (
              <span className="font-label-md text-label-md text-error">{statusLabel}</span>
            )}
          </div>
        </div>
      </div>

      <div className="flex items-center gap-4 relative z-10 shrink-0">
        <div className="flex flex-col items-end leading-none">
          <span className="font-stats-number text-stats-number tabular-nums text-on-surface">
            {formatPrice(player.price)}
          </span>
          {showPoints && (
            <span className="mt-1 font-label-md text-[10px] uppercase tracking-wide text-on-surface-variant">
              {playerPoints(player)} PTS
            </span>
          )}
        </div>
        <button
          aria-label={selected ? `Remove ${player.name} from squad` : `Add ${player.name} to squad`}
          className={`w-10 h-10 rounded-full flex items-center justify-center transition-all disabled:opacity-40 disabled:cursor-not-allowed ${
            selected
              ? 'bg-[#8DAA3C] text-white shadow-md hover:bg-error hover:text-on-error'
              : 'bg-[#F5F3F0] border border-[#E5E6E1] text-on-surface hover:bg-[#EDF2DF] hover:text-[#4D631B] hover:border-[#CBD8A8]'
          }`}
          disabled={!selected && addDisabled}
          onClick={() => onToggle(player.id)}
          type="button"
        >
          <span className="material-symbols-outlined">{selected ? 'check' : 'add'}</span>
        </button>
      </div>
    </div>
  )
}

export default PlayerCard
