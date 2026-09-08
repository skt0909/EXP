import PlayerMarker from '../PlayerMarker/PlayerMarker'
import './PitchLineup.css'

/** bench is optional -- a Dream11 XI has no substitutes at all, so rendering an
 *  empty "Bench:" strip under one would be misleading rather than merely bare. */
function PitchLineup({ lineup, bench = [] }) {
  return (
    <section className="pitch-lineup">
      <div className="pitch-lineup__field">
        <div className="pitch-lineup__overlay" />
        <div className="pitch-lineup__row pitch-lineup__row--gk">
          {lineup.GK.map((p) => (
            <PlayerMarker key={p.player_id} {...p} position="GK" />
          ))}
        </div>
        <div className="pitch-lineup__row pitch-lineup__row--def">
          {lineup.DEF.map((p) => (
            <PlayerMarker key={p.player_id} {...p} position="DEF" />
          ))}
        </div>
        <div className="pitch-lineup__row pitch-lineup__row--mid">
          {lineup.MID.map((p) => (
            <PlayerMarker key={p.player_id} {...p} position="MID" />
          ))}
        </div>
        <div className="pitch-lineup__row pitch-lineup__row--fwd">
          {lineup.FWD.map((p) => (
            <PlayerMarker key={p.player_id} {...p} position="FWD" />
          ))}
        </div>
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
