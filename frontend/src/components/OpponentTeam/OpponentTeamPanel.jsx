import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import PitchLineup from '../PitchLineup/PitchLineup'
import PlayerPointsSheet from '../PlayerPointsSheet/PlayerPointsSheet'
import { fetchContestTeam, ForbiddenError, NotFoundError } from '../../api/dream11'

const ROWS = ['GK', 'DEF', 'MID', 'FWD']

const CAPTAIN_MULTIPLIER = 2
const VICE_CAPTAIN_MULTIPLIER = 1.5

/** Group the flat players array into the {GK,DEF,MID,FWD} shape PitchLineup
 *  wants, and map the API's field names onto PlayerMarker's props. `points`
 *  from the API is deliberately PRE-multiplier, so the marker can show the raw
 *  score and the "×2" separately rather than a single conflated number. */
function toLineup(players) {
  const lineup = Object.fromEntries(ROWS.map((position) => [position, []]))

  for (const player of players) {
    const row = lineup[player.position]
    if (!row) continue // an unknown position is a data problem, not a crash
    row.push({
      ...player,
      captain: player.is_captain,
      viceCaptain: player.is_vice_captain,
      multiplier: player.is_captain
        ? CAPTAIN_MULTIPLIER
        : player.is_vice_captain
          ? VICE_CAPTAIN_MULTIPLIER
          : 1,
    })
  }

  return lineup
}

