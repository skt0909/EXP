import { TONE } from '../../data/scoringRules'

/**
 * The shared vocabulary behind both "How Points Work" screens.
 *
 * Both screens are the same object -- an eyebrow, a headline, then cards of
 * labelled rows with a value pill on the right -- so they share these parts
 * rather than each growing their own copy that drifts. What differs (the
 * Dream11 filter tabs, its grouped clean-sheet block and dark captain tiles, the
 * FPL screen's three headline stats) stays in the pages.
 *
 * TONE -> palette is resolved here, once. Note `on-tertiary-container` (#fe225f)
 * used as a BACKGROUND for deductions: that is genuinely the crimson in the
 * design, and the token pairs with white text at full contrast. Reaching for a
 * raw hex here would put the same colour in two places.
 */
const PILL_BASE =
  'shrink-0 inline-flex items-center justify-center min-w-[44px] px-sm py-1 rounded-full font-stats-number text-stats-number'

const PILL_TONES = {
  [TONE.POSITIVE]: 'bg-secondary-container text-on-secondary-container',
  [TONE.NEGATIVE]: 'bg-on-tertiary-container text-white',
  [TONE.NEUTRAL]: 'bg-surface-container-highest text-on-surface-variant',
  [TONE.MULTIPLIER]: 'bg-primary-container text-white',
  [TONE.MULTIPLIER_STRONG]: 'bg-primary text-white',
}

const DOT_TONES = {
  [TONE.POSITIVE]: 'bg-secondary',
  [TONE.NEGATIVE]: 'bg-on-tertiary-container',
  [TONE.NEUTRAL]: 'bg-outline-variant',
  [TONE.MULTIPLIER]: 'bg-primary-container',
  [TONE.MULTIPLIER_STRONG]: 'bg-primary',
}

export function ValuePill({ tone = TONE.NEUTRAL, children }) {
  return <span className={`${PILL_BASE} ${PILL_TONES[tone] ?? PILL_TONES[TONE.NEUTRAL]}`}>{children}</span>
}

/** "Differs from Classic FPL" — the Dream11 screen's most useful affordance. */
export function DiffersBadge({ className = '' }) {
  return (
    <span
      className={`inline-flex items-center shrink-0 px-sm py-[2px] rounded-full bg-surface-container-highest text-on-surface-variant font-label-md text-[10px] uppercase tracking-wider ${className}`}
    >
      Differs from Classic FPL
    </span>
  )
}

/**
 * One rule. `dot` renders the small tone-coloured bullet the classic screen puts
 * before single-line entries; rows with a sublabel skip it, matching the design.
 */
