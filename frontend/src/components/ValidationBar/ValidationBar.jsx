function Check({ ok, label }) {
  return (
    <span
      className={`flex items-center gap-1 font-label-md text-label-md ${
        ok ? 'text-secondary' : 'text-error'
      }`}
    >
      <span className="material-symbols-outlined text-[16px]">
        {ok ? 'check_circle' : 'radio_button_unchecked'}
      </span>
      {label}
    </span>
  )
}

/**
 * Sits directly above BottomNav (bottom-[72px]) rather than at bottom-0, so the
 * submit control and the nav never overlap -- the Stitch export replaced this
 * bar with nav markup entirely, which left the design with no way to submit.
 */
function ValidationBar({
  selectedCount,
  countValid,
  budgetValid,
  clubValid,
  isValid,
  submitting,
  locked,
  onSubmit,
  serverErrors,
}) {
  return (
    <div className="fixed bottom-[72px] left-1/2 -translate-x-1/2 w-full max-w-[600px] z-40 px-md pb-sm pt-sm bg-surface/95 backdrop-blur-lg border-t border-outline-variant shadow-[0_-4px_20px_rgba(0,0,0,0.05)]">
      <div className="flex justify-between items-center gap-sm mb-sm flex-wrap">
        <Check
          ok={countValid}
          label={countValid ? '15 players selected' : `Need 15 (${selectedCount}/15)`}
        />
        <Check ok={budgetValid} label="Within Budget" />
        <Check ok={clubValid} label="Club limits" />
      </div>

      {serverErrors && serverErrors.length > 0 && (
        <ul className="mb-sm flex flex-col gap-xs" role="alert">
          {serverErrors.map((err) => (
            <li className="font-label-md text-label-md leading-normal text-error" key={err}>
              {err}
            </li>
          ))}
        </ul>
      )}

      {/* A past-deadline rejection is an expected state, not a failure: the
          backend enforces it with a DB trigger and returns a clean 422, so it
          reads as a locked notice rather than an error. */}
      {locked ? (
        <div
          className="w-full bg-surface-container-high text-on-surface-variant rounded-lg py-3.5 font-headline-sm text-headline-sm flex items-center justify-center gap-2"
          data-testid="squad-locked"
        >
          <span className="material-symbols-outlined">lock</span>
          Deadline passed — squad locked
        </div>
      ) : (
        <button
          className="w-full bg-primary-container text-on-primary rounded-lg py-3.5 font-headline-sm text-headline-sm flex items-center justify-center gap-2 shadow-md active:scale-[0.98] transition-all disabled:opacity-50 disabled:active:scale-100 disabled:cursor-not-allowed"
          data-testid="submit-squad"
          disabled={!isValid || submitting}
          onClick={onSubmit}
          type="button"
        >
          <span className="material-symbols-outlined">verified</span>
          {submitting ? 'Submitting…' : 'Submit Squad'}
        </button>
      )}
    </div>
  )
}

export default ValidationBar
