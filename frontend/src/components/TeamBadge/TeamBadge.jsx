import { getTeamByPlayer } from '../../data/premierLeague2026'

/**
 * A club badge in a small rounded frame, falling back to the short name
 * (e.g. "ARS") when a team can't be resolved -- same fallback PlayerJersey
 * uses for its own missing-team case, so an unrecognized club never renders
 * a broken image.
 *
 * Extracted from DashboardPage.jsx (FPL mode), which had this as a private,
 * unexported function -- Contest mode's fixture/contest lists had no
 * equivalent and were rendering team names as plain text with no badge at
 * all. `shortName` is the value already available everywhere a fixture or
 * contest names a team (`fixture.home_team`, `contest.home_team`, etc.);
 * `getTeamByPlayer` accepts `{ club: shortName }` as a stand-in "player".
 */
function TeamBadge({ shortName, name, size = 'md' }) {
  const team = getTeamByPlayer({ club: shortName })
  const dims = size === 'sm' ? 'w-7 h-7' : 'w-9 h-9'
  const imgDims = size === 'sm' ? 'w-[22px] h-[22px]' : 'w-7 h-7'

  return (
    <div
      className={`${dims} rounded-full bg-surface-container flex items-center justify-center overflow-hidden border border-outline-variant shrink-0`}
    >
      {team?.badge ? (
        <img alt={`${name ?? shortName} badge`} className={`${imgDims} object-contain`} draggable="false" src={team.badge} />
      ) : (
        <span className="font-label-md text-[10px] text-primary">{shortName}</span>
      )}
    </div>
  )
}

export default TeamBadge