function formatCountdown(ms) {
  const totalMinutes = Math.floor(ms / 60000)
  const days = Math.floor(totalMinutes / 1440)
  const hours = Math.floor((totalMinutes % 1440) / 60)
  const minutes = totalMinutes % 60

  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${minutes}m`
  return `${minutes}m`
}

/** Informational, not an error: the contest simply hasn't locked yet. */
function HiddenUntilKickoff({ message, kickoffTime }) {
  const [now, setNow] = useState(() => Date.now())

  // Only tick while there's actually a future kickoff to count down to.
  const kickoffMs = kickoffTime ? new Date(kickoffTime).getTime() : null
  const remaining = kickoffMs != null && !Number.isNaN(kickoffMs) ? kickoffMs - now : null
  const counting = remaining != null && remaining > 0

  useEffect(() => {
    if (!counting) return undefined
    const id = setInterval(() => setNow(Date.now()), 30000)
    return () => clearInterval(id)
  }, [counting])

  return (
    <div
      className="bg-secondary-container text-on-secondary-container rounded-xl p-md flex flex-col items-center gap-xs text-center"
      data-testid="opponent-team-hidden"
    >
      <span className="material-symbols-outlined text-[28px]">lock_clock</span>
      <p className="font-headline-sm text-headline-sm">Teams are revealed at kickoff</p>
      <p className="font-body-md text-body-md opacity-80">
        {message || 'Everyone picks blind until the contest locks.'}
      </p>
      {counting && (
        <p className="font-label-md text-label-md mt-xs" data-testid="opponent-team-countdown">
          Kickoff in {formatCountdown(remaining)}
        </p>
      )}
    </div>
  )
}

/** Also informational: this member joined but never picked. */
function NoTeamSubmitted() {
  return (
    <div
      className="bg-surface-container-high text-on-surface-variant rounded-xl p-md flex flex-col items-center gap-xs text-center"
      data-testid="opponent-team-empty"
    >
      <span className="material-symbols-outlined text-[28px]">person_off</span>
      <p className="font-headline-sm text-headline-sm text-on-surface">
        This manager hasn&apos;t picked a team.
      </p>
    </div>
  )
}

/**
 * One member's Dream11 XI. `opponent` is a leaderboard row.
 *
 * The three non-success outcomes are handled separately on purpose: 403 and 404
 * are ordinary states of the game (hidden until kickoff / never picked) and get
 * their own panels, while anything else really is a failure and gets the error
 * treatment. See api/dream11.js's fetchContestTeam.
 *
 * isLocked gates the Edit Team button alongside isSelf: this panel is also
 * used to view a rival's team (once the contest locks, per fetchContestTeam's
 * own 403 rule above), so isSelf alone isn't enough -- editing must stop the
 * moment the contest locks, same as submitting a first team does.
 */
function OpponentTeamPanel({ contestId, opponent, kickoffTime, isSelf = false, isLocked = false, onClose }) {
  const [team, setTeam] = useState(null)
  const [status, setStatus] = useState('loading') // loading | ready | hidden | no-team | error
  const [message, setMessage] = useState('')
  const [selectedPlayer, setSelectedPlayer] = useState(null)

  useEffect(() => {
    let cancelled = false
    setStatus('loading')
    setTeam(null)
    setMessage('')
    setSelectedPlayer(null)

    fetchContestTeam({ contest_id: contestId, user_id: opponent.user_id })
      .then((data) => {
        if (cancelled) return
        setTeam(data)
        setStatus('ready')
      })
      .catch((err) => {
        if (cancelled) return
        if (err instanceof ForbiddenError) {
          setMessage(err.message)
          setStatus('hidden')
        } else if (err instanceof NotFoundError) {
          setStatus('no-team')
        } else {
          setMessage(err.message || 'Could not load this team')
          setStatus('error')
        }
      })

    return () => {
      cancelled = true
    }
  }, [contestId, opponent.user_id])

  const title = opponent.team_name || opponent.username || `User ${opponent.user_id}`

  return (
    <section
      aria-label={`${title} team`}
      className="bg-surface-container-lowest rounded-xl border border-outline-variant shadow-sm p-md flex flex-col gap-md"
      data-testid="opponent-team-panel"
    >
      <header className="flex items-start justify-between gap-sm">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h2 className="font-headline-sm text-headline-sm text-primary truncate">{title}</h2>
            {isSelf && (
              <span className="bg-primary text-on-primary text-[9px] px-1.5 py-0.5 rounded">ME</span>
            )}
          </div>
          {opponent.username && (
            <p className="font-label-md text-label-md text-on-surface-variant">
              {opponent.username}
            </p>
          )}
        </div>
        <div className="flex items-center gap-sm shrink-0">
          {status === 'ready' && (
            <div className="text-right">
              <p className="font-stats-number text-stats-number text-primary">
                {team.live_total_points}
              </p>
              <p className="font-label-md text-label-md text-on-surface-variant opacity-70">Pts</p>
            </div>
          )}
          {onClose && (
            <button
              aria-label="Close team view"
              className="w-8 h-8 rounded-full bg-surface-container-high text-on-surface-variant flex items-center justify-center"
              onClick={onClose}
              type="button"
            >
              <span className="material-symbols-outlined text-[18px]">close</span>
            </button>
          )}
        </div>
      </header>

      {status === 'loading' && (
        <p className="font-body-md text-body-md text-on-surface-variant">Loading team…</p>
      )}

      {status === 'hidden' && <HiddenUntilKickoff kickoffTime={kickoffTime} message={message} />}

      {status === 'no-team' && <NoTeamSubmitted />}

      {status === 'error' && (
        <div className="bg-error-container text-on-error-container rounded-lg p-sm" role="alert">
          <p className="font-label-md text-label-md">{message}</p>
        </div>
      )}

      {status === 'ready' && (
        <>
          <PitchLineup lineup={toLineup(team.players)} onSelectPlayer={setSelectedPlayer} />
          <dl className="grid grid-cols-3 gap-sm text-center">
            <div>
              <dt className="font-label-md text-label-md text-on-surface-variant">Credits</dt>
              <dd className="font-body-md text-body-md text-on-surface">
                {team.total_credit_cost}
              </dd>
            </div>
            <div>
              <dt className="font-label-md text-label-md text-on-surface-variant">C / VC bonus</dt>
              <dd className="font-body-md text-body-md text-on-surface">
                +{team.captain_bonus} / +{team.vice_captain_bonus}
              </dd>
            </div>
            <div>
              <dt className="font-label-md text-label-md text-on-surface-variant">Rank</dt>
              <dd className="font-body-md text-body-md text-on-surface">
                {team.contest_rank || '—'}
              </dd>
            </div>
          </dl>
          {isSelf && !isLocked && (
            <Link
              className="w-full text-center bg-primary-container text-on-primary-container rounded-lg py-sm font-label-md text-label-md uppercase tracking-wider hover:opacity-90 transition-opacity"
              to={`/dream11/contests/${contestId}/edit`}
            >
              Edit Team
            </Link>
          )}
        </>
      )}

      {selectedPlayer && (
        <PlayerPointsSheet onClose={() => setSelectedPlayer(null)} player={selectedPlayer} />
      )}
    </section>
  )
}

export default OpponentTeamPanel
