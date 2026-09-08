import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
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
import { fetchChipsUsed } from '../../api/chips'
import PlayerJersey from '../../components/PlayerJersey/PlayerJersey'
import { normalizePremierLeaguePlayer } from '../../data/premierLeague2026'

const POSITION_ORDER = ['GK', 'DEF', 'MID', 'FWD']
const STARTING_XI_SIZE = 11
const FORMATION_OPTIONS = [
  { label: '3-4-3', counts: { DEF: 3, MID: 4, FWD: 3 } },
  { label: '3-5-2', counts: { DEF: 3, MID: 5, FWD: 2 } },
  { label: '4-3-3', counts: { DEF: 4, MID: 3, FWD: 3 } },
  { label: '4-4-2', counts: { DEF: 4, MID: 4, FWD: 2 } },
  { label: '4-5-1', counts: { DEF: 4, MID: 5, FWD: 1 } },
  { label: '5-2-3', counts: { DEF: 5, MID: 2, FWD: 3 } },
  { label: '5-3-2', counts: { DEF: 5, MID: 3, FWD: 2 } },
  { label: '5-4-1', counts: { DEF: 5, MID: 4, FWD: 1 } },
]

const CHIPS = [
  { type: 'wildcard', label: 'Wildcard' },
  { type: 'triple_captain', label: 'Triple Captain' },
  { type: 'bench_boost', label: 'Bench Boost' },
  { type: 'free_hit', label: 'Free Hit' },
]

function chipAvailable(chipType, chipsUsed) {
  if (!chipsUsed) return false
  if (chipType === 'wildcard') return chipsUsed.wildcard_remaining > 0
  return chipsUsed[`${chipType}_available`] === true
}

function computeFormationErrors(startingIds, squadById, captainId, viceCaptainId) {
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
  if (counts.DEF < 3 || counts.DEF > 5) errors.push(`Defenders must be between 3 and 5 -- currently ${counts.DEF}`)
  if (counts.MID < 2 || counts.MID > 5) errors.push(`Midfielders must be between 2 and 5 -- currently ${counts.MID}`)
  if (counts.FWD < 1 || counts.FWD > 3) errors.push(`Forwards must be between 1 and 3 -- currently ${counts.FWD}`)
  if (!captainId) errors.push('Select a captain')
  if (!viceCaptainId) errors.push('Select a vice-captain')

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

function SortablePitchPlayer({ player, isCaptain, isVice, onOpenPopover, onBench }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: player.player_id })
  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.4 : 1,
  }

  return (
    <div
      className="pitch-player relative w-[68px] flex flex-col items-center group cursor-pointer"
      onClick={() => onOpenPopover(player.player_id)}
      ref={setNodeRef}
      style={style}
    >
      {/* Drag handle stays a separate hit area: the card itself opens the
          captain sheet on tap, so the whole card can't be the handle. */}
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

      <PlayerJersey
        captain={isCaptain}
        player={player}
        size={isCaptain ? 'lg' : 'md'}
        viceCaptain={isVice}
      />
    </div>
  )
}

function SortableBenchPlayer({ player, subLabel, canMoveUp, canMoveDown, canPromote, onMoveUp, onMoveDown, onPromote }) {
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
      <span className="font-label-md text-[9px] text-on-surface-variant w-[42px] shrink-0">
        {subLabel}
      </span>
      <PlayerJersey player={player} size="xs" showName={false} />
      <span className="font-body-md text-body-md text-on-surface truncate flex-1 min-w-0">
        {player.name}
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
      </div>
    </div>
  )
}

