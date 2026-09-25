import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useOutletContext, useParams } from 'react-router-dom'
import PlayerCard from '../../components/PlayerCard/PlayerCard'
import PlayerJersey from '../../components/PlayerJersey/PlayerJersey'
import TeamBadge from '../../components/TeamBadge/TeamBadge'
import { fetchFixtures } from '../../api/fixtures'
import {
  deleteSavedTeam,
  editContestTeam,
  fetchContest,
  fetchContestPool,
  fetchContestTeam,
  fetchFixturePool,
  fetchSavedTeams,
  saveContestTeam,
  submitContestTeam,
  LockedError,
  NotFoundError,
  ValidationError,
} from '../../api/dream11'
import { MODE_CONTESTS, MODE_HOME } from '../../config/appMode'
import { isContestLocked } from '../../data/contestStatus'
import DetailHeader from '../../components/DetailHeader/DetailHeader'

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

const TEAM_NAME_MAX = 25
const TEAM_NAME_KEY = (contestId) => `pitchside.teamName.${contestId}`
const NAME_SUGGESTIONS = [
  'The Gunners XI', 'Midweek Maulers', 'Top Corner Titans', 'Clean Sheet Club',
  'Red Card Rejects', 'Tactical Nightmare', 'Set Piece Specialists', 'The Underdogs',
]

function suggestTeamName(current) {
  const pool = NAME_SUGGESTIONS.filter((name) => name !== current)
  return pool[Math.floor(Math.random() * pool.length)] ?? NAME_SUGGESTIONS[0]
}

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
/**
 * Tapping a pitch player opens this inline action popover instead of removing
 * them outright -- captain/vice used to live in two <select> dropdowns below
 * the pitch (mockup: "Match — Team Selection & Naming"), which meant setting
 * an armband and looking at the pitch were two different places on screen.
 */
function CaptainMenu({ player, isCaptain, isVice, onSetCaptain, onSetVice, onClose }) {
  return (
    <div
      className="absolute top-full mt-2 left-1/2 -translate-x-1/2 z-[100] w-40 overflow-hidden rounded-xl border border-[#2A312A] bg-[#161B16] shadow-[0_12px_32px_rgba(0,0,0,0.42)] py-1 flex flex-col"
      data-testid={`captain-menu-${player.id}`}
    >
      <button
        className="flex items-center justify-between gap-3 px-3 py-2.5 text-left font-label-md text-label-md font-semibold text-white hover:bg-[#202720] disabled:opacity-50"
        disabled={isCaptain}
        onClick={() => {
          onSetCaptain(player.id)
          onClose()
        }}
        type="button"
      >
        <span>Captain</span>
        <span className="rounded-full bg-[#9ACD32] px-2 py-0.5 text-[10px] font-bold text-[#1A2410]">2x</span>
      </button>
      <button
        className="flex items-center justify-between gap-3 px-3 py-2.5 text-left font-label-md text-label-md font-semibold text-[#D4D9D4] hover:bg-[#202720] disabled:opacity-50"
        disabled={isVice}
        onClick={() => {
          onSetVice(player.id)
          onClose()
        }}
        type="button"
      >
        <span>Vice-Captain</span>
        <span className="rounded-full border border-[#8DBF3A] bg-[#1E261C] px-2 py-0.5 text-[10px] font-bold text-[#8DBF3A]">1.5x</span>
      </button>
      <button
        className="flex items-center justify-between border-t border-[#2C322C] px-3 py-2.5 text-left font-label-md text-label-md font-semibold text-[#E5484D] hover:bg-[#202720] disabled:cursor-not-allowed disabled:opacity-35"
        disabled={!isCaptain && !isVice}
        onClick={() => {
          if (isCaptain) onSetCaptain(null)
          if (isVice) onSetVice(null)
          onClose()
        }}
        type="button"
      >
        <span>Remove role</span>
        <span aria-hidden="true" className="text-base leading-none">×</span>
      </button>
    </div>
  )
}

