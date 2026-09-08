import { getTeamByPlayer } from '../../data/premierLeague2026'
import './PlayerJersey.css'

function hexToRgb(hex) {
  const normalized = String(hex ?? '#ffffff').replace('#', '')
  const value = normalized.length === 3
    ? normalized.split('').map((part) => part + part).join('')
    : normalized
  const parsed = Number.parseInt(value, 16)
  if (Number.isNaN(parsed)) return [255, 255, 255]
  return [(parsed >> 16) & 255, (parsed >> 8) & 255, parsed & 255]
}

function readableTextColor(hex) {
  const [r, g, b] = hexToRgb(hex).map((channel) => {
    const srgb = channel / 255
    return srgb <= 0.03928 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4
  })
  const luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
  return luminance > 0.48 ? '#111111' : '#ffffff'
}

function shortName(name) {
  const parts = String(name ?? '').trim().split(/\s+/).filter(Boolean)
  if (parts.length === 0) return ''
  return parts.at(-1).slice(0, 10)
}

function PlayerJersey({
  player,
  captain = false,
  viceCaptain = false,
  size = 'md',
  showName = true,
  className = '',
}) {
  const team = getTeamByPlayer(player)
  const squadNumber = player?.squadNumber ?? player?.squad_number ?? player?.shirt_number
  const numberColor = readableTextColor(team?.numberBaseColor ?? team?.primaryColor)

  return (
    <div className={`player-jersey player-jersey--${size} ${className}`.trim()}>
      <div className="player-jersey__image-wrap">
        {captain && <span className="player-jersey__badge player-jersey__badge--captain">C</span>}
        {viceCaptain && !captain && (
          <span className="player-jersey__badge player-jersey__badge--vice">VC</span>
        )}
        {team ? (
          <img
            alt={`${team.name} blank jersey`}
            className="player-jersey__image"
            draggable="false"
            src={team.jersey}
          />
        ) : (
          <div className="player-jersey__fallback" aria-hidden="true" />
        )}
        {squadNumber != null && (
          <span className="player-jersey__number" style={{ color: numberColor }}>
            {squadNumber}
          </span>
        )}
      </div>
      {showName && (
        <span className="player-jersey__name" title={player?.name}>
          {shortName(player?.name)}
        </span>
      )}
    </div>
  )
}

export default PlayerJersey
