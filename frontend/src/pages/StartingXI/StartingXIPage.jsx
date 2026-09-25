import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import FplHeader from '../../components/FplHeader/FplHeader'
import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  getFirstCollision,
  pointerWithin,
  useDroppable,
  useSensor,
  useSensors,
} from '@dnd-kit/core'
import { SortableContext, arrayMove, rectSortingStrategy, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { fetchCurrentSquad } from '../../api/squad'
import { fetchCurrentSelection, submitGwSelection } from '../../api/gwSelection'
import { LockedError } from '../../api/client'
import PlayerJersey from '../../components/PlayerJersey/PlayerJersey'
import { normalizePremierLeaguePlayer } from '../../data/premierLeague2026'
import { computeSwapEligibility } from '../../data/tacticalSwapEligibility'
import { kickoffLabel } from '../../data/kickoff'
import DashboardIcon from '../../components/DashboardIcon/DashboardIcon'

const POSITION_ORDER = ['GK', 'DEF', 'MID', 'FWD']
const STARTING_XI_SIZE = 11
const DEFAULT_TACTIC = 'balanced'
const TACTIC_BONUS_POSITION = {
  attack: 'FWD',
  defence: 'DEF',
  balanced: 'MID',
}
const TACTICS = [
  { id: 'attack', label: 'Attack', bonusPosition: 'FWD' },
  { id: 'defence', label: 'Defence', bonusPosition: 'DEF' },
  { id: 'balanced', label: 'Balanced', bonusPosition: 'MID' },
]
const FORMATION_OPTIONS = [
  { label: '3-4-3', counts: { DEF: 3, MID: 4, FWD: 3 } },
  { label: '3-5-2', counts: { DEF: 3, MID: 5, FWD: 2 } },
  { label: '4-3-3', counts: { DEF: 4, MID: 3, FWD: 3 } },
  { label: '4-4-2', counts: { DEF: 4, MID: 4, FWD: 2 } },
  { label: '4-5-1', counts: { DEF: 4, MID: 5, FWD: 1 } },
  { label: '5-3-2', counts: { DEF: 5, MID: 3, FWD: 2 } },
  { label: '5-4-1', counts: { DEF: 5, MID: 4, FWD: 1 } },
]

function benchOrderForSubmit(benchIds, squadById) {
  const benchPlayers = benchIds.map((id) => squadById.get(id)).filter(Boolean)
  const backupGk = benchPlayers.find((p) => p.position === 'GK')
  const outfield = benchPlayers.filter((p) => p.position !== 'GK')
  return backupGk ? [backupGk.player_id, ...outfield.map((p) => p.player_id)] : benchIds
}

// Mirrors the tactical selection rules closely enough for immediate UI
// feedback. This duplicates backend logic on purpose, not by oversight: there is
// no dry-run/validate endpoint (GET /gw_selection only plays back what was
// already saved -- see that file's own docstring), and POST /gw_selection is
// the only way to ask the server "is this legal," which would mean a round
// trip on every drag. So this is the same pattern used elsewhere for instant
// feedback while editing; it is NOT the source of truth. The Save Team
// response is -- a formation that passes here can still be rejected there if
// the two ever drift, and the submitError banner (not this check) is what
// renders on that mismatch.
function computeSelectionErrors(startingIds, benchIds, squadById, tactic, bonusPlayerIds, swaps) {
  const errors = []
  const counts = { GK: 0, DEF: 0, MID: 0, FWD: 0 }
  for (const id of startingIds) {
    const pos = squadById.get(id)?.position
    if (pos) counts[pos] += 1
  }

  if (startingIds.length !== STARTING_XI_SIZE) {
    errors.push(`Starting XI must have exactly ${STARTING_XI_SIZE} players -- currently ${startingIds.length}`)
  }
  if (counts.GK !== 1) errors.push(`Exactly 1 goalkeeper required -- currently ${counts.GK}`)
  if (counts.DEF < 3) errors.push(`At least 3 defenders required -- currently ${counts.DEF}`)
  if (counts.MID < 3) errors.push(`At least 3 midfielders required -- currently ${counts.MID}`)
  if (counts.FWD < 1) errors.push(`At least 1 forward required -- currently ${counts.FWD}`)
  if (benchIds.length !== 4) errors.push(`Bench must have exactly 4 players -- currently ${benchIds.length}`)

  const submittedBench = benchOrderForSubmit(benchIds, squadById)
  if (submittedBench.length === 4) {
    const [slot12, ...outfieldSlots] = submittedBench
    if (squadById.get(slot12)?.position !== 'GK') errors.push('Bench slot 12 must be the backup goalkeeper')
    if (outfieldSlots.some((id) => squadById.get(id)?.position === 'GK')) {
      errors.push('Bench slots 13-15 must be outfield players')
    }
  }

  const bonusPosition = TACTIC_BONUS_POSITION[tactic]
  if (!bonusPosition) {
    errors.push('Select a valid tactic')
  } else {
    if (tactic === 'attack' && counts.FWD < 2) {
      errors.push('Attack tactic requires at least 2 starting forwards')
    }
    if (bonusPlayerIds.length !== 2) {
      errors.push(`Select exactly 2 Bonus ${bonusPosition} players -- currently ${bonusPlayerIds.length}`)
    }
    const duplicateBonus = new Set(bonusPlayerIds).size !== bonusPlayerIds.length
    if (duplicateBonus) errors.push('Bonus Players must be distinct')
    for (const id of bonusPlayerIds) {
      if (!startingIds.includes(id)) {
        errors.push(`Bonus Player ${id} must be in the starting XI`)
      } else if (squadById.get(id)?.position !== bonusPosition) {
        errors.push(`Bonus Player ${squadById.get(id)?.name ?? id} must be a ${bonusPosition}`)
      }
    }
  }

  const tacticalBenchIds = submittedBench.slice(2, 4)
  const usedSwapPlayers = new Set()
  if (swaps.length > 2) errors.push(`At most 2 tactical swaps are allowed -- currently ${swaps.length}`)
  for (const swap of swaps) {
    const outgoing = squadById.get(swap.player_out_id)
    const incoming = squadById.get(swap.player_in_id)
    if (!startingIds.includes(swap.player_out_id)) {
      errors.push('Each swap needs an outgoing starter')
    }
    if (bonusPlayerIds.includes(swap.player_out_id)) {
      errors.push(`${outgoing?.name ?? 'A Bonus Player'} cannot be swapped out`)
    }
    if (!tacticalBenchIds.includes(swap.player_in_id)) {
      errors.push('Each incoming player must be in Tactical Sub slot 14 or 15')
    }
    if (outgoing && incoming && outgoing.position !== incoming.position) {
      errors.push(`${outgoing.name} and ${incoming.name} must play the same position`)
    }
    if (usedSwapPlayers.has(swap.player_out_id) || usedSwapPlayers.has(swap.player_in_id)) {
      errors.push('A player can only be used in one tactical swap')
    }
    usedSwapPlayers.add(swap.player_out_id)
    usedSwapPlayers.add(swap.player_in_id)
  }

  return errors
}

// A container needs its own droppable zone (not just its items') so a
// card can be dropped onto genuinely empty space -- SortableContext alone
// only creates drop targets around existing items, which doesn't help
// the very first drop onto an empty pitch.
function DroppableContainer({ id, className, children }) {
  const { setNodeRef } = useDroppable({ id })
  return (
    <div ref={setNodeRef} id={id} className={className}>
      {children}
    </div>
  )
}

function SortablePitchPlayer({ player, isBonus, bonusEligible, onToggleBonus, onBench }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: player.player_id })
  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.4 : 1,
  }

  return (
    <div
      className="pitch-player relative w-[68px] flex flex-col items-center group cursor-pointer"
      onClick={() => {
        if (bonusEligible) onToggleBonus(player.player_id)
      }}
      ref={setNodeRef}
      style={style}
    >
      {/* Drag handle stays a separate hit area: the card itself opens the
          Bonus toggle on tap, so the whole card can't be the handle. */}
      <span
        aria-label={`Drag ${player.name}`}
        className="absolute -top-2 -left-2 z-30 bg-surface text-on-surface-variant rounded-full p-0.5 shadow-sm border border-outline-variant opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity cursor-grab active:cursor-grabbing"
        {...attributes}
        {...listeners}
        onClick={(e) => e.stopPropagation()}
      >
        <span className="material-symbols-outlined text-[14px] block">drag_indicator</span>
      </span>

      <span
        aria-label={`Bench ${player.name}`}
        className="absolute -top-2 -right-2 z-20 bg-surface text-on-surface rounded-full p-0.5 shadow-sm border border-outline-variant opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity"
        onClick={(e) => {
          e.stopPropagation()
          onBench(player)
        }}
        role="button"
        tabIndex={-1}
      >
        <span className="material-symbols-outlined text-[14px] block">swap_horiz</span>
      </span>

      {isBonus && (
        <span
          aria-label="Bonus Player"
          className="mb-0.5 inline-flex h-5 items-center gap-1 rounded-full border border-primary-container/30 bg-primary-fixed px-1.5 text-primary shadow-sm"
          title="Selected tactical performance token"
        >
          <DashboardIcon name="bonusStar" size={12} />
        </span>
      )}
      <PlayerJersey
        player={player}
        size={isBonus ? 'lg' : 'md'}
      />
      <span className="mt-0.5 rounded bg-surface-container-lowest/90 px-1.5 py-0.5 text-[9px] font-semibold leading-none text-on-surface-variant shadow-sm">
        {player.position}
      </span>
    </div>
  )
}

