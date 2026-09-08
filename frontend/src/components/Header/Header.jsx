import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../../auth/AuthContext'

/**
 * TopAppBar: brand on the left, account menu on the right.
 *
 * Navigation lives entirely in BottomNav -- the old header nav duplicated it
 * with unlabelled icons and the two could disagree about which page was
 * active.
 */
function Header() {
  const { user, teamName, logout, deleteAccount } = useAuth()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [deleting, setDeleting] = useState(false)

  function handleLogout() {
    logout()
    navigate('/login', { replace: true })
  }

  // Irreversible (no undelete endpoint), so a native confirm() gate before
  // ever calling the API -- same bar as any one-click destructive action.
  async function handleDeleteAccount() {
    if (deleting) return
    const confirmed = window.confirm(
      'Delete your account? This cannot be undone -- you will be logged out and will not be able to log back in with this email.'
    )
    if (!confirmed) return

    setDeleting(true)
    try {
      await deleteAccount()
      navigate('/login', { replace: true })
    } catch (err) {
      // deleteAccount() only calls logout() AFTER the API call succeeds, so
      // a failure here means the account is untouched and the session is
      // still live -- stay on the page and say so, rather than navigating
      // to /login and implying a delete (or logout) that didn't happen.
      window.alert(err.message || 'Could not delete account. Please try again.')
    } finally {
      setDeleting(false)
    }
  }

  return (
    <header className="sticky top-0 z-40 bg-surface border-b border-outline-variant">
      <div className="flex justify-between items-center h-16 px-md w-full max-w-[600px] mx-auto">
        <div className="flex items-center gap-2 min-w-0">
          <span className="material-symbols-outlined text-primary">sports_soccer</span>
          <span className="font-headline-sm text-headline-sm font-extrabold text-primary-container truncate">
            {teamName || 'PitchSide AI'}
          </span>
        </div>

        <div className="relative">
          <button
            aria-label="Account"
            className="w-8 h-8 rounded-full bg-surface-container-highest border border-outline-variant flex items-center justify-center hover:bg-surface-container-high transition-colors"
            onClick={() => setOpen((v) => !v)}
            type="button"
          >
            <span className="material-symbols-outlined text-on-surface-variant text-[20px]">
              person
            </span>
          </button>

          {open && (
            <div className="absolute right-0 mt-sm w-56 bg-surface-container-lowest border border-outline-variant rounded-lg shadow-md p-sm z-50">
              <p className="font-label-md text-label-md text-on-surface-variant px-sm pb-sm truncate">
                {user?.email}
              </p>
              <button
                className="w-full text-left px-sm py-sm rounded font-body-md text-body-md text-error hover:bg-error-container transition-colors flex items-center gap-sm"
                onClick={handleLogout}
                type="button"
              >
                <span className="material-symbols-outlined text-[18px]">logout</span>
                Log out
              </button>

              <hr className="my-sm border-outline-variant" />

              <button
                className="w-full text-left px-sm py-sm rounded font-body-md text-body-md text-error hover:bg-error-container transition-colors flex items-center gap-sm disabled:opacity-50"
                onClick={handleDeleteAccount}
                disabled={deleting}
                type="button"
              >
                <span className="material-symbols-outlined text-[18px]">delete_forever</span>
                {deleting ? 'Deleting account…' : 'Delete account'}
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  )
}

export default Header
