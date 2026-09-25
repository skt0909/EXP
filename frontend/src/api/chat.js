import { request } from './client'

/**
 * mode/contest_id mirror Context_assembler/main.py's ChatRequest: mode
 * defaults to 'tactical' (the starting-XI squad) when omitted, and
 * contest_id is only meaningful -- and required by the backend -- when
 * mode is 'dream11' (that squad's Quick 11 team for one contest).
 */
export function sendChatMessage({ season, gameweek, message, mode, contest_id }) {
  return request('/chat', {
    method: 'POST',
    body: {
      season,
      gameweek: Number(gameweek),
      message,
      mode,
      contest_id,
    },
  })
}
