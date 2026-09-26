import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import BottomNav from '../../components/BottomNav/BottomNav'
import FplHeader from '../../components/FplHeader/FplHeader'
import { fetchTeamDashboard } from '../../api/team'
import { useAuth } from '../../auth/AuthContext'
import { useSquadStatus } from '../../hooks/useSquadStatus'
import { useGameweek } from '../../config/gameweek'
import PlayerJersey from '../../components/PlayerJersey/PlayerJersey'
import { normalizePremierLeaguePlayer } from '../../data/premierLeague2026'
import TacticalPlayerSheet from '../../components/TacticalPlayerSheet/TacticalPlayerSheet'
import DashboardIcon from '../../components/DashboardIcon/DashboardIcon'

const ROWS = ['GK', 'DEF', 'MID', 'FWD']

const ROLE_LABELS = {
  starter: 'Starter',
  swapped_out: 'Banked · Swapped Out',
  swapped_in: 'Swapped In',
  auto_sub_cover: 'Auto Sub',
  auto_sub_replaced: 'Replaced',
  bench_unused: 'Bench',
}

// The dashboard's one main state pill, replacing the old three-pill cluster
// (Locked/Open + Scored/Not Scored + Provisional) with the single badge the
// four dashboard states actually call for: Open (state 2), In Process
// (state 3 -- locked but gw_scores/gameweeks.scored_at hasn't landed yet),
// Final (state 4 -- gameweeks.scored_at is set). State 1 (no squad) never
// reaches this component's data-driven branch at all.
function gameweekPhase(data) {
  if (!data.is_locked) return { key: 'open', label: 'Open', className: 'bg-secondary-container text-on-secondary-container' }
  if (data.scored) return { key: 'final', label: 'Final', className: 'bg-secondary text-on-secondary' }
  return { key: 'in_process', label: 'In Process', className: 'bg-amber-500 text-white' }
}

const TACTIC_LABELS = {
  attack: 'Attack',
  defence: 'Defence',
  balanced: 'Balanced',
}

function formatPoints(value) {
  return value == null ? '--' : value
}

function formatPlus(value) {
  return value == null ? '--' : `+${value}`
}

function formatCountdown(deadlineIso) {
  if (!deadlineIso) return null
  const diffMs = new Date(deadlineIso).getTime() - Date.now()
  if (diffMs <= 0) return 'Deadline passed'
  const totalMinutes = Math.floor(diffMs / 60000)
  const days = Math.floor(totalMinutes / (24 * 60))
  const hours = Math.floor((totalMinutes % (24 * 60)) / 60)
  const minutes = totalMinutes % 60
  return days > 0 ? `${days}d ${hours}h` : `${hours}h ${minutes}m`
}

function shortName(name) {
  return name.slice(0, 3).toUpperCase()
}

// The four fixed bench roles, in the exact order GET /team already returns
// them (Results/team_dashboard.py builds `bench` from `ORDER BY
// sx.position_slot`, i.e. 12, 13, 14, 15) -- so the array index alone tells
// us the static role, independent of what happened on matchday (that's
// `player.role`, a different vocabulary: starter/swapped_out/swapped_in/
// auto_sub_cover/auto_sub_replaced/bench_unused).
const BENCH_SLOT_LABELS = ['Backup GK', 'Auto Sub', 'Tactical Sub', 'Tactical Sub']

const PITCH_LINES_STYLE = {
  backgroundImage:
    'linear-gradient(rgba(255,255,255,0.12) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.12) 1px, transparent 1px)',
  backgroundSize: '20px 20px',
}

