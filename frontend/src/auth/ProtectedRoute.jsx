import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from './AuthContext'

/**
 * Gate for every in-app route. While the boot-time GET /auth/me is still in
 * flight this renders nothing rather than redirecting -- otherwise a reload
 * with a perfectly valid token would flash the login screen before bouncing
 * back.
 *
 * The attempted location rides along in state so LoginPage can return the
 * user to where they were headed instead of always dumping them on /.
 */
function ProtectedRoute() {
  const { user, loading } = useAuth()
  const location = useLocation()

  if (loading) return null
  if (!user) return <Navigate to="/login" replace state={{ from: location }} />
  return <Outlet />
}

export default ProtectedRoute