export function RuleRow({ row, showDot = false }) {
  const tone = row.tone ?? TONE.NEUTRAL

  return (
    <div className="flex items-center justify-between gap-md py-sm">
      <div className="min-w-0 flex items-center gap-sm">
        {showDot && row.dot && (
          <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${DOT_TONES[tone] ?? DOT_TONES[TONE.NEUTRAL]}`} />
        )}
        <div className="min-w-0">
          <div className="flex items-center gap-sm flex-wrap">
            <span className="font-body-md text-body-md text-on-surface">{row.label}</span>
            {row.differs && <DiffersBadge />}
          </div>
          {row.sublabel && (
            <p className="font-label-md text-[11px] normal-case tracking-normal text-on-surface-variant mt-[2px]">
              {row.sublabel}
            </p>
          )}
        </div>
      </div>
      <ValuePill tone={tone}>{row.value}</ValuePill>
    </div>
  )
}

/**
 * A callout inside a card -- the tactic note, the rollover cap, the
 * no-bench warning. `icon` is optional so a note can be pure text with only a
 * Differs badge above it, as the Dream11 captaincy card needs.
 */
export function RuleNote({ note }) {
  return (
    <div className="flex gap-sm p-gutter rounded-xl bg-[#F7F6FB]">
      {note.icon && (
        <span className="material-symbols-outlined text-on-surface-variant text-[20px] shrink-0">
          {note.icon}
        </span>
      )}
      <div className="min-w-0">
        {note.differs && <DiffersBadge className="mb-sm" />}
        {note.title && (
          <p className="font-body-md text-body-md font-bold text-on-surface mb-[2px]">{note.title}</p>
        )}
        <p className="font-body-md text-[13px] leading-relaxed text-on-surface-variant">{note.body}</p>
      </div>
    </div>
  )
}

/**
 * Card shell: tinted icon + title header, then whatever the section holds.
 *
 * The header's trailing "?" in the mockups is a help affordance with nothing
 * behind it -- there is no per-section help content anywhere in this project --
 * so it is deliberately not rendered. A button that does nothing is worse than
 * no button.
 */
export function RuleCard({ icon, title, badge, number, children }) {
  return (
    <section className="rounded-[20px] bg-white border border-[#E5E6E1] overflow-hidden shadow-[0_2px_8px_rgba(0,0,0,0.04)]">
      <header className="flex items-center gap-sm px-md py-gutter bg-white border-b border-[#E5E6E1]">
        {number != null ? (
          <span className="w-6 h-6 shrink-0 rounded-full bg-primary-container/15 text-primary-container font-headline-sm text-[12px] flex items-center justify-center">
            {number}
          </span>
        ) : (
          <span className="material-symbols-outlined text-primary-container text-[20px]">{icon}</span>
        )}
        <h2 className="font-headline-sm text-[17px] font-bold text-on-surface flex-1 min-w-0 truncate">{title}</h2>
        {badge}
      </header>
      <div className="px-md py-sm flex flex-col gap-sm">{children}</div>
    </section>
  )
}

/**
 * The mockup's "General + Tactical + Sub Bonus = GW Total" strip -- purely a
 * visual explainer of how the three real fields on GET /team combine, not a
 * live computation (no per-gameweek numbers to show here, only the concept).
 */
export function ScoreEquationStrip() {
  const terms = [
    { key: 'general', label: 'General' },
    { key: 'tactical', label: 'Tactical' },
    { key: 'sub-bonus', label: 'Sub Bonus' },
  ]
  return (
    <div className="flex items-center justify-between gap-xs rounded-xl bg-surface-container-lowest border border-outline-variant shadow-sm px-sm py-gutter">
      {terms.map((term, i) => (
        <span className="contents" key={term.key}>
          {i > 0 && <span className="font-headline-sm text-on-surface-variant">+</span>}
          <span className="px-sm py-[3px] rounded-md bg-surface-container-low font-label-md text-[10px] font-semibold text-on-surface whitespace-nowrap">
            {term.label}
          </span>
        </span>
      ))}
      <span className="font-headline-sm text-on-surface-variant">=</span>
      <span className="px-sm py-[3px] rounded-md bg-primary-container text-white font-label-md text-[10px] font-bold whitespace-nowrap">
        GW Total
      </span>
    </div>
  )
}

/**
 * Rows separated by hairlines rather than each carrying its own border, so the
 * first and last rows sit flush against the card padding.
 */
export function RuleRowList({ rows, showDots = false }) {
  return (
    <div className="divide-y divide-outline-variant/60">
      {rows.map((row) => (
        <RuleRow key={row.key} row={row} showDot={showDots} />
      ))}
    </div>
  )
}

/**
 * Page chrome shared by both screens: back affordance, title, eyebrow, intro.
 *
 * The back arrow uses history rather than a fixed destination -- these screens
 * are reachable from more than one place (the FPL dashboard, a contest page),
 * and sending a user somewhere they didn't come from is worse than no arrow.
 * Falls back to the mode's home when there is no history to pop.
 */
export function ScoringPageHeader({ eyebrow, eyebrowTone, tag, title, intro, onBack }) {
  return (
    <header className="flex flex-col gap-gutter">
      <div className="flex items-center gap-sm">
        <button
          aria-label="Go back"
          className="w-9 h-9 -ml-1 rounded-full flex items-center justify-center text-on-surface hover:bg-surface-container-high transition-colors"
          onClick={onBack}
          type="button"
        >
          <span className="material-symbols-outlined">arrow_back</span>
        </button>
        <h1 className="font-headline-sm text-headline-sm text-on-surface">How Points Work</h1>
      </div>

      <div className="flex items-center gap-sm flex-wrap">
        <span
          className={`inline-flex items-center gap-1 px-sm py-[3px] rounded-full font-label-md text-[10px] uppercase tracking-wider ${eyebrowTone}`}
        >
          {eyebrow}
        </span>
        {tag && (
          <span className="inline-flex items-center gap-1 px-sm py-[3px] rounded-full bg-secondary-container text-on-secondary-container font-label-md text-[10px] uppercase tracking-wider">
            <span className="w-1.5 h-1.5 rounded-full bg-secondary" />
            {tag}
          </span>
        )}
      </div>

      <div>
        <h2 className="font-display-lg text-display-lg text-on-surface">{title}</h2>
        <p className="font-body-md text-body-md text-on-surface-variant mt-sm">{intro}</p>
      </div>
    </header>
  )
}
