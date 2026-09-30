import TeamBadge from '../TeamBadge/TeamBadge'

/**
 * The home-badge/name/HOME .. VS or score .. AWAY/name/away-badge row.
 * Originally the Matches screen's own MatchCard; shared here so the
 * Leagues list's contest cards can use the exact same component instead
 * of a smaller, differently-laid-out copy of it.
 *
 * Takes anything fixture-or-contest-shaped with home_team/away_team (short
 * codes -- displayed as-is, matching what Matches always showed) and
 * optionally home_score/away_score (shown instead of "VS" once both are
 * set) and home_team_name/away_team_name (badge alt text only). A Quick 11
 * contest carries the first two and neither score field, so it always
 * reads "VS" here -- correct, since a contest has no classic match score
 * to show.
 */
function Scoreline({ fixture }) {
  const hasScore = fixture.home_score != null && fixture.away_score != null
  return (
    <div className="flex items-center justify-between gap-sm">
      <div className="flex items-center gap-sm flex-1 min-w-0">
        <TeamBadge name={fixture.home_team_name} shortName={fixture.home_team} />
        <div className="min-w-0">
          <p className="font-headline-sm text-headline-sm text-on-surface truncate">{fixture.home_team}</p>
          <p className="font-label-md text-[9px] font-bold tracking-wider text-on-surface-variant">HOME</p>
        </div>
      </div>
      {hasScore ? (
        <p className="font-stats-number text-stats-number text-on-surface shrink-0 px-sm">
          {fixture.home_score}–{fixture.away_score}
        </p>
      ) : (
        <p className="font-label-md text-[10px] font-bold tracking-wider text-on-surface-variant shrink-0 px-sm">VS</p>
      )}
      <div className="flex items-center justify-end gap-sm flex-1 min-w-0 text-right">
        <div className="min-w-0">
          <p className="font-headline-sm text-headline-sm text-on-surface truncate">{fixture.away_team}</p>
          <p className="font-label-md text-[9px] font-bold tracking-wider text-on-surface-variant">AWAY</p>
        </div>
        <TeamBadge name={fixture.away_team_name} shortName={fixture.away_team} />
      </div>
    </div>
  )
}

export default Scoreline
