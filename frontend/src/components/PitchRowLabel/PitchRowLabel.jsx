const LABELS = { GK: 'GK', DEF: 'DEF', MID: 'MID', FWD: 'FWD' }

/**
 * One label per pitch ROW -- "GK"/"DEF"/"MID"/"FWD" in bold white text at
 * the row's left edge -- not one per player. Shared by every pitch (Tactic
 * mode squad-build, Dashboard, Starting XI, Quick 11 mode) so the four
 * can't drift apart.
 *
 * Absolutely positioned, vertically centred: the row it's placed in must
 * itself be `relative` (every caller's row container already is, or gets
 * `relative` added alongside this).
 */
function PitchRowLabel({ position }) {
  return (
    <span
      className="absolute left-1.5 top-1/2 -translate-y-1/2 z-20 pointer-events-none select-none font-label-md text-[13px] font-black uppercase tracking-wider text-white [text-shadow:0_1px_3px_rgba(0,0,0,0.65),0_0_4px_rgba(0,0,0,0.45)]"
    >
      {LABELS[position] ?? position}
    </span>
  )
}

export default PitchRowLabel
