import { request } from './client'

export function sendChatMessage({ season, gameweek, message }) {
  return request('/chat', {
    method: 'POST',
    body: {
      season,
      gameweek: Number(gameweek),
      message,
    },
  })
}