function StartingXIPage() {
  const { settings } = useOutletContext()
  const { user_id, season, gameweek } = settings

  const [squad, setSquad] = useState(null)
  const [chipsUsed, setChipsUsed] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(null)

  const [startingIds, setStartingIds] = useState([])
  const [benchIds, setBenchIds] = useState([])
  const [captainId, setCaptainId] = useState(null)
  const [viceCaptainId, setViceCaptainId] = useState(null)
  const [chipUsed, setChipUsed] = useState(null)

  const [captainPopoverPlayerId, setCaptainPopoverPlayerId] = useState(null)
  const [chipConfirmTarget, setChipConfirmTarget] = useState(null)
  const [activeDragId, setActiveDragId] = useState(null)

  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState(null)
  // Set once the backend rejects a write because the deadline has passed.
  // Sticky for the rest of the visit: the deadline doesn't un-pass.
  const [locked, setLocked] = useState(false)
  const [submitSuccess, setSubmitSuccess] = useState(null)

  const sensors = useSensors(
    // distance threshold means a plain tap still registers as a click
    // (opens the captain popover / fires button onClick) instead of
    // always starting a drag -- only a deliberate press-and-move drags.
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates })
  )

  const lastOverIdRef = useRef(null)

  // Order within the starting XI is meaningless (only membership + captain
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
        const benchItemContainers = args.droppableContainers.filter((c) => benchIds.includes(c.id))
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
    [benchIds]
  )

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setLoadError(null)
    Promise.all([
      fetchCurrentSquad({ season, gameweek }),
      fetchCurrentSelection({ season, gameweek }),
      fetchChipsUsed({ season, gameweek }),
    ])
      .then(([squadData, selectionData, chipsData]) => {
        if (cancelled) return
        const normalizedSquadData = {
          ...squadData,
          players: (squadData.players ?? []).map(normalizePremierLeaguePlayer),
        }
        setSquad(normalizedSquadData)
        setChipsUsed(chipsData)
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
          // Captaincy dies with the player it was assigned to.
          setCaptainId(squadIds.has(selectionData.captain_id) ? selectionData.captain_id : null)
          setViceCaptainId(
            squadIds.has(selectionData.vice_captain_id) ? selectionData.vice_captain_id : null
          )
          setChipUsed(selectionData.chip_used)
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

  const benchPlayers = useMemo(() => benchIds.map((id) => squadById.get(id)).filter(Boolean), [benchIds, squadById])

  const formationErrors = useMemo(
    () => computeFormationErrors(startingIds, squadById, captainId, viceCaptainId),
    [startingIds, squadById, captainId, viceCaptainId]
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
    if (!nextStartingSet.has(captainId)) setCaptainId(null)
    if (!nextStartingSet.has(viceCaptainId)) setViceCaptainId(null)
  }

  function toggleStarting(player) {
    const isStarting = startingIds.includes(player.player_id)
    if (isStarting) {
      setStartingIds((prev) => prev.filter((id) => id !== player.player_id))
      if (captainId === player.player_id) setCaptainId(null)
      if (viceCaptainId === player.player_id) setViceCaptainId(null)
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

  function moveBench(index, direction) {
    setBenchIds((prev) => {
      const next = [...prev]
      const swapWith = index + direction
      if (swapWith < 0 || swapWith >= next.length) return prev
      ;[next[index], next[swapWith]] = [next[swapWith], next[index]]
      return next
    })
  }

  function openCaptainPopover(playerId) {
    if (!startingIds.includes(playerId)) return
    setCaptainPopoverPlayerId(playerId)
  }

  function makeCaptain(playerId) {
    setCaptainId(playerId)
    if (viceCaptainId === playerId) setViceCaptainId(null)
    setCaptainPopoverPlayerId(null)
  }

  function makeViceCaptain(playerId) {
    setViceCaptainId(playerId)
    if (captainId === playerId) setCaptainId(null)
    setCaptainPopoverPlayerId(null)
  }

  function handleChipTap(chipType) {
    if (chipUsed === chipType) {
      setChipUsed(null) // deselecting a pending (not-yet-saved) chip needs no confirmation
      return
    }
    if (!chipAvailable(chipType, chipsUsed)) return
    setChipConfirmTarget(chipType)
  }

  function confirmChipActivation() {
    setChipUsed(chipConfirmTarget)
    setChipConfirmTarget(null)
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
      if (captainId === active.id) setCaptainId(null)
      if (viceCaptainId === active.id) setViceCaptainId(null)
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
      setBenchIds((prev) => {
        const oldIndex = prev.indexOf(active.id)
        const newIndex = prev.indexOf(over.id)
        if (oldIndex === -1 || newIndex === -1) return prev
        return arrayMove(prev, oldIndex, newIndex)
      })
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
        bench_order: benchIds,
        captain_id: captainId,
        vice_captain_id: viceCaptainId,
        chip_used: chipUsed,
      })
      setSubmitSuccess('Team saved.')
      const refreshedChips = await fetchChipsUsed({ season, gameweek })
      setChipsUsed(refreshedChips)
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

  const captainPopoverPlayer = captainPopoverPlayerId != null ? squadById.get(captainPopoverPlayerId) : null
  const chipConfirmLabel = CHIPS.find((c) => c.type === chipConfirmTarget)?.label
  const activeDragPlayer = activeDragId != null ? squadById.get(activeDragId) : null

  return (
    <>
      <main className="w-full flex flex-col pb-[180px]">
        <div className="flex overflow-x-auto gap-2 px-md py-sm border-b border-surface-container w-full [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          {CHIPS.map((chip) => {
            const active = chipUsed === chip.type
            const available = chipAvailable(chip.type, chipsUsed)
            return (
              <button
                className={`shrink-0 px-4 py-1.5 rounded-full font-label-md text-label-md border transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
                  active
                    ? 'bg-primary-container text-on-primary border-primary-container shadow-sm'
                    : 'border-outline-variant text-on-surface-variant hover:bg-surface-container-high'
                }`}
                disabled={!available && !active}
                key={chip.type}
                onClick={() => handleChipTap(chip.type)}
                type="button"
              >
                {chip.label}
              </button>
            )
          })}
        </div>

        <div className="flex justify-between items-center gap-sm mt-md mb-sm px-md">
          <label className="flex items-center gap-2">
            <span className="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider">
              Formation
            </span>
            <select
              aria-label="Formation"
              className="rounded-lg border border-outline-variant bg-surface-container-lowest px-2 h-9 font-label-md text-label-md text-on-surface focus:border-secondary focus:ring-1 focus:ring-secondary outline-none"
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
          <div
            className="bg-surface-container-lowest px-3 py-1.5 rounded-full flex items-center gap-2 shadow-sm border border-outline-variant"
            data-testid="formation-pill"
          >
            <span className="font-label-md text-label-md text-on-surface">{formationLabel}</span>
            {formationErrors.length === 0 ? (
              <>
                <span className="material-symbols-outlined text-secondary text-[16px]">
                  check_circle
                </span>
                <span className="font-label-md text-[10px] text-on-surface-variant whitespace-nowrap">
                  Valid
                </span>
              </>
            ) : (
              <span className="font-label-md text-[10px] text-error whitespace-nowrap">
                {formationErrors.length} issue{formationErrors.length === 1 ? '' : 's'}
              </span>
            )}
          </div>
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
                      isCaptain={captainId === p.player_id}
                      isVice={viceCaptainId === p.player_id}
                      onOpenPopover={openCaptainPopover}
                      onBench={toggleStarting}
                    />
                  ))}
                </div>
              ))}
            </DroppableContainer>
          </SortableContext>

          <section className="px-md mt-md">
            <h3 className="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider mb-sm">
              Bench
            </h3>
            <SortableContext items={benchIds} strategy={verticalListSortingStrategy}>
              <DroppableContainer
                className="bg-surface-container-low rounded-xl p-2 border border-outline-variant shadow-sm flex flex-col gap-2 min-h-[64px]"
                id="bench-container"
              >
                {benchPlayers.map((p, i) => (
                  <SortableBenchPlayer
                    key={p.player_id}
                    player={p}
                    subLabel={`${['1st', '2nd', '3rd', '4th'][i] ?? `${i + 1}th`} Sub`}
                    canMoveUp={i > 0}
                    canMoveDown={i < benchPlayers.length - 1}
                    canPromote={p.position === 'GK' || startingIds.length < STARTING_XI_SIZE}
                    onMoveUp={() => moveBench(i, -1)}
                    onMoveDown={() => moveBench(i, 1)}
                    onPromote={() => toggleStarting(p)}
                  />
                ))}
              </DroppableContainer>
            </SortableContext>
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

        {formationErrors.length > 0 && (
          <div className="mx-md mt-md bg-error-container text-on-error-container rounded-lg p-sm flex flex-col gap-xs" role="alert">
            {formationErrors.map((msg, i) => (
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
            disabled={formationErrors.length > 0 || submitting}
            onClick={handleSave}
            type="button"
          >
            <span className="material-symbols-outlined">save</span>
            {submitting ? 'Saving…' : 'Save Team'}
          </button>
        )}
      </div>

      {captainPopoverPlayer && (
        <div className="fixed inset-0 z-[60] bg-black/40 flex items-end justify-center p-md" onClick={() => setCaptainPopoverPlayerId(null)}>
          <div className="w-full max-w-[600px] bg-surface-container-lowest rounded-xl p-md shadow-lg flex flex-col gap-sm" onClick={(e) => e.stopPropagation()}>
            <h2 className="font-headline-sm text-headline-sm text-primary">{captainPopoverPlayer.name}</h2>
            <button className="flex items-center gap-sm p-sm rounded-lg border border-outline-variant hover:bg-surface-container-low transition-colors font-body-md text-body-md text-on-surface text-left" onClick={() => makeCaptain(captainPopoverPlayer.player_id)}>
              <span className="w-6 h-6 rounded-full bg-primary-container text-on-primary flex items-center justify-center font-stats-number text-[11px]">C</span>
              Make Captain
            </button>
            <button className="flex items-center gap-sm p-sm rounded-lg border border-outline-variant hover:bg-surface-container-low transition-colors font-body-md text-body-md text-on-surface text-left" onClick={() => makeViceCaptain(captainPopoverPlayer.player_id)}>
              <span className="w-6 h-6 rounded-full bg-surface-variant text-on-surface-variant flex items-center justify-center font-stats-number text-[11px]">V</span>
              Make Vice-Captain
            </button>
            <button className="px-4 py-2 rounded-lg border border-outline-variant font-label-md text-label-md text-on-surface hover:bg-surface-container-low transition-colors" onClick={() => setCaptainPopoverPlayerId(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {chipConfirmTarget && (
        <div className="fixed inset-0 z-[60] bg-black/40 flex items-end justify-center p-md" onClick={() => setChipConfirmTarget(null)}>
          <div className="w-full max-w-[600px] bg-surface-container-lowest rounded-xl p-md shadow-lg flex flex-col gap-sm" onClick={(e) => e.stopPropagation()}>
            <h2 className="font-headline-sm text-headline-sm text-primary">Activate {chipConfirmLabel} for this gameweek?</h2>
            <p className="font-body-md text-body-md text-on-surface-variant">This can't be undone this season once you save your team.</p>
            <div className="flex gap-sm justify-end mt-sm">
              <button className="px-4 py-2 rounded-lg border border-outline-variant font-label-md text-label-md text-on-surface hover:bg-surface-container-low transition-colors" onClick={() => setChipConfirmTarget(null)}>
                Cancel
              </button>
              <button className="px-4 py-2 rounded-lg bg-primary-container text-on-primary font-label-md text-label-md shadow-sm active:scale-95 transition-all" onClick={confirmChipActivation}>
                Activate
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

export default StartingXIPage
