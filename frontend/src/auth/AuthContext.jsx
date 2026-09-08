import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import * as authApi from '../api/auth'
import { clearToken, getToken, setToken, setUnauthorizedHandler } from '../api/client'

const AuthContext = createContext(null)

// team_name is captured at registration and echoed back by POST /auth/register,
// but GET /auth/me returns only id/email/username -- so it's kept here for the
// rest of the session and re-read from GET /team where a page needs it after a
// reload. Registration writes it to public.users.team_name server-side; the
// client never has to send it again.
const TEAM_NAME_KEY = 'pitchside.team_name'

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [teamName, setTeamName] = useState(() => localStorage.getItem(TEAM_NAME_KEY))
  // Starts true so ProtectedRoute renders nothing instead of bouncing to
  // /login during the first GET /auth/me -- a reload with a valid token must
  // not flash the login screen.
  const [loading, setLoading] = useState(true)

  const logout = useCallback(() => {
    clearToken()
    localStorage.removeItem(TEAM_NAME_KEY)
    setUser(null)
    setTeamName(null)
  }, [])

  // Any 401 from any endpoint drops the session, so a token the server no
  // longer accepts can't leave the UI in a half-authenticated state.
  useEffect(() => {
    setUnauthorizedHandler(() => logout())
    return () => setUnauthorizedHandler(null)
  }, [logout])

  // Restore the session from the stored token on boot. The token is only
  // trusted after /auth/me confirms it -- expiry and revocation are decided
  // by the server, never by inspecting the JWT client-side.
  useEffect(() => {
    let cancelled = false
    if (!getToken()) {
      setLoading(false)
      return
    }
    authApi
      .fetchCurrentUser()
      .then((u) => {
        if (!cancelled) setUser(u)
      })
      .catch(() => {
        if (!cancelled) logout()
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [logout])

  const applyToken = useCallback(async (tokenResponse) => {
    setToken(tokenResponse.access_token)
    if (tokenResponse.team_name) {
      localStorage.setItem(TEAM_NAME_KEY, tokenResponse.team_name)
      setTeamName(tokenResponse.team_name)
    }
    const me = await authApi.fetchCurrentUser()
    setUser(me)
    return me
  }, [])

  const login = useCallback(
    async (credentials) => applyToken(await authApi.login(credentials)),
    [applyToken]
  )

  const register = useCallback(
    async (details) => applyToken(await authApi.register(details)),
    [applyToken]
  )

  // The backend soft-deletes (scrubs identity fields, stamps deleted_at)
  // rather than hard-deleting -- see Data/auth.py's DELETE /auth/me. Either
  // way the session is over from the client's point of view, so this just
  // calls the endpoint and then reuses logout()'s local cleanup; there is
  // no undelete, so a caller should confirm with the user before calling
  // this (Header.jsx's delete button does).
  const deleteAccount = useCallback(async () => {
    await authApi.deleteAccount()
    logout()
  }, [logout])

  const value = useMemo(
    () => ({ user, teamName, loading, login, register, logout, deleteAccount }),
    [user, teamName, loading, login, register, logout, deleteAccount]
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside an AuthProvider')
  return ctx
}
