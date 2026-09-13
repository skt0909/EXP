import { useState } from 'react'
import { Link, Navigate } from 'react-router-dom'
import FormField from '../../components/FormField/FormField'
import { forgotPassword } from '../../api/auth'
import { useAuth } from '../../auth/AuthContext'

function ForgotPasswordPage() {
  const { user } = useAuth()
  const [email, setEmail] = useState('')
  const [submitting, setSubmitting] = useState(false)
  // Once true, stays true regardless of what's typed next -- the whole point
  // of the generic message is that resubmitting can't reveal anything new.
  const [submitted, setSubmitted] = useState(false)

  if (user) return <Navigate to="/dashboard" replace />

  async function handleSubmit(e) {
    e.preventDefault()
    setSubmitting(true)
    try {
      // The backend returns this exact response whether or not the email is
      // registered (auth.py's forgot_password) -- there is nothing to branch
      // on here, and nothing an error path could say that wouldn't leak
      // account existence. A network/server failure still shows the same
      // copy: the alternative is telling an attacker their probe reached a
      // real backend, which is worse than a false "check your email" for a
      // typo'd address.
      await forgotPassword({ email })
    } finally {
      setSubmitting(false)
      setSubmitted(true)
    }
  }

  return (
    <div className="bg-surface text-on-surface min-h-screen flex items-center justify-center font-body-md text-body-md">
      <main className="relative z-10 w-full max-w-[400px] px-safe-margin py-xl flex flex-col items-center">
        <div className="mb-xl text-center">
          <h1 className="font-display-lg text-display-lg text-primary mb-sm">PitchSide AI</h1>
          <p className="font-body-lg text-body-lg text-on-surface-variant">
            {submitted ? 'Check the console for your reset link.' : "Forgot your password?"}
          </p>
        </div>

        {submitted ? (
          <div className="w-full flex flex-col gap-md items-center text-center">
            <p className="font-body-md text-body-md text-on-surface-variant">
              If that email is registered, a reset link has been sent. (This
              project has no email sending yet -- for now, check the backend
              server's log output for the link.)
            </p>
            <Link
              className="font-body-md text-body-md text-on-surface-variant hover:text-primary transition-colors"
              to="/login"
            >
              Back to <span className="font-bold text-primary">Log In</span>
            </Link>
          </div>
        ) : (
          <>
            <form className="w-full flex flex-col gap-md" onSubmit={handleSubmit}>
              <FormField
                autoComplete="email"
                icon="mail"
                id="email"
                label="Email"
                name="email"
                onChange={(e) => setEmail(e.target.value)}
                placeholder="manager@pitchside.ai"
                required
                type="email"
                value={email}
              />

              <button
                className="w-full bg-primary-container text-on-primary-container rounded-lg py-md font-label-md text-label-md uppercase tracking-wider hover:opacity-90 active:scale-[0.98] transition-all shadow-md mt-sm disabled:opacity-50 disabled:active:scale-100"
                disabled={submitting}
                type="submit"
              >
                {submitting ? 'Sending…' : 'Send Reset Link'}
              </button>
            </form>

            <div className="mt-xl text-center">
              <Link
                className="font-body-md text-body-md text-on-surface-variant hover:text-primary transition-colors"
                to="/login"
              >
                Back to <span className="font-bold text-primary">Log In</span>
              </Link>
            </div>
          </>
        )}
      </main>
    </div>
  )
}

export default ForgotPasswordPage