// The bench GK: never draggable, never reordered against the 3 outfield subs
// -- there's only ever one, so there's nothing to prioritise it against.
// resolve_autosubs (Results/scoring.py) finds it by ml.players.position at
// scoring time, never by its slot in bench_order, so it doesn't need (and
// must not claim) an outfield-style priority label like "4th Sub".
function ReserveGkPlayer({ player, canPromote, onPromote }) {
  return (
    <div className="bench-player flex items-center gap-sm p-sm bg-surface-container-lowest rounded-lg border border-outline-variant">
      <span className="w-[18px] shrink-0" aria-hidden="true" />
      <span className="font-label-md text-[9px] text-on-surface-variant w-[72px] shrink-0">
        12 Auto GK
      </span>
      <PlayerJersey player={player} size="xs" showName={false} />
      <span className="flex-1 min-w-0 flex flex-col">
        <span className="font-body-md text-body-md text-on-surface truncate">{player.name}</span>
        <span className="font-label-md text-[9px] text-on-surface-variant">{player.position}</span>
      </span>
      <div className="flex items-center gap-1 shrink-0">
        <button
          aria-label={`Move ${player.name} into starting XI`}
          className="w-7 h-7 rounded flex items-center justify-center text-on-surface-variant bg-surface-container border border-outline-variant hover:bg-surface-container-high transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
          disabled={!canPromote}
          onClick={onPromote}
          type="button"
        >
          <span className="material-symbols-outlined text-[14px]">person_add</span>
        </button>
      </div>
    </div>
  )
}

function SortableBenchPlayer({
  player,
  subLabel,
  canMoveUp,
  canMoveDown,
  canPromote,
  canPlanSwap,
  onMoveUp,
  onMoveDown,
  onPromote,
  onPlanSwap,
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: player.player_id })
  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.4 : 1,
  }

  const ctrl =
    'w-7 h-7 rounded flex items-center justify-center text-on-surface-variant bg-surface-container ' +
    'border border-outline-variant hover:bg-surface-container-high transition-colors ' +
    'disabled:opacity-30 disabled:cursor-not-allowed'

  return (
    <div
      className="bench-player flex items-center gap-sm p-sm bg-surface-container-lowest rounded-lg border border-outline-variant"
      ref={setNodeRef}
      style={style}
    >
      <span
        aria-label={`Drag ${player.name}`}
        className="text-on-surface-variant cursor-grab active:cursor-grabbing shrink-0"
        {...attributes}
        {...listeners}
      >
        <span className="material-symbols-outlined text-[18px] block">drag_indicator</span>
      </span>
      <span className="font-label-md text-[9px] text-on-surface-variant w-[72px] shrink-0 inline-flex items-center gap-1">
        {subLabel.includes('Tactical') && (
          <DashboardIcon className="text-primary" name="bonusStar" size={10} />
        )}
        {subLabel}
      </span>
      <PlayerJersey player={player} size="xs" showName={false} />
      <span className="flex-1 min-w-0 flex flex-col">
        <span className="font-body-md text-body-md text-on-surface truncate">{player.name}</span>
        <span className="font-label-md text-[9px] text-on-surface-variant">{player.position}</span>
      </span>
      <div className="flex items-center gap-1 shrink-0">
        <button
          aria-label={`Move ${player.name} up`}
          className={ctrl}
          disabled={!canMoveUp}
          onClick={onMoveUp}
          type="button"
        >
          <span className="material-symbols-outlined text-[14px]">arrow_upward</span>
        </button>
        <button
          aria-label={`Move ${player.name} down`}
          className={ctrl}
          disabled={!canMoveDown}
          onClick={onMoveDown}
          type="button"
        >
          <span className="material-symbols-outlined text-[14px]">arrow_downward</span>
        </button>
        <button
          aria-label={`Move ${player.name} into starting XI`}
          className={ctrl}
          disabled={!canPromote}
          onClick={onPromote}
          type="button"
        >
          <span className="material-symbols-outlined text-[14px]">person_add</span>
        </button>
        <button
          aria-label={`Plan tactical swap for ${player.name}`}
          className={ctrl}
          disabled={!canPlanSwap}
          onClick={onPlanSwap}
          type="button"
        >
          <span className="material-symbols-outlined text-[14px]">published_with_changes</span>
        </button>
      </div>
    </div>
  )
}

