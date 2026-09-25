import { useState } from 'react'
import { Link, Navigate, useNavigate, useSearchParams } from 'react-router-dom'
import FormField from '../../components/FormField/FormField'
import { resetPassword } from '../../api/auth'
import { useAuth } from '../../auth/AuthContext'
import AuthBrand from '../../components/AuthBrand/AuthBrand'

const MIN_PASSWORD_LENGTH = 8

function ResetPasswordPage() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const token = searchParams.get('token')

  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [formErrors, setFormErrors] = useState([])
  const [submitting, setSubmitting] = useState(false)
  const [success, setSuccess] = useState(false)

  if (user) return <Navigate to="/dashboard" replace />

  async function handleSubmit(e) {
    e.preventDefault()
    setFormErrors([])

    if (newPassword !== confirmPassword) {
      setFormErrors(['Passwords do not match'])
      return
    }

    setSubmitting(true)
    try {
      // POST /auth/reset-password issues no token -- resetting a password
      // isn't a session, it's a credential change. Sending them to /login to
      // sign in with it fresh is the same shape every other auth action here
      // already uses (register, delete-account) for "now go authenticate".
      await resetPassword({ token, new_password: newPassword })
      setSuccess(true)
    } catch (err) {
      // "invalid or expired reset link" (already-used, expired, or
      // malformed token) arrives the same way any other 422 does --
      // auth.py's reset_password doesn't distinguish which, on purpose.
      setFormErrors(err.errors ?? [err.message || 'Could not reset your password'])
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="bg-surface text-on-surface min-h-screen flex items-center justify-center font-body-md text-body-md">
      <main className="relative z-10 w-full max-w-[390px] px-safe-margin py-lg flex flex-col items-center">
        <AuthBrand subtitle={success ? 'Password reset' : 'Choose a new password'} />

        {!token ? (
          <div className="w-full flex flex-col gap-md items-center text-center">
            <p className="font-body-md text-body-md text-error" role="alert">
              This reset link is missing its token -- open the link from your
              reset email/log entry again.
            </p>
            <Link
              className="font-body-md text-body-md text-on-surface-variant hover:text-primary transition-colors"
              to="/forgot-password"
            >
              Request a new <span className="font-bold text-primary">reset link</span>
            </Link>
          </div>
        ) : success ? (
          <div className="w-full flex flex-col gap-md items-center text-center">
            <p className="font-body-md text-body-md text-on-surface-variant">
              Your password has been reset. Log in with your new password.
            </p>
            <button
              className="w-full bg-primary-container text-white rounded-lg py-md font-label-md text-label-md uppercase tracking-wider hover:bg-primary active:scale-[0.98] transition-all shadow-[0_4px_12px_rgba(79,102,0,0.18)]"
              onClick={() => navigate('/login', { replace: true })}
              type="button"
            >
              Go to Log In
            </button>
          </div>
        ) : (
          <form className="w-full flex flex-col gap-md" onSubmit={handleSubmit}>
            <FormField
              autoComplete="new-password"
              helpText={`Must be at least ${MIN_PASSWORD_LENGTH} characters.`}
              icon="lock"
              id="new-password"
              label="New Password"
              name="new-password"
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder="••••••••"
              required
              trailing={
                <button
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                  className="absolute right-sm top-1/2 -translate-y-1/2 text-on-surface-variant hover:text-primary transition-colors focus:outline-none"
                  onClick={() => setShowPassword((v) => !v)}
                  type="button"
                >
                  <span className="material-symbols-outlined">
                    {showPassword ? 'visibility_off' : 'visibility'}
                  </span>
                </button>
              }
              type={showPassword ? 'text' : 'password'}
              value={newPassword}
            />

            <FormField
              autoComplete="new-password"
              icon="lock"
              id="confirm-password"
              label="Confirm New Password"
              name="confirm-password"
              onChange={(e) => setConfirmPassword(e.target.value)}
              placeholder="••••••••"
              required
              type={showPassword ? 'text' : 'password'}
              value={confirmPassword}
            />

            {formErrors.length > 0 && (
              <div
                className="bg-error-container text-on-error-container rounded-lg p-sm font-body-md text-body-md flex flex-col gap-xs"
                role="alert"
              >
                {formErrors.map((e) => (
                  <span key={e}>{e}</span>
                ))}
              </div>
            )}

            <button
              className="w-full bg-primary-container text-white rounded-lg py-md font-label-md text-label-md uppercase tracking-wider hover:bg-primary active:scale-[0.98] transition-all shadow-[0_4px_12px_rgba(79,102,0,0.18)] mt-sm disabled:opacity-50 disabled:active:scale-100"
              disabled={submitting}
              type="submit"
            >
              {submitting ? 'Resetting…' : 'Reset Password'}
            </button>
          </form>
        )}

        <div className="mt-xl text-center">
          <Link
            className="font-body-md text-body-md text-on-surface-variant hover:text-primary transition-colors"
            to="/login"
          >
            Back to <span className="font-bold text-primary">Log In</span>
          </Link>
        </div>
      </main>
    </div>
  )
}

export default ResetPasswordPage
