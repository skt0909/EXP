import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../../auth/AuthContext'

/**
 * The account icon button + its dropdown (email, Log out, Delete account).
 *
 * Extracted out of Header.jsx, which is only mounted for Layout-wrapped pages
 * (Chat, Transfers, Squad, Leagues, Matches, Dream11...). DashboardPage and
 * SquadSelectionPage draw their own chrome instead of using Layout, so before
 * this existed they each had their own copy of the account icon -- and both
 * copies had drifted: Dashboard's button called itself "Log out" but only
 * navigated to /login without ever calling logout(), so the token stayed
 * valid in localStorage after a user believed they'd signed out; Squad
 * Selection's was a plain <div>, not a button at all, with no click handler
 * and no aria-label. Neither was "the account icon, shared" -- there were
 * three independent implementations, only one of which worked.
 *
 * Self-contained (reads useAuth() itself) so it drops into any header with
 * no props, and every mount behaves identically by construction rather than
 * by three copies being kept in sync by hand.
 */
function AccountMenu() {
  const { user, logout, deleteAccount } = useAuth()
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
    <div className="relative">
      <button
        aria-label="Account"
        className="w-8 h-8 rounded-full bg-surface-container-highest border border-outline-variant flex items-center justify-center hover:bg-surface-container-high transition-colors"
        onClick={() => setOpen((v) => !v)}
        type="button"
      >
        <span className="material-symbols-outlined text-on-surface-variant text-[20px]">person</span>
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
            disabled={deleting}
            onClick={handleDeleteAccount}
            type="button"
          >
            <span className="material-symbols-outlined text-[18px]">delete_forever</span>
            {deleting ? 'Deleting account…' : 'Delete account'}
          </button>
        </div>
      )}
    </div>
  )
}

export default AccountMenu
