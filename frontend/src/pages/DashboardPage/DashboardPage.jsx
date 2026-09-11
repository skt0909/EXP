import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import BottomNav from '../../components/BottomNav/BottomNav'
import FplHeader from '../../components/FplHeader/FplHeader'
import { fetchFixtures } from '../../api/fixtures'
import { fetchTeamDashboard } from '../../api/team'
import { useAuth } from '../../auth/AuthContext'
import { useSquadStatus } from '../../hooks/useSquadStatus'
import { useGameweek } from '../../config/gameweek'
import PlayerJersey from '../../components/PlayerJersey/PlayerJersey'
import TeamBadge from '../../components/TeamBadge/TeamBadge'
import { normalizePremierLeaguePlayer } from '../../data/premierLeague2026'

const ROWS = ['GK', 'DEF', 'MID', 'FWD']

const LIVE_BADGE = {
  live: { label: 'Live', className: 'bg-secondary text-on-secondary' },
  final: { label: 'Final', className: 'bg-surface-container-highest text-on-surface-variant' },
  upcoming: { label: 'Upcoming', className: 'bg-surface-container-high text-on-surface-variant' },
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

function PlayerChip({ player, multiplier, big = false }) {
  return (
    <div
      className={`flex flex-col items-center relative ${player.is_autosubbed_out ? 'opacity-50 grayscale' : ''}`}
    >
      <div className="relative">
        <PlayerJersey
          captain={player.is_captain}
          player={player}
          size={big ? 'lg' : 'md'}
          viceCaptain={player.is_vice_captain}
        />
        {player.is_autosubbed_out && (
          <div className="absolute -top-2 -right-2 bg-error text-white text-[8px] font-bold px-1 py-0.5 rounded-sm flex items-center z-30 whitespace-nowrap">
            <span className="material-symbols-outlined text-[10px]">arrow_downward</span> Sub
          </div>
        )}
        {player.is_autosubbed_in && (
          <div className="absolute -top-2 -right-2 bg-secondary text-white text-[8px] font-bold px-1 py-0.5 rounded-sm flex items-center z-30 whitespace-nowrap">
            <span className="material-symbols-outlined text-[10px]">arrow_upward</span> Sub In
          </div>
        )}
      </div>

      <div className="bg-surface-container-lowest px-2 py-0.5 rounded shadow-sm flex flex-col items-center min-w-[60px] gap-xs">
        <span className="font-label-md text-label-md text-secondary">
          {player.points}
          {player.is_captain && multiplier > 1 && (
            <span className="text-[10px] font-normal text-on-surface-variant ml-0.5">
              ×{multiplier}
            </span>
          )}
        </span>
      </div>
    </div>
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

function StatTile({ label, value, icon, iconClass = 'text-on-surface-variant opacity-50' }) {
  return (
    <div className="bg-surface-container-lowest rounded-lg p-sm shadow-sm border border-outline-variant flex justify-between items-center h-full gap-sm">
      <div className="flex flex-col min-w-0">
        <span className="font-label-md text-label-md text-on-surface-variant uppercase">{label}</span>
        <span className="font-headline-sm text-headline-sm text-on-surface truncate">{value}</span>
      </div>
      <span className={`material-symbols-outlined ${iconClass}`}>{icon}</span>
    </div>
  )
}

function formatFixtureTime(kickoffIso) {
  if (!kickoffIso) return 'TBC'
  return new Intl.DateTimeFormat(undefined, {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(kickoffIso))
}

function FixtureRow({ fixture }) {
  const hasScore = fixture.home_score != null && fixture.away_score != null

  return (
    <div className="bg-surface-container-lowest rounded-xl p-sm border border-outline-variant shadow-sm">
      <div className="flex items-center justify-between gap-sm">
        <div className="flex items-center gap-sm min-w-0 flex-1">
          <TeamBadge name={fixture.home_team_name} shortName={fixture.home_team} />
          <div className="min-w-0">
            <div className="font-body-md text-body-md font-semibold text-on-surface truncate">
              {fixture.home_team}
            </div>
            <div className="font-label-md text-[10px] text-on-surface-variant truncate">
              Home
            </div>
          </div>
        </div>

        <div className="flex flex-col items-center shrink-0 min-w-[72px]">
          <span className="font-stats-number text-[16px] text-primary">
            {hasScore ? `${fixture.home_score} - ${fixture.away_score}` : 'vs'}
          </span>
          <span className="font-label-md text-[10px] uppercase tracking-wide text-on-surface-variant">
            GW{fixture.gameweek}
          </span>
        </div>

        <div className="flex items-center gap-sm min-w-0 flex-1 justify-end text-right">
          <div className="min-w-0">
            <div className="font-body-md text-body-md font-semibold text-on-surface truncate">
              {fixture.away_team}
            </div>
            <div className="font-label-md text-[10px] text-on-surface-variant truncate">
              Away
            </div>
          </div>
          <TeamBadge name={fixture.away_team_name} shortName={fixture.away_team} />
        </div>
      </div>

      <div className="mt-sm pt-sm border-t border-outline-variant flex items-center justify-between gap-sm">
        <span className="font-label-md text-label-md text-on-surface-variant truncate">
          {formatFixtureTime(fixture.kickoff_time)}
        </span>
        <span className="font-label-md text-[10px] uppercase tracking-wide text-secondary">
          {fixture.status}
        </span>
      </div>
    </div>
  )
}

function DashboardPage() {
  const { user } = useAuth()
  const { hasSquad } = useSquadStatus()
  const { season, gameweek } = useGameweek()
  const navigate = useNavigate()

  const [data, setData] = useState(null)
  const [fixtures, setFixtures] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setLoadError(null)

    Promise.all([
      fetchTeamDashboard({ season, gameweek }),
      fetchFixtures({
        season,
        upcoming_only: true,
      }),
    ])
      .then(([body, fixturesBody]) => {
        if (cancelled) return
        setData(normalizeDashboardPlayers(body))
        setFixtures((fixturesBody ?? []).slice(0, 5))
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
  }, [user.id, season, gameweek])

  const pitchRows = useMemo(() => {
    if (!data) return []
    return ROWS.map((pos) => data.lineup[pos]).filter((row) => row.length > 0)
  }, [data])

  const countdown = data ? formatCountdown(data.deadline) : null
  const badge = data ? (LIVE_BADGE[data.live_status] ?? LIVE_BADGE.upcoming) : null

  return (
    <div className="bg-background text-on-background font-body-md min-h-screen pb-24">
      <FplHeader title="Dashboard" />

      <main className="px-safe-margin max-w-[600px] mx-auto w-full flex flex-col gap-lg">
        {loading && <p className="font-body-md text-on-surface-variant mt-lg">Loading your team…</p>}
        {loadError && (
          <p className="font-body-md text-error mt-lg" role="alert">
            {loadError}
          </p>
        )}

        {data && (
          <>
            <section className="flex flex-col items-center text-center mt-sm">
              <div className="flex items-center gap-sm mb-1">
                <h2 className="font-headline-sm text-headline-sm text-on-surface">
                  Gameweek {data.gameweek}
                </h2>
                <span
                  className={`text-[10px] font-bold px-2 py-0.5 rounded-sm tracking-widest uppercase ${badge.className}`}
                  data-testid="live-status"
                >
                  {badge.label}
                </span>
              </div>
              {countdown && (
                <div className="inline-flex items-center gap-xs bg-surface-container-high px-sm py-xs rounded-full mt-xs">
                  <span className="material-symbols-outlined text-label-md text-error">timer</span>
                  <span className="font-label-md text-label-md text-error">
                    {countdown === 'Deadline passed' ? countdown : `Deadline in ${countdown}`}
                  </span>
                </div>
              )}
            </section>

            <section className="grid grid-cols-2 gap-sm">
              <div className="bg-surface-container-lowest rounded-xl p-md shadow-sm border border-outline-variant flex flex-col items-center justify-center text-center">
                <span className="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider mb-xs">
                  GW Points
                </span>
                <span className="font-display-lg text-display-lg text-primary" data-testid="gw-points">
                  {data.gw_points}
                </span>
                <div className="flex items-center gap-xs mt-xs text-secondary">
                  <span className="material-symbols-outlined text-[16px]">trending_up</span>
                  <span className="font-label-md text-label-md">
                    {data.gw_average != null ? `Avg: ${data.gw_average}` : 'Avg: --'}
                  </span>
                </div>
              </div>
              <div className="flex flex-col gap-sm">
                <StatTile icon="emoji_events" label="Season Total" value={data.season_total} />
                <StatTile
                  icon="arrow_upward"
                  iconClass="text-secondary opacity-50"
                  label="Overall Rank"
                  value={
                    data.overall_rank != null
                      ? `${data.overall_rank} / ${data.overall_rank_total}`
                      : '--'
                  }
                />
              </div>
            </section>

            <section className="bg-primary-container rounded-xl p-md flex justify-between items-center shadow-md">
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
            </section>

            {data.has_lineup ? (
              <section className="rounded-xl overflow-hidden shadow-sm border border-outline-variant relative flex flex-col">
                {data.chip_used === 'bench_boost' && (
                  <div
                    className="bg-surface-container-highest p-sm text-center font-label-md text-label-md text-on-surface flex items-center justify-center gap-2"
                    data-testid="bench-boost-banner"
                  >
                    <span className="material-symbols-outlined text-[16px] text-secondary">
                      check_circle
                    </span>
                    Bench Boost Active — all 15 players counted
                  </div>
                )}

                <div className="bg-secondary w-full aspect-[3/4] relative flex flex-col justify-between py-md">
                  <div className="absolute inset-0 bg-gradient-to-b from-black/20 via-transparent to-black/30 pointer-events-none" />
                  {pitchRows.map((row, i) => (
                    <div
                      className="flex justify-around w-full px-sm relative z-10"
                      key={ROWS[i] ?? i}
                    >
                      {row.map((p) => (
                        <PlayerChip
                          big={p.is_captain}
                          key={p.player_id}
                          multiplier={data.captain_multiplier}
                          player={p}
                        />
                      ))}
                    </div>
                  ))}
                </div>

                <div className="bg-surface-container-lowest p-sm border-t border-outline-variant flex justify-between items-center text-on-surface-variant gap-sm">
                  <span className="font-label-md text-label-md truncate">
                    Bench:{' '}
                    {data.bench.map((p) => `${shortName(p.name)} (${p.points})`).join(', ') || '--'}
                  </span>
                  <span className="material-symbols-outlined text-[16px] shrink-0">chair</span>
                </div>
              </section>
            ) : (
              <section className="bg-surface-container-lowest rounded-xl p-lg border border-outline-variant flex flex-col items-center gap-sm text-center">
                <span className="material-symbols-outlined text-on-surface-variant">group_add</span>
                <p className="font-body-md text-body-md text-on-surface-variant">
                  You haven't set a starting XI for Gameweek {data.gameweek} yet.
                </p>
              </section>
            )}

            {/* Points breakdown. Every number comes from GET /team, which reads
                gw_scores directly -- captain_bonus is final_points - raw_points,
                the captain's EXTRA only, so the captain is never counted twice.
                Nothing on this page recomputes points. */}
            <section
              className="bg-surface-container-lowest rounded-xl p-md shadow-sm border border-outline-variant"
              data-testid="points-breakdown"
            >
              <h3 className="font-body-md text-body-md font-bold text-on-surface mb-3 border-b border-outline-variant pb-2">
                Points Breakdown
              </h3>
              {data.has_score ? (
                <div className="flex flex-col gap-2">
                  <div className="flex justify-between items-center font-body-md text-body-md">
                    <span className="text-on-surface-variant">Raw Points</span>
                    <span className="font-bold text-on-surface" data-testid="raw-points">
                      {data.raw_points}
                    </span>
                  </div>
                  <div className="flex justify-between items-center font-body-md text-body-md">
                    <span className="text-on-surface-variant">Captain Bonus</span>
                    <span className="font-bold text-secondary" data-testid="captain-bonus">
                      +{data.captain_bonus}
                    </span>
                  </div>
                  <div className="flex justify-between items-center font-body-md text-body-md">
                    <span className="text-on-surface-variant">Transfer Hits</span>
                    <span className="font-bold text-error" data-testid="transfer-hits">
                      {data.hit_deductions > 0 ? `-${data.hit_deductions}` : '0'}
                    </span>
                  </div>
                  <div className="flex justify-between items-center font-body-lg text-body-lg font-bold border-t border-outline-variant pt-2 mt-1">
                    <span className="text-on-surface">Final Total</span>
                    <span
                      className="text-primary font-stats-number text-stats-number"
                      data-testid="final-total"
                    >
                      {data.final_total}
                    </span>
                  </div>
                </div>
              ) : (
                <p className="font-body-md text-body-md text-on-surface-variant">
                  This gameweek hasn't been scored yet.
                </p>
              )}
            </section>

            {/* Squad Selection lives here, not in the nav. */}
            <section className="grid grid-cols-1 gap-sm mb-lg">
              <button
                className="bg-surface-container-lowest hover:bg-surface-container-low transition-colors rounded-xl p-md border border-outline-variant flex items-center justify-between gap-sm shadow-sm"
                onClick={() => navigate('/squad-selection')}
              >
                <div className="flex items-center gap-sm">
                  <div className="w-10 h-10 rounded-full bg-surface-container flex items-center justify-center text-primary">
                    <span className="material-symbols-outlined">groups</span>
                  </div>
                  <span className="font-body-md text-body-md font-semibold text-on-surface whitespace-nowrap">
                    My Squad
                  </span>
                </div>
                <span className="material-symbols-outlined text-outline">chevron_right</span>
              </button>

              <button
                className="bg-surface-container-lowest hover:bg-surface-container-low transition-colors rounded-xl p-md border border-outline-variant flex items-center justify-between gap-sm shadow-sm"
                onClick={() => navigate('/transfers')}
              >
                <div className="flex items-center gap-sm">
                  <div className="w-10 h-10 rounded-full bg-surface-container flex items-center justify-center text-primary">
                    <span className="material-symbols-outlined">swap_horiz</span>
                  </div>
                  <span className="font-body-md text-body-md font-semibold text-on-surface whitespace-nowrap">
                    Make Transfers
                  </span>
                </div>
                <span className="material-symbols-outlined text-outline">chevron_right</span>
              </button>

              {/* Same gate as the nav: no squad, no XI to set. */}
              <button
                className="bg-surface-container-lowest hover:bg-surface-container-low transition-colors rounded-xl p-md border border-outline-variant flex items-center justify-between gap-sm shadow-sm disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-surface-container-lowest"
                data-testid="set-starting-xi"
                disabled={!hasSquad}
                onClick={() => navigate('/squad')}
                title={hasSquad ? undefined : 'Submit your 15-player squad first'}
              >
                <div className="flex items-center gap-sm">
                  <div className="w-10 h-10 rounded-full bg-surface-container flex items-center justify-center text-primary">
                    <span className="material-symbols-outlined">{hasSquad ? 'group_add' : 'lock'}</span>
                  </div>
                  <span className="font-body-md text-body-md font-semibold text-on-surface whitespace-nowrap">
                    Set Starting XI
                  </span>
                </div>
                <span className="material-symbols-outlined text-outline">chevron_right</span>
              </button>

              {/* Ungated, unlike the three above: the rules are the one thing a
                  manager with no squad yet actually needs to read. */}
              <button
                className="bg-surface-container-lowest hover:bg-surface-container-low transition-colors rounded-xl p-md border border-outline-variant flex items-center justify-between gap-sm shadow-sm"
                data-testid="how-points-work"
                onClick={() => navigate('/scoring')}
              >
                <div className="flex items-center gap-sm">
                  <div className="w-10 h-10 rounded-full bg-surface-container flex items-center justify-center text-primary">
                    <span className="material-symbols-outlined">help</span>
                  </div>
                  <span className="font-body-md text-body-md font-semibold text-on-surface whitespace-nowrap">
                    How Points Work
                  </span>
                </div>
                <span className="material-symbols-outlined text-outline">chevron_right</span>
              </button>

              <div className="flex flex-col gap-sm">
                <div className="bg-surface-container-lowest rounded-xl p-md border border-outline-variant flex items-center justify-between gap-sm shadow-sm">
                  <div className="flex items-center gap-sm">
                    <div className="w-10 h-10 rounded-full bg-surface-container flex items-center justify-center text-primary">
                      <span className="material-symbols-outlined">calendar_month</span>
                    </div>
                    <span className="font-body-md text-body-md font-semibold text-on-surface whitespace-nowrap">
                      Fixtures
                    </span>
                  </div>
                  <span className="font-label-md text-label-md text-on-surface-variant">
                    Upcoming
                  </span>
                </div>

                {fixtures.length > 0 ? (
                  <div className="flex flex-col gap-sm">
                    {fixtures.map((fixture) => (
                      <FixtureRow fixture={fixture} key={fixture.fixture_id} />
                    ))}
                  </div>
                ) : (
                  <div className="bg-surface-container-lowest rounded-xl p-md border border-outline-variant text-center">
                    <p className="font-body-md text-body-md text-on-surface-variant">
                      No upcoming fixtures found.
                    </p>
                  </div>
                )}
              </div>
            </section>
          </>
        )}
      </main>

      <BottomNav />
    </div>
  )
}

export default DashboardPage
