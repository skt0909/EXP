import PlayerMarker from '../PlayerMarker/PlayerMarker'
import './PitchLineup.css'

/** One position row. Column count is set from THIS row's own player count
 *  (grid-template-columns: repeat(n, 1fr)), not a fixed layout shared by
 *  every row -- a legal 5-a-side DEF/MID row divides the same row width
 *  into 5 equal columns instead of 5 fixed-width markers overflowing past
 *  it. --dense (>=5) also scales the marker/jersey/label down a notch via
 *  PitchLineup.css, since 5 equal columns are narrower than 3-4's. */
function PitchRow({ modifier, players, position }) {
  const count = players.length || 1
  return (
    <div
      className={`pitch-lineup__row pitch-lineup__row--${modifier}${
        players.length >= 5 ? ' pitch-lineup__row--dense' : ''
      }`}
      style={{ gridTemplateColumns: `repeat(${count}, 1fr)` }}
    >
      {players.map((p) => (
        <PlayerMarker key={p.player_id} {...p} position={position} />
      ))}
    </div>
  )
}

/** bench is optional -- a Dream11 XI has no substitutes at all, so rendering an
 *  empty "Bench:" strip under one would be misleading rather than merely bare. */
function PitchLineup({ lineup, bench = [] }) {
  return (
    <section className="pitch-lineup">
      <div className="pitch-lineup__field">
        <div className="pitch-lineup__overlay" />
        <PitchRow modifier="gk" players={lineup.GK} position="GK" />
        <PitchRow modifier="def" players={lineup.DEF} position="DEF" />
        <PitchRow modifier="mid" players={lineup.MID} position="MID" />
        <PitchRow modifier="fwd" players={lineup.FWD} position="FWD" />
      </div>
      {bench.length > 0 && (
        <div className="pitch-lineup__bench">
          <span>Bench: {bench.map((p) => `${p.name} (${p.points})`).join(', ')}</span>
          <span className="material-symbols-outlined">chair</span>
        </div>
      )}
    </section>
  )
}

export default PitchLineup
