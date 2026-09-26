import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { buildDream11Rules } from '../../data/scoringRules'
import { useScoringRules } from '../../hooks/useScoringRules'
import { MODE_CONTESTS, MODE_HOME } from '../../config/appMode'
import {
  DiffersBadge,
  RuleCard,
  RuleNote,
  RuleRowList,
  ScoringPageHeader,
} from '../../components/ScoringRules/ScoringRuleParts'

/**
 * Contests mode's "How Points Work".
 *
 * Route lives under /dream11, so modeForPath already classifies it as Contests
 * and BottomNav renders the Contests tab set unprompted -- no nav changes were
 * needed for either screen. (The Dream11 mockup drew the FPL tabs here, which
 * would have been wrong in this mode.)
 *
 * The filter row is real, not decorative: sections carry a `filter` tag in
 * data/scoringRules.js and untagged ones (Discipline, Squad & Lineup) show only
 * under "All Rules".
 *
 * The "Differs from Classic FPL" badges are computed from the payload, which is
 * why this screen reads BOTH halves of GET /scoring-rules -- see
 * buildDream11Rules.
 */
function Dream11ScoringPage() {
  const navigate = useNavigate()
  const [filter, setFilter] = useState('all')
  const { rules, loading, error } = useScoringRules()

  function handleBack() {
    if (window.history.state?.idx > 0) navigate(-1)
    else navigate(MODE_HOME[MODE_CONTESTS])
  }

  const content = rules ? buildDream11Rules(rules) : null
  const sections = !content
    ? []
    : filter === 'all'
      ? content.sections
      : content.sections.filter((section) => section.filter === filter)

  return (
    <div className="min-h-screen bg-[#FBF9F5] px-4 py-3 pb-28 flex flex-col gap-4 font-['Helvetica_Neue',Helvetica,Arial,sans-serif]">
      <ScoringPageHeader
        eyebrow="Quick 11 Rules"
        eyebrowTone="bg-[#EEEAF8] text-[#76527D]"
        intro={content?.intro ?? 'Single-Match Daily Fantasy scoring dynamics.'}
        onBack={handleBack}
        tag={content?.statusLabel}
        title={content?.title ?? 'Scoring Breakdown'}
      />

      {loading && (
        <p className="font-body-md text-body-md text-on-surface-variant">Loading scoring rules…</p>
      )}

      {error && (
        <div className="bg-error-container text-on-error-container rounded-lg p-sm" role="alert">
          <p className="font-label-md text-label-md">{error}</p>
        </div>
      )}

      {content && (
        <>
          {/* Horizontally scrollable so four labels never wrap or clip at 390px --
              the same failure mode BottomNav's labels have hit before. */}
          <div
            aria-label="Filter rules"
            className="flex gap-2 overflow-x-auto -mx-4 px-4 pb-1"
            role="tablist"
          >
            {content.filters.map((option) => {
              const active = option.key === filter
              return (
                <button
                  aria-selected={active}
                  // px-gutter and normal tracking, not label-md's 0.05em: with the
                  // wider spacing the four labels overflow 390px and "Multipliers"
                  // clips behind the scroll edge.
                  className={`shrink-0 px-gutter py-sm rounded-full font-label-md text-[13px] tracking-normal transition-colors ${
                    active
                      ? 'bg-[#1B1C1A] text-white'
                      : 'bg-white border border-[#E5E6E1] text-on-surface-variant hover:bg-[#F5F3F0]'
                  }`}
                  key={option.key}
                  onClick={() => setFilter(option.key)}
                  role="tab"
                  type="button"
                >
                  {option.label}
                </button>
              )
            })}
          </div>

          {sections.map((section) => (
            <RuleCard icon={section.icon} key={section.key} title={section.title}>
              {/* Grouped block: the clean-sheet trio sits under one heading because
                  the minutes threshold applies to all three at once. */}
              {section.groups?.map((group) => (
                <div className="rounded-xl bg-[#F7F6FB] p-gutter" key={group.key}>
                  {/* Badge stacked under the heading rather than beside it -- side
                      by side, "Clean Sheet (at 54+ mins)" wraps mid-phrase at 390px. */}
                  <div className="mb-xs">
                    <h3 className="font-body-md text-body-md font-bold text-on-surface">
                      {group.title}
                    </h3>
                    {group.differs && <DiffersBadge className="mt-xs" />}
                  </div>
                  <RuleRowList rows={group.rows} />
                </div>
              ))}

              {/* Captain/vice as paired dark tiles, per the design -- these are the
                  two multipliers, not a list of rules. */}
              {section.tiles && (
                <div className="grid grid-cols-2 gap-sm">
                  {section.tiles.map((tile) => (
                    <div
                      className="flex flex-col items-center gap-1 py-md rounded-lg bg-[#1B101B] shadow-sm"
                      key={tile.key}
                    >
                      <span className="font-label-md text-[10px] uppercase tracking-wider text-white/70">
                        {tile.label}
                      </span>
                      <span className="font-headline-md text-headline-md text-white">
                        {tile.value}
                      </span>
                    </div>
                  ))}
                </div>
              )}

              {section.rows && <RuleRowList rows={section.rows} />}

              {section.notes?.map((note) => (
                <RuleNote key={note.key} note={note} />
              ))}
            </RuleCard>
          ))}
        </>
      )}
    </div>
  )
}

export default Dream11ScoringPage
