import { request } from './client'

// auth:false -- these two are how you GET a token, so sending one is
// meaningless and a stale/expired token must not make login fail.
export function register({ email, username, password, team_name }) {
  return request('/auth/register', {
    method: 'POST',
    auth: false,
    body: { email, username, password, team_name: team_name || null },
  })
}

export function login({ email, password }) {
  return request('/auth/login', { method: 'POST', auth: false, body: { email, password } })
}

export function fetchCurrentUser() {
  return request('/auth/me')
}

export function deleteAccount() {
  return request('/auth/me', { method: 'DELETE' })
}
