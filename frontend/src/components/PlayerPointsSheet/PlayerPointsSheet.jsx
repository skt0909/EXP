import { useEffect, useRef, useState } from 'react'
import PlayerJersey from '../PlayerJersey/PlayerJersey'
import './PlayerPointsSheet.css'

// Order matters -- this is the order rows render in. Each maps one field of
// Game_logic/dream11.py's Dream11PointsBreakdown (itself a mirror of
// dream11_scoring.py's dream11_points_breakdown()) to a label. eligibleKey
// (when present) hides the row entirely for a position that could never
// earn it (a FWD's clean sheet, a MID's saves) -- shown at 0 it would read
// as "this happened and scored nothing" rather than "not applicable here".
// onlyIfNonZero rows (cards/own goals/penalties) are rare events shown only
// when they actually occurred, so a routine card-free match isn't padded
// with five permanent zero rows.
const ROWS = [
  { key: 'goals', label: 'Goals', countKey: 'goals_count' },
  { key: 'assists', label: 'Assists', countKey: 'assists_count' },
  { key: 'clean_sheet', label: 'Clean sheet', eligibleKey: 'clean_sheet_eligible' },
  { key: 'goals_conceded', label: 'Goals conceded', countKey: 'goals_conceded_count', eligibleKey: 'goals_conceded_eligible' },
  { key: 'saves', label: 'Saves', countKey: 'saves_count', eligibleKey: 'saves_eligible' },
  { key: 'yellow_cards', label: 'Yellow cards', countKey: 'yellow_cards_count', onlyIfNonZero: true },
  { key: 'red_cards', label: 'Red card', countKey: 'red_cards_count', onlyIfNonZero: true },
  { key: 'own_goals', label: 'Own goals', countKey: 'own_goals_count', onlyIfNonZero: true },
  { key: 'penalties_saved', label: 'Penalties saved', countKey: 'penalties_saved_count', onlyIfNonZero: true },
  { key: 'penalties_missed', label: 'Penalties missed', countKey: 'penalties_missed_count', onlyIfNonZero: true },
]

function signedValueClass(value) {
  if (value > 0) return 'player-points-sheet__value--positive'
  if (value < 0) return 'player-points-sheet__value--negative'
  return 'player-points-sheet__value--neutral'
}

// Whole numbers print bare ("+12"); Dream11's captain/vice bonus (raw total
// times 0.5 or 1.0) can land on a half point, which prints with one decimal
// ("+2.5") rather than being rounded away.
function formatSigned(value) {
  const rounded = Math.round(value * 10) / 10
  const magnitude = Number.isInteger(rounded) ? rounded : rounded.toFixed(1)
  return rounded > 0 ? `+${magnitude}` : String(magnitude)
}

/** Swipe-down-to-dismiss on the header/handle area only, not the whole sheet
 *  (a breakdown row's text shouldn't hijack a vertical scroll gesture if the
 *  list ever grows tall enough to need one). Threshold-based, not physics --
 *  past DISMISS_THRESHOLD_PX on release it closes, otherwise it snaps back. */
const DISMISS_THRESHOLD_PX = 80

function usePointerDismiss(onClose) {
  const [dragY, setDragY] = useState(0)
  const startY = useRef(null)
  const dragging = useRef(false)

  function onPointerDown(e) {
    startY.current = e.clientY
    dragging.current = true
    e.currentTarget.setPointerCapture?.(e.pointerId)
  }
  function onPointerMove(e) {
    if (!dragging.current) return
    const delta = e.clientY - startY.current
    if (delta > 0) setDragY(delta)
  }
  function onPointerUp() {
    if (!dragging.current) return
    dragging.current = false
    if (dragY > DISMISS_THRESHOLD_PX) onClose()
    else setDragY(0)
  }

  return { dragY, onPointerDown, onPointerMove, onPointerUp, onPointerCancel: onPointerUp }
}

/**
 * Bottom sheet for one player's Dream11 scoring breakdown, opened by tapping
 * their jersey on OpponentTeamPanel's read-only pitch (PitchLineup's
 * onSelectPlayer). Sits ABOVE BottomNav (bottom-[88px] in
 * PlayerPointsSheet.css, matching ValidationBar/PickTeamPage's own submit-bar
 * convention) rather than at bottom-0 covering it, so the nav stays visible
 * and tappable while this is open.
 *
 * player is a PitchLineup lineup item: {..., breakdown, captain, viceCaptain,
 * multiplier, minutes, points, position, club, name}. breakdown is null for
 * a finalized contest (Game_logic/dream11.py's UserTeamPlayerResponse --
 * dream11.team_players only persists the final total, not the itemized
 * components, once a result is frozen); the captain/vice bonus row and the
 * total row don't need it -- both are arithmetic on `points`, which is
 * always present -- so only the itemized section is replaced by a note.
 */
