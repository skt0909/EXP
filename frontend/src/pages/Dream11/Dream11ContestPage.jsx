import { useEffect, useState } from 'react'
import { Link, useNavigate, useOutletContext, useParams } from 'react-router-dom'
import OpponentTeamPanel from '../../components/OpponentTeam/OpponentTeamPanel'
import TeamBadge from '../../components/TeamBadge/TeamBadge'
import { CONTEST_STATUS, CONTEST_STATUS_CLASSES, contestStatus, isContestLocked } from '../../data/contestStatus'
import { fetchContestLeaderboard } from '../../api/dream11'
import { MODE_CONTESTS, MODE_HOME } from '../../config/appMode'
import DetailHeader from '../../components/DetailHeader/DetailHeader'

function kickoffLabel(kickoffTime) {
  if (!kickoffTime) return 'Kickoff time TBC'
  const date = new Date(kickoffTime)
  if (Number.isNaN(date.getTime())) return 'Kickoff time TBC'
  return date.toLocaleString(undefined, {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

// Individually-drawn "waiting for rival" rows past this count collapse into
// one summary row -- a 50-cap public contest with 3 members shouldn't render
// 47 empty slots.
const MAX_DRAWN_SLOTS = 5

/**
 * The unfilled seats in a contest that hasn't locked yet, styled after the
 * leaderboard rows above them so an open slot reads as "a real seat, not
 * taken yet" rather than a different kind of thing on the page.
 */
function OpenSlots({ contest, onInvite }) {
  const open = Math.max(0, contest.max_members - contest.member_count)
  if (open === 0) return null

  const drawn = Math.min(open, MAX_DRAWN_SLOTS)
  const overflow = open - drawn

  return (
    <>
      {Array.from({ length: drawn }, (_, i) => (
        <div
          className="w-full bg-surface-container-lowest rounded-xl p-md border border-dashed border-outline-variant flex items-center gap-md"
          data-testid="open-slot"
          key={i}
        >
          <span className="material-symbols-outlined text-[28px] text-outline-variant w-8 shrink-0 text-center">
            person_add
          </span>
          <span className="min-w-0 flex-1">
            <span className="block italic font-bold text-on-surface-variant">Waiting for rival…</span>
            <span className="block text-on-surface-variant text-xs">Share code to start match</span>
          </span>
          <button
            className="shrink-0 bg-surface-container-high text-on-surface font-label-md text-label-md px-3 py-1.5 rounded-full flex items-center gap-1"
            onClick={onInvite}
            type="button"
          >
            <span className="material-symbols-outlined text-[16px]">share</span>
            Invite
          </button>
        </div>
      ))}
      {overflow > 0 && (
        <p className="font-label-md text-label-md text-on-surface-variant text-center py-1">
          {overflow} more open spot{overflow === 1 ? '' : 's'}
        </p>
      )}
    </>
  )
}

/**
 * A Dream11 contest's leaderboard, and the entry point to every member's XI.
 *
 * Rows without has_submitted_team are shown but not tappable -- opening one
 * could only ever land on "this manager hasn't picked a team", so the row says
 * so upfront instead of making the user find out the hard way.
 */
function Dream11ContestPage() {
  const { contestId } = useParams()
  const { settings } = useOutletContext()
  const { user_id } = settings
  const navigate = useNavigate()

  const [data, setData] = useState(null)
  const [selectedUserId, setSelectedUserId] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)

  async function copyCode(code) {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // Clipboard access can be denied (permissions, non-HTTPS, older
      // browsers) -- the code is already on-screen as plain text, so a
      // failed copy just means the user selects it manually instead.
    }
  }

  async function shareCode(contest) {
    const text = `Join my "${contest.name}" contest on PitchSide — code ${contest.code}`
    if (navigator.share) {
      try {
        await navigator.share({ text })
      } catch {
        // AbortError when the user cancels the native share sheet -- not a
        // failure worth surfacing.
      }
    } else {
      copyCode(contest.code)
    }
  }

  function handleBack() {
    if (window.history.state?.idx > 0) navigate(-1)
    else navigate(MODE_HOME[MODE_CONTESTS])
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')

    fetchContestLeaderboard({ contest_id: contestId, user_id })
      .then((body) => {
        if (!cancelled) setData(body)
      })
      .catch((err) => {
        if (!cancelled) setError(err.message || 'Could not load this contest')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [contestId, user_id])

  const contest = data?.contest ?? null
  // Locked from kickoff, not only once the 5-minute lock sweep sets
  // is_locked -- see data/contestStatus.js.
  const locked = isContestLocked(contest)
  const status = contest ? contestStatus(contest) : null
  const rows = data?.rows ?? []
  const selectedRow = rows.find((row) => row.user_id === selectedUserId) ?? null

  return (
    <main className="w-full px-safe-margin py-md flex flex-col gap-lg">
      {loading ? (
        <p className="font-body-md text-body-md text-on-surface-variant">Loading contest…</p>
      ) : error ? (
        <div className="bg-error-container text-on-error-container rounded-lg p-sm" role="alert">
          <p className="font-label-md text-label-md">{error}</p>
        </div>
      ) : (
        <>
          <div>
            <DetailHeader onBack={handleBack} title="Leaderboard" />
            <div className="flex items-center gap-xs">
              <TeamBadge shortName={contest.home_team} size="sm" />
              <p className="font-label-md text-label-md text-on-surface-variant uppercase tracking-wider">
                {contest.home_team} v {contest.away_team} · GW{contest.gameweek}
              </p>
              <TeamBadge shortName={contest.away_team} size="sm" />
            </div>
            <h2 className="font-display-lg text-display-lg text-primary">{contest.name}</h2>
            <p className="font-body-md text-body-md text-on-surface-variant mt-sm flex items-center gap-1.5 flex-wrap">
              <span>
                {contest.member_count}/{contest.max_members} members · code{' '}
                <span className="font-bold text-on-surface tracking-wider">{contest.code}</span>
              </span>
              <button
                aria-label="Copy invite code"
                className="inline-flex items-center gap-1 text-on-surface-variant hover:text-primary-container transition-colors"
                data-testid="copy-code"
                onClick={() => copyCode(contest.code)}
                type="button"
              >
                <span className="material-symbols-outlined text-[16px]">
                  {copied ? 'check' : 'content_copy'}
                </span>
                <span className="font-label-md text-label-md">{copied ? 'Copied' : 'Copy'}</span>
              </button>
            </p>
            <div className="flex items-center gap-2 mt-sm">
              <span
                className={`px-2 py-0.5 rounded text-[10px] font-bold tracking-wider uppercase ${
                  CONTEST_STATUS_CLASSES[status.status]
                }`}
                data-testid="dream11-contest-status"
              >
                {status.label}
              </span>
              <span className="font-label-md text-label-md text-on-surface-variant">
                {kickoffLabel(contest.kickoff_time)}
              </span>
            </div>

            {/* Dream11 scores nothing like classic -- assists are +20, clean
                sheets need 54 minutes, and both armbands apply at once. Anyone
                reading a leaderboard here needs that within reach. */}
            <Link
              className="inline-flex items-center gap-sm mt-md px-md py-sm rounded-full bg-surface-container-low text-on-surface hover:bg-surface-container-high transition-colors"
              to="/dream11/scoring"
            >
              <span className="material-symbols-outlined text-[18px] text-primary-container">
                help
              </span>
              <span className="font-label-md text-label-md">How points work</span>
            </Link>
          </div>

          <section aria-label="Leaderboard" className="flex flex-col gap-sm">
            <h2 className="font-headline-sm text-headline-sm text-primary">Leaderboard</h2>
            {status.status === CONTEST_STATUS.CANCELLED && (
              <div
                className="bg-error-container text-on-error-container rounded-lg p-sm"
                data-testid="dream11-contest-cancelled"
                role="status"
              >
                <p className="font-label-md text-label-md">
                  {status.detail} — this contest has been cancelled. There is no result, and no points count.
                </p>
              </div>
            )}
            {status.status === CONTEST_STATUS.LIVE && (
              <p className="font-label-md text-label-md text-on-surface-variant">
                The match is live — teams are locked and can no longer be changed.
              </p>
            )}
            {status.status === CONTEST_STATUS.COMPLETED && (
              <p className="font-label-md text-label-md text-on-surface-variant">
                Final result — these points and ranks won&apos;t change.
              </p>
            )}
            {!locked && (
              <p className="font-label-md text-label-md text-on-surface-variant">
                Rival teams stay hidden until kickoff — you can still open your own.
              </p>
            )}
            {rows.map((row, index) => {
              const isMe = row.user_id === user_id
              const openable = row.has_submitted_team
              const label = row.team_name || row.username || `User ${row.user_id}`

              return (
                <button
                  aria-label={openable ? `View ${label}'s team` : `${label} has no team`}
                  className={`w-full text-left bg-surface-container-lowest rounded-xl p-md border shadow-sm transition-shadow flex items-center gap-md ${
                    row.user_id === selectedUserId ? 'border-primary-container' : 'border-outline-variant'
                  } ${openable ? 'hover:shadow-md' : 'opacity-70 cursor-default'}`}
                  data-testid="leaderboard-row"
                  disabled={!openable}
                  key={row.user_id}
                  onClick={() => setSelectedUserId(row.user_id)}
                  type="button"
                >
                  <span className="font-stats-number text-stats-number text-on-surface-variant w-8 shrink-0">
                    {/* A cancelled contest has no result, so no ranks either. */}
                    {status.status === CONTEST_STATUS.CANCELLED ? '–' : row.rank || index + 1}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="font-bold text-primary flex items-center gap-2">
                      <span className="truncate">{label}</span>
                      {isMe && (
                        <span className="bg-primary text-on-primary text-[9px] px-1.5 py-0.5 rounded shrink-0">
                          ME
                        </span>
                      )}
                    </span>
                    <span className="block text-on-surface-variant text-xs">
                      {openable ? row.username ?? '' : 'No team submitted'}
                    </span>
                  </span>
                  <span className="text-right shrink-0">
                    <span className="block font-stats-number text-stats-number text-primary">
                      {row.total_points}
                    </span>
                    <span className="block font-label-md text-label-md text-on-surface-variant opacity-70">
                      Pts
                    </span>
                  </span>
                </button>
              )
            })}
            {!locked && <OpenSlots contest={contest} onInvite={() => shareCode(contest)} />}
          </section>

          {selectedRow && (
            <OpponentTeamPanel
              contestId={contestId}
              isLocked={locked}
              isSelf={selectedRow.user_id === user_id}
              kickoffTime={contest.kickoff_time}
              onClose={() => setSelectedUserId(null)}
              opponent={selectedRow}
            />
          )}

          {/* Only while there's a seat left to fill -- a full or locked
              contest has no one left to invite. */}
          {!locked && contest.member_count < contest.max_members && (
            <div className="h-16" aria-hidden="true" />
          )}
        </>
      )}

      {contest && !locked && contest.member_count < contest.max_members && (
        <div className="fixed bottom-[72px] left-1/2 -translate-x-1/2 w-full max-w-[600px] z-40 px-md py-sm bg-surface/95 backdrop-blur-lg border-t border-outline-variant">
          <button
            className="w-full bg-primary text-on-primary rounded-lg py-3 font-label-md text-label-md flex items-center justify-center gap-2"
            data-testid="share-invite-code"
            onClick={() => shareCode(contest)}
            type="button"
          >
            <span className="material-symbols-outlined text-[18px]">ios_share</span>
            {`Share Invite Code (${contest.code})`}
          </button>
        </div>
      )}
    </main>
  )
}

export default Dream11ContestPage
