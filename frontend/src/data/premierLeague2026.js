const ASSET_BASE = '/assets/premier-league/2026-27'

export const PREMIER_LEAGUE_2026_TEAMS = [
  { id: 'arsenal', name: 'Arsenal', shortName: 'ARS', primaryColor: '#db0007', numberBaseColor: '#db0007' },
  { id: 'aston-villa', name: 'Aston Villa', shortName: 'AVL', primaryColor: '#95bfe5', numberBaseColor: '#95bfe5' },
  { id: 'bournemouth', name: 'Bournemouth', shortName: 'BOU', primaryColor: '#d71920', numberBaseColor: '#d71920' },
  { id: 'brentford', name: 'Brentford', shortName: 'BRE', primaryColor: '#e30613', numberBaseColor: '#e30613' },
  { id: 'brighton', name: 'Brighton', shortName: 'BHA', primaryColor: '#0057b8', numberBaseColor: '#0057b8' },
  { id: 'chelsea', name: 'Chelsea', shortName: 'CHE', primaryColor: '#034694', numberBaseColor: '#034694' },
  { id: 'coventry-city', name: 'Coventry City', shortName: 'COV', primaryColor: '#77c7e8', numberBaseColor: '#77c7e8' },
  { id: 'crystal-palace', name: 'Crystal Palace', shortName: 'CRY', primaryColor: '#1b458f', numberBaseColor: '#1b458f' },
  { id: 'everton', name: 'Everton', shortName: 'EVE', primaryColor: '#003399', numberBaseColor: '#003399' },
  { id: 'fulham', name: 'Fulham', shortName: 'FUL', primaryColor: '#ffffff', numberBaseColor: '#ffffff' },
  { id: 'hull-city', name: 'Hull City', shortName: 'HUL', primaryColor: '#f5a800', numberBaseColor: '#f5a800' },
  { id: 'ipswich-town', name: 'Ipswich Town', shortName: 'IPS', primaryColor: '#0057b8', numberBaseColor: '#0057b8' },
  { id: 'leeds-united', name: 'Leeds', shortName: 'LEE', primaryColor: '#ffffff', numberBaseColor: '#ffffff' },
  { id: 'liverpool', name: 'Liverpool', shortName: 'LIV', primaryColor: '#c8102e', numberBaseColor: '#c8102e' },
  { id: 'manchester-city', name: 'Man City', shortName: 'MCI', primaryColor: '#6cabdd', numberBaseColor: '#6cabdd' },
  { id: 'manchester-united', name: 'Man Utd', shortName: 'MUN', primaryColor: '#da291c', numberBaseColor: '#da291c' },
  { id: 'newcastle-united', name: 'Newcastle', shortName: 'NEW', primaryColor: '#111111', numberBaseColor: '#111111' },
  { id: 'nottingham-forest', name: "Nott'm Forest", shortName: 'NFO', primaryColor: '#dd0000', numberBaseColor: '#dd0000' },
  { id: 'sunderland', name: 'Sunderland', shortName: 'SUN', primaryColor: '#eb172b', numberBaseColor: '#eb172b' },
  { id: 'tottenham-hotspur', name: 'Spurs', shortName: 'TOT', primaryColor: '#ffffff', numberBaseColor: '#ffffff' },
].map((team) => ({
  ...team,
  badge: `${ASSET_BASE}/teams/${team.id}.png`,
  jersey: `${ASSET_BASE}/jerseys/${team.id}.png`,
}))

export const teamsById = new Map(PREMIER_LEAGUE_2026_TEAMS.map((team) => [team.id, team]))

const TEAM_ALIASES = new Map(
  PREMIER_LEAGUE_2026_TEAMS.flatMap((team) => [
    [team.id, team.id],
    [team.name.toLowerCase(), team.id],
    [team.shortName.toLowerCase(), team.id],
  ])
)

TEAM_ALIASES.set('avl', 'aston-villa')
TEAM_ALIASES.set('bha', 'brighton')
TEAM_ALIASES.set('bou', 'bournemouth')
TEAM_ALIASES.set('bre', 'brentford')
TEAM_ALIASES.set('cry', 'crystal-palace')
TEAM_ALIASES.set('ips', 'ipswich-town')
TEAM_ALIASES.set('leeds united', 'leeds-united')
TEAM_ALIASES.set('lee', 'leeds-united')
TEAM_ALIASES.set('man city', 'manchester-city')
TEAM_ALIASES.set('manchester city', 'manchester-city')
TEAM_ALIASES.set('mci', 'manchester-city')
TEAM_ALIASES.set('man utd', 'manchester-united')
TEAM_ALIASES.set('manchester united', 'manchester-united')
TEAM_ALIASES.set('mun', 'manchester-united')
TEAM_ALIASES.set('newcastle united', 'newcastle-united')
TEAM_ALIASES.set('new', 'newcastle-united')
TEAM_ALIASES.set('nfo', 'nottingham-forest')
TEAM_ALIASES.set('nottingham forest', 'nottingham-forest')
TEAM_ALIASES.set("nott'm forest", 'nottingham-forest')
TEAM_ALIASES.set('spurs', 'tottenham-hotspur')
TEAM_ALIASES.set('tottenham hotspur', 'tottenham-hotspur')
TEAM_ALIASES.set('tot', 'tottenham-hotspur')

function toSlug(value) {
  return String(value ?? '')
    .trim()
    .toLowerCase()
    .replace(/&/g, 'and')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}

export function teamIdFromPlayer(player) {
  const candidate = player?.teamId ?? player?.team_id ?? player?.team ?? player?.club
  if (candidate == null) return null

  const key = String(candidate).trim().toLowerCase()
  return TEAM_ALIASES.get(key) ?? TEAM_ALIASES.get(toSlug(key)) ?? toSlug(key)
}

export function getTeamByPlayer(player) {
  return teamsById.get(teamIdFromPlayer(player))
}

export function normalizePremierLeaguePlayer(player) {
  const teamId = teamIdFromPlayer(player)
  return {
    id: player.id ?? player.player_id,
    name: player.name,
    teamId,
    squadNumber: player.squadNumber ?? player.squad_number ?? player.shirt_number ?? null,
    position: player.position,
    price: player.price,
    points: player.points ?? player.fpl_points ?? 0,
    ...player,
  }
}
