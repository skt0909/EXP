import PlayerJersey from '../PlayerJersey/PlayerJersey'
import './PlayerMarker.css'

// onSelect is optional -- PickTeamPage's own builder markers (a different,
// separate implementation, not this component) don't need this, and no
// current caller passes it except OpponentTeamPanel's read-only pitch.
function PlayerMarker({
  name,
  points,
  position,
  captain = false,
  viceCaptain = false,
  multiplier,
  onSelect,
  ...player
}) {
  const jerseyPlayer = { ...player, name, position }
  const Tag = onSelect ? 'button' : 'div'

  return (
    <Tag
      className={`player-marker ${captain ? 'player-marker--captain' : ''} ${onSelect ? 'player-marker--tappable' : ''}`}
      onClick={onSelect ? () => onSelect({ ...player, name, position, points, captain, viceCaptain, multiplier }) : undefined}
      type={onSelect ? 'button' : undefined}
    >
      <PlayerJersey
        captain={captain}
        player={jerseyPlayer}
        showName={false}
        size={captain ? 'lg' : 'md'}
        viceCaptain={viceCaptain}
      />
      <div className={`player-marker__tag ${captain ? 'player-marker__tag--captain' : ''}`}>
        <span className="player-marker__name">{name}</span>
        <span className="player-marker__points">
          {points}
          {/* Vice-captains carry a multiplier too in Dream11 (1.5x, applied
              simultaneously with the captain's 2x rather than as a fallback),
              so this isn't captain-only any more. FPL callers pass multiplier=1
              for a vice who didn't inherit the armband, which renders nothing. */}
          {multiplier > 1 && (captain || viceCaptain) && (
            <span className="player-marker__multiplier">×{multiplier}</span>
          )}
        </span>
      </div>
    </Tag>
  )
}

export default PlayerMarker