function PlayerPointsSheet({ player, onClose }) {
  const { dragY, onPointerDown, onPointerMove, onPointerUp, onPointerCancel } = usePointerDismiss(onClose)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    // Mount closed, then flip a frame later so the slide-up actually
    // transitions instead of snapping straight to its open position.
    const id = requestAnimationFrame(() => setOpen(true))
    return () => cancelAnimationFrame(id)
  }, [])

  if (!player) return null

  const badge = player.captain ? 'C' : player.viceCaptain ? 'VC' : null
  const bonusMultiplier = player.captain || player.viceCaptain ? player.multiplier - 1 : 0
  const bonusPoints = bonusMultiplier > 0 ? player.points * bonusMultiplier : 0
  const total = player.points + bonusPoints

  const dragging = dragY > 0
  const panelStyle = dragging
    ? { transform: `translate(-50%, ${dragY}px)`, transition: 'none' }
    : undefined

  return (
    <>
      <div
        className={`player-points-sheet__backdrop ${open ? 'player-points-sheet__backdrop--open' : ''}`}
        onClick={onClose}
      />
      <section
        aria-label={`${player.name}'s scoring breakdown`}
        className={`player-points-sheet ${open ? 'player-points-sheet--open' : ''}`}
        data-testid="player-points-sheet"
        role="dialog"
        style={panelStyle}
      >
        {/* Only the handle bar itself starts a drag -- NOT the whole header,
            which used to also wrap the close button. setPointerCapture()
            below claims every subsequent pointer event for whatever element
            onPointerDown fires on, which silently ate the close button's own
            click once it was nested inside this same area (caught live: the
            X button stopped closing the sheet). The handle's hit area is
            padded well past its visible 4px bar via CSS, so it's still an
            easy real-finger target despite being visually small. */}
        <div
          className="player-points-sheet__handle-area"
          onPointerCancel={onPointerCancel}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
        >
          <div className="player-points-sheet__handle" />
        </div>
        <header className="player-points-sheet__header">
          <PlayerJersey
            captain={player.captain}
            player={player}
            showName={false}
            size="sm"
            viceCaptain={player.viceCaptain}
          />
          <div className="player-points-sheet__identity">
            <div className="player-points-sheet__name-row">
              <span className="player-points-sheet__name">{player.name}</span>
              {badge && <span className="player-points-sheet__badge">{badge}</span>}
            </div>
            <span className="player-points-sheet__meta">
              {player.position} · {player.minutes}&apos;
            </span>
          </div>
          <button
            aria-label="Close"
            className="player-points-sheet__close"
            onClick={onClose}
            type="button"
          >
            <span className="material-symbols-outlined">close</span>
          </button>
        </header>

        <div className="player-points-sheet__body">
          {player.breakdown ? (
            <>
              <h3 className="player-points-sheet__section-title">Scoring breakdown</h3>
              <dl className="player-points-sheet__rows">
                {ROWS.filter((row) => {
                  if (row.eligibleKey && !player.breakdown[row.eligibleKey]) return false
                  if (row.onlyIfNonZero && player.breakdown[row.countKey] === 0) return false
                  return true
                }).map((row) => {
                  const value = player.breakdown[row.key]
                  return (
                    <div className="player-points-sheet__row" key={row.key}>
                      <dt className="player-points-sheet__label">
                        {row.label}
                        {row.countKey != null && ` (${player.breakdown[row.countKey]})`}
                      </dt>
                      <dd className={`player-points-sheet__value ${signedValueClass(value)}`}>
                        {formatSigned(value)}
                      </dd>
                    </div>
                  )
                })}
                <div className="player-points-sheet__row player-points-sheet__row--neutral">
                  <dt className="player-points-sheet__label">Minutes played</dt>
                  <dd className="player-points-sheet__value player-points-sheet__value--neutral">
                    {player.minutes}&apos;
                  </dd>
                </div>
              </dl>
            </>
          ) : (
            <p className="player-points-sheet__frozen-note">
              This contest is finalized — the per-category breakdown isn&apos;t kept once a
              result is frozen, only the final total below.
            </p>
          )}

          {bonusMultiplier > 0 && (
            <div className="player-points-sheet__bonus-row">
              <span>
                {player.captain ? 'Captain' : 'Vice-captain'} bonus (+{bonusMultiplier * 100}% extra)
              </span>
              <span className="player-points-sheet__value player-points-sheet__value--positive">
                {formatSigned(bonusPoints)}
              </span>
            </div>
          )}

          <div className="player-points-sheet__divider" />
          <div className="player-points-sheet__total-row">
            <span>Contribution to team total</span>
            <span>{formatSigned(total)}</span>
          </div>
        </div>
      </section>
    </>
  )
}

export default PlayerPointsSheet