function PitchSlot({
  player, captainId, viceId, onSetCaptain, onSetVice, dense = false, menuOpen, onToggleMenu, onCloseMenu,
}) {
  if (!player) return null
  return (
    <div className={`relative flex flex-col items-center gap-1 min-w-0 ${menuOpen ? 'z-[100]' : 'z-10'}`}>
      <button
        className="flex flex-col items-center gap-1 min-w-0"
        onClick={() => onToggleMenu(player.id)}
        title={`${player.name} — tap to set captain, vice-captain, or remove`}
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
      {menuOpen && (
        <CaptainMenu
          isCaptain={player.id === captainId}
          isVice={player.id === viceId}
          onClose={onCloseMenu}
          onSetCaptain={onSetCaptain}
          onSetVice={onSetVice}
          player={player}
        />
      )}
    </div>
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

function formationOf(players) {
  const counts = Object.fromEntries(ROWS.map((p) => [p, 0]))
  for (const p of players) if (counts[p.position] != null) counts[p.position] += 1
  return ROWS.map((p) => counts[p]).join('-')
}

/**
 * One "how many teams have I made" card -- a saved lineup for this fixture,
 * reusable across any contest on it (Game_logic/dream11.py's whole player
 * pool is drawn from a fixture's two clubs, so a saved team only makes
 * sense for another contest on this SAME match). Tapping the card loads it
 * into the picker below; the × deletes it outright, independent of loading.
 */
function SavedTeamCard({ team, onLoad, onDelete }) {
  return (
    <div className="relative shrink-0 w-32">
      <button
        className="w-full h-full bg-surface-container-lowest rounded-lg border border-outline-variant p-sm text-left hover:border-secondary transition-colors"
        data-testid="saved-team-card"
        onClick={onLoad}
        type="button"
      >
        <p className="font-label-md text-label-md text-on-surface font-bold truncate pr-4">
          {team.name}
        </p>
        <p className="font-label-md text-label-md text-on-surface-variant mt-1">
          {formationOf(team.players)}
        </p>
      </button>
      <button
        aria-label={`Delete saved team ${team.name}`}
        className="absolute top-1 right-1 w-5 h-5 rounded-full bg-surface-container-high text-on-surface-variant hover:bg-error-container hover:text-on-error-container flex items-center justify-center"
        onClick={onDelete}
        type="button"
      >
        <span className="material-symbols-outlined text-[12px]">close</span>
      </button>
    </div>
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
 *
 * mode="build" (routed at /matches/:fixtureId/build) is a THIRD, contest-
 * less use of this same picker: build an 11-player lineup for a fixture and
 * save it as a reusable team (saveContestTeam), with no contest created at
 * all. It loads its pool from fetchFixturePool (fixtureId route param, live
 * preview prices) instead of fetchContest/fetchContestPool (contestId,
 * frozen prices), synthesizes a minimal `contest`-shaped object so the rest
 * of this component's render doesn't need its own branch for every field,
 * and hides the Create/Save Team submit bar entirely -- Save as reusable
 * team (already contest-independent) is the only action available.
 */
function PickTeamPage({ mode = 'create' }) {
  const isEdit = mode === 'edit'
  const isBuild = mode === 'build'
  const { contestId, fixtureId } = useParams()
  const { settings } = useOutletContext()
  const { user_id, season } = settings
  const navigate = useNavigate()
  // Local-storage keys (team name draft) need SOME id to key off -- build
  // mode has no contestId, so fixtureId stands in for it there.
  const storageKey = contestId ?? `fixture-${fixtureId}`

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
  const [menuPlayerId, setMenuPlayerId] = useState(null)
  // Client-side only: the backend team record has no name field, so this
  // rides along in localStorage per contest rather than in the submit body.
  const [teamName, setTeamName] = useState('')
  const [editingName, setEditingName] = useState(false)
  const [savedTeams, setSavedTeams] = useState([])
  const [savingTemplate, setSavingTemplate] = useState(false)
  const [saveFormOpen, setSaveFormOpen] = useState(false)
  // True right after a successful save, until the lineup changes again --
  // disables Save as reusable team so the exact same 11+captain/vice can't
  // be saved twice in a row by accident, without blocking a genuine re-save
  // once you've actually changed something.
  const [justSaved, setJustSaved] = useState(false)
  const [saveTemplateName, setSaveTemplateName] = useState('')

  function handleBack() {
    if (window.history.state?.idx > 0) navigate(-1)
    else navigate(MODE_HOME[MODE_CONTESTS])
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setServerErrors([])

    // build mode has no contest at all -- load the fixture's live-priced
    // pool instead, and synthesize a minimal `contest`-shaped object (rules
    // as constants, matching Game_logic/dream11.py's own DREAM11_* defaults)
    // so the rest of this component doesn't need a second render branch for
    // every field that normally comes from a real contest summary.
    if (isBuild) {
      Promise.all([
        fetchFixtures({ season }),
        fetchFixturePool({ fixture_id: fixtureId }),
      ])
        .then(([allFixtures, poolBody]) => {
          if (cancelled) return
          const fixture = allFixtures.find((f) => f.fixture_id === Number(fixtureId))
          setContest({
            fixture_id: Number(fixtureId),
            name: fixture ? `${fixture.home_team} vs ${fixture.away_team}` : 'Build a Team',
            home_team: fixture?.home_team,
            away_team: fixture?.away_team,
            kickoff_time: fixture?.kickoff_time,
            code: null,
            is_locked: false,
            budget_cap: 100,
            team_size: 11,
            max_players_per_club: 7,
            captain_multiplier: 2,
            vice_captain_multiplier: 1.5,
          })
          setLocked(false)
          setPool(
            poolBody.map((p) => ({ ...p, id: p.player_id, price: p.credit_price, points: p.rolling_points }))
          )
        })
        .catch((err) => {
          if (!cancelled) setServerErrors([err.message || 'Could not load this match'])
        })
        .finally(() => {
          if (!cancelled) setLoading(false)
        })
      return () => {
        cancelled = true
      }
    }

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
        // From kickoff, not only once the lock sweep sets is_locked -- the
        // backend refuses team changes from kickoff (data/contestStatus.js).
        setLocked(isContestLocked(contestBody))
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
  }, [contestId, fixtureId, isBuild, isEdit, season, user_id])

  // Left open across kickoff, the picker locks itself at kickoff rather
  // than letting the user build a team the server will refuse.
  useEffect(() => {
    if (locked || !contest?.kickoff_time) return undefined
    const msUntilKickoff = new Date(contest.kickoff_time).getTime() - Date.now()
    // setTimeout overflows past ~24.8 days; the page will be reloaded long before.
    if (msUntilKickoff > 2 ** 31 - 1) return undefined
    const timer = setTimeout(() => setLocked(true), Math.max(0, msUntilKickoff))
    return () => clearTimeout(timer)
  }, [contest, locked])

  useEffect(() => {
    let stored = ''
    try {
      stored = localStorage.getItem(TEAM_NAME_KEY(storageKey)) ?? ''
    } catch {
      // Non-fatal: the field just starts blank for this session.
    }
    setTeamName(stored || suggestTeamName())
  }, [storageKey])

  useEffect(() => {
    // Available in EDIT mode too, not just create: a saved template is
    // fixture-scoped, so swapping an already-submitted team for one you'd
    // saved earlier is just as valid a use as starting a brand new contest
    // with it. Loading one here only changes local picker state -- nothing
    // is written back to the real contest until Save Changes is pressed.
    if (!contest?.fixture_id) return undefined
    let cancelled = false
    fetchSavedTeams({ fixture_id: contest.fixture_id })
      .then((teams) => {
        if (!cancelled) setSavedTeams(teams)
      })
      .catch(() => {
        // Non-fatal: the picker still works from scratch if this fails.
      })
    return () => {
      cancelled = true
    }
  }, [isEdit, contest?.fixture_id])

  function loadSavedTeam(team) {
    setSelectedIds(team.players.map((p) => p.player_id).filter((id) => byId.has(id)))
    setCaptainId(team.players.find((p) => p.is_captain)?.player_id ?? null)
    setViceId(team.players.find((p) => p.is_vice_captain)?.player_id ?? null)
    setMenuPlayerId(null)
    // Carries the saved team's own name into the entry name you'd submit --
    // matches Create League's own saved-team submission using the same name
    // (Dream11ContestsPage.jsx's handleCreate).
    updateTeamName(team.name)
  }

  async function handleDeleteSavedTeam(event, savedTeamId) {
    event.stopPropagation()
    if (!window.confirm('Delete this saved team? This cannot be undone.')) return
    try {
      await deleteSavedTeam({ saved_team_id: savedTeamId })
      setSavedTeams((teams) => teams.filter((t) => t.saved_team_id !== savedTeamId))
    } catch (err) {
      setServerErrors([err.message || 'Could not delete this saved team'])
    }
  }

  function openSaveTemplateForm() {
    if (selectedIds.length !== teamSize) return
    // Mirrors save_team's own server-side check (Game_logic/dream11.py --
    // it prices the pool live and runs the same _validate_team budget rule
    // submit_team uses), so an over-budget lineup gets rejected here
    // instantly rather than only after a round-trip. The server remains
    // the real authority; this is just the same rule shown earlier.
    if (cost > budgetCap) {
      setServerErrors([`Over budget by ${(cost - budgetCap).toFixed(1)} credits -- cannot save until you're within budget.`])
      return
    }
    setServerErrors([])
    setSaveTemplateName(teamName || 'My Team')
    setSaveFormOpen(true)
  }

  // Inline form, not window.prompt() -- a native prompt() can be silently
  // blocked (embedded webviews, popup-blocking, repeated-dialog abuse
  // prevention) and returns null with no error at all when it is, which
  // reads as "I tapped the button and nothing happened" -- exactly the bug
  // report this replaced. An inline input can't be blocked the same way,
  // and always gives visible feedback either way.
  async function handleSaveTemplate() {
    if (savingTemplate || !saveTemplateName.trim()) return

    setSavingTemplate(true)
    setServerErrors([])
    try {
      const saved = await saveContestTeam({
        fixture_id: contest.fixture_id,
        name: saveTemplateName.trim(),
        player_ids: selectedIds,
        captain_id: captainId,
        vice_captain_id: viceId,
      })
      setSavedTeams((teams) => [saved, ...teams])
      setSaveFormOpen(false)
      setJustSaved(true)
    } catch (err) {
      setServerErrors(err instanceof ValidationError ? err.errors : [err.message || 'Could not save this team'])
    } finally {
      setSavingTemplate(false)
    }
  }

  function updateTeamName(next) {
    const trimmed = next.slice(0, TEAM_NAME_MAX)
    setTeamName(trimmed)
    try {
      localStorage.setItem(TEAM_NAME_KEY(storageKey), trimmed)
    } catch {
      // Non-fatal: the name just won't survive a reload this session.
    }
  }

  const byId = useMemo(() => new Map(pool.map((p) => [p.id, p])), [pool])
  const selected = useMemo(
    () => selectedIds.map((id) => byId.get(id)).filter(Boolean),
    [selectedIds, byId]
  )

  // Any change to WHO's on the pitch or who holds the armbands un-disables
  // Save as reusable team -- justSaved only ever means "this exact lineup,
  // right now, is already saved", not "saving is done for this session".
  useEffect(() => {
    setJustSaved(false)
  }, [selectedIds, captainId, viceId])

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
        // Was local-only (localStorage) until dream11.teams.entry_name
        // existed to hold it -- now it actually reaches the leaderboard.
        team_name: teamName || undefined,
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
        <p className="font-body-md text-body-md text-on-surface-variant">
          {isBuild ? 'Loading match…' : 'Loading contest…'}
        </p>
      </main>
    )
  }
  if (!contest) {
    return (
      <main className="w-full px-safe-margin py-md">
        <p className="font-body-md text-body-md text-on-surface-variant">
          {isBuild ? 'Match not found.' : 'Contest not found.'}
        </p>
      </main>
    )
  }

  const remaining = countdown(contest.kickoff_time)
  const canAddMore = selectedIds.length < teamSize

  return (
    <main className={`w-full px-4 py-3 flex flex-col gap-4 bg-[#FBF9F5] min-h-screen font-['Helvetica_Neue',Helvetica,Arial,sans-serif] ${isBuild ? 'pb-md' : 'pb-[180px]'}`}>
      <div>
        <DetailHeader
          onBack={handleBack}
          title={isBuild ? 'Build a Team' : isEdit ? 'Edit Team' : 'Pick Team'}
        />
        <div className="flex items-center justify-between gap-3">
          <h2 className="font-display-lg text-[24px] font-bold text-on-surface">{contest.name}</h2>
          <span className="rounded-md bg-[#F5F3F0] px-2.5 py-1 font-label-md text-xs font-semibold text-on-surface">
            {contest.home_team} vs {contest.away_team}
          </span>
        </div>
        <div className="flex items-center gap-xs">
          <TeamBadge shortName={contest.home_team} size="sm" />
          <p className="font-body-md text-body-md text-on-surface-variant">
            {contest.home_team} vs {contest.away_team}
            {!isBuild && ' · Single Match Contest'}
          </p>
          <TeamBadge shortName={contest.away_team} size="sm" />
        </div>
        {/* No lock/countdown or invite code in build mode -- neither means
            anything without a real contest behind it. */}
        {!isBuild && remaining && (
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
        {isBuild && (
          <p className="font-label-md text-label-md text-on-surface-variant mt-1">
            Build a lineup and save it for later — no contest is created here. Create one from the
            Leagues page when you're ready to play it.
          </p>
        )}
      </div>

      {locked && (
        <div className="bg-error-container text-on-error-container rounded-lg p-sm" role="alert">
          <p className="font-label-md text-label-md">
            The match has kicked off — teams are locked and can no longer be changed.
          </p>
        </div>
      )}

      <div className="bg-white rounded-[20px] p-4 border border-[#E5E6E1] shadow-[0_2px_8px_rgba(0,0,0,0.04)]">
        <div className="mb-2 flex items-center justify-between">
          <p className="font-label-md text-[10px] font-bold text-on-surface-variant uppercase tracking-wider">Team Name</p>
          <span className="font-label-md text-[10px] text-on-surface-variant">{teamName.length}/{TEAM_NAME_MAX}</span>
        </div>
        <div className="flex items-center gap-sm">
          {editingName ? (
            <input
              autoFocus
              className="flex-1 min-w-0 rounded-lg border border-outline-variant bg-surface px-3 h-10 font-body-md text-body-md text-on-surface"
              data-testid="team-name-input"
              maxLength={TEAM_NAME_MAX}
              onBlur={() => setEditingName(false)}
              onChange={(event) => updateTeamName(event.target.value)}
              onKeyDown={(event) => event.key === 'Enter' && setEditingName(false)}
              value={teamName}
            />
          ) : (
            <p className="flex-1 min-w-0 font-headline-sm text-headline-sm text-on-surface truncate">
              {teamName}
            </p>
          )}
          <button
            aria-label="Edit team name"
            className="w-9 h-9 shrink-0 rounded-full flex items-center justify-center text-on-surface-variant hover:bg-surface-container-high transition-colors"
            onClick={() => setEditingName((v) => !v)}
            type="button"
          >
            <span className="material-symbols-outlined text-[18px]">edit</span>
          </button>
        </div>
        <div className="flex items-center justify-between mt-2">
          <button
            className="rounded-full bg-[#8DAA3C]/15 px-3 py-1.5 font-label-md text-label-md font-bold text-[#4D631B] flex items-center gap-1"
            data-testid="suggest-team-name"
            onClick={() => updateTeamName(suggestTeamName(teamName))}
            type="button"
          >
            ⚡ Suggest Name
          </button>
        </div>
      </div>

      {/* Shown in both create and edit mode -- see the effect above. */}
      {savedTeams.length > 0 && (
        <div>
          <p className="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider mb-2">
            Your saved teams for this match ({savedTeams.length})
          </p>
          <div className="flex gap-sm overflow-x-auto pb-1">
            {savedTeams.map((team) => (
              <SavedTeamCard
                key={team.saved_team_id}
                onDelete={(event) => handleDeleteSavedTeam(event, team.saved_team_id)}
                onLoad={() => loadSavedTeam(team)}
                team={team}
              />
            ))}
          </div>
        </div>
      )}

      <div className="grid grid-cols-2 gap-3">
        <div className="bg-white rounded-[20px] p-4 border border-[#E5E6E1] text-left shadow-[0_2px_8px_rgba(0,0,0,0.04)]">
          <p className="font-label-md text-[10px] font-bold text-on-surface-variant uppercase tracking-wider">
            Credits remaining
          </p>
          <p
            className={`mt-2 font-stats-number text-[28px] leading-none tabular-nums ${cost > budgetCap ? 'text-error' : 'text-on-surface'}`}
            data-testid="credits-remaining"
          >
            {(budgetCap - cost).toFixed(1)}
          </p>
        </div>
        <div className="bg-white rounded-[20px] p-4 border border-[#E5E6E1] text-left shadow-[0_2px_8px_rgba(0,0,0,0.04)]">
          <p className="font-label-md text-[10px] font-bold text-on-surface-variant uppercase tracking-wider">
            Players
          </p>
          <p className="mt-2 font-stats-number text-[28px] leading-none tabular-nums text-[#8DAA3C]" data-testid="player-count">
            {selectedIds.length}/{teamSize}
          </p>
        </div>
      </div>

      {/* A full DEF or MID row is 5 across at 390-420px, so rows get their own
          vertical space and the jerseys sit tight rather than wrapping into
          each other's name tags. */}
      <div
        className="relative overflow-visible bg-gradient-to-b from-[#086834] to-[#0F7B42] rounded-[24px] p-3 flex flex-col justify-evenly gap-md min-h-[420px] shadow-sm"
        data-testid="pitch"
      >
        <div aria-hidden="true" className="pointer-events-none absolute inset-3 rounded-[18px] border border-white/40">
          <span className="absolute left-0 right-0 top-1/2 border-t border-white/40" />
          <span className="absolute left-1/2 top-1/2 h-20 w-20 -translate-x-1/2 -translate-y-1/2 rounded-full border border-white/40" />
          <span className="absolute left-1/2 top-0 h-14 w-32 -translate-x-1/2 border-x border-b border-white/40" />
          <span className="absolute bottom-0 left-1/2 h-14 w-32 -translate-x-1/2 border-x border-t border-white/40" />
        </div>
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
              className="relative z-10 focus-within:z-[90] grid items-start justify-items-center gap-1.5"
              key={position}
              style={{ gridTemplateColumns: `repeat(${Math.max(rowCount, 1)}, 1fr)` }}
            >
              {inRow.map((player) => (
                <PitchSlot
                  captainId={captainId}
                  dense={dense}
                  key={player.id}
                  menuOpen={menuPlayerId === player.id}
                  onCloseMenu={() => setMenuPlayerId(null)}
                  onSetCaptain={(id) => {
                    setCaptainId(id)
                    if (viceId === id) setViceId(null)
                  }}
                  onSetVice={(id) => {
                    setViceId(id)
                    if (captainId === id) setCaptainId(null)
                  }}
                  onToggleMenu={(id) => setMenuPlayerId((cur) => (cur === id ? null : id))}
                  player={player}
                  viceId={viceId}
                />
              ))}
              {showAdd && <AddSlot dense={dense} onClick={setFilter} position={position} />}
            </div>
          )
        })}
      </div>

      <p className="rounded-[20px] border border-[#E5E6E1] bg-white p-3 font-label-md text-label-md leading-relaxed text-on-surface-variant flex items-start gap-2 shadow-sm">
        <span className="material-symbols-outlined text-[16px]">info</span>
        No auto-subs. Selected players must play to score. Tap a player on the pitch to set
        captain ({contest.captain_multiplier}x) or vice-captain ({contest.vice_captain_multiplier}x).
      </p>

      {/* Independent of submitting to THIS contest -- a template is fixture-
          scoped, not contest-scoped, so it's just as useful whether you're
          building fresh or editing an already-submitted team. */}
      {saveFormOpen ? (
        <div className="bg-surface-container-lowest rounded-xl p-md border border-outline-variant flex flex-col gap-sm">
          <label className="font-label-md text-label-md text-on-surface-variant" htmlFor="save-template-name">
            Save this lineup as
          </label>
          <div className="flex items-center gap-sm">
            <input
              autoFocus
              className="flex-1 min-w-0 rounded-lg border border-outline-variant bg-surface px-3 h-10 font-body-md text-body-md text-on-surface"
              data-testid="save-template-name-input"
              id="save-template-name"
              maxLength={TEAM_NAME_MAX}
              onChange={(event) => setSaveTemplateName(event.target.value)}
              onKeyDown={(event) => event.key === 'Enter' && handleSaveTemplate()}
              value={saveTemplateName}
            />
            <button
              className="h-10 px-4 rounded-lg bg-secondary text-on-secondary font-label-md text-label-md shrink-0 disabled:opacity-40"
              data-testid="save-template-confirm"
              disabled={savingTemplate || !saveTemplateName.trim()}
              onClick={handleSaveTemplate}
              type="button"
            >
              {savingTemplate ? 'Saving…' : 'Save'}
            </button>
            <button
              aria-label="Cancel"
              className="w-10 h-10 shrink-0 rounded-full flex items-center justify-center text-on-surface-variant hover:bg-surface-container-high transition-colors"
              onClick={() => setSaveFormOpen(false)}
              type="button"
            >
              <span className="material-symbols-outlined text-[18px]">close</span>
            </button>
          </div>
          {/* build mode has no bottom submit bar to show serverErrors in --
              create/edit also see this here now, on top of the bottom bar's
              own copy, which is harmless duplication. */}
          {serverErrors.length > 0 && (
            <p className="font-label-md text-label-md text-error" data-testid="save-template-error">
              {serverErrors[0]}
            </p>
          )}
        </div>
      ) : isBuild ? (
        // The only action on this screen -- styled as the primary CTA
        // rather than the secondary text-link create/edit use, where it
        // sits alongside a real Create/Save Team submit bar.
        <button
          className="w-full bg-primary text-on-primary rounded-lg py-3 font-label-md text-label-md disabled:opacity-40 flex items-center justify-center gap-1.5"
          data-testid="save-as-team"
          disabled={selectedIds.length !== teamSize || justSaved || cost > budgetCap}
          onClick={openSaveTemplateForm}
          type="button"
        >
          <span className="material-symbols-outlined text-[18px]">
            {justSaved ? 'check_circle' : 'bookmark_add'}
          </span>
          {justSaved ? 'Saved' : 'Save as reusable team'}
        </button>
      ) : (
        <button
          className="self-start font-label-md text-label-md text-secondary flex items-center gap-1 disabled:opacity-40"
          data-testid="save-as-team"
          disabled={selectedIds.length !== teamSize || justSaved || cost > budgetCap}
          onClick={openSaveTemplateForm}
          type="button"
        >
          <span className="material-symbols-outlined text-[16px]">
            {justSaved ? 'check_circle' : 'bookmark_add'}
          </span>
          {justSaved ? 'Saved' : 'Save as reusable team'}
        </button>
      )}

      <div className="flex gap-2 overflow-x-auto pb-1">
        {['ALL', ...ROWS, ...clubs].map((value) => (
          <button
            className={`px-3 py-1.5 rounded-full font-label-md text-label-md font-bold whitespace-nowrap shrink-0 border transition-colors ${
              filter === value
                ? 'bg-[#1B1C1A] text-white border-[#1B1C1A]'
                : 'bg-white text-on-surface-variant border-[#E5E6E1]'
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

      {/* No Create/Save Team action in build mode -- there is no contest to
          submit to. Save as reusable team (above) is the only save action,
          and isn't gated on budget/formation validity the way a real
          submission is (Game_logic/dream11.py's saved teams skip the
          budget check entirely), so it doesn't belong behind this bar. */}
      {!isBuild && (
        <div className="fixed bottom-[72px] left-1/2 -translate-x-1/2 w-full max-w-[600px] z-40 px-4 py-3 bg-[#FBF9F5]/95 backdrop-blur-lg border-t border-[#E5E6E1]">
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
            className="w-full bg-[#8DAA3C] text-white rounded-[20px] py-4 font-label-md text-base font-bold shadow-md disabled:opacity-40 flex items-center justify-center gap-2 active:scale-[0.99] transition-all"
            data-testid="submit-team"
            disabled={!isValid || submitting || locked}
            onClick={handleSubmit}
            type="button"
          >
            {submitting ? (
              isEdit ? 'Saving…' : 'Creating…'
            ) : (
              <>
                <span className="material-symbols-outlined text-[18px]">check_circle</span>
                {isEdit ? 'Save Team' : 'Create Team'}
              </>
            )}
          </button>
        </div>
      )}
    </main>
  )
}

export default PickTeamPage
