import TeamBadge from '../TeamBadge/TeamBadge'
import { getTeamByPlayer } from '../../data/premierLeague2026'

function teamFullName(shortName) {
  return getTeamByPlayer({ club: shortName })?.name ?? shortName
}

function matchDateTime(kickoffTime) {
  if (!kickoffTime) return null
  const date = new Date(kickoffTime)
  if (Number.isNaN(date.getTime())) return null
  const datePart = date.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
  const timePart = date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
  return `${datePart} · ${timePart}`
}

/**
 * The match-context card shown below the header on both the Team screen
 * (PickTeamPage) and the Leaderboard (Dream11ContestPage) -- one shared
 * component so the two can never drift apart. Real club badges (TeamBadge,
 * enlarged to "lg") and real fixture data throughout. No venue line: the
 * backend has no stadium field to read one from, so nothing is shown rather
 * than a fabricated one.
 */
function FixtureBanner({ homeTeam, awayTeam, gameweek, kickoffTime, contestType }) {
  const when = matchDateTime(kickoffTime)

  return (
    <section className="pt-sm pb-1" data-testid="fixture-banner">
      <div className="bg-white rounded-2xl p-4 border border-[#E5E6E1] shadow-[0_2px_8px_rgba(0,0,0,0.04)] text-center">
        {(gameweek || contestType) && (
          <div className="flex items-center justify-center gap-2 font-label-md text-[10px] font-bold uppercase tracking-wider text-on-surface-variant mb-2">
            {gameweek && (
              <span className="inline-flex items-center px-2 py-0.5 rounded-full bg-[#EDF2DF] text-[#4D631B] font-semibold normal-case tracking-normal">
                GW{gameweek}
              </span>
            )}
            {gameweek && contestType && <span>•</span>}
            {contestType && <span>{contestType}</span>}
          </div>
        )}

        <div className="flex items-center justify-center gap-6 py-1">
          <div className="flex flex-col items-center w-24">
            <TeamBadge name={teamFullName(homeTeam)} shortName={homeTeam} size="lg" />
            <span className="font-label-md text-[13px] font-bold text-on-surface tracking-tight mt-1.5 truncate max-w-full">
              {teamFullName(homeTeam)}
            </span>
            <span className="font-label-md text-[10px] font-semibold text-on-surface-variant uppercase tracking-wider">
              Home
            </span>
          </div>

          <span className="w-7 h-7 shrink-0 rounded-full bg-[#F5F3F0] border border-[#E5E6E1] flex items-center justify-center font-label-md text-[10px] font-black text-on-surface-variant shadow-sm">
            VS
          </span>

          <div className="flex flex-col items-center w-24">
            <TeamBadge name={teamFullName(awayTeam)} shortName={awayTeam} size="lg" />
            <span className="font-label-md text-[13px] font-bold text-on-surface tracking-tight mt-1.5 truncate max-w-full">
              {teamFullName(awayTeam)}
            </span>
            <span className="font-label-md text-[10px] font-semibold text-on-surface-variant uppercase tracking-wider">
              Away
            </span>
          </div>
        </div>

        {when && (
          <div className="mt-2 pt-2 border-t border-[#E5E6E1] flex items-center justify-center gap-1.5 font-label-md text-[11px] font-medium text-on-surface-variant">
            <span className="material-symbols-outlined text-[14px]">schedule</span>
            <span>{when}</span>
          </div>
        )}
      </div>
    </section>
  )
}

export default FixtureBanner
