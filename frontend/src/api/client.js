import { API_BASE } from './config'

const TOKEN_KEY = 'pitchside.token'

// Single place the JWT is read/written. Every request goes through request()
// below, so no call site ever builds an Authorization header itself -- adding
// a new endpoint client cannot forget to authenticate.
export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY)
}

// Thrown for a 422. The backend collects every validation failure into
// detail: [...] (see Game_logic/squad_selection.py's _validate_squad and
// auth.py's _validate_registration), so the UI can render them all at once.
export class ValidationError extends Error {
  constructor(errors) {
    super(errors.join('; '))
    this.name = 'ValidationError'
    this.errors = errors
  }
}

// Thrown for a 401. AuthProvider listens for this to drop the session --
// a token that the server rejects is not worth keeping.
export class AuthError extends Error {
  constructor(message) {
    super(message)
    this.name = 'AuthError'
  }
}

// Thrown when the gameweek deadline has passed. The backend enforces the lock
// with a DB trigger and surfaces it as a 422 whose message mentions the lock
// (Game_logic/starting_xi.py, transfers.py), so this is a 422 subtype rather
// than its own status -- the UI shows a locked state, not an error toast.
export class LockedError extends Error {
  constructor(errors) {
    super(errors.join('; '))
    this.name = 'LockedError'
    this.errors = errors
  }
}

// Thrown for a 403. Distinct from AuthError: the caller IS authenticated, the
// server just won't hand over this particular resource. Dream11's opponent-team
// endpoint (Game_logic/dream11.py's get_user_team) returns it for the rule that
// hides rival XIs until the contest locks at kickoff -- a deliberate game state
// callers should render as information, not as a failed request.
export class ForbiddenError extends Error {
  constructor(message) {
    super(message)
    this.name = 'ForbiddenError'
  }
}

// Thrown for a 404. Often a real, expected state rather than a fault -- e.g.
// asking for a Dream11 team from a member who simply never picked one.
export class NotFoundError extends Error {
  constructor(message) {
    super(message)
    this.name = 'NotFoundError'
  }
}

function detailToErrors(detail) {
  if (Array.isArray(detail)) {
    return detail.map((d) => (typeof d === 'string' ? d : d.msg ?? JSON.stringify(d)))
  }
  if (typeof detail === 'string') return [detail]
  if (detail) return [JSON.stringify(detail)]
  return ['Request failed']
}

function looksLocked(errors) {
  return errors.some((e) => /lock|deadline|has already started/i.test(e))
}

let onUnauthorized = null

/** AuthProvider registers a callback so a 401 anywhere logs the user out. */
export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn
}

export async function request(path, { method = 'GET', body, auth = true } = {}) {
  const headers = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  const token = getToken()
  if (auth && token) headers.Authorization = `Bearer ${token}`

  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })

  // 204/empty bodies are legal; don't let JSON.parse turn one into an error.
  const text = await res.text()
  const data = text ? JSON.parse(text) : null

  if (res.ok) return data

  if (res.status === 401) {
    if (auth && onUnauthorized) onUnauthorized()
    throw new AuthError(detailToErrors(data?.detail)[0])
  }
  if (res.status === 422) {
    const errors = detailToErrors(data?.detail)
    throw looksLocked(errors) ? new LockedError(errors) : new ValidationError(errors)
  }
  // 403/404 carry the server's own explanation, which is written to be shown
  // to the user as-is ("opponents' teams are hidden until the contest locks at
  // kickoff"), so pass the message through rather than inventing one.
  if (res.status === 403) throw new ForbiddenError(detailToErrors(data?.detail)[0])
  if (res.status === 404) throw new NotFoundError(detailToErrors(data?.detail)[0])
  throw new Error(detailToErrors(data?.detail)[0] || `${method} ${path} failed: ${res.status}`)
}
