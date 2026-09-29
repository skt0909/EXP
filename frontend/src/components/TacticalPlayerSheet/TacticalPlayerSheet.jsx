import { useEffect, useState } from 'react'
import DashboardIcon from '../DashboardIcon/DashboardIcon'
import PlayerJersey from '../PlayerJersey/PlayerJersey'
import { gameweekContribution, tacticalBreakdownRows } from '../../data/tacticalRuleLabels'

function points(value) {
  if (value == null) return '--'
  return value > 0 ? `+${value}` : String(value)
}

// Category rows for one kind of points. Every number is the backend's
// (GET /team: the engine for a live gameweek, the stored snapshot for a
// settled one); this only lays them out.
function BreakdownSection({ title, rows, total, accent = '' }) {
  if (!rows.length) return null
  return (
    <section className="py-sm" aria-label={title}>
      <h3 className="font-label-md text-label-md text-on-surface-variant mb-1">{title}</h3>
      <ul className="flex flex-col gap-0.5">
        {rows.map((row) => (
          <li className="flex justify-between text-sm" data-testid="tactical-breakdown-row" key={row.rule}>
            <span className="text-on-surface-variant">{row.label}</span>
            <span className={`font-semibold ${accent}`}>{points(row.points)}</span>
          </li>
        ))}
        <li className="flex justify-between text-sm border-t border-outline-variant/60 pt-0.5 mt-0.5">
          <span className="font-semibold">{title} total</span>
          <span className={`font-semibold ${accent}`}>{points(total)}</span>
        </li>
      </ul>
    </section>
  )
}

function TacticalPlayerSheet({ player, onClose }) {
  const [open, setOpen] = useState(false)

  useEffect(() => {
    const id = requestAnimationFrame(() => setOpen(true))
    return () => cancelAnimationFrame(id)
  }, [])

  if (!player) return null
  // null when both are withheld, so points() renders "--", never a fake 0.
  const total = gameweekContribution(player)
  const generalRows = tacticalBreakdownRows(player.general_breakdown)
  const tacticalRows = tacticalBreakdownRows(player.tactical_breakdown)
  const breakdownAvailable = player.breakdown_available !== false

  return (
    <>
      <button
        aria-label="Close player details"
        className={`fixed inset-0 z-[55] bg-black/30 transition-opacity ${open ? 'opacity-100' : 'opacity-0'}`}
        onClick={onClose}
        type="button"
      />
      <section
        aria-label={`${player.name} performance details`}
        className={`fixed bottom-[88px] left-1/2 z-[60] w-full max-w-[600px] -translate-x-1/2 rounded-t-2xl border border-outline-variant bg-surface-container-lowest p-md shadow-[0_-12px_32px_rgba(23,24,22,0.14)] transition-transform duration-300 ${open ? 'translate-y-0' : 'translate-y-full'}`}
        data-testid="tactical-player-sheet"
        role="dialog"
      >
        <div className="mx-auto mb-sm h-1 w-10 rounded-full bg-outline-variant" />
        <header className="flex items-center gap-sm border-b border-outline-variant pb-md">
          <PlayerJersey player={player} showName={false} size="sm" />
          <div className="min-w-0 flex-1">
            <h2 className="truncate font-headline-sm text-headline-sm text-on-surface">{player.name}</h2>
            <p className="font-label-md text-label-md text-on-surface-variant">
              {player.position} · {player.club}
            </p>
          </div>
          <button aria-label="Close" className="w-10 h-10 rounded-full flex items-center justify-center hover:bg-surface-container-high" onClick={onClose} type="button">
            <DashboardIcon name="close" size={19} />
          </button>
        </header>
        {player.minutes != null && (
          <p className="font-label-md text-label-md text-on-surface-variant pt-sm">
            {player.minutes} minute{player.minutes === 1 ? '' : 's'} played
          </p>
        )}
        {breakdownAvailable ? (
          <div className="divide-y divide-outline-variant/60">
            <BreakdownSection rows={generalRows} title="General points" total={player.general_points} />
            <BreakdownSection accent="text-secondary" rows={tacticalRows} title="Tactical points" total={player.tactical_points} />
          </div>
        ) : (
          <p className="font-label-md text-label-md text-on-surface-variant pt-sm" data-testid="tactical-breakdown-unavailable">
            The scoring breakdown isn&apos;t available for this gameweek. The team totals shown on the dashboard are the final result.
          </p>
        )}
        <dl className="divide-y divide-outline-variant/60 py-sm">
          <div className="flex justify-between py-sm"><dt className="text-on-surface-variant">General points</dt><dd className="font-semibold">{points(player.general_points)}</dd></div>
          <div className="flex justify-between py-sm"><dt className="text-on-surface-variant">Tactical points</dt><dd className="font-semibold text-secondary">{points(player.tactical_points)}</dd></div>
          <div className="flex justify-between py-sm"><dt className="text-on-surface-variant">Role</dt><dd className="font-semibold capitalize">{(player.role ?? 'player').replaceAll('_', ' ')}</dd></div>
          <div className="flex justify-between py-sm text-base"><dt className="font-semibold">Gameweek contribution</dt><dd className="font-bold text-primary">{points(total)}</dd></div>
        </dl>
      </section>
    </>
  )
}

export default TacticalPlayerSheet
