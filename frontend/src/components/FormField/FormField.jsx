/**
 * The one input treatment used by every auth form.
 *
 * This exists because the Stitch Register screen shipped with its own
 * `.input-field` CSS class that used Tailwind's build-time theme() helper
 * inside a plain <style> block. The Play CDN never compiles that, so the
 * browser dropped the border/radius/background declarations and three of the
 * four fields rendered with no visible box at all -- confirmed by measuring
 * computed styles (border-color came out as the forms-plugin default grey with
 * 0px radius, against Login's outline-variant at 8px).
 *
 * Sharing one component means Login and Register cannot diverge again: there
 * is a single markup structure -- label, then a `relative` wrapper holding an
 * absolutely-positioned icon plus the input -- and one set of utility classes.
 */
const BASE_INPUT =
  'w-full bg-surface-container-lowest rounded-lg py-sm pl-[36px] font-body-md text-body-md text-on-surface ' +
  'focus:outline-none focus:border-secondary focus:ring-1 focus:ring-secondary transition-colors'

function FormField({
  id,
  label,
  icon,
  error,
  helpText,
  trailing,
  className = '',
  ...inputProps
}) {
  const errorId = error ? `${id}-error` : undefined
  const helpId = helpText ? `${id}-help` : undefined
  const describedBy = [errorId, helpId].filter(Boolean).join(' ') || undefined

  return (
    <div className="flex flex-col gap-xs">
      <label className="font-label-md text-label-md text-on-surface" htmlFor={id}>
        {label}
      </label>

      <div className="relative">
        <span
          className={`material-symbols-outlined absolute left-sm top-1/2 -translate-y-1/2 ${
            error ? 'text-error' : 'text-on-surface-variant'
          }`}
        >
          {icon}
        </span>
        <input
          aria-describedby={describedBy}
          aria-invalid={error ? 'true' : undefined}
          className={`${BASE_INPUT} ${error ? 'border border-error' : 'border border-outline-variant'} ${
            trailing ? 'pr-[36px]' : 'pr-sm'
          } ${className}`}
          id={id}
          {...inputProps}
        />
        {trailing}
      </div>

      {/* gap-gutter (12px) plus leading-normal on the message: the label-md
          type token sets line-height 1, so a wrapped error would otherwise
          collide with itself and with the helper text underneath. */}
      {(error || helpText) && (
        <div className="flex flex-col gap-gutter">
          {error && (
            <div className="flex items-start gap-xs text-error" id={errorId}>
              <span className="material-symbols-outlined text-[16px] leading-normal shrink-0">
                error
              </span>
              <p className="font-label-md text-label-md leading-normal text-error">{error}</p>
            </div>
          )}
          {helpText && (
            <p
              className="font-label-md text-label-md leading-normal text-on-surface-variant"
              id={helpId}
            >
              {helpText}
            </p>
          )}
        </div>
      )}
    </div>
  )
}

export default FormField