function BenchCard({ player, slotLabel, swap, outgoingName, onSelect }) {
  const muted = player.counted === false && player.role === 'bench_unused'
  return (
    <button
      className={`bg-surface-container-low rounded-xl p-2 flex flex-col items-center justify-between text-center border border-outline-variant/30 min-h-[140px] ${muted ? 'opacity-70' : ''}`}
      onClick={() => onSelect(player)}
      type="button"
    >
      <span className="bg-surface-container-highest text-on-surface text-[9px] font-semibold px-2 py-0.5 rounded-full mb-1 leading-tight uppercase whitespace-nowrap inline-flex items-center gap-1">
        {slotLabel.startsWith('Tactical') && (
          <DashboardIcon className="text-primary" name="bonusStar" size={10} />
        )}
        {slotLabel}
      </span>
      <PlayerJersey player={player} showName={false} size="sm" />
      <div className="flex flex-col items-center w-full mt-1">
        <span className="text-[11px] font-bold text-on-surface leading-tight truncate w-full">
          {player.name}
        </span>
        <span className="text-[9px] text-on-surface-variant font-medium leading-tight">
          {player.position} · {shortName(player.club ?? '')}
        </span>
      </div>
      <span className="text-[9px] font-label-md text-on-surface-variant mt-1">
        G {formatPoints(player.general_points)} · T {formatPoints(player.tactical_points)}
      </span>
      {swap && (
        <span className="text-[8px] font-semibold text-secondary leading-tight mt-1">
          {player.role === 'swapped_in'
            ? `✓ Swapped in${outgoingName ? ` for ${outgoingName}` : ''}`
            : `→ in for ${outgoingName ?? swap.player_out_id}`}
        </span>
      )}
      {swap && swap.sub_bonus > 0 && (
        <span className="text-[8px] font-bold text-white bg-secondary px-1.5 py-0.5 rounded-full mt-1 whitespace-nowrap">
          +{swap.sub_bonus} Sub Bonus
        </span>
      )}
    </button>
  )
}

function PlayerTile({ player, big = false, gameweekIsLive = false, onSelect }) {
  const roleLabel = ROLE_LABELS[player.role] ?? player.role ?? 'Player'
  const muted = player.counted === false
  const total = player.general_points == null && player.tactical_points == null
    ? null
    : (player.general_points ?? 0) + (player.tactical_points ?? 0)
  // Whose match has actually produced stats so far -- there's no literal
  // "kicked off" flag on PlayerLine, so this reads it off real numbers: any
  // General Points, or a non-empty rule breakdown, means the tactical engine
  // has already scored at least one stat for him this gameweek.
  const playerIsLive = gameweekIsLive && ((player.general_points ?? 0) > 0 || (player.general_breakdown?.length ?? 0) > 0)
  return (
    <button
      className={`flex flex-col items-center relative ${muted ? 'opacity-55 grayscale' : ''}`}
      onClick={() => onSelect(player)}
      type="button"
    >
      <div className="relative">
        <PlayerJersey
          player={player}
          size={big ? 'lg' : 'md'}
        />
        {player.is_bonus && (
          <span
            className="absolute -top-1.5 -right-2 bg-primary-fixed text-primary rounded-full h-5 px-1.5 flex items-center justify-center shadow-sm border border-primary-container/30 z-30"
            title="Tactical bonus player"
          >
            <DashboardIcon name="bonusStar" size={12} />
          </span>
        )}
        {playerIsLive && !muted && (
          <span className="absolute -bottom-0.5 -right-0.5 w-2.5 h-2.5 rounded-full bg-secondary border border-white z-30 animate-pulse" title="Match in progress" />
        )}
        {roleLabel && player.role !== 'starter' && (
          player.role === 'swapped_out' ? (
            <span
              aria-label="Banked, swapped out"
              className="absolute -top-2 -left-2 z-30 w-6 h-6 rounded-full bg-primary-fixed text-primary border border-primary-container/30 shadow-sm flex items-center justify-center"
              title="Banked · Swapped Out"
            >
              <DashboardIcon name="substitution" size={14} />
            </span>
          ) : (
            <div className="absolute -top-2 -left-2 bg-surface-container-highest text-on-surface-variant text-[8px] font-bold px-1 py-0.5 rounded-sm z-30 whitespace-nowrap max-w-[90px] leading-tight">
              {roleLabel}
            </div>
          )
        )}
      </div>

      <div className="bg-surface-container-lowest px-2 py-0.5 rounded shadow-sm flex flex-col items-center min-w-[60px] gap-xs mt-0.5">
        <span className="font-label-md text-label-md font-bold text-primary" data-testid="player-total-points">
          {total == null ? '--' : `${total >= 0 ? '+' : ''}${total}`}
        </span>
        <span className="font-label-md text-[9px] text-on-surface-variant">
          G {formatPoints(player.general_points)} · T {formatPoints(player.tactical_points)}
        </span>
      </div>
    </button>
  )
}

