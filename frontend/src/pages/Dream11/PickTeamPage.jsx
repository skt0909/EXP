import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useOutletContext, useParams } from 'react-router-dom'
import PlayerCard from '../../components/PlayerCard/PlayerCard'
import PlayerJersey from '../../components/PlayerJersey/PlayerJersey'
import TeamBadge from '../../components/TeamBadge/TeamBadge'
import {
  editContestTeam,
  fetchContest,
  fetchContestPool,
  fetchContestTeam,
  submitContestTeam,
  LockedError,
  NotFoundError,
  ValidationError,
} from '../../api/dream11'
import { MODE_CONTESTS, MODE_HOME } from '../../config/appMode'

// Mirrors Game_logic/dream11.py's _validate_team. Duplicated deliberately: the
// server stays the authority and re-checks everything, but a builder that only
// learns its team is illegal after a round-trip is unusable. Any change here
// has to track that function.
const LIMITS = {
  GK: { min: 1, max: 1 },
  DEF: { min: 3, max: 5 },
  MID: { min: 3, max: 5 },
  FWD: { min: 1, max: 3 },
}
const ROWS = ['GK', 'DEF', 'MID', 'FWD']

const asCredits = (price) => price?.toFixed(1)

function countdown(kickoffTime) {
  if (!kickoffTime) return null
  const ms = new Date(kickoffTime).getTime() - Date.now()
  if (Number.isNaN(ms) || ms <= 0) return null
  const totalMinutes = Math.floor(ms / 60000)
  const days = Math.floor(totalMinutes / 1440)
  const hours = Math.floor((totalMinutes % 1440) / 60)
  const minutes = totalMinutes % 60
  if (days > 0) return `${days}d ${hours}h ${minutes}m`
  if (hours > 0) return `${hours}h ${minutes}m`
  return `${minutes}m`
}

function validate(selected, captainId, viceId, budgetCap, teamSize, maxPerClub) {
  const errors = []
  const counts = Object.fromEntries(ROWS.map((p) => [p, selected.filter((s) => s.position === p).length]))

  // Both clubs field 30-odd players, so without this cap a team could be one
  // side's entire XI.
  const clubCounts = {}
  for (const player of selected) clubCounts[player.club] = (clubCounts[player.club] ?? 0) + 1

  if (selected.length !== teamSize) errors.push(`Pick exactly ${teamSize} players (${selected.length}/${teamSize})`)
  for (const position of ROWS) {
    const { min, max } = LIMITS[position]
    if (counts[position] < min || counts[position] > max) {
      errors.push(
        min === max
          ? `Need exactly ${min} ${position}`
          : `${position} must be between ${min} and ${max} (have ${counts[position]})`
      )
    }
  }
  for (const [club, count] of Object.entries(clubCounts)) {
    if (count > maxPerClub) errors.push(`At most ${maxPerClub} players from ${club} (have ${count})`)
  }
  const cost = selected.reduce((sum, p) => sum + p.credit_price, 0)
  if (cost > budgetCap) errors.push(`Over budget by ${(cost - budgetCap).toFixed(1)} credits`)
  if (!captainId) errors.push('Pick a captain')
  if (!viceId) errors.push('Pick a vice-captain')
  if (captainId && captainId === viceId) errors.push('Captain and vice-captain must differ')

  return { errors, counts, cost, clubCounts }
}

// dense = this row has 5 total slots (players + the Add button, when still
// showing one) -- DEF/MID's legal maximum. Scales the jersey/label down a
// notch, matching PitchLineup's read-only equivalent, since the row's grid
// (below) gives 5 slots a narrower 1/5th-width column than 3-4 get.
function PitchSlot({ player, captainId, viceId, onRemove, dense = false }) {
  if (!player) return null
  return (
    <button
      className="flex flex-col items-center gap-1 min-w-0"
      onClick={() => onRemove(player.id)}
      title={`Remove ${player.name}`}
      type="button"
    >
      <PlayerJersey
        captain={player.id === captainId}
        player={player}
        showName={false}
        size={dense ? 'xs' : 'sm'}
        viceCaptain={player.id === viceId}
      />
      <span className="bg-surface-container-lowest rounded px-1.5 py-0.5 shadow-sm">
        <span
          className="font-label-md text-label-md text-on-surface whitespace-nowrap"
          style={dense ? { fontSize: '10px' } : undefined}
        >
          {player.name.length > 9 ? `${player.name.slice(0, 9)}…` : player.name}
        </span>
      </span>
    </button>
  )
}

function AddSlot({ position, onClick, dense = false }) {
  return (
    <button
      aria-label={`Browse ${position} players`}
      className={`rounded-full border-2 border-dashed border-on-secondary/60 flex items-center justify-center bg-surface/10 ${
        dense ? 'w-9 h-9' : 'w-12 h-12'
      }`}
      onClick={() => onClick(position)}
      type="button"
    >
      <span className="material-symbols-outlined text-on-secondary">add</span>
    </button>
  )
}

