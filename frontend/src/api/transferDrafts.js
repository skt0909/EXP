import { request } from './client'

// The transfer cart: pairs staged for a gameweek but not yet confirmed.
// These endpoints touch ONLY the transfer_drafts table -- staging a pair
// moves no squad, no budget and no free-transfer count. Confirming is
// still submitTransfers() in ./transfers.js, unchanged.

export function fetchTransferDrafts({ season, gameweek }) {
  const params = new URLSearchParams({ season, gameweek })
  return request(`/transfer-drafts?${params}`)
}

// Idempotent, keyed on player_out_id: sending a new player_in_id for a
// player already in the cart replaces that pair rather than adding one.
export function putTransferDraft({ season, gameweek, player_out_id, player_in_id }) {
  return request('/transfer-drafts', {
    method: 'PUT',
    body: {
      season,
      gameweek: Number(gameweek),
      player_out_id: Number(player_out_id),
      player_in_id: Number(player_in_id),
    },
  })
}

// Resolves to the remaining cart, so callers don't need a follow-up list call.
export function deleteTransferDraft({ id, season, gameweek }) {
  const params = new URLSearchParams({ season, gameweek })
  return request(`/transfer-drafts/${id}?${params}`, { method: 'DELETE' })
}
