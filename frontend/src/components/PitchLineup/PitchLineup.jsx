import PlayerMarker from '../PlayerMarker/PlayerMarker'
import './PitchLineup.css'

/** One position row. Column count is set from THIS row's own player count
 *  (grid-template-columns: repeat(n, 1fr)), not a fixed layout shared by
 *  every row -- a legal 5-a-side DEF/MID row divides the same row width
 *  into 5 equal columns instead of 5 fixed-width markers overflowing past
 *  it. --dense (>=5) also scales the marker/jersey/label down a notch via
 *  PitchLineup.css, since 5 equal columns are narrower than 3-4's. */
function PitchRow({ modifier, players, position, onSelectPlayer }) {
  const count = players.length || 1
  return (
    <div
      className={`pitch-lineup__row pitch-lineup__row--${modifier}${
        players.length >= 5 ? ' pitch-lineup__row--dense' : ''
      }`}
      style={{ gridTemplateColumns: `repeat(${count}, 1fr)` }}
    >
      {players.map((p) => (
        <PlayerMarker key={p.player_id} {...p} onSelect={onSelectPlayer} position={position} />
      ))}
    </div>
  )
}

/** bench is optional -- a Dream11 XI has no substitutes at all, so rendering an
 *  empty "Bench:" strip under one would be misleading rather than merely bare.
 *  onSelectPlayer is also optional -- omit it (e.g. a future read-only,
 *  non-interactive use of this same component) and markers render as plain,
 *  untappable divs, same as before this prop existed. */
function PitchLineup({ lineup, bench = [], onSelectPlayer }) {
  return (
    <section className="pitch-lineup">
      <div className="pitch-lineup__field">
        <div className="pitch-lineup__overlay" />
        <PitchRow modifier="gk" onSelectPlayer={onSelectPlayer} players={lineup.GK} position="GK" />
        <PitchRow modifier="def" onSelectPlayer={onSelectPlayer} players={lineup.DEF} position="DEF" />
        <PitchRow modifier="mid" onSelectPlayer={onSelectPlayer} players={lineup.MID} position="MID" />
        <PitchRow modifier="fwd" onSelectPlayer={onSelectPlayer} players={lineup.FWD} position="FWD" />
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