/**
 * Dream11 team builder: 11 players from one fixture, 100 credits, C and VC.
 *
 * Rows run GK at the top down to FWD, matching the contest pitch rather than
 * PitchView's squad-building layout (which is fixed at a 15-man 2/5/5/3 shape
 * and doesn't fit a variable Dream11 formation).
 *
 * mode="edit" (routed at /dream11/contests/:contestId/edit, see App.jsx)
 * reuses this exact picker for an already-submitted team: the only
 * differences are (a) the initial selection is pre-populated from
 * fetchContestTeam instead of starting empty, and (b) submit calls
 * editContestTeam (PATCH, full-lineup replace) instead of submitContestTeam
 * (POST, create-only) -- both send the same {player_ids, captain_id,
 * vice_captain_id} shape, so nothing about the pitch/validation/budget UI
 * below needs to know which mode it's in.
 */
function PickTeamPage({ mode = 'create' }) {
  const isEdit = mode === 'edit'
  const { contestId } = useParams()
  const { settings } = useOutletContext()
  const { user_id } = settings
  const navigate = useNavigate()

  const [contest, setContest] = useState(null)
  const [pool, setPool] = useState([])
  const [selectedIds, setSelectedIds] = useState([])
  const [captainId, setCaptainId] = useState(null)
  const [viceId, setViceId] = useState(null)
  const [filter, setFilter] = useState('ALL')
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [serverErrors, setServerErrors] = useState([])
  const [locked, setLocked] = useState(false)

  function handleBack() {
    if (window.history.state?.idx > 0) navigate(-1)
    else navigate(MODE_HOME[MODE_CONTESTS])
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setServerErrors([])

    Promise.all([
      fetchContest({ contest_id: contestId, user_id }),
      fetchContestPool({ contest_id: contestId }),
      // Only edit mode needs the existing team, to pre-populate the picker
      // below instead of starting from an empty selection.
      isEdit ? fetchContestTeam({ contest_id: contestId, user_id }) : Promise.resolve(null),
    ])
      .then(([contestBody, poolBody, teamBody]) => {
        if (cancelled) return
        setContest(contestBody)
        setLocked(contestBody.is_locked)
        // PlayerCard/PlayerJersey key off `id`; the API speaks player_id.
        // points: PlayerCard's playerPoints() reads player.points, same prop
        // FPL's squad-selection screen already relies on -- rolling_points is
        // null for a player with no prior gameweek history, which playerPoints
        // reads as 0 (a real "no data" case, not a real zero score, but the
        // same reading FPL's own COALESCE-to-0 points column already uses).
        setPool(
          poolBody.map((p) => ({ ...p, id: p.player_id, price: p.credit_price, points: p.rolling_points }))
        )
        if (teamBody) {
          setSelectedIds(teamBody.players.map((p) => p.player_id))
          setCaptainId(teamBody.players.find((p) => p.is_captain)?.player_id ?? null)
          setViceId(teamBody.players.find((p) => p.is_vice_captain)?.player_id ?? null)
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setServerErrors([
            err instanceof NotFoundError
              ? 'No existing team to edit -- go back and submit one first.'
              : err.message || 'Could not load this contest',
          ])
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [contestId, isEdit, user_id])

  const byId = useMemo(() => new Map(pool.map((p) => [p.id, p])), [pool])
  const selected = useMemo(
    () => selectedIds.map((id) => byId.get(id)).filter(Boolean),
    [selectedIds, byId]
  )

  const budgetCap = contest?.budget_cap ?? 100
  const teamSize = contest?.team_size ?? 11
  // Sourced from the contest rather than hardcoded, like every other rule the
  // summary ships -- so the cap can't drift from Game_logic/dream11.py's.
  const maxPerClub = contest?.max_players_per_club ?? 7
  const { errors, counts, cost, clubCounts } = validate(
    selected, captainId, viceId, budgetCap, teamSize, maxPerClub
  )
  const isValid = errors.length === 0

  const clubs = useMemo(() => [...new Set(pool.map((p) => p.club))], [pool])

  const visible = useMemo(() => {
    if (filter === 'ALL') return pool
    if (ROWS.includes(filter)) return pool.filter((p) => p.position === filter)
    return pool.filter((p) => p.club === filter)
  }, [pool, filter])

  function toggle(id) {
    setSelectedIds((ids) => {
      if (ids.includes(id)) {
        // Dropping a player must drop any armband they were carrying, or the
        // request would name a captain who isn't in the team.
        if (captainId === id) setCaptainId(null)
        if (viceId === id) setViceId(null)
        return ids.filter((x) => x !== id)
      }
      const player = byId.get(id)
      if (!player) return ids
      const positionCount = ids.filter((x) => byId.get(x)?.position === player.position).length
      const clubCount = ids.filter((x) => byId.get(x)?.club === player.club).length
      if (
        ids.length >= teamSize ||
        positionCount >= LIMITS[player.position].max ||
        clubCount >= maxPerClub
      ) {
        return ids
      }
      return [...ids, id]
    })
  }

  async function handleSubmit() {
    setSubmitting(true)
    setServerErrors([])
    try {
      const submitFn = isEdit ? editContestTeam : submitContestTeam
      await submitFn({
        contest_id: contestId,
        user_id,
        player_ids: selectedIds,
        captain_id: captainId,
        vice_captain_id: viceId,
      })
      navigate(`/dream11/contests/${contestId}`)
    } catch (err) {
      if (err instanceof LockedError) {
        setLocked(true)
        setServerErrors(err.errors)
      } else if (err instanceof ValidationError) {
        setServerErrors(err.errors)
      } else {
        setServerErrors([err.message || 'Could not submit your team'])
      }
    } finally {
      setSubmitting(false)
    }
  }

  if (loading) {
    return (
      <main className="w-full px-safe-margin py-md">
        <p className="font-body-md text-body-md text-on-surface-variant">Loading contest…</p>
      </main>
    )
  }
  if (!contest) {
    return (
      <main className="w-full px-safe-margin py-md">
        <p className="font-body-md text-body-md text-on-surface-variant">Contest not found.</p>
      </main>
    )
  }

  const remaining = countdown(contest.kickoff_time)
  const canAddMore = selectedIds.length < teamSize

  return (
    <main className="w-full px-safe-margin py-md flex flex-col gap-md pb-[180px]">
      <div>
        {/* Back-arrow + static h1, same convention as the "How Points Work"
            screens -- a drill-down from a contest, not a BottomNav
            destination. The h1 must name the PAGE, never the contest --
            contest.name moved to the subtitle below. */}
        <div className="flex items-center gap-sm -ml-1 mb-1">
          <button
            aria-label="Go back"
            className="w-9 h-9 rounded-full flex items-center justify-center text-on-surface hover:bg-surface-container-high transition-colors"
            onClick={handleBack}
            type="button"
          >
            <span className="material-symbols-outlined">arrow_back</span>
          </button>
          <h1 className="font-headline-sm text-headline-sm text-on-surface">
            {isEdit ? 'Edit Team' : 'Pick Team'}
          </h1>
        </div>
        <h2 className="font-display-lg text-display-lg text-primary">{contest.name}</h2>
        <div className="flex items-center gap-xs">
          <TeamBadge shortName={contest.home_team} size="sm" />
          <p className="font-body-md text-body-md text-on-surface-variant">
            {contest.home_team} vs {contest.away_team} · Single Match Contest
          </p>
          <TeamBadge shortName={contest.away_team} size="sm" />
        </div>
        {remaining && (
          <p className="font-label-md text-label-md text-on-surface-variant mt-1">
            Locks in {remaining}
          </p>
        )}
        {/* Creating a contest now drops you straight in here, so this is where
            the invite code has to be findable -- it's the only thing that lets
            anyone else join. */}
        {contest.code && (
          <p className="font-label-md text-label-md text-on-surface-variant mt-1">
            Invite code{' '}
            <span className="font-bold text-primary tracking-wider" data-testid="invite-code">
              {contest.code}
            </span>
          </p>
        )}
      </div>

      {locked && (
        <div className="bg-error-container text-on-error-container rounded-lg p-sm" role="alert">
          <p className="font-label-md text-label-md">
            This contest has locked — teams can no longer be submitted.
          </p>
        </div>
      )}

      <div className="flex gap-md">
        <div className="flex-1 bg-surface-container-lowest rounded-xl p-md border border-outline-variant text-center">
          <p className="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider">
            Credits remaining
          </p>
          <p
            className={`font-stats-number text-stats-number ${cost > budgetCap ? 'text-error' : 'text-primary'}`}
            data-testid="credits-remaining"
          >
            {(budgetCap - cost).toFixed(1)}
          </p>
        </div>
        <div className="flex-1 bg-surface-container-lowest rounded-xl p-md border border-outline-variant text-center">
          <p className="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider">
            Players
          </p>
          <p className="font-stats-number text-stats-number text-primary" data-testid="player-count">
            {selectedIds.length}/{teamSize}
          </p>
        </div>
      </div>

      {/* A full DEF or MID row is 5 across at 390-420px, so rows get their own
          vertical space and the jerseys sit tight rather than wrapping into
          each other's name tags. */}
      <div
        className="bg-secondary rounded-xl p-sm flex flex-col justify-evenly gap-md min-h-[360px] bg-[repeating-linear-gradient(0deg,transparent,transparent_12%,rgba(255,255,255,0.08)_12%,rgba(255,255,255,0.08)_24%)]"
        data-testid="pitch"
      >
        {ROWS.map((position) => {
          const inRow = selected.filter((p) => p.position === position)
          const showAdd = canAddMore && inRow.length < LIMITS[position].max
          // The Add button occupies a slot in the row too -- a row showing
          // 4 players + Add is visually just as tight as 5 real players
          // (DEF/MID's legal max), so both get the same dense treatment.
          const rowCount = inRow.length + (showAdd ? 1 : 0)
          const dense = rowCount >= 5
          return (
            <div
              className="grid items-start justify-items-center gap-1.5"
              key={position}
              style={{ gridTemplateColumns: `repeat(${Math.max(rowCount, 1)}, 1fr)` }}
            >
              {inRow.map((player) => (
                <PitchSlot
                  captainId={captainId}
                  dense={dense}
                  key={player.id}
                  onRemove={toggle}
                  player={player}
                  viceId={viceId}
                />
              ))}
              {showAdd && <AddSlot dense={dense} onClick={setFilter} position={position} />}
            </div>
          )
        })}
      </div>

      <p className="font-label-md text-label-md text-on-surface-variant flex items-center gap-1">
        <span className="material-symbols-outlined text-[16px]">info</span>
        No auto-subs. Selected players must play to score.
      </p>

      {selected.length > 0 && (
        <div className="grid grid-cols-2 gap-sm">
          <label className="flex flex-col gap-1">
            <span className="font-label-md text-label-md text-on-surface-variant">
              Captain ({contest.captain_multiplier}x)
            </span>
            <select
              className="rounded-lg border border-outline-variant bg-surface px-3 h-10 font-body-md text-body-md text-on-surface"
              data-testid="captain-select"
              onChange={(event) => setCaptainId(Number(event.target.value) || null)}
              value={captainId ?? ''}
            >
              <option value="">Select…</option>
              {selected.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="font-label-md text-label-md text-on-surface-variant">
              Vice ({contest.vice_captain_multiplier}x)
            </span>
            <select
              className="rounded-lg border border-outline-variant bg-surface px-3 h-10 font-body-md text-body-md text-on-surface"
              data-testid="vice-select"
              onChange={(event) => setViceId(Number(event.target.value) || null)}
              value={viceId ?? ''}
            >
              <option value="">Select…</option>
              {selected.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}

      <div className="flex gap-2 overflow-x-auto pb-1">
        {['ALL', ...ROWS, ...clubs].map((value) => (
          <button
            className={`px-3 py-1 rounded-full font-label-md text-label-md whitespace-nowrap shrink-0 border ${
              filter === value
                ? 'bg-primary text-on-primary border-primary'
                : 'bg-surface-container-lowest text-on-surface-variant border-outline-variant'
            }`}
            data-testid={`filter-${value}`}
            key={value}
            onClick={() => setFilter(value)}
            type="button"
          >
            {value === 'ALL' ? 'All' : value}
            {ROWS.includes(value) && ` ${counts[value]}`}
            {/* Club chips carry a count too, so "7/7" explains why that side's
                add buttons have gone dead rather than looking broken. */}
            {clubs.includes(value) && ` ${clubCounts[value] ?? 0}/${maxPerClub}`}
          </button>
        ))}
      </div>

      <div className="flex flex-col gap-sm">
        {visible.map((player) => (
          <PlayerCard
            addDisabled={
              !canAddMore ||
              counts[player.position] >= LIMITS[player.position].max ||
              (clubCounts[player.club] ?? 0) >= maxPerClub ||
              locked
            }
            formatPrice={asCredits}
            key={player.id}
            onToggle={toggle}
            player={player}
            selected={selectedIds.includes(player.id)}
            showPoints
          />
        ))}
      </div>

      <div className="fixed bottom-[72px] left-1/2 -translate-x-1/2 w-full max-w-[600px] z-40 px-md py-sm bg-surface/95 backdrop-blur-lg border-t border-outline-variant">
        {(errors.length > 0 || serverErrors.length > 0) && (
          <p
            className="font-label-md text-label-md text-error mb-sm truncate"
            data-testid="validation-message"
            title={[...serverErrors, ...errors].join(' · ')}
          >
            {serverErrors[0] ?? errors[0]}
          </p>
        )}
        <button
          className="w-full bg-primary text-on-primary rounded-lg py-3 font-label-md text-label-md disabled:opacity-40"
          data-testid="submit-team"
          disabled={!isValid || submitting || locked}
          onClick={handleSubmit}
          type="button"
        >
          {submitting
            ? isEdit
              ? 'Saving…'
              : 'Submitting…'
            : `${isEdit ? 'Save Changes' : 'Submit Team'} (${cost.toFixed(1)} credits)`}
        </button>
      </div>
    </main>
  )
}

export default PickTeamPage