function StartingXIPage() {
  const { settings } = useOutletContext()
  const { user_id, season, gameweek } = settings

  const [squad, setSquad] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(null)

  const [startingIds, setStartingIds] = useState([])
  const [benchIds, setBenchIds] = useState([])
  const [tactic, setTactic] = useState(DEFAULT_TACTIC)
  const [bonusPlayerIds, setBonusPlayerIds] = useState([])
  const [swaps, setSwaps] = useState([])

  const [planningSwapInId, setPlanningSwapInId] = useState(null)
  const [activeDragId, setActiveDragId] = useState(null)
  // Which of the two bench-related sections is showing -- per the "Starting
  // XI & Team Management" mockup, Auto Sub Players and Tactical Sub share one
  // tabbed panel instead of stacking both sections at once.
  const [benchTab, setBenchTab] = useState('auto')
  // {fpl_id (string) -> {first_kickoff, last_end}} from GET /gw_selection,
  // covering all 15 squad players. Drives the Tactical Swap eligibility
  // panel and picker -- see data/tacticalSwapEligibility.js.
  const [fixtureWindows, setFixtureWindows] = useState({})

  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState(null)
  // Set once the backend rejects a write because the deadline has passed.
  // Sticky for the rest of the visit: the deadline doesn't un-pass.
  const [locked, setLocked] = useState(false)
  const [submitSuccess, setSubmitSuccess] = useState(null)

  const sensors = useSensors(
    // distance threshold means a plain tap still registers as a click
    // (toggles Bonus / fires button onClick) instead of
    // always starting a drag -- only a deliberate press-and-move drags.
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates })
  )

  const lastOverIdRef = useRef(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setLoadError(null)
    Promise.all([
      fetchCurrentSquad({ season, gameweek }),
      fetchCurrentSelection({ season, gameweek }),
    ])
      .then(([squadData, selectionData]) => {
        if (cancelled) return
        const normalizedSquadData = {
          ...squadData,
          players: (squadData.players ?? []).map(normalizePremierLeaguePlayer),
        }
        setSquad(normalizedSquadData)
        setFixtureWindows(selectionData.fixture_windows ?? {})
        // Known up front now, not just discovered from a failed save --
        // deadline_has_passed() is the same check the write path itself
        // makes, so a manager who opens this page after the deadline sees
        // the locked state immediately.
        if (selectionData.deadline_passed) setLocked(true)
        if (selectionData.has_selection) {
          // Reconcile the saved selection against the CURRENT squad. A
          // transfer made after this gameweek's XI was saved leaves the
          // selection pointing at players who are no longer owned: they can't
          // render (squadById has no entry) but they still counted towards
          // startingIds.length, so the XI reported 12 players while showing
          // 11 and "Save Team" stayed disabled with an error the user had no
          // way to clear. Conversely a transferred-IN player belonged to no
          // list at all and simply vanished from the page.
          const squadIds = new Set(normalizedSquadData.players.map((p) => p.player_id))
          const starting = selectionData.player_ids.filter((id) => squadIds.has(id))
          const bench = selectionData.bench_order.filter((id) => squadIds.has(id))

          // Anyone owned but unplaced (a new signing) starts on the bench --
          // never silently promoted into the XI.
          const placed = new Set([...starting, ...bench])
          const unplaced = normalizedSquadData.players
            .map((p) => p.player_id)
            .filter((id) => !placed.has(id))

          setStartingIds(starting)
          setBenchIds([...bench, ...unplaced])
          setTactic(selectionData.tactic ?? DEFAULT_TACTIC)
          setBonusPlayerIds((selectionData.bonus_player_ids ?? []).filter((id) => starting.includes(id)))
          setSwaps(selectionData.swaps ?? [])
        } else {
          setStartingIds([])
          setBenchIds(normalizedSquadData.players.map((p) => p.player_id))
          setTactic(DEFAULT_TACTIC)
          setBonusPlayerIds([])
          setSwaps([])
        }
      })
      .catch((err) => {
        if (cancelled) return
        setLoadError(err.message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [user_id, season, gameweek])

  const squadById = useMemo(
    () => new Map((squad?.players ?? []).map((p) => [p.player_id, p])),
    [squad]
  )
  const allSquadIds = useMemo(() => (squad?.players ?? []).map((p) => p.player_id), [squad])

  // Bench is always exactly "the squad minus whoever's starting" -- keep it
  // in sync with startingIds while preserving whatever manual reorder
  // priority the player already set for players who stay benched.
  useEffect(() => {
    setBenchIds((prev) => {
      const stillBenched = prev.filter((id) => !startingIds.includes(id) && allSquadIds.includes(id))
      const newlyBenched = allSquadIds.filter((id) => !startingIds.includes(id) && !stillBenched.includes(id))
      return [...stillBenched, ...newlyBenched]
    })
  }, [startingIds, allSquadIds])

  const groupedStarting = useMemo(() => {
    const groups = { GK: [], DEF: [], MID: [], FWD: [] }
    for (const id of startingIds) {
      const p = squadById.get(id)
      if (p) groups[p.position]?.push(p)
    }
    return groups
  }, [startingIds, squadById])
  const canUseAttack = groupedStarting.FWD.length >= 2

  const benchPlayers = useMemo(() => benchIds.map((id) => squadById.get(id)).filter(Boolean), [benchIds, squadById])
  // Split for display only -- benchIds itself stays one array (its order is
  // what's actually submitted as bench_order). The reserve GK is always
  // wherever it falls in that array; only the outfield players' RELATIVE
  // order to each other is the real substitution priority resolve_autosubs
  // reads, so filtering here can't disturb it.
  const benchGk = useMemo(() => benchPlayers.find((p) => p.position === 'GK') ?? null, [benchPlayers])
  const outfieldBench = useMemo(() => benchPlayers.filter((p) => p.position !== 'GK'), [benchPlayers])
  const outfieldBenchIds = useMemo(() => outfieldBench.map((p) => p.player_id), [outfieldBench])
  const submittedBenchOrder = useMemo(() => benchOrderForSubmit(benchIds, squadById), [benchIds, squadById])
  const tacticalBench = useMemo(
    () => submittedBenchOrder.slice(2, 4).map((id) => squadById.get(id)).filter(Boolean),
    [submittedBenchOrder, squadById]
  )
  // Real squad spend (all 15 players), not just the starting XI -- matches
  // what the manager actually paid, same field ReplacementRow already uses.
  const teamValue = useMemo(
    () => (squad?.players ?? []).reduce((sum, p) => sum + (p.price ?? 0), 0),
    [squad]
  )
  const bonusPosition = TACTIC_BONUS_POSITION[tactic]
  const bonusCandidates = useMemo(
    () => startingIds.map((id) => squadById.get(id)).filter((p) => p?.position === bonusPosition),
    [startingIds, squadById, bonusPosition]
  )

  // Recomputed on every render that changes starters, Bonus Players, bench
  // slots or fixture data -- per the brief, this must never go stale while
  // the manager is still editing. Purely advisory: it decides what this
  // screen SHOWS before a save, never what POST /gw_selection accepts --
  // that's still validate_selection, server-side.
  const swapEligibility = useMemo(
    () =>
      computeSwapEligibility({
        starters: startingIds.map((id) => squadById.get(id)).filter(Boolean),
        bonusPlayerIds,
        tacticalSubs: tacticalBench,
        fixtureWindows,
        existingSwaps: swaps,
      }),
    [startingIds, squadById, bonusPlayerIds, tacticalBench, fixtureWindows, swaps]
  )

  useEffect(() => {
    setBonusPlayerIds((prev) =>
      prev.filter((id) => startingIds.includes(id) && squadById.get(id)?.position === bonusPosition)
    )
  }, [startingIds, squadById, bonusPosition])

  useEffect(() => {
    if (tactic === 'attack' && !canUseAttack) {
      setTactic(DEFAULT_TACTIC)
    }
  }, [tactic, canUseAttack])

  useEffect(() => {
    const tacticalIds = new Set(submittedBenchOrder.slice(2, 4))
    setSwaps((prev) =>
      prev.filter((swap) => startingIds.includes(swap.player_out_id) && tacticalIds.has(swap.player_in_id))
    )
  }, [startingIds, submittedBenchOrder])

  // Order within the starting XI is meaningless (only membership + Bonus
  // matter), so resolving "over" against every individual pitch card via
  // closestCenter caused a real bug: as a dragged card crossed into the
  // pitch, the bench list reflowed underneath the (stationary) pointer,
  // which flipped the nearest-item result back to a bench card, which
  // moved the item back, which reflowed the pitch, which flipped it
  // forward again -- an infinite per-frame oscillation between the two
  // containers. Fix: treat the whole pitch as one stable drop target
  // (pointerWithin against #starting-container) instead of resolving to
  // whichever pitch card happens to be nearest. Bench still resolves to
  // individual items via closestCenter, since sub-priority order there
  // is real and needs precise reordering.
  const collisionDetectionStrategy = useCallback(
    (args) => {
      const pointerCollisions = pointerWithin(args)

      if (pointerCollisions.some((c) => c.id === 'starting-container')) {
        lastOverIdRef.current = 'starting-container'
        return [{ id: 'starting-container' }]
      }

      if (pointerCollisions.some((c) => c.id === 'bench-container')) {
        // Only the 3 outfield subs are individually sortable -- the reserve
        // GK isn't wrapped in useSortable (there's nothing to prioritise it
        // against), so it never appears here as a per-item drop target.
        const benchItemContainers = args.droppableContainers.filter((c) => outfieldBenchIds.includes(c.id))
        const benchCollisions = closestCenter({ ...args, droppableContainers: benchItemContainers })
        const overId = getFirstCollision(benchCollisions, 'id') ?? 'bench-container'
        lastOverIdRef.current = overId
        return [{ id: overId }]
      }

      const fallback = closestCenter(args)
      const overId = getFirstCollision(fallback, 'id')
      if (overId != null) {
        lastOverIdRef.current = overId
        return fallback
      }
      return lastOverIdRef.current ? [{ id: lastOverIdRef.current }] : []
    },
    [outfieldBenchIds]
  )

  const selectionErrors = useMemo(
    () => computeSelectionErrors(startingIds, benchIds, squadById, tactic, bonusPlayerIds, swaps),
    [startingIds, benchIds, squadById, tactic, bonusPlayerIds, swaps]
  )
  const formationLabel = useMemo(() => {
    const counts = { GK: 0, DEF: 0, MID: 0, FWD: 0 }
    for (const id of startingIds) {
      const pos = squadById.get(id)?.position
      if (pos) counts[pos] += 1
    }
    return `${counts.DEF}-${counts.MID}-${counts.FWD}`
  }, [startingIds, squadById])

  function applyFormation(label) {
    const formation = FORMATION_OPTIONS.find((option) => option.label === label)
    if (!formation || !squad) return

    const pickFromPosition = (position, count) => {
      const current = startingIds.filter((id) => squadById.get(id)?.position === position)
      const available = squad.players
        .filter((p) => p.position === position && !current.includes(p.player_id))
        .map((p) => p.player_id)
      return [...current, ...available].slice(0, count)
    }

    const nextStartingIds = [
      ...pickFromPosition('GK', 1),
      ...pickFromPosition('DEF', formation.counts.DEF),
      ...pickFromPosition('MID', formation.counts.MID),
      ...pickFromPosition('FWD', formation.counts.FWD),
    ]
    const nextStartingSet = new Set(nextStartingIds)
    const nextBenchIds = [
      ...benchIds.filter((id) => !nextStartingSet.has(id) && allSquadIds.includes(id)),
      ...startingIds.filter((id) => !nextStartingSet.has(id)),
      ...allSquadIds.filter((id) => !nextStartingSet.has(id) && !benchIds.includes(id) && !startingIds.includes(id)),
    ]

    setStartingIds(nextStartingIds)
    setBenchIds(nextBenchIds)
    setBonusPlayerIds((prev) => prev.filter((id) => nextStartingSet.has(id)))
    setSwaps((prev) => prev.filter((swap) => nextStartingSet.has(swap.player_out_id)))
  }

  // A blank gameweek (no fixture at all) sorts last, not first: such a
  // player can start (having no fixture isn't an error for a starter) but
  // can never be the incoming or outgoing side of a Tactical Swap (D4: "both
  // players need a fixture this gameweek"), so he's the last player this
  // auto-fill should push toward the swap-eligible bench slots.
  const kickoffMs = (player) => {
    const iso = player?.first_kickoff
    return iso ? new Date(iso).getTime() : Number.POSITIVE_INFINITY
  }

  // Auto-arranges the XI and bench so every legal Tactical Swap the manager
  // might plan already satisfies the timing rule (Gameplay/selection_rules.py:
  // incoming's first kickoff strictly after outgoing's last fixture ends),
  // instead of the manager discovering by trial and error which pairs are
  // even eligible.
  //
  // The mechanism: within each outfield position, sort by kickoff and start
  // the EARLIEST players, benching the LATEST. Because a Tactical Swap is
  // always same-position (D4), every bench player at a position therefore
  // has a kickoff at or after every starter at that same position -- so ANY
  // same-position starter/bench pairing already satisfies "incoming after
  // outgoing". Of the 3 leftover outfield players (15-player squad, 11
  // starters, 1 bench GK), the 2 with the LATEST kickoff go into the
  // swap-eligible Tactical slots (14, 15); the earliest of the 3 goes into
  // the Auto Sub slot (13), which carries no timing rule at all.
  //
  // Caveat, stated rather than hidden: this compares each player's FIRST
  // kickoff only. In a double Gameweek the rule actually cares about the
  // outgoing player's LAST fixture ending -- exact for the common
  // single-fixture case, an approximation otherwise. A kickoff-time tie
  // between an outgoing and incoming player of the same position (two
  // teams sharing a kickoff slot) is the one case "strictly after" can
  // still reject; selectionErrors below will say so if it happens.
  function autoFillByKickoff() {
    const formation = FORMATION_OPTIONS.find((option) => option.label === formationLabel) ?? FORMATION_OPTIONS[2]
    if (!squad) return

    const byPosition = { GK: [], DEF: [], MID: [], FWD: [] }
    for (const p of squad.players) byPosition[p.position]?.push(p)
    for (const pos of POSITION_ORDER) byPosition[pos].sort((a, b) => kickoffMs(a) - kickoffMs(b))

    const startingGk = byPosition.GK[0]
    const startingDef = byPosition.DEF.slice(0, formation.counts.DEF)
    const startingMid = byPosition.MID.slice(0, formation.counts.MID)
    const startingFwd = byPosition.FWD.slice(0, formation.counts.FWD)
    const nextStartingIds = [startingGk, ...startingDef, ...startingMid, ...startingFwd]
      .filter(Boolean)
      .map((p) => p.player_id)

    const benchGkPlayer = byPosition.GK[1] ?? null
    const leftoverOutfield = [
      ...byPosition.DEF.slice(formation.counts.DEF),
      ...byPosition.MID.slice(formation.counts.MID),
      ...byPosition.FWD.slice(formation.counts.FWD),
    ].sort((a, b) => kickoffMs(b) - kickoffMs(a)) // latest first
    const tacticalSubs = leftoverOutfield.slice(0, 2) // slots 14, 15: swap-eligible
    const autoSub = leftoverOutfield.slice(2) // slot 13: the earliest-kickoff leftover

    const nextBenchIds = [benchGkPlayer, ...autoSub, ...tacticalSubs]
      .filter(Boolean)
      .map((p) => p.player_id)

    const nextStartingSet = new Set(nextStartingIds)
    setStartingIds(nextStartingIds)
    setBenchIds(nextBenchIds)
    setBonusPlayerIds((prev) => prev.filter((id) => nextStartingSet.has(id)))
    setSwaps([])
  }

  function toggleStarting(player) {
    const isStarting = startingIds.includes(player.player_id)
    if (isStarting) {
      setStartingIds((prev) => prev.filter((id) => id !== player.player_id))
      setBonusPlayerIds((prev) => prev.filter((id) => id !== player.player_id))
      setSwaps((prev) => prev.filter((swap) => swap.player_out_id !== player.player_id))
      return
    }
    if (player.position === 'GK') {
      // Single-select among the squad's GKs -- swap the starting GK rather
      // than stacking a second one in (formation requires exactly 1).
      setStartingIds((prev) => [...prev.filter((id) => squadById.get(id)?.position !== 'GK'), player.player_id])
      return
    }
    if (startingIds.length >= STARTING_XI_SIZE) return // full -- bench someone first
    setStartingIds((prev) => [...prev, player.player_id])
  }

  // The reserve GK's position within benchIds is arbitrary (see ReserveGkPlayer's
  // comment), so reordering the 3 outfield subs never needs to touch it --
  // just splice it back in wherever it already was. This is what both the
  // drag handles and the up/down buttons funnel through, since they're the
  // same underlying action (change bench_order) and must behave identically.
  function reorderOutfieldBench(nextOutfieldIds) {
    const gkId = benchIds.find((id) => squadById.get(id)?.position === 'GK')
    const nextBenchIds = gkId ? [gkId, ...nextOutfieldIds] : nextOutfieldIds
    setBenchIds(nextBenchIds)
    return nextBenchIds
  }

  function moveOutfieldBench(index, direction) {
    const swapWith = index + direction
    if (swapWith < 0 || swapWith >= outfieldBenchIds.length) return
    const next = [...outfieldBenchIds]
    ;[next[index], next[swapWith]] = [next[swapWith], next[index]]
    reorderOutfieldBench(next)
  }

  function toggleBonusPlayer(playerId) {
    const player = squadById.get(playerId)
    if (!player || player.position !== bonusPosition) return
    setBonusPlayerIds((prev) => {
      if (prev.includes(playerId)) return prev.filter((id) => id !== playerId)
      if (prev.length >= 2) return prev
      return [...prev, playerId]
    })
  }

  function addSwap(playerInId, playerOutId) {
    if (!playerInId || !playerOutId) return
    setSwaps((prev) => {
      const withoutIncoming = prev.filter((swap) => swap.player_in_id !== playerInId)
      if (withoutIncoming.some((swap) => swap.player_out_id === playerOutId)) return withoutIncoming
      if (withoutIncoming.length >= 2) return withoutIncoming
      return [...withoutIncoming, { player_out_id: playerOutId, player_in_id: playerInId }]
    })
    setPlanningSwapInId(null)
  }

  function removeSwap(playerInId) {
    setSwaps((prev) => prev.filter((swap) => swap.player_in_id !== playerInId))
  }

  function findContainer(id) {
    if (id === 'starting-container' || id === 'bench-container') return id === 'starting-container' ? 'starting' : 'bench'
    if (startingIds.includes(id)) return 'starting'
    if (benchIds.includes(id)) return 'bench'
    return null
  }

  function handleDragStart(event) {
    setActiveDragId(event.active.id)
  }

  function handleDragOver(event) {
    const { active, over } = event
    if (!over) return
    const activeContainer = findContainer(active.id)
    const overContainer = findContainer(over.id)
    if (!activeContainer || !overContainer || activeContainer === overContainer) return

    const activePlayer = squadById.get(active.id)
    if (!activePlayer) return

    if (overContainer === 'starting') {
      if (activePlayer.position === 'GK') {
        const currentGkId = startingIds.find((id) => squadById.get(id)?.position === 'GK')
        setStartingIds((prev) => [...prev.filter((id) => squadById.get(id)?.position !== 'GK'), active.id])
        setBenchIds((prev) => {
          const withoutActive = prev.filter((id) => id !== active.id)
          return currentGkId ? [currentGkId, ...withoutActive] : withoutActive
        })
      } else {
        if (startingIds.length >= STARTING_XI_SIZE) return // full -- reject, item stays on bench
        setStartingIds((prev) => [...prev, active.id])
        setBenchIds((prev) => prev.filter((id) => id !== active.id))
      }
    } else {
      setStartingIds((prev) => prev.filter((id) => id !== active.id))
      setBenchIds((prev) => (prev.includes(active.id) ? prev : [...prev, active.id]))
      setBonusPlayerIds((prev) => prev.filter((id) => id !== active.id))
      setSwaps((prev) => prev.filter((swap) => swap.player_out_id !== active.id))
    }
  }

  function handleDragEnd(event) {
    setActiveDragId(null)
    const { active, over } = event
    if (!over || active.id === over.id) return
    const activeContainer = findContainer(active.id)
    const overContainer = findContainer(over.id)
    if (!activeContainer || activeContainer !== overContainer) return

    if (activeContainer === 'starting') {
      setStartingIds((prev) => {
        const oldIndex = prev.indexOf(active.id)
        const newIndex = prev.indexOf(over.id)
        if (oldIndex === -1 || newIndex === -1) return prev
        return arrayMove(prev, oldIndex, newIndex)
      })
    } else {
      // Only the 3 outfield subs are sortable drag targets (the reserve GK
      // isn't), so active/over here are always outfield ids -- reorder that
      // subset and route through the same full-resubmit path the up/down
      // buttons use.
      const oldIndex = outfieldBenchIds.indexOf(active.id)
      const newIndex = outfieldBenchIds.indexOf(over.id)
      if (oldIndex === -1 || newIndex === -1) return
      reorderOutfieldBench(arrayMove(outfieldBenchIds, oldIndex, newIndex))
    }
  }

  async function handleSave() {
    setSubmitting(true)
    setSubmitError(null)
    setSubmitSuccess(null)
    try {
      await submitGwSelection({
        season,
        gameweek,
        player_ids: startingIds,
        bench_order: submittedBenchOrder,
        tactic,
        bonus_player_ids: bonusPlayerIds,
        swaps,
      })
      setSubmitSuccess('Team saved.')
    } catch (err) {
      // Past the deadline the DB trigger enforce_selection_lock_fn rejects the
      // write and the backend turns it into a clean 422. That's an expected
      // state, not a failure, so it becomes a locked banner rather than a red
      // error list -- and the save button goes away entirely.
      if (err instanceof LockedError) setLocked(true)
      setSubmitError(err.errors ?? [err.message])
    } finally {
      setSubmitting(false)
    }
  }

  if (loading) {
    return <div className="w-full px-safe-margin py-lg font-body-md text-body-md text-on-surface-variant">Loading squad...</div>
  }
  if (loadError) {
    return <div className="w-full px-safe-margin py-lg font-body-md text-body-md text-on-surface-variant">Couldn't load your squad: {loadError}</div>
  }
  if (!squad || squad.players.length === 0) {
    return <div className="w-full px-safe-margin py-lg font-body-md text-body-md text-on-surface-variant">No squad found for this season yet -- select your squad first.</div>
  }

  const activeDragPlayer = activeDragId != null ? squadById.get(activeDragId) : null

  return (
    <>
      <FplHeader title="Starting XI" />
      <main className="w-full flex flex-col pb-[180px] bg-[#FBF9F5] min-h-screen">
        <section className="mx-4 mt-3 rounded-[20px] border border-[#E5E6E1] bg-white p-4 shadow-sm flex flex-col gap-3">
          <h2 className="font-label-md text-[11px] font-bold uppercase tracking-wider text-on-surface-variant">Select Mode</h2>
          <div className="grid grid-cols-3 gap-1 rounded-full bg-[#F0F0EA] p-1" role="tablist" aria-label="Tactic">
            {TACTICS.map((option) => {
              const active = tactic === option.id
              const disabled = option.id === 'attack' && !canUseAttack
              return (
                <button
                  aria-pressed={active}
                  className={`h-10 rounded-full font-label-md text-label-md transition-all disabled:opacity-40 disabled:cursor-not-allowed ${
                    active
                      ? 'bg-[#667D28] text-white shadow-sm'
                      : 'text-on-surface-variant hover:bg-white hover:text-on-surface'
                  }`}
                  disabled={disabled}
                  key={option.id}
                  onClick={() => setTactic(option.id)}
                  title={disabled ? 'Attack needs at least 2 starting forwards' : undefined}
                  type="button"
                >
                  {option.label}
                </button>
              )
            })}
          </div>
          <div className="flex items-center justify-between gap-sm border-t border-[#E5E6E1] pt-3">
            <span className="font-label-md text-[11px] font-bold text-on-surface-variant uppercase tracking-wider">
              Bonus {bonusPosition}
            </span>
            <span className={`rounded-full px-2 py-1 font-label-md text-[10px] font-bold ${bonusPlayerIds.length === 2 ? 'bg-[#EDF2DF] text-[#667D28]' : 'bg-error-container text-error'}`}>
              {bonusPlayerIds.length}/2
            </span>
          </div>
          <div className="flex flex-wrap gap-2">
            {bonusCandidates.length === 0 ? (
              <span className="font-body-md text-body-md text-on-surface-variant">
                No eligible starters in this formation.
              </span>
            ) : (
              bonusCandidates.map((player) => {
                const active = bonusPlayerIds.includes(player.player_id)
                const full = bonusPlayerIds.length >= 2 && !active
                return (
                  <button
                    aria-pressed={active}
                    className={`px-3 py-1.5 rounded-full border font-label-md text-label-md transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
                      active
                        ? 'bg-[#EDF2DF] text-[#667D28] border-[#CBD8A8]'
                        : 'bg-white border-[#E5E6E1] text-on-surface hover:bg-[#F7F7F2]'
                    }`}
                    disabled={full}
                    key={player.player_id}
                    onClick={() => toggleBonusPlayer(player.player_id)}
                    type="button"
                  >
                    {active && <DashboardIcon className="inline-block mr-1 align-[-2px]" name="bonusStar" size={12} />}
                    {player.name}
                  </button>
                )
              })
            )}
          </div>
        </section>

        <div className="mx-4 mt-3 grid grid-cols-[1fr_auto] items-center gap-3 rounded-t-[20px] border border-[#E5E6E1] bg-white p-4 shadow-sm">
          <label className="flex items-center gap-2">
            <span className="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider">
              Formation
            </span>
            <select
              aria-label="Formation"
              className="rounded-full border border-[#E5E6E1] bg-[#FBF9F5] px-3 h-9 font-label-md text-label-md font-bold text-on-surface focus:border-[#667D28] focus:ring-1 focus:ring-[#667D28] outline-none"
              onChange={(e) => applyFormation(e.target.value)}
              value={formationLabel}
            >
              {FORMATION_OPTIONS.map((option) => (
                <option key={option.label} value={option.label}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <div className="flex flex-col items-end shrink-0">
            <span className="font-label-md text-[10px] text-on-surface-variant">Team Value</span>
            <span className="font-headline-sm text-body-md text-on-surface">£{teamValue.toFixed(1)}m</span>
          </div>
          <div
            className="col-span-2 bg-[#EDF2DF] px-3 py-2 rounded-full flex items-center justify-center gap-2 border border-[#CBD8A8]"
            data-testid="formation-pill"
          >
            <span className="font-label-md text-label-md text-on-surface">{formationLabel}</span>
            {selectionErrors.length === 0 ? (
              <>
                <DashboardIcon className="text-[#667D28]" name="check" size={16} strokeWidth={2.4} />
                <span className="font-label-md text-[10px] text-on-surface-variant whitespace-nowrap">
                  Valid
                </span>
              </>
            ) : (
              <span className="font-label-md text-[10px] text-error whitespace-nowrap">
                {selectionErrors.length} issue{selectionErrors.length === 1 ? '' : 's'}
              </span>
            )}
          </div>
        </div>

        <div className="mx-4 mb-3">
          <button
            className="w-full h-11 rounded-b-[20px] border-x border-b border-[#E5E6E1] bg-white text-on-surface font-label-md text-label-md font-bold flex items-center justify-center gap-2 hover:bg-[#F7F7F2] active:bg-[#EDF2DF] transition-colors"
            data-testid="auto-fill-by-kickoff"
            onClick={autoFillByKickoff}
            title="Starts the earliest-kickoff players per position and puts the two latest-kickoff outfield subs in the Tactical slots, so any same-position swap you plan is automatically timing-legal."
            type="button"
          >
            <DashboardIcon className="text-[#667D28]" name="timer" size={18} />
            Auto-Fill by Kickoff Order
          </button>
        </div>

        <DndContext
          sensors={sensors}
          collisionDetection={collisionDetectionStrategy}
          onDragStart={handleDragStart}
          onDragOver={handleDragOver}
          onDragEnd={handleDragEnd}
        >
          <SortableContext items={startingIds} strategy={rectSortingStrategy}>
            {/* The whole pitch is ONE droppable ('starting-container') -- see
                collisionDetectionStrategy above. Don't add per-row droppables. */}
            <DroppableContainer
              className="mx-md relative aspect-[3/4] bg-secondary rounded-xl overflow-hidden shadow-sm border border-outline-variant flex flex-col justify-between py-6 px-2 bg-[repeating-linear-gradient(0deg,transparent,transparent_10%,rgba(0,0,0,0.04)_10%,rgba(0,0,0,0.04)_20%)]"
              id="starting-container"
            >
              {POSITION_ORDER.map((pos) => (
                <div className="flex justify-center gap-4 flex-wrap" key={pos}>
                  {groupedStarting[pos].map((p) => (
                    <SortablePitchPlayer
                      key={p.player_id}
                      player={p}
                      isBonus={bonusPlayerIds.includes(p.player_id)}
                      bonusEligible={p.position === bonusPosition}
                      onToggleBonus={toggleBonusPlayer}
                      onBench={toggleStarting}
                    />
                  ))}
                </div>
              ))}
            </DroppableContainer>
          </SortableContext>

          <div className="flex bg-surface-container-high p-1 rounded-xl mx-md mt-md" role="tablist" aria-label="Bench view">
            <button
              aria-pressed={benchTab === 'auto'}
              className={`flex-1 py-2 text-center rounded-lg font-headline-sm text-body-md transition-all ${
                benchTab === 'auto'
                  ? 'bg-primary-container text-on-primary shadow-sm'
                  : 'text-on-surface-variant hover:text-on-surface'
              }`}
              onClick={() => setBenchTab('auto')}
              type="button"
            >
              Auto Sub Players
            </button>
            <button
              aria-pressed={benchTab === 'tactical'}
              className={`flex-1 py-2 text-center rounded-lg font-headline-sm text-body-md transition-all ${
                benchTab === 'tactical'
                  ? 'bg-primary-container text-on-primary shadow-sm'
                  : 'text-on-surface-variant hover:text-on-surface'
              }`}
              onClick={() => setBenchTab('tactical')}
              type="button"
            >
              Tactical Sub{swaps.length > 0 ? ` (${swaps.length}/2)` : ''}
            </button>
          </div>

          <section className={`px-md mt-md ${benchTab === 'auto' ? '' : 'hidden'}`}>
            <h3 className="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider mb-sm">
              Bench
            </h3>
            {/* One droppable zone for the whole bench (so a pitch player can
                still be dropped anywhere in it), but only the 3 outfield subs
                are individually sortable -- the reserve GK renders as its own
                fixed row, not part of that SortableContext, since there's
                only ever one and nothing to prioritise it against. */}
            <DroppableContainer
              className="bg-surface-container-low rounded-xl p-2 border border-outline-variant shadow-sm flex flex-col gap-2 min-h-[64px]"
              id="bench-container"
            >
              {benchGk && (
                // Always promotable: bringing in the reserve GK just swaps
                // it with whoever's starting in goal (see toggleStarting's
                // GK branch), never adds a 12th starter.
                <ReserveGkPlayer canPromote onPromote={() => toggleStarting(benchGk)} player={benchGk} />
              )}
              <SortableContext items={outfieldBenchIds} strategy={verticalListSortingStrategy}>
                {outfieldBench.map((p, i) => (
                  <SortableBenchPlayer
                    canMoveDown={i < outfieldBench.length - 1}
                    canMoveUp={i > 0}
                    canPromote={startingIds.length < STARTING_XI_SIZE}
                    canPlanSwap={i >= 1}
                    key={p.player_id}
                    onMoveDown={() => moveOutfieldBench(i, 1)}
                    onMoveUp={() => moveOutfieldBench(i, -1)}
                    onPromote={() => toggleStarting(p)}
                    onPlanSwap={() => setPlanningSwapInId(p.player_id)}
                    player={p}
                    subLabel={['13 Auto Sub', '14 Tactical', '15 Tactical'][i] ?? `${i + 13}`}
                  />
                ))}
              </SortableContext>
            </DroppableContainer>
          </section>

          {/* Advisory only -- shows what the picker below will accept before
              the manager opens it, using the exact same eligibility data
              (fixture_windows from GET /gw_selection) and the identical
              strictly-after rule the server enforces at submission
              (Gameplay/selection_rules.py::last_fixture_end). Never gates
              Save Team; selectionErrors below is what does that. */}
          {tacticalBench.length > 0 && (
            <section className={`px-md mt-md ${benchTab === 'tactical' ? '' : 'hidden'}`}>
              <h3 className="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider mb-sm">
                Tactical Swap Options
              </h3>
              <div className="flex flex-col gap-sm">
                {swapEligibility.map(({ incoming, incomingWindow, eligible, ineligible }) => (
                  <article
                    className="bg-surface-container-lowest rounded-xl p-sm border border-outline-variant shadow-sm flex flex-col gap-1.5"
                    key={incoming.player_id}
                  >
                    <p className="font-body-md text-body-md font-bold text-on-surface">
                      {incoming.name}{' '}
                      <span className="font-label-md text-label-md font-normal text-on-surface-variant">
                        ({incoming.position}) · kicks off{' '}
                        {incomingWindow ? kickoffLabel(incomingWindow.first_kickoff) : 'no fixture this gameweek'}
                      </span>
                    </p>
                    {eligible.length > 0 && (
                      <div className="flex items-start gap-xs bg-secondary-container/25 text-on-secondary-container font-label-md text-label-md font-semibold p-xs rounded-lg border border-secondary-container/50">
                        <span className="material-symbols-outlined text-[16px] mt-0.5 shrink-0" style={{ fontVariationSettings: "'FILL' 1" }}>
                          check_circle
                        </span>
                        <span>
                          Can replace:{' '}
                          <span className="font-bold">
                            {eligible
                              .map((p) => {
                                const w = fixtureWindows[p.player_id] ?? fixtureWindows[String(p.player_id)]
                                return `${p.name}${w ? ` (${kickoffLabel(w.first_kickoff)})` : ''}`
                              })
                              .join(', ')}
                          </span>
                        </span>
                      </div>
                    )}
                    {ineligible.map((p) => (
                      <div
                        className="flex items-start gap-xs bg-error-container/60 text-on-error-container font-label-md text-label-md p-xs rounded-lg border border-error-container leading-tight"
                        key={p.player_id}
                      >
                        <span className="material-symbols-outlined text-[16px] mt-0.5 shrink-0" style={{ fontVariationSettings: "'FILL' 1" }}>
                          cancel
                        </span>
                        <span>
                          Can&apos;t replace {p.name}: {p.reason}
                        </span>
                      </div>
                    ))}
                    {eligible.length === 0 && ineligible.length === 0 && (
                      <p className="font-label-md text-label-md text-on-surface-variant">
                        No same-position, non-Bonus starter to compare against.
                      </p>
                    )}
                  </article>
                ))}
              </div>
            </section>
          )}

          <section className={`px-md mt-md ${benchTab === 'tactical' ? '' : 'hidden'}`}>
            <div className="flex items-center justify-between mb-sm">
              <h3 className="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider">
                Tactical Swaps
              </h3>
              <span className="font-label-md text-label-md text-on-surface-variant">{swaps.length}/2</span>
            </div>
            <div className="bg-surface-container-low rounded-xl p-2 border border-outline-variant shadow-sm flex flex-col gap-2">
              {tacticalBench.length === 0 ? (
                <p className="font-body-md text-body-md text-on-surface-variant px-2 py-1">
                  Put outfield players in slots 14 and 15 to plan swaps.
                </p>
              ) : (
                tacticalBench.map((incoming, i) => {
                  const existing = swaps.find((swap) => swap.player_in_id === incoming.player_id)
                  const outgoing = existing ? squadById.get(existing.player_out_id) : null
                  const eligibility = swapEligibility[i] ?? { eligible: [], ineligible: [] }
                  const plannedOutgoingIds = new Set(swaps
                    .filter((swap) => swap.player_in_id !== incoming.player_id)
                    .map((swap) => swap.player_out_id))
                  // Same-position, non-Bonus, timing-eligible AND not already
                  // spoken for by the other swap slot.
                  const pickable = eligibility.eligible.filter((p) => !plannedOutgoingIds.has(p.player_id))
                  const blocked = [
                    ...eligibility.ineligible,
                    ...eligibility.eligible
                      .filter((p) => plannedOutgoingIds.has(p.player_id))
                      .map((p) => ({ ...p, reason: 'already planned as the other Tactical Sub’s swap' })),
                  ]
                  return (
                    <div
                      className="bg-surface-container-lowest border border-outline-variant rounded-lg p-sm flex flex-col gap-sm"
                      key={incoming.player_id}
                    >
                      <div className="flex items-center gap-sm">
                        <span className="font-label-md text-[10px] text-on-surface-variant w-[72px] shrink-0">
                          Slot {14 + i}
                        </span>
                        <PlayerJersey player={incoming} size="xs" showName={false} />
                        <div className="min-w-0 flex-1">
                          <p className="font-body-md text-body-md text-on-surface truncate">{incoming.name}</p>
                          <p className="font-label-md text-[10px] text-on-surface-variant">
                            {incoming.position}
                            {outgoing ? ` · for ${outgoing.name} (${outgoing.position})` : ' · No planned swap'}
                          </p>
                        </div>
                        {existing ? (
                          locked ? (
                            <span
                              className="w-8 h-8 rounded flex items-center justify-center text-on-surface-variant bg-surface-container border border-outline-variant"
                              title="Deadline passed"
                            >
                              <span className="material-symbols-outlined text-[16px]">lock</span>
                            </span>
                          ) : (
                            <button
                              aria-label={`Remove swap for ${incoming.name}`}
                              className="w-8 h-8 rounded flex items-center justify-center text-on-surface-variant bg-surface-container border border-outline-variant hover:bg-surface-container-high"
                              onClick={() => removeSwap(incoming.player_id)}
                              type="button"
                            >
                              <span className="material-symbols-outlined text-[16px]">close</span>
                            </button>
                          )
                        ) : (
                          !locked && (
                            <button
                              aria-label={`Choose outgoing player for ${incoming.name}`}
                              className="w-8 h-8 rounded flex items-center justify-center text-on-surface-variant bg-surface-container border border-outline-variant hover:bg-surface-container-high disabled:opacity-30 disabled:cursor-not-allowed"
                              disabled={(pickable.length === 0 && blocked.length === 0) || swaps.length >= 2}
                              onClick={() => setPlanningSwapInId(incoming.player_id)}
                              type="button"
                            >
                              <span className="material-symbols-outlined text-[16px]">add</span>
                            </button>
                          )
                        )}
                      </div>
                      {!locked && planningSwapInId === incoming.player_id && (
                        <div className="flex flex-col gap-2">
                          {pickable.length === 0 && blocked.length === 0 ? (
                            <span className="font-body-md text-body-md text-on-surface-variant">
                              No same-position non-Bonus starter available.
                            </span>
                          ) : (
                            <>
                              <div className="flex flex-wrap gap-2">
                                {pickable.map((candidate) => (
                                  <button
                                    className="px-3 py-1.5 rounded-lg border border-outline-variant bg-surface-container text-on-surface font-label-md text-label-md hover:bg-surface-container-high"
                                    key={candidate.player_id}
                                    onClick={() => addSwap(incoming.player_id, candidate.player_id)}
                                    type="button"
                                  >
                                    {candidate.name}
                                  </button>
                                ))}
                              </div>
                              {blocked.length > 0 && (
                                <ul className="flex flex-col gap-1">
                                  {blocked.map((candidate) => (
                                    <li
                                      className="flex flex-col gap-0.5 px-3 py-1.5 rounded-lg border border-outline-variant bg-surface-container-low text-on-surface-variant opacity-70"
                                      key={candidate.player_id}
                                    >
                                      <span className="font-label-md text-label-md">{candidate.name}</span>
                                      <span className="font-label-md text-[10px] text-error">{candidate.reason}</span>
                                    </li>
                                  ))}
                                </ul>
                              )}
                            </>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })
              )}
            </div>
          </section>

          <DragOverlay>
            {activeDragPlayer ? (
              <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-surface-container-lowest border border-primary-container shadow-lg font-body-md text-body-md text-on-surface">
                <PlayerJersey player={activeDragPlayer} size="xs" showName={false} />
                <span>{activeDragPlayer.name}</span>
              </div>
            ) : null}
          </DragOverlay>
        </DndContext>

        {selectionErrors.length > 0 && (
          <div className="mx-md mt-md bg-error-container text-on-error-container rounded-lg p-sm flex flex-col gap-xs" role="alert">
            {selectionErrors.map((msg, i) => (
              <p key={i}>{msg}</p>
            ))}
          </div>
        )}
        {submitError && (
          <div className="mx-md mt-md bg-error-container text-on-error-container rounded-lg p-sm flex flex-col gap-xs" role="alert">
            {submitError.map((msg, i) => (
              <p key={i}>{msg}</p>
            ))}
          </div>
        )}
        {submitSuccess && (
          <div className="mx-md mt-md bg-secondary-container text-on-secondary-container rounded-lg p-sm font-body-md text-body-md starting-xi-page__success">
            {submitSuccess}
          </div>
        )}
      </main>

      {/* Sits above Layout's BottomNav (which this page no longer renders
          itself -- it used to, and with Layout now providing one that meant
          two navs stacked on /squad). */}
      <div className="fixed bottom-[88px] left-1/2 -translate-x-1/2 w-full max-w-[600px] z-40 px-md pb-sm">
        {locked ? (
          <div
            className="w-full bg-surface-container-high text-on-surface-variant rounded-xl py-3.5 font-headline-sm text-headline-sm flex items-center justify-center gap-2 shadow-md"
            data-testid="xi-locked"
          >
            <span className="material-symbols-outlined">lock</span>
            Deadline passed — team locked
          </div>
        ) : (
          <button
            className="w-full bg-primary-container text-on-primary rounded-xl py-3.5 font-headline-sm text-headline-sm flex items-center justify-center gap-2 shadow-md active:scale-[0.98] transition-all disabled:opacity-50 disabled:active:scale-100 disabled:cursor-not-allowed"
            data-testid="save-team"
            disabled={selectionErrors.length > 0 || submitting}
            onClick={handleSave}
            type="button"
          >
            <span className="material-symbols-outlined">save</span>
            {submitting ? 'Saving…' : 'Save Team'}
          </button>
        )}
      </div>
    </>
  )
}

export default StartingXIPage
