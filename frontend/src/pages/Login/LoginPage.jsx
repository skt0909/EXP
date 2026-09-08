import { useState } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../../auth/AuthContext'

const FIELD_CLASS =
  'w-full bg-surface-container-lowest border border-outline-variant rounded-lg py-sm pl-[36px] font-body-md text-body-md text-on-surface ' +
  'focus:outline-none focus:border-secondary focus:ring-1 focus:ring-secondary transition-colors'

function LoginPage() {
  const { user, login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  // Already signed in (e.g. hit /login by hand) -- don't show a second login.
  if (user) return <Navigate to="/dashboard" replace />

  async function handleSubmit(e) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await login({ email, password })
      // Back to whatever ProtectedRoute bounced them off, else the hub.
      navigate(location.state?.from?.pathname ?? '/dashboard', { replace: true })
    } catch (err) {
      // The backend deliberately returns one generic message for both "no such
      // account" and "wrong password" (auth.py's INVALID_CREDENTIALS_MESSAGE)
      // so the UI must not try to be more specific than that.
      setError(err.message || 'Invalid email or password')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="bg-surface text-on-surface min-h-screen flex items-center justify-center font-body-md text-body-md">
      <main className="relative z-10 w-full max-w-[400px] px-safe-margin py-xl flex flex-col items-center">
        <div className="mb-xl text-center">
          <h1 className="font-display-lg text-display-lg text-primary mb-sm">PitchSide AI</h1>
          <p className="font-body-lg text-body-lg text-on-surface-variant">Welcome back, manager.</p>
        </div>

        <form className="w-full flex flex-col gap-md" onSubmit={handleSubmit}>
          <div className="flex flex-col gap-xs">
            <label className="font-label-md text-label-md text-on-surface" htmlFor="email">
              Email
            </label>
            <div className="relative">
              <span className="material-symbols-outlined absolute left-sm top-1/2 -translate-y-1/2 text-on-surface-variant">
                mail
              </span>
              <input
                autoComplete="email"
                className={`${FIELD_CLASS} pr-sm`}
                id="email"
                name="email"
                onChange={(e) => setEmail(e.target.value)}
                placeholder="manager@pitchside.ai"
                required
                type="email"
                value={email}
              />
            </div>
          </div>

          <div className="flex flex-col gap-xs">
            <label className="font-label-md text-label-md text-on-surface" htmlFor="password">
              Password
            </label>
            <div className="relative">
              <span className="material-symbols-outlined absolute left-sm top-1/2 -translate-y-1/2 text-on-surface-variant">
                lock
              </span>
              <input
                autoComplete="current-password"
                className={`${FIELD_CLASS} pr-[36px]`}
                id="password"
                name="password"
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                required
                type={showPassword ? 'text' : 'password'}
                value={password}
              />
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
            </div>
          </div>

          {error && (
            <div
              className="bg-error-container text-on-error-container rounded-lg p-sm font-body-md text-body-md flex items-center gap-sm mt-xs"
              role="alert"
            >
              <span className="material-symbols-outlined">error</span>
              {error}
            </div>
          )}

          <button
            className="w-full bg-primary-container text-on-primary-container rounded-lg py-md font-label-md text-label-md uppercase tracking-wider hover:opacity-90 active:scale-[0.98] transition-all shadow-md mt-sm disabled:opacity-50 disabled:active:scale-100"
            disabled={submitting}
            type="submit"
          >
            {submitting ? 'Logging in…' : 'Log In'}
          </button>
        </form>

        <div className="mt-xl text-center">
          <Link
            className="font-body-md text-body-md text-on-surface-variant hover:text-primary transition-colors"
            to="/register"
          >
            Don't have an account? <span className="font-bold text-primary">Sign up</span>
          </Link>
        </div>
      </main>
    </div>
  )
}

export default LoginPage
