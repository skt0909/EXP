import { useState } from 'react'
import { Link, Navigate, useNavigate } from 'react-router-dom'
import FormField from '../../components/FormField/FormField'
import { useAuth } from '../../auth/AuthContext'

const MIN_PASSWORD_LENGTH = 8

// The backend returns every validation failure at once as detail: [...]
// (auth.py's _validate_registration). Each message is routed to the field it
// belongs to so errors appear inline rather than as one undifferentiated list.
function splitErrors(errors) {
  const byField = { email: null, username: null, password: null, team_name: null }
  const other = []
  for (const err of errors) {
    const e = err.toLowerCase()
    if (e.includes('email')) byField.email = err
    else if (e.includes('username')) byField.username = err
    else if (e.includes('password')) byField.password = err
    else if (e.includes('team_name')) byField.team_name = err
    else other.push(err)
  }
  return { byField, other }
}

function RegisterPage() {
  const { user, register } = useAuth()
  const navigate = useNavigate()

  const [form, setForm] = useState({ email: '', username: '', password: '', team_name: '' })
  const [showPassword, setShowPassword] = useState(false)
  const [fieldErrors, setFieldErrors] = useState({})
  const [formErrors, setFormErrors] = useState([])
  const [submitting, setSubmitting] = useState(false)

  if (user) return <Navigate to="/squad-selection" replace />

  const update = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }))

  async function handleSubmit(e) {
    e.preventDefault()
    setSubmitting(true)
    setFieldErrors({})
    setFormErrors([])
    try {
      await register(form)
      // Step 3 of the flow: a brand-new manager has no squad, so send them
      // straight to squad selection rather than a dashboard with nothing on it.
      navigate('/squad-selection', { replace: true })
    } catch (err) {
      if (err.errors) {
        const { byField, other } = splitErrors(err.errors)
        setFieldErrors(byField)
        setFormErrors(other)
      } else {
        setFormErrors([err.message || 'Registration failed'])
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="bg-background text-on-background min-h-screen flex flex-col antialiased">
      <main className="relative z-10 flex-grow flex flex-col items-center justify-center w-full max-w-[600px] mx-auto px-safe-margin py-xl">
        <div className="mb-xl flex flex-col items-center text-center">
          <h1 className="font-display-lg text-display-lg text-primary tracking-tight">PitchSide AI</h1>
          <p className="font-body-md text-body-md text-on-surface-variant mt-sm">
            Manager Registration
          </p>
        </div>

        <div className="w-full bg-surface-container-lowest/85 backdrop-blur-md border border-white/30 shadow-sm rounded-xl p-md md:p-lg">
          <form className="flex flex-col w-full gap-md" noValidate onSubmit={handleSubmit}>
            <FormField
              autoComplete="email"
              error={fieldErrors.email}
              icon="mail"
              id="email"
              label="Email address"
              name="email"
              onChange={update('email')}
              placeholder="manager@pitchside.ai"
              required
              type="email"
              value={form.email}
            />

            <FormField
              autoComplete="username"
              error={fieldErrors.username}
              helpText="This is your name as a manager."
              icon="person"
              id="username"
              label="Manager Name (Username)"
              name="username"
              onChange={update('username')}
              placeholder="Your manager name"
              required
              type="text"
              value={form.username}
            />

            {/* team_name is collected here and written to public.users.team_name
                by POST /auth/register -- the column GET /team and the league
                leaderboard already read. It is not resent at squad submission. */}
            <FormField
              error={fieldErrors.team_name}
              helpText="Give your fantasy team a unique name (e.g., Saka Potatoes)."
              icon="sports_soccer"
              id="team_name"
              label="Fantasy Team Name"
              name="team_name"
              onChange={update('team_name')}
              placeholder="e.g. Saka Potatoes"
              type="text"
              value={form.team_name}
            />

            <FormField
              autoComplete="new-password"
              error={fieldErrors.password}
              helpText={`Must be at least ${MIN_PASSWORD_LENGTH} characters.`}
              icon="lock"
              id="password"
              label="Password"
              name="password"
              onChange={update('password')}
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
              value={form.password}
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
              className="w-full bg-primary-container text-on-primary font-headline-sm text-headline-sm py-md rounded-lg mt-sm shadow-md flex items-center justify-center active:scale-[0.98] transition-all disabled:opacity-50 disabled:active:scale-100"
              disabled={submitting}
              type="submit"
            >
              <span>{submitting ? 'Creating…' : 'Create Account'}</span>
              {!submitting && <span className="material-symbols-outlined ml-sm">arrow_forward</span>}
            </button>

            <p className="text-center font-label-md text-label-md leading-normal text-on-surface-variant mt-md">
              By creating an account, you agree to our{' '}
              <a className="text-primary font-bold hover:underline" href="#">
                Terms
              </a>{' '}
              &amp;{' '}
              <a className="text-primary font-bold hover:underline" href="#">
                Privacy Policy
              </a>
              .
            </p>
          </form>
        </div>

        <div className="mt-xl text-center">
          <p className="font-body-md text-body-md text-on-background">
            Already have an account?{' '}
            <Link className="text-primary font-bold hover:underline ml-xs" to="/login">
              Log in
            </Link>
          </p>
        </div>
      </main>
    </div>
  )
}

export default RegisterPage