function normalizeDashboardPlayers(body) {
  const lineup = Object.fromEntries(
    ROWS.map((position) => [
      position,
      (body.lineup?.[position] ?? []).map(normalizePremierLeaguePlayer),
    ])
  )
  return {
    ...body,
    lineup,
    bench: (body.bench ?? []).map(normalizePremierLeaguePlayer),
  }
}

function StatTile({ label, value, icon, iconClass = 'text-on-surface-variant' }) {
  return (
    <div className="bg-white rounded-[20px] px-md py-sm shadow-sm border border-[#E5E6E1] flex justify-between items-center h-full gap-sm">
      <div className="flex flex-col min-w-0">
        <span className="font-label-md text-label-md text-on-surface-variant uppercase">{label}</span>
        <span className="font-headline-sm text-headline-sm text-on-surface truncate">{value}</span>
      </div>
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-[#E9E9D8]">
        <DashboardIcon className={iconClass} name={icon} size={19} />
      </span>
    </div>
  )
}

function DashboardPage() {
  const { user } = useAuth()
  const { hasSquad, loading: squadStatusLoading } = useSquadStatus()
  const { season, gameweek } = useGameweek()
  const navigate = useNavigate()

  // Defaults to the real current gameweek and never needs to guard against
  // gameweek being null on first render -- GameweekProvider (config/gameweek.jsx)
  // already blocks the whole authenticated app from rendering until it
  // resolves, so by the time this component exists, `gameweek` is a real
  // number. Stepping back only ever goes to a settled week: GET /team is
  // already fully (season, gameweek)-scoped for everything except team
  // value/bank (see the two sections below that special-case it), so no
  // backend change was needed to view history -- this is the one piece of
  // real state the feature adds.
  const [selectedGameweek, setSelectedGameweek] = useState(() => gameweek)

  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(null)
  const [showBreakdown, setShowBreakdown] = useState(false)
  const [selectedPlayer, setSelectedPlayer] = useState(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setLoadError(null)

    fetchTeamDashboard({ season, gameweek: selectedGameweek })
      .then((body) => {
        if (cancelled) return
        setData(normalizeDashboardPlayers(body))
      })
      .catch((err) => {
        if (!cancelled) setLoadError(err.message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [user.id, season, selectedGameweek])

  const pitchRows = useMemo(() => {
    if (!data) return []
    return ROWS.map((pos) => data.lineup[pos]).filter((row) => row.length > 0)
  }, [data])

  // Every named player this manager has, XI plus bench -- used to resolve a
  // Tactical Swap's `player_out_id` to a display name on the bench card that
  // replaced him.
  const allPlayers = useMemo(() => {
    if (!data) return []
    return [...Object.values(data.lineup).flat(), ...data.bench]
  }, [data])

  const countdown = data ? formatCountdown(data.deadline) : null
  const phase = data ? gameweekPhase(data) : null
  // "Still moving" -- ANY fixture in this gameweek unfinished. Drives the
  // LIVE tag by GW Points and the pulse dot on player shirts, both only
  // meaningful once the gameweek is locked and in progress (state 3).
  const isLive = data ? phase.key === 'in_process' && data.provisional : false
  const noSquadKnown = !squadStatusLoading && !hasSquad

  return (
    <div className="bg-[#FBF9F5] text-on-background font-body-md min-h-screen pb-24">
      <FplHeader helpTo="/scoring" showHelp title="Dashboard" />

      <main className="px-4 max-w-[600px] mx-auto w-full flex flex-col gap-4">
        {loading && <p className="font-body-md text-on-surface-variant mt-lg">Loading your team…</p>}
        {loadError && (
          <p className="font-body-md text-error mt-lg" role="alert">
            {loadError}
          </p>
        )}

        {data && (
          <>
            <section className="flex flex-col items-stretch text-center mt-3 w-full">
              {/* Steps 1..current -- never beyond, since there's no future
                  data to show. Past weeks are already blocked from editing
                  at the DB level (enforce_selection_lock/
                  enforce_transfers_immutability). */}
              <div className="grid grid-cols-[40px_1fr_40px] items-center gap-xs w-full bg-white border border-[#E5E6E1] rounded-full p-1 shadow-sm mb-1">
                <button
                  aria-label="Previous gameweek"
                  className="w-10 h-10 rounded-full flex items-center justify-center text-on-surface-variant hover:bg-[#F0F0EA] active:scale-90 transition-all disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-transparent"
                  data-testid="gw-picker-prev"
                  disabled={selectedGameweek <= 1}
                  onClick={() => setSelectedGameweek((gw) => gw - 1)}
                  type="button"
                >
                  <DashboardIcon name="chevronLeft" size={18} />
                </button>
                <div className="flex min-w-0 items-center justify-center gap-2">
                  <h2 className="font-headline-sm text-headline-sm text-on-surface whitespace-nowrap">
                    Gameweek {data.gameweek}
                  </h2>
                  <span
                    className={`text-[10px] font-bold px-2.5 py-1 rounded-full tracking-widest uppercase flex items-center gap-1 ${phase.className}`}
                    data-testid="phase-state"
                  >
                    {phase.key === 'in_process' && (
                      <DashboardIcon className="animate-spin" name="sync" size={12} />
                    )}
                    {phase.key === 'final' && (
                      <DashboardIcon name="check" size={12} strokeWidth={2.2} />
                    )}
                    {phase.label}
                  </span>
                </div>
                <button
                  aria-label="Next gameweek"
                  className="w-10 h-10 rounded-full flex items-center justify-center text-on-surface-variant hover:bg-[#F0F0EA] active:scale-90 transition-all disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-transparent"
                  data-testid="gw-picker-next"
                  disabled={selectedGameweek >= gameweek}
                  onClick={() => setSelectedGameweek((gw) => gw + 1)}
                  type="button"
                >
                  <DashboardIcon name="chevronRight" size={18} />
                </button>
              </div>
              {countdown && (
                <div className="self-center inline-flex items-center gap-2 bg-white border border-[#E5E6E1] px-3 py-1.5 rounded-full mt-2 shadow-sm">
                  <span className="h-2 w-2 rounded-full bg-secondary animate-pulse" />
                  <span className="font-label-md text-label-md font-bold text-on-surface">
                    {countdown === 'Deadline passed' ? countdown : `Deadline: ${countdown}`}
                  </span>
                </div>
              )}
              {!data.scored && data.scored_reason && (
                <p className="font-body-md text-body-md text-on-surface-variant mt-sm">
                  {data.scored_reason}
                </p>
              )}
            </section>

            <section className="grid grid-cols-2 gap-3">
              <button
                className="bg-white rounded-[24px] p-5 shadow-sm border border-[#E5E6E1] flex min-h-[188px] flex-col items-start justify-between text-left cursor-pointer hover:bg-[#F7F7F2] transition-colors"
                data-testid="gw-points-toggle"
                onClick={() => setShowBreakdown((v) => !v)}
                type="button"
              >
                <span className="flex w-full items-center justify-between gap-xs">
                  <span className="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider">
                    GW Points
                  </span>
                  {isLive && (
                    <span className="bg-[#E9E9D8] text-secondary text-[8px] font-bold px-2 py-1 rounded-full uppercase tracking-wide flex items-center gap-1">
                      <span className="w-1.5 h-1.5 rounded-full bg-secondary animate-pulse" />
                      Live
                    </span>
                  )}
                </span>
                <span className="font-display-lg text-[56px] leading-none tabular-nums text-on-surface" data-testid="gw-points">
                  {formatPoints(data.total ?? data.gw_points)}
                </span>
                <div className="flex items-center gap-xs rounded-full bg-[#E9E9D8] px-2.5 py-1 text-secondary">
                  <DashboardIcon name={showBreakdown ? 'expandUp' : 'expandDown'} size={16} />
                  <span className="font-label-md text-label-md">
                    {data.gw_average != null ? `Avg: ${data.gw_average}` : 'Avg: --'}
                  </span>
                </div>
                {showBreakdown && (
                  <div className="w-full mt-sm pt-sm border-t border-outline-variant flex flex-col gap-1 text-left" data-testid="gw-points-breakdown">
                    {data.scored ? (
                      <>
                        <div className="flex justify-between font-label-md text-[11px]">
                          <span className="text-on-surface-variant">General</span>
                          <span className="font-bold text-on-surface">{formatPoints(data.general_points)}</span>
                        </div>
                        <div className="flex justify-between font-label-md text-[11px]">
                          <span className="text-on-surface-variant">Tactical</span>
                          <span className="font-bold text-secondary">{formatPlus(data.tactical_points)}</span>
                        </div>
                        <div className="flex justify-between font-label-md text-[11px]">
                          <span className="text-on-surface-variant">Sub Bonus</span>
                          <span className="font-bold text-secondary">{formatPlus(data.sub_bonus)}</span>
                        </div>
                      </>
                    ) : (
                      <p className="font-label-md text-[11px] text-on-surface-variant">
                        {data.scored_reason ?? "Not scored under the current rules yet."}
                      </p>
                    )}
                  </div>
                )}
              </button>
              <div className="flex flex-col gap-3">
                <StatTile icon="trophy" iconClass="text-secondary" label="Season Total" value={data.season_total} />
                <StatTile
                  icon="chart"
                  label="Overall Rank"
                  value={
                    data.overall_rank != null
                      ? `${data.overall_rank} / ${data.overall_rank_total}`
                      : '--'
                  }
                />
              </div>
            </section>

            <section className={data.team_value_available
              ? 'bg-primary-container rounded-[20px] p-md flex justify-between items-center shadow-md'
              : 'bg-white rounded-[20px] px-md py-3 flex items-center gap-3 border border-[#E5E6E1] shadow-sm'}>
              {data.team_value_available ? (
                <>
                  <div>
                    <span className="font-label-md text-label-md text-primary-fixed-dim uppercase block mb-xs">
                      Team Value
                    </span>
                    <span className="font-headline-sm text-headline-sm text-on-primary">
                      £{data.team_value.toFixed(1)}m
                    </span>
                  </div>
                  <div className="w-px h-8 bg-surface-tint opacity-30" />
                  <div className="text-right">
                    <span className="font-label-md text-label-md text-primary-fixed-dim uppercase block mb-xs">
                      In the Bank
                    </span>
                    <span className="font-headline-sm text-headline-sm text-on-primary">
                      £{data.bank.toFixed(1)}m
                    </span>
                  </div>
                </>
              ) : (
                // False only for a settled gameweek with no
                // user_gameweek_finance row (Results/scoring.py writes one
                // alongside gw_scores at scoring time -- see that module's
                // UPSERT_GW_FINANCE_STMT). Genuinely missing, not something
                // to fall back to today's live figure for: that would
                // misrepresent today's number as this gameweek's history.
                <>
                  <span className="shrink-0 rounded-full bg-[#E9E9D8] px-2 py-1 font-label-md text-[9px] font-bold tracking-wider text-secondary">
                    NOTICE
                  </span>
                  <p
                    className="font-label-md text-label-md text-on-surface-variant text-left"
                    data-testid="team-value-unavailable"
                  >
                    Team value wasn't recorded for this gameweek.
                  </p>
                </>
              )}
            </section>

            {data.has_lineup ? (
              <>
                <section className="rounded-xl overflow-hidden shadow-sm border border-outline-variant relative flex flex-col">
                  <div
                    className="bg-secondary w-full aspect-[3/4] relative flex flex-col justify-between py-md"
                    style={PITCH_LINES_STYLE}
                  >
                    <div className="absolute inset-0 bg-gradient-to-b from-black/20 via-transparent to-black/30 pointer-events-none" />
                    {data.tactic && (
                      <span
                        className="absolute top-2 left-2 z-20 bg-surface-container-lowest/90 backdrop-blur-sm text-on-surface text-[10px] font-bold px-2 py-1 rounded-full shadow-sm flex items-center gap-1"
                        data-testid="pitch-tactic-pill"
                      >
                        <DashboardIcon name="balance" size={12} />
                        {(TACTIC_LABELS[data.tactic] ?? data.tactic).toUpperCase()}
                      </span>
                    )}
                    {pitchRows.map((row, i) => (
                      <div
                        className="flex justify-around w-full px-sm relative z-10"
                        key={ROWS[i] ?? i}
                      >
                        {row.map((p) => (
                          <PlayerTile
                            big={p.is_bonus}
                            key={p.player_id}
                            player={p}
                            gameweekIsLive={isLive}
                            onSelect={setSelectedPlayer}
                          />
                        ))}
                      </div>
                    ))}
                  </div>
                </section>

                <section className="bg-surface-container-lowest rounded-xl p-md shadow-sm border border-outline-variant flex flex-col gap-sm">
                  <div className="flex items-center gap-1">
                    <span className="font-body-md text-body-md font-bold text-on-surface">Bench</span>
                    <DashboardIcon className="text-on-surface-variant" name="bench" size={16} />
                  </div>
                  <div className="grid grid-cols-4 gap-2 pt-1">
                    {data.bench.map((p, i) => {
                      const swap = data.swaps?.find((s) => s.player_in_id === p.player_id)
                      const outgoing = swap
                        ? allPlayers.find((op) => op.player_id === swap.player_out_id)
                        : null
                      return (
                        <BenchCard
                          key={p.player_id}
                          outgoingName={outgoing?.name}
                          player={p}
                          slotLabel={BENCH_SLOT_LABELS[i] ?? 'Bench'}
                          swap={swap}
                          onSelect={setSelectedPlayer}
                        />
                      )
                    })}
                  </div>
                </section>
              </>
            ) : (
              <section className="rounded-xl overflow-hidden shadow-sm border border-outline-variant relative">
                <div
                  className="w-full aspect-[3/4] relative flex items-center justify-center p-md bg-secondary"
                  style={PITCH_LINES_STYLE}
                >
                  <div className="absolute inset-0 bg-gradient-to-b from-black/20 via-transparent to-black/30 pointer-events-none" />
                  <div className="relative z-10 w-full max-w-[280px] bg-surface-container-lowest/95 backdrop-blur-sm rounded-xl p-md border border-outline-variant shadow-md flex flex-col items-center text-center">
                    <div className="w-12 h-12 rounded-full bg-primary-container text-on-primary flex items-center justify-center mb-3 shadow-sm">
                      <DashboardIcon name="squad" size={24} />
                    </div>
                    <h3 className="font-headline-sm text-headline-sm text-on-surface mb-1">
                      {noSquadKnown ? 'Select your squad' : 'Set your Starting XI'}
                    </h3>
                    <p className="font-label-md text-label-md text-on-surface-variant mb-4">
                      {noSquadKnown
                        ? 'Choose your players to get started.'
                        : `Pick your XI for Gameweek ${data.gameweek} before the deadline.`}
                    </p>
                    <button
                      className="w-full bg-primary-container hover:opacity-95 text-on-primary font-semibold py-2 px-4 rounded-lg flex items-center justify-center gap-2 shadow-sm transition-colors"
                      onClick={() => navigate(noSquadKnown ? '/squad-selection' : '/squad')}
                      type="button"
                    >
                      <DashboardIcon name="add" size={18} />
                      <span>{noSquadKnown ? 'Select Squad' : 'Set Starting XI'}</span>
                    </button>
                  </div>
                </div>
              </section>
            )}

            {data.has_lineup && (
              <section className="bg-surface-container-lowest rounded-xl p-md shadow-sm border border-outline-variant">
                <div className="flex items-center justify-between mb-3 border-b border-outline-variant pb-2">
                  <h3 className="font-body-md text-body-md font-bold text-on-surface flex items-center gap-2">
                    <span className="w-7 h-7 rounded-lg bg-primary-fixed text-primary border border-primary-container/30 flex items-center justify-center">
                      <DashboardIcon name="substitution" size={16} />
                    </span>
                    Tactical Swaps
                  </h3>
                  <span className="font-label-md text-label-md text-on-surface-variant">
                    {data.swaps?.length ?? 0}/2
                  </span>
                </div>
                {data.swaps?.length ? (
                  <div className="flex flex-col gap-2">
                    {data.swaps.map((swap) => {
                      const outgoing = [...Object.values(data.lineup).flat(), ...data.bench]
                        .find((p) => p.player_id === swap.player_out_id)
                      const incoming = [...Object.values(data.lineup).flat(), ...data.bench]
                        .find((p) => p.player_id === swap.player_in_id)
                      return (
                        <div
                          className="flex items-center justify-between gap-sm bg-surface-container-low rounded-lg p-sm"
                          key={`${swap.player_out_id}-${swap.player_in_id}`}
                        >
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2 min-w-0">
                              <span className="font-body-md text-body-md text-on-surface truncate">
                                {outgoing?.name ?? swap.player_out_id}
                              </span>
                              <DashboardIcon className="text-primary shrink-0" name="substitution" size={18} />
                              <span className="font-body-md text-body-md text-on-surface truncate">
                                {incoming?.name ?? swap.player_in_id}
                              </span>
                            </div>
                            <p className="font-label-md text-[10px] text-on-surface-variant">
                              General {formatPoints(swap.general_out)} to {formatPoints(swap.general_in)}
                            </p>
                          </div>
                          <span className="font-label-md text-label-md text-secondary shrink-0">
                            {formatPlus(swap.sub_bonus)}
                          </span>
                        </div>
                      )
                    })}
                  </div>
                ) : (
                  <p className="font-body-md text-body-md text-on-surface-variant">
                    No tactical swaps were executed for this gameweek.
                  </p>
                )}
              </section>
            )}

            {/* Points breakdown. Every number comes from GET /team; live rows
                come from the tactical engine and committed rows from gw_scores. */}
            <section
              className="bg-surface-container-lowest rounded-xl p-md shadow-sm border border-outline-variant"
              data-testid="points-breakdown"
            >
              <h3 className="font-body-md text-body-md font-bold text-on-surface mb-3 border-b border-outline-variant pb-2">
                Points Breakdown
              </h3>
              {data.scored ? (
                <div className="flex flex-col gap-2">
                  <div className="flex justify-between items-center font-body-md text-body-md">
                    <span className="text-on-surface-variant">General Points</span>
                    <span className="font-bold text-on-surface" data-testid="general-points">
                      {formatPoints(data.general_points)}
                    </span>
                  </div>
                  <div className="flex justify-between items-center font-body-md text-body-md">
                    <span className="text-on-surface-variant">Tactical Points</span>
                    <span className="font-bold text-secondary" data-testid="tactical-points">
                      {formatPlus(data.tactical_points)}
                    </span>
                  </div>
                  <div className="flex justify-between items-center font-body-md text-body-md">
                    <span className="text-on-surface-variant">Sub Bonus</span>
                    <span className="font-bold text-secondary" data-testid="sub-bonus">
                      {formatPlus(data.sub_bonus)}
                    </span>
                  </div>
                  <div className="flex justify-between items-center font-body-lg text-body-lg font-bold border-t border-outline-variant pt-2 mt-1">
                    <span className="text-on-surface">Total</span>
                    <span
                      className="text-primary font-stats-number text-stats-number"
                      data-testid="final-total"
                    >
                      {formatPoints(data.total ?? data.final_total)}
                    </span>
                  </div>
                </div>
              ) : (
                <p className="font-body-md text-body-md text-on-surface-variant">
                  {data.scored_reason ?? "This gameweek isn't scored under the current rules."}
                </p>
              )}
            </section>

            {/* Squad, Transfers and Starting XI are all reachable from
                BottomNav now -- these quick-action rows (and the Fixtures
                list, which duplicated the Matches tab) were removed as
                redundant. Rules stay here: they're the one thing a manager
                with no squad yet still needs to read. */}
            <section className="mb-lg">
              <button
                className="w-full bg-surface-container-lowest hover:bg-surface-container-low transition-colors rounded-xl p-md border border-outline-variant flex items-center justify-between gap-sm shadow-sm"
                data-testid="how-points-work"
                onClick={() => navigate('/scoring')}
              >
                <div className="flex items-center gap-sm">
                  <div className="w-10 h-10 rounded-full bg-surface-container flex items-center justify-center text-primary">
                    <DashboardIcon name="help" size={20} />
                  </div>
                  <span className="font-body-md text-body-md font-semibold text-on-surface whitespace-nowrap">
                    How Points Work
                  </span>
                </div>
                <DashboardIcon className="text-outline" name="chevronRight" size={18} />
              </button>
            </section>
          </>
        )}
      </main>

      {selectedPlayer && (
        <TacticalPlayerSheet onClose={() => setSelectedPlayer(null)} player={selectedPlayer} />
      )}

      <BottomNav />
    </div>
  )
}

export default DashboardPage
