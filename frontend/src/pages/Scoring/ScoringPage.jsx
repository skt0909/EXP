import { useNavigate } from 'react-router-dom'
import { buildClassicRules, TONE } from '../../data/scoringRules'
import { useScoringRules } from '../../hooks/useScoringRules'
import { MODE_FPL, MODE_HOME } from '../../config/appMode'
import { useGameweek } from '../../config/gameweek'
import {
  RuleCard,
  RuleNote,
  RuleRowList,
  ScoringPageHeader,
} from '../../components/ScoringRules/ScoringRuleParts'

/**
 * FPL mode's "How Points Work".
 *
 * Sits under Layout, so BottomNav renders itself with the FPL tab set and the
 * mode toggle stays where every other page has it. The mockup drew a bottom nav
 * of its own; reusing Layout's is what keeps this screen from being the one
 * place the nav disagrees with the rest of the app.
 *
 * Every number comes from GET /scoring-rules, which the backend builds from the
 * constants Results/scoring.py actually applies -- see data/scoringRules.js.
 */
const HIGHLIGHT_TONES = {
  [TONE.POSITIVE]: 'text-on-secondary-container',
  [TONE.NEGATIVE]: 'text-on-tertiary-container',
  [TONE.MULTIPLIER]: 'text-primary-container',
}

function ScoringPage() {
  const navigate = useNavigate()
  const { rules, loading, error } = useScoringRules()
  const { season } = useGameweek()

  function handleBack() {
    // `idx > 0` means there is somewhere to pop back to within this app.
    if (window.history.state?.idx > 0) navigate(-1)
    else navigate(MODE_HOME[MODE_FPL])
  }

  const content = rules ? buildClassicRules(rules) : null

  return (
    <div className="px-safe-margin py-md flex flex-col gap-md">
      <ScoringPageHeader
        eyebrow="Classic FPL Rules"
        eyebrowTone="bg-primary-container text-white"
        intro={
          content?.intro ??
          'Standard Fantasy Premier League rules and scoring breakdown.'
        }
        onBack={handleBack}
        tag={content?.statusLabel}
        title={content?.title ?? 'Scoring System'}
      />

      {/* Season comes from the real current gameweek, not frozen into the
          copy -- the mockup's "2024/25" was already a season out of date. */}
      <p className="font-label-md text-label-md text-on-surface-variant -mt-sm">
        Season {season}
      </p>

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
          {/* Three headline numbers, evenly split. */}
          <div className="grid grid-cols-3 gap-sm rounded-xl bg-surface-container-lowest border border-outline-variant p-md">
            {content.highlights.map((item, index) => (
              <div
                className={`flex flex-col items-center text-center gap-1 ${
                  index > 0 ? 'border-l border-outline-variant' : ''
                }`}
                key={item.key}
              >
                <span className="font-label-md text-[10px] uppercase tracking-wider text-on-surface-variant">
                  {item.label}
                </span>
                <span
                  className={`font-stats-number text-stats-number ${
                    HIGHLIGHT_TONES[item.tone] ?? 'text-on-surface'
                  }`}
                >
                  {item.value}
                </span>
              </div>
            ))}
          </div>

          {content.sections.map((section) => (
            <RuleCard icon={section.icon} key={section.key} title={section.title}>
              {section.rows && <RuleRowList rows={section.rows} showDots />}
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

export default ScoringPage
