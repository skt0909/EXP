import { request } from './client'

export function fetchTransfersUsed({ season, gameweek }) {
  const params = new URLSearchParams({ season, gameweek })
  return request(`/transfers/used?${params}`)
}

export function fetchTransferHistory({ season }) {
  const params = new URLSearchParams({ season })
  return request(`/transfers/history?${params}`)
}

// Throws ValidationError on a 422 carrying the backend's full list of failures
// (Game_logic/transfers.py collects every error, not just the first), or
// LockedError once the deadline has passed. See api/client.js.
export function submitTransfers({ season, gameweek, transfers }) {
  return request('/transfers', {
    method: 'POST',
    body: { season, gameweek: Number(gameweek), transfers },
  })
}
